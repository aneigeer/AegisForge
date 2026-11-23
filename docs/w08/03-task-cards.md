# W08 可派工任务卡（T01–T06）

- 依据：`docs/w08/02-design.md`（§2 文件清单 / §3 签名 / §5 事件冻结 / §6 任务列表）、`docs/w08/01-prd.md`（AC-01…AC-21）
- 编号与设计 §6 **一致**（T01 契约与模型 → T02 读回与判定+迁移 → **T03 状态推进** → T04 补偿 → T05 Recovery → T06 调度+矩阵）
- 硬顺序：**T03 必须先于 T05/T06 合入**，否则端到端跑不通（`EXECUTING→VERIFYING` 无调用方 = 验证永不触发）
- 全局禁止：不加 API `execute`/`consume`；不摘 `api` 的 `lab_net`；不引第三方依赖；不做 PG 写路径/第二写动作/前端；验证不复用执行连接；模型自报不进任何判定

---

## T01｜契约与模型（依赖：无｜文件 4：建 2 / 改 2）

**建**：`core/verification/__init__.py`、`core/verification/models.py`
**改**：`core/tools/contracts.py`（`CompensationSpec.to_public_dict()`，`label` 禁含 `rollback`）、`core/config.py`（`reconcile_interval_s` / `reconcile_older_than_s` / `verification_perf_samples`）

**关键签名**
```python
Verdict = Literal["PASS", "FAIL", "UNKNOWN"]
Disposition = Literal["SAFE_TO_COMPENSATE", "HUMAN_ADVICE_ONLY", "MANUAL_INTERVENTION"]
class AssertionOutcome(BaseModel):   # extra="forbid"
    assertion_id: str; metric: str; expected: str; tolerance: str
    source: Literal["step_observed","target_readback","audit_event"]
    observed: str | int | None; verdict: Verdict; reason: str | None
    evidence_ref: str | None; collected_at: datetime
class VerificationResult(BaseModel): # extra="forbid"
    result_id: str; tenant_id: str; task_id: str; tool_call_id: str; plan_digest: str
    verdict: Verdict; outcomes: tuple[AssertionOutcome, ...]
    disposition: Disposition | None; approval_required: bool
    claimed_success: bool; collected_at: datetime
```

**完成判据**：AC-04 前置（模型 `extra="forbid"`、枚举封闭）；配置 `gt=0, le=3600`、`ge=1, le=50` 越界被拒（`REQUEST_INVALID`）
**禁止**：不得改 `DESCRIPTOR_KEYS` / `ACTION_DIGEST_VERSION` / 任何冻结向量；不得给 `VerificationResult` 加自然语言字段
**验收**：`pytest tests/unit -q`（回归不破）+ `python -c "from core.verification.models import Verdict; assert set(Verdict.__args__)=={'PASS','FAIL','UNKNOWN'}"`

---

## T02｜读回与判定 + 迁移 008（依赖：T01｜文件 8）

**建**：`database/migrations/008_w08_verification.sql`、`database/migrations/rollback/008_rollback.sql`、`core/verification/{plan,disposition,readback,perf,repository}.py`、`adapters/mysql/readback.py`

**关键签名**
```python
class VerificationPlan(BaseModel):   # extra="forbid"
    plan_digest: str; assertions: tuple[VerificationAssertion, ...]
    @classmethod
    def from_disclosure(cls, approval, *, definition, parameters) -> "VerificationPlan": ...
def resolve_expected(expected: str, parameters: Mapping[str, Any]) -> ResolvedExpected
def aggregate(outcomes: Sequence[AssertionOutcome]) -> Verdict          # FAIL > UNKNOWN > PASS
def resolve_disposition(verdict: Verdict, *, evidence_sufficient: bool) -> DispositionDecision
def is_allowed(verdict: Verdict, disposition: Disposition) -> bool      # 九格矩阵
class ReadbackPort(Protocol):
    @property
    def read_only(self) -> bool: ...
    def client_identity(self) -> int: ...
    def read_target(self, target: TargetObject) -> ReadbackSample: ...
def percentile(samples: Sequence[float], p: float) -> float             # 排序索引法
class VerificationResultRepository:
    def insert(self, result: VerificationResult) -> str: ...
    def list_by_tool_call(self, tenant_id: str, tool_call_id: str) -> list[VerificationResult]: ...
```

**完成判据**：AC-01/02/03/04/05/06/07/09/12 全绿；迁移可升可回滚；`verification_results` 只追加（`dg_ctl` 无 UPDATE/DELETE）
**禁止**：`expected` 解析**不得用 `eval`/`exec`**；`ReadbackPort` **不得接受 SQL 字符串/host/port/DSN**；回读**不得**复用 `step_observed.result`，**不得**在执行事务内读
**验收**：`pytest tests/unit/test_verification_plan.py tests/unit/test_verification_perf.py tests/unit/test_disposition_policy.py tests/integration/test_w08_verifier_readback.py -q`

---

## T03｜状态推进与判定-executor（依赖：T02｜文件 1）

**建**：`core/verification/verifier.py`

**关键签名**
```python
class Verifier:
    def __init__(self, *, plan_source, readback: ReadbackPort, step_records,
                 results: VerificationResultRepository, tasks, audit, settings) -> None: ...
    def verify(self, task: Task, *, tool_call_id: str, approval_id: str) -> VerificationResult: ...
    # 形参不存在 model_output / llm / narrative（AC-04 用 inspect.signature 锁死）
```

**`EXECUTING→VERIFYING`（A3b，卡内显式一步）**
1. **谁调用**：`Verifier.verify()` 第一步，`self._tasks.transition(task.task_id, TaskStatus.VERIFYING)`；
2. **时点**：执行侧 `step_observed` 已写入之后、读取 plan 与任何采集之前；
3. **幂等**：`task.status is VERIFYING` → 跳过（不重复涨 `state_version`）；
4. **失败处理**：推进异常或后续任一步抛错 → `except` 内落 `NEEDS_RECONCILIATION`（`EXECUTING→NEEDS_RECONCILIATION` 合法）+ `VERIFICATION_FAILED(reason="VERIFIER_ERROR")`；**不得停留在 `EXECUTING`**。

**出边**：`PASS→COMPLETED`；`FAIL+证据足→FAILED`；`FAIL+不足→MANUAL_INTERVENTION`；`UNKNOWN→NEEDS_RECONCILIATION`；**每条前调 `can_transition`**，非法则记 `NO_LEGAL_TRANSITION` 并升级人工
**完成判据**：AC-08 全绿
**禁止**：不得改 `core/tasks/state_machine.py`（不新增状态/不改边表）；不得在此卡碰 `core/approval/*`
**验收**：`pytest tests/integration/test_w08_verifier_rejects.py -q`

---

## T04｜补偿：独立审批链（依赖：T03｜文件 3）

**建**：`core/verification/compensation.py`
**改**：`core/approval/repository.py`（`compensation_of` 可空 + 唯一键，冲突映射领域错误）、`core/approval/service.py`（`create(compensation_of=...)`）

**关键签名**
```python
class CompensationService:
    def request(self, *, original_approval, before, after, principal) -> Approval: ...
    def assert_independent(self, compensation: Approval, original: Approval) -> None: ...
# 补偿 params = {keys, note: before, expected_note: after}（与原动作互换 → digest 必然不同）
```

**完成判据**：AC-10/11 全绿；若用原 `approval_id` 驱动补偿 → 拒绝且目标行不变；`is_rollback is False`；三项 `COMPENSATION_*` 事件各 1 条
**禁止**：`compensation_of` **不得进 `action_digest`**；不得新增写工具、不得升 `tool_version`；面向人字段**禁含 `rollback`**
**验收**：`pytest tests/unit/test_compensation_contract.py tests/integration/test_w08_compensation.py -q`

---

## T05｜RecoveryController（依赖：T01、T03｜文件 3）

**建**：`core/recovery/{__init__,models,controller}.py`

**关键签名**
```python
class RecoveryController:      # 不持有 registry / gateway / dsn
    def __init__(self, *, audit, tasks, *, max_duplicate_calls=2,
                 no_progress_threshold=2, max_repair_retries=1) -> None: ...
    def observe_call(self, call_key: str) -> RecoveryAction: ...
    def observe_evidence(self, evidence_digest: str) -> RecoveryAction: ...
    def refute(self, hypothesis_id: str, *, reason: str) -> Hypothesis: ...   # REFUTED + plan_version+1
    def handle_invalid_output(self, attempt: int) -> RecoveryAction: ...
    def reconcile_unknown(self, *, action_digest: str, idempotency_key: str,
                          target_objects: tuple, evidence_sufficient: bool) -> RecoveryOutcome: ...
```

**完成判据**：AC-13/14/15/16 全绿；`tool_calls` 零增长；`explanation` 恒非空；非法输出 `retry_count == 1`
**禁止**：不得持有 `tool registry` / `gateway` / DSN（结构上不得能重放写）；不得物理删除 `Hypothesis` 历史；`AGENT_LOOP_DETECTED`/`AGENT_NO_PROGRESS` **复用既有事件**，不得新建同类
**验收**：`pytest tests/unit/test_recovery_controller.py tests/integration/test_w08_recovery_e2e.py -q`

---

## T06｜生产调度 + 崩溃矩阵 + 演练报告（依赖：T04、T05｜文件 5）

**改**：`workers/runner.py`（`RunReport.reconciled` + `run_once()` 调 `sweep()` **恰好 1 次**）、`workers/stack.py`（装配 Verifier / Readback / Recovery / Reconciler）
**建**：`tests/unit/test_worker_scheduler.py`、`tests/integration/test_w08_crash_matrix.py`（7 用例）、`docs/w08/08-drill-report.md`

**关键签名**
```python
@dataclass(frozen=True)
class RunReport:
    cycles: int = 0; discovered: int = 0; executed: int = 0
    denied: int = 0; skipped: int = 0; failed: int = 0
    unknown_outcome: int = 0; reconciled: int = 0        # ＋W08
```

**T06 边界裁定（主理人 4 条，必须照做）**
1. `RunReport.reconciled` **改 3 处**：字段声明 + `merged()` 逐字段累加 + `to_public_dict()`；**漏 `merged()` 会让 `run_forever` 静默丢计数**。`workers/cli.py:117` 直接 `payload.update(...)`，cli 不改。
2. **`clean` 不含 `reconciled`**；退出码保持 `0/1/2` 原义。`reconciled>0` 是正常收敛，不得让退出码变 1。加单测：「`reconciled>0` 且无失败时 `run_once()` 返回 0」；同步 `test_worker_runner.py::test_run_report_clean_semantics`。
3. **`reconciled = sum(1 for r in sweep() if r.transitioned)`**（实际落账数，见 `ReconcileResult.transitioned`）；候选数另设 `reconcile_scanned`（可选，仅日志），**不得用 `len(results)`**。
4. **授权改 `workers/stack.py`**：`WorkerStack` 暴露 `reconciler`（`Reconciler(step_records, task_repository, audit)`，`:63-71` 三依赖 stack 已有），**runner 不得自建**；`tests/unit/test_worker_layering.py` 增断言「`Reconciler(` 在 `workers/` 内只出现在 `stack.py`」。
> 另：CLI JSON 新增 `reconciled` 键属**对外可观测变化**，发布说明需登记；`DG_RECONCILE_OLDER_THAN_S` 已在 T01 落地，T06 只读取，不再临时加配置项。

**完成判据**：AC-17/18/19/21 全绿；`sweep` 抛错时 `clean=False` 且**循环不中断**（`RECONCILIATION_FAILED`）；7 类注入各自独立可定位
**禁止**：不得改 `core/execution/reconciler.py` 内部（只加调度）；不得为测试在生产代码加分支（只用 `context_factory`/`LeaseService`/`step_records`/stack 依赖等**现有接缝**）；不得动 `docker-compose.yml`（KD-2 属 P1-W08-01，单列提交）；不得在 runner 里自建 `Reconciler`
**验收**：`pytest tests/unit/test_worker_scheduler.py tests/integration/test_w08_crash_matrix.py tests/unit/test_verification_determinism.py tests/unit/test_worker_layering.py -q`

---

## 汇总

| 卡 | 依赖 | 文件数 | 对应 AC |
|---|---|---|---|
| T01 | — | 4 | AC-04（前置） |
| T02 | T01 | 8 | AC-01/02/03/04/05/06/07/09/12 |
| T03 | T02 | 1 | AC-08 |
| T04 | T03 | 3 | AC-10/11 |
| T05 | T01、T03 | 3 | AC-13/14/15/16 |
| T06 | T04、T05 | 5 | AC-17/18/19/21 |

未覆盖 AC：AC-20（引擎与写面不回退）由**回归**保证——`pytest tests/unit/test_tools_contracts.py tests/unit/test_write_tool_gate.py -q`，任一卡改动后须复跑。
