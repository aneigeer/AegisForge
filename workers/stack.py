"""W07 收口：独立 Worker 进程的**组合根**。

为什么同进程装配 Gateway：

- `ToolGateway.execute()` 已经是执行链的**唯一**入口：验签 → nonce 防重放 →
  租约/fencing 权威校验 → 策略决策 → 消费一次性审批 → `STEP_PREPARED` 门禁 →
  Executor。跨进程再包一层协议只会让"谁验签"变模糊，且需要新增跨进程信任边界；
- `Worker` 只需要 `GatewayPort`（`execute(envelope, params, *, now=None)`），
  `ToolGateway` 的方法签名与之完全一致，可直接注入，无需适配器。

装配纪律：

1. 写工具**只能**经 `adapters/lab_assembly.py::build_lab_stack()` 拿到——本模块
   不自行组装 `ToolRegistry`，因此不存在第二条写入口（W07 AC 的生产装配引用）；
2. 控制库客户端与 LAB 读写客户端在此创建并持有 DSN，**不下传**给 `core/worker/`；
3. 不装配 `ModelGateway`：Worker 不调用模型（`core/config.py::model_provider`
   保持三态原样，`core/models/` 零改动）；
4. 不新增 API 端点：执行链不向 HTTP 暴露（`docs/w06/02-design.md:163,302`）；
5. 报告发布器只读既有仓储 + 只追加快照表；不新增写工具、不新增端点
   （W10 L2：`workers/report_assembly.py`，失败不阻塞、不回滚、不新增事件类型）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from adapters.lab_assembly import build_lab_stack, is_lab_mysql_target
from adapters.mysql.readback import MysqlReadbackAdapter
from core.approval.service import ApprovalService
from core.audit.service import AuditService
from core.config import Settings
from core.errors import ConfigError
from core.execution.reconciler import Reconciler
from core.execution.step_records import StepRecordRepository
from core.gateway.leases import LeaseRepository, LeaseService
from core.gateway.tool_gateway import ToolGateway
from core.recovery.controller import RecoveryController
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.security.models import AuthMethod, ExecutionEnvelope, Principal
from core.security.policy import PolicyEngine
from core.security.roles import Role
from core.tasks.models import Task
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import ToolContext
from core.tools.registry import ToolRegistry
from core.verification.repository import VerificationResultRepository
from core.verification.verifier import Verifier
from core.worker.worker import Worker
from database.mysql.connection import MysqlClient
from workers import __version__ as WORKERS_VERSION
from workers.report_assembly import TaskReportPublisher, build_report_publisher

CONTROL_DATABASE_ID = "dg_system"


def _principal_resolver(tenant_id: str) -> Callable[[str], Principal]:
    """构造"用户 id → 服务端主体"的解析器。

    注意（**已知边界**）：本仓库没有用户目录（W05 只有 `dev_local`/`token` 两种
    认证态，账户表在 W08 之后），因此这里把任意 actor 解析为**同租户 Operator**。

    这样做仍然安全的理由：

    - 租户由租约事实决定，解析器不会把 actor 提升到别的租户；
    - R2 写路径在 `PolicyEngine.decide()` 里**先**返回 `APPROVAL_REQUIRED`，
      不看 actor 权限，放行只发生在 Gateway 第 8b 步消费一次性 grant；
    - 审批消费用的是 Gateway 内部构造的 `worker:<worker_id>` 主体
      （`core/gateway/tool_gateway.py:180-190`），`envelope.actor_user_id` 只参与
      租户一致性校验、策略判定与审计标注。
    """

    def resolve(user_id: str) -> Principal:
        return Principal(
            user_id=user_id,
            tenant_id=tenant_id,
            roles=(Role.OPERATOR,),
            auth_method=AuthMethod.DEV_LOCAL,
        )

    return resolve


@dataclass(frozen=True)
class ApprovalPlanSource:
    """`Verifier.plan_source` 的组合根适配器。

    `Verifier` 的端口只吃 `approval_id`（`get(approval_id) -> ApprovalDetail`），
    而 `ApprovalService.get()` 必须显式带租户。租户由组合根**固定**注入，不接受
    调用方传入——否则一次调用就能跨租户读取别人的披露快照。
    """

    approvals: ApprovalService
    tenant_id: str

    def get(self, approval_id: str) -> Any:
        return self.approvals.get(approval_id, tenant_id=self.tenant_id)


@dataclass(frozen=True)
class WorkerStack:
    """一套可运行的 Worker 执行栈；字段全部显式，便于自检与测试。"""

    settings: Settings
    worker_id: str
    tenant_id: str
    environment_id: str
    control_client: MysqlClient
    ro_client: MysqlClient
    rw_client: MysqlClient
    repository: TaskRepository
    #: 受控状态迁移入口（`WAITING_APPROVAL → EXECUTING`），只走白名单状态机。
    task_service: TaskService
    lease_service: LeaseService
    approvals: ApprovalService
    audit: AuditService
    step_records: StepRecordRepository
    registry: ToolRegistry
    environment_row: dict[str, Any]
    gateway: ToolGateway
    worker: Worker
    #: W08：独立验证编排器（`Verifier.verify()`）；旧测试替身可不提供。
    verifier: Verifier | None = None
    #: W08：独立**只读**回读端口（AC-05 异路径采集，client 与执行侧不同实例）。
    readback: MysqlReadbackAdapter | None = None
    #: W08：未观测调用核对端口；`run_once()` 每轮调用 `sweep()` 恰好一次。
    reconciler: Reconciler | None = None
    #: W09：脱敏闸门（闸门①/② 的 audit 与 quarantine 落点从这里取）。
    #: `None` 表示调用方未装配 —— 各通道内部会就地构造**恒开**的默认闸门，
    #: 因此本字段只影响"事件/隔离落点"，不构成"可关闭开关"。
    redaction: RedactionGate | None = None
    #: W10 L2：报告发布器；`None` 表示未装配（历史替身），`run_once()` 不做任何
    #: 报告动作，零行为变化。它只读既有仓储 + 只追加快照，不持有 DSN。
    report_publisher: TaskReportPublisher | None = None
    #: P6-T1：只读诊断派发消费者；`None` 表示历史替身未装配。
    readonly_dispatcher: Any | None = None

    @property
    def lab_write_enabled(self) -> bool:
        """环境事实是否允许写工具（LAB + mysql）。"""
        return is_lab_mysql_target(self.environment_row, self.environment_row.get("db_engine"))

    def worker_for(self, actor_user_id: str) -> Worker:
        """按执行主体派生一个 Worker（共享端口与签名密钥，独立租约状态）。

        为什么每个候选都要新建：`Worker` 把当前租约缓存在实例上，跨候选复用会
        让"租约归属哪个任务"变成隐式状态；新建实例让一次执行 = 一个租约，最直观。
        """
        return Worker(
            self.worker_id,
            self.tenant_id,
            actor_user_id,
            self.lease_service,
            self.gateway,
            signing_key=self.settings.gateway_key(),
        )

    def recovery_for(self, task_id: str, run_id: str) -> RecoveryController:
        """按任务派生一个**已绑定**审计落点的恢复控制器。

        为什么每个任务都要新建：`RecoveryController` 的循环/无进展计数与
        `task_id`/`run_id` 都是实例状态。共享一个未绑定实例会让恢复事件
        **静默丢弃**（`skipped_events`），因此这里强制绑定后再交给调用方。
        """
        controller = RecoveryController(audit=self.audit, tasks=self.task_service)
        controller.bind(task_id=task_id, run_id=run_id)
        return controller


def _mysql_client(settings: Settings, dsn: Any, *, database_id: str, read_only: bool) -> MysqlClient:
    return MysqlClient(
        dsn,
        database_id=database_id,
        read_only=read_only,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )


def build_worker_stack(
    settings: Settings,
    *,
    worker_id: str | None = None,
    tenant_id: str | None = None,
) -> WorkerStack:
    """装配独立 Worker 执行栈（唯一组合入口）。

    Args:
        settings: 已校验的进程配置（含控制库/LAB 读写 DSN）。
        worker_id: 覆盖 `settings.worker_id`；供 `--worker-id` 使用。
        tenant_id: 覆盖 `settings.default_tenant_id`；供 `--tenant` 使用。

    Raises:
        ConfigError: 控制库缺少环境事实、或控制库账号未配置时（fail closed）。
    """
    resolved_worker_id = (worker_id or settings.worker_id).strip()
    if not resolved_worker_id:
        raise ConfigError("Worker 身份不能为空", details={"field": "worker_id"})
    resolved_tenant = (tenant_id or settings.default_tenant_id).strip()
    if not resolved_tenant:
        raise ConfigError("Worker 租户不能为空", details={"field": "tenant_id"})

    control_client = _mysql_client(
        settings,
        settings.control_mysql_dsn(),
        database_id=CONTROL_DATABASE_ID,
        read_only=False,
    )
    ro_client = _mysql_client(
        settings,
        settings.ro_dsn,
        database_id=settings.database_id,
        read_only=True,
    )
    rw_client = _mysql_client(
        settings,
        settings.rw_dsn,
        database_id=settings.database_id,
        read_only=False,
    )

    repository = TaskRepository(control_client)
    environment_id = settings.default_environment_id
    environment_row = repository.get_environment(environment_id, tenant_id=resolved_tenant)
    if environment_row is None:
        # 没有服务端环境事实就无法判定 LAB/PROD 与引擎：绝不猜，直接不装配。
        raise ConfigError(
            "控制库缺少环境事实，禁止装配 Worker 执行栈",
            details={
                "environment_id": environment_id,
                "tenant_id": resolved_tenant,
                "reason": "ENVIRONMENT_NOT_FOUND",
            },
        )

    # AC④：生产装配**必须**引用唯一装配点，不得在 workers/ 里手搓注册表。
    lab_stack = build_lab_stack(
        settings=settings,
        control_client=control_client,
        environment_row=environment_row,
        ro_client=ro_client,
        rw_client=rw_client,
    )
    registry: ToolRegistry = lab_stack["registry"]
    repository = lab_stack["repository"]
    audit: AuditService = lab_stack["audit"]
    approvals: ApprovalService = lab_stack["approvals"]
    step_records: StepRecordRepository = lab_stack["step_records"]
    executor = lab_stack["executor"]

    lease_service = LeaseService(LeaseRepository(control_client))
    task_service = TaskService(repository, registry)
    principal_resolver = _principal_resolver(resolved_tenant)

    def context_factory(envelope: ExecutionEnvelope, task: Task) -> ToolContext:
        """按工具声明的副作用选择连接：写工具用 rw，其余用 ro。

        工具实现（如 `adapters/mysql/lab_repair.py`）会自行断言
        `context.client.read_only is False`，因此这里的选择不是"优化"而是契约。
        """
        tool = registry.get(envelope.tool_name)
        definition = getattr(tool, "definition")
        client = rw_client if definition.side_effect == "write" else ro_client
        return ToolContext(client=client, task_id=task.task_id, run_id=task.run_id)

    gateway = ToolGateway(
        registry,
        repository,
        lease_service,
        PolicyEngine(policy_version=settings.policy_version),
        executor,
        principal_resolver,
        context_factory,
        settings=settings,
        approval_service=approvals,
        audit=audit,
    )
    worker = Worker(
        resolved_worker_id,
        resolved_tenant,
        settings.worker_actor_user_id,
        lease_service,
        gateway,
        signing_key=settings.gateway_key(),
    )

    # W08：验证 / 回读 / 核对三件套。回读必须用**独立只读 client**（AC-05），
    # 与执行侧 rw_client 不同实例；`plan_source` 的租户由组合根固定。
    readback = MysqlReadbackAdapter(ro_client)
    verifier = Verifier(
        plan_source=ApprovalPlanSource(approvals=approvals, tenant_id=resolved_tenant),
        readback=readback,
        step_records=step_records,
        results=VerificationResultRepository(control_client),
        tasks=task_service,
        audit=audit,
        settings=settings,
    )
    reconciler = Reconciler(step_records, repository, audit)

    # W10 L2（设计 §4.3）：报告通道的脱敏闸门**只创建一个实例**，同时写入
    # `WorkerStack.redaction` 槽位并注入报告发布器，使两者 `is` 同一对象。
    # `quarantine=None` 与当前生产口径一致：`REVIEW_REQUIRED` 时事件里记
    # `quarantine_skipped: true`，留痕不丢（丢的是隔离行，不是证据）。
    redaction_gate = RedactionGate(DataRedactionEngine(), audit=audit)
    report_publisher = build_report_publisher(
        settings=settings,
        tenant_id=resolved_tenant,
        control_client=control_client,
        audit=audit,
        gate=redaction_gate,
        approvals=approvals,
        step_records=step_records,
    )

    from core.execution.dispatch_queue import (
        ReadonlyDispatchQueue,
        WorkerReadonlyDispatcher,
    )
    from database.observers import build_observer_registry

    readonly_dispatcher = WorkerReadonlyDispatcher(
        ReadonlyDispatchQueue(control_client),
        build_observer_registry(),
        ro_client=ro_client,
        worker_id=resolved_worker_id,
        redaction_gate=redaction_gate,
    )

    return WorkerStack(
        settings=settings,
        worker_id=resolved_worker_id,
        tenant_id=resolved_tenant,
        environment_id=environment_id,
        control_client=control_client,
        ro_client=ro_client,
        rw_client=rw_client,
        repository=repository,
        task_service=task_service,
        lease_service=lease_service,
        approvals=approvals,
        audit=audit,
        step_records=step_records,
        registry=registry,
        environment_row=dict(environment_row),
        gateway=gateway,
        worker=worker,
        verifier=verifier,
        readback=readback,
        reconciler=reconciler,
        redaction=redaction_gate,
        report_publisher=report_publisher,
        readonly_dispatcher=readonly_dispatcher,
    )


__all__ = [
    "CONTROL_DATABASE_ID",
    "WORKERS_VERSION",
    "ApprovalPlanSource",
    "WorkerStack",
    "build_worker_stack",
]
