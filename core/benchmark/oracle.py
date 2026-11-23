"""W10 T01：Oracle 隔离底座（AC-15 / AC-19 / AC-21）。

三重闸门（顺序不可调，设计 ``02-design.md`` §9.2）：

1. **静态导入白名单**：``audit_oracle_imports(root)`` 扫全仓 ``.py``，产品路径
   出现 ``load_oracle`` / ``OracleTruth`` 引用即判违规；
2. **运行期终态闸门**：``OracleGate.read()`` **自己**调 ``status_reader(task_id)``，
   用 ``core/tasks/state_machine.py:152 is_terminal()``（其唯一事实来源是
   ``core/tasks/models.py:31 TERMINAL_STATUSES``）判定；绝不另造终态集合；
3. **单次读取 + 审计留痕**：同一 ``(run_id, scenario_id)`` 终态后只允许读 1 次，
   每次允许/拒绝都 best-effort 写审计事件。

隔离硬约束（任务卡禁止项 5）：``expected_root_cause`` / ``injection_params`` /
``truth_assertions`` 一律**不得**进入日志、异常 ``details``、审计 payload 或
返回给上层的运行产物；本模块只外泄判定事实（reason / status / ids / bool）。

与 W04 的边界（任务卡 S9 / 设计 §9.3）：``core/faults/**`` **零改动**。本模块
不复制 JSON 读取、hash 校验或 ``fault_seed == data_seed`` 校验 —— 这些全部由
默认 loader ``core.faults.store.load_oracle`` 内部完成；这里只决定**何时允许读、
读几次、如何留痕**。

边界口径（任务卡 S8）：``NEEDS_RECONCILIATION`` / ``MANUAL_INTERVENTION``
**不是终态**（它们不在 ``TERMINAL_STATUSES`` 中）。这是有意的保守口径：任务
仍待核对/需人工介入时，评测端读取 Ground Truth 会反过来影响后续判断，因此
一律拒绝，并把判定留给唯一权威 ``is_terminal()``。
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, NoReturn, Protocol

from core.audit.events import AuditEventType, make_event
from core.errors import OracleAccessDenied, OracleLeakDetected
from core.faults.store import load_oracle
from core.logging import get_logger
from core.tasks.models import TaskStatus
from core.tasks.state_machine import is_terminal

logger = get_logger(__name__)

# 待外发文本（prompt / 上下文 / 训练样本 / 运行产物）中禁止出现的 Oracle 字段名。
# 扫描结果只返回**字段名**，绝不返回命中处的原文片段。
ORACLE_FORBIDDEN_FIELDS: tuple[str, ...] = ("expected_root_cause", "fault_seed")

# 允许引用 Oracle 读取入口的**唯一**白名单（相对仓库根的 posix 路径）。
_ORACLE_TOKENS: tuple[str, ...] = ("load_oracle", "OracleTruth")
ORACLE_IMPORT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "core/benchmark/oracle.py",
        "core/faults/store.py",
        "core/faults/models.py",
        "core/faults/__init__.py",
        "scripts/fault_lab.py",
    }
)

ALERT_ORACLE_AUDIT_UNAVAILABLE = "ORACLE_AUDIT_UNAVAILABLE"

REASON_TERMINAL_OK = "TERMINAL_OK"
REASON_NOT_TERMINAL = "NOT_TERMINAL"
REASON_ALREADY_READ = "ALREADY_READ"

# 静态扫描时跳过的目录：虚拟环境/缓存/构建产物目录不是仓库源码，且数量极大。
# 隐藏目录（以 "." 开头）一并剪枝；其余目录（含 datasets/、runs/、docs/）照扫。
_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
        "node_modules",
        "venv",
        "site-packages",
        "build",
        "dist",
        "dbguardian.egg-info",
    }
)

# 文档字符串/注释里提及符号名不算「读取逻辑」；只有真实的 import、名字引用
# 或调用（AST 节点）才算。benchmarks/** 是冻结数据资产目录，出现读取逻辑即违规。
_ORACLE_SYMBOL_NAMES: frozenset[str] = frozenset(_ORACLE_TOKENS)


# 异常类由 T01 追加在 ``core/errors.py``（403 / 400）。此处只做重导出，
# 不重复定义，保证 ``from core.benchmark.oracle import OracleLeakDetected`` 可用，
# 同时避免同一错误码出现两套类定义。
__all__ = [
    "ALERT_ORACLE_AUDIT_UNAVAILABLE",
    "ORACLE_FORBIDDEN_FIELDS",
    "ORACLE_IMPORT_ALLOWLIST",
    "REASON_ALREADY_READ",
    "REASON_NOT_TERMINAL",
    "REASON_TERMINAL_OK",
    "OracleAccessDenied",
    "OracleAccessRecord",
    "OracleGate",
    "OracleLeakDetected",
    "OraclePort",
    "assert_no_oracle_leak",
    "audit_oracle_imports",
    "scan_texts_for_oracle_leak",
]


@dataclass(frozen=True)
class OracleAccessRecord:
    """一次 Oracle 读取决策的**判定事实**记录（不含任何真值字段）。"""

    scenario_id: str
    task_id: str
    run_id: str
    allowed: bool
    reason: str  # TERMINAL_OK / NOT_TERMINAL / ALREADY_READ
    task_status: str
    read_at: str


class OraclePort(Protocol):
    """评测侧依赖的只读端口；``OracleGate`` 是其唯一实现。"""

    def read(self, scenario_id: str, *, task_id: str, run_id: str) -> Any: ...


def scan_texts_for_oracle_leak(texts: Iterable[str]) -> tuple[str, ...]:
    """扫描待外发文本，返回命中的 **Oracle 字段名**（按常量顺序、去重）。

    契约（任务卡 S3）：返回值只可能是 ``ORACLE_FORBIDDEN_FIELDS`` 中的字段名，
    **绝不**包含命中处的原文片段、行号、上下文或任何真值内容。未命中返回 ``()``。
    匹配大小写不敏感，避免 ``Expected_Root_Cause`` 之类变体绕过。
    """

    normalized: list[str] = [str(text).lower() for text in texts]
    hits: list[str] = []
    for field in ORACLE_FORBIDDEN_FIELDS:
        token = field.lower()
        if any(token in text for text in normalized):
            hits.append(field)
    return tuple(hits)


def assert_no_oracle_leak(texts: Iterable[str]) -> None:
    """待外发文本命中 Oracle 字段名即抛 ``OracleLeakDetected``（400）。

    ``details`` 只带 ``leaked_fields``（字段名列表），**不**带原文片段，避免
    异常本身成为真值外泄通道（任务卡禁止项 5）。
    """

    hits = scan_texts_for_oracle_leak(texts)
    if hits:
        raise OracleLeakDetected(
            "待外发文本命中 Oracle 真值字段名：" + ", ".join(hits),
            details={"leaked_fields": list(hits)},
        )


def _has_oracle_read_logic(path: Path, text: str) -> bool:
    """判定 ``benchmarks/**`` 下的文件是否含真实的 Oracle 读取逻辑。

    用 AST 而非纯文本：docstring / 注释里出现 ``load_oracle`` 只是常量字符串，
    不构成读取逻辑；``import``、``Name``、``Attribute``、``Call`` 节点才算。
    语法无法解析时保守判为违规（宁可多报，不静默放过）。
    """

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _ORACLE_SYMBOL_NAMES:
                return True
            if isinstance(func, ast.Attribute) and func.attr in _ORACLE_SYMBOL_NAMES:
                return True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(alias.name in _ORACLE_SYMBOL_NAMES for alias in node.names):
                return True
        elif isinstance(node, ast.Name) and node.id in _ORACLE_SYMBOL_NAMES:
            return True
        elif isinstance(node, ast.Attribute) and node.attr in _ORACLE_SYMBOL_NAMES:
            return True
    return False


def audit_oracle_imports(root: Path) -> tuple[str, ...]:
    """静态导入白名单审计：返回白名单之外的相对 posix 路径（升序、去重）。

    规则（任务卡 S1 / 设计 §9.1 S1）：

    - 命中符号 ``load_oracle`` / ``OracleTruth`` 的 ``.py`` 才参与判定；
    - ``ORACLE_IMPORT_ALLOWLIST`` 中的路径放行；
    - ``tests/**`` 放行（测试可引用 Oracle 做断言）；
    - ``benchmarks/**`` 是冻结数据资产目录：只放行**不含读取逻辑**的文件
      （docstring 提及符号名不算），一旦出现 import / 调用即计入违规；
    - 其余路径只要命中符号名即计入违规。

    返回的是**路径**，不是源码内容，避免审计工具自身成为泄漏通道。
    """

    base = Path(root)
    violations: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(".") and name not in _SKIP_DIR_NAMES
        ]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = Path(dirpath) / filename
            relative = path.relative_to(base).as_posix()
            if relative in ORACLE_IMPORT_ALLOWLIST:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                # 非 UTF-8 源码或不可读文件：保守计入违规，不静默跳过。
                violations.add(relative)
                continue
            if not any(token in text for token in _ORACLE_TOKENS):
                continue
            parts = path.relative_to(base).parts
            if parts and parts[0] == "tests":
                continue
            if parts and parts[0] == "benchmarks":
                if _has_oracle_read_logic(path, text):
                    violations.add(relative)
                continue
            violations.add(relative)
    return tuple(sorted(violations))


class OracleGate:
    """Oracle 唯一读取闸门：终态判定 + 单次读取 + 审计留痕。

    ``status_reader`` 由组合根注入，闸门**自己**读取任务状态；``read()`` 不接受
    调用方传入的 ``task_status``（否则闸门可被伪造，任务卡禁止项 3）。
    ``audit`` 为组合根装配的 best-effort 审计端口；审计不可写**不阻塞**评测，
    但记 ``alert=ORACLE_AUDIT_UNAVAILABLE``（任务卡 S7）。若该端口带
    ``tenant_id`` 属性（组合根显式绑定），闸门把它作为过滤列来源传给
    ``try_emit``；否则由 ``AuditService`` 从 ``tasks.tenant_id`` 解析，
    **绝不**从 payload 取租户（W06 冻结口径）。

    本类**没有** ``enabled`` / ``strict`` 开关，也不存在跳过隔离的路径。
    """

    def __init__(
        self,
        *,
        status_reader: Callable[[str], TaskStatus],
        audit: Any | None = None,
        loader: Callable[[str], Any] = load_oracle,
    ) -> None:
        self._status_reader = status_reader
        self._audit = audit
        self._loader = loader
        self._records: list[OracleAccessRecord] = []
        self._read_keys: set[tuple[str, str]] = set()

    @property
    def records(self) -> tuple[OracleAccessRecord, ...]:
        """全部决策记录（allowed + refused），按发生顺序，不可被外部改写。"""

        return tuple(self._records)

    def read(self, scenario_id: str, *, task_id: str, run_id: str) -> Any:
        """读取 Oracle 真值；返回 ``loader`` 的结果（默认 ``OracleTruth``）。

        闸门顺序不可调：① 终态闸门 → ② 单次闸门 → ③ 审计留痕 + 加载。
        真值只在调用方局部变量内存活，本类不落盘、不写日志、不进审计 payload。
        """

        # ① 终态闸门：状态只能由闸门自己读。is_terminal() 的终态集合唯一事实
        # 来源是 core/tasks/models.py:31 的 TERMINAL_STATUSES；本模块不自造集合。
        # NEEDS_RECONCILIATION / MANUAL_INTERVENTION 不在其中 → 一律拒绝（S8）。
        status = self._status_reader(task_id)
        if not is_terminal(status):
            self._deny(
                scenario_id=scenario_id,
                task_id=task_id,
                run_id=run_id,
                status=status,
                reason=REASON_NOT_TERMINAL,
            )

        # ② 单次闸门：同一 (run_id, scenario_id) 只允许终态后读 1 次（AC-19）。
        key = (run_id, scenario_id)
        if key in self._read_keys:
            self._deny(
                scenario_id=scenario_id,
                task_id=task_id,
                run_id=run_id,
                status=status,
                reason=REASON_ALREADY_READ,
            )

        # ③ 审计留痕（best-effort，失败不阻塞），随后调 loader。
        # 审计写在加载之前，确保「曾允许读取」这一判定事实一定留痕。
        self._emit_audit(
            event_type=AuditEventType.ORACLE_READ,
            scenario_id=scenario_id,
            task_id=task_id,
            run_id=run_id,
            status=status,
            reason=REASON_TERMINAL_OK,
            allowed=True,
        )
        truth = self._loader(scenario_id)
        self._read_keys.add(key)
        self._record(
            scenario_id=scenario_id,
            task_id=task_id,
            run_id=run_id,
            status=status,
            reason=REASON_TERMINAL_OK,
            allowed=True,
        )
        return truth

    def _deny(
        self,
        *,
        scenario_id: str,
        task_id: str,
        run_id: str,
        status: TaskStatus,
        reason: str,
    ) -> NoReturn:
        """记录 refused 决策 → 发拒绝审计 → 抛 403（details 只含判定事实）。"""

        self._record(
            scenario_id=scenario_id,
            task_id=task_id,
            run_id=run_id,
            status=status,
            reason=reason,
            allowed=False,
        )
        self._emit_audit(
            event_type=AuditEventType.ORACLE_ACCESS_DENIED,
            scenario_id=scenario_id,
            task_id=task_id,
            run_id=run_id,
            status=status,
            reason=reason,
            allowed=False,
        )
        raise OracleAccessDenied(
            f"Oracle 读取被闸门拒绝：{reason}",
            details={
                "reason": reason,
                "task_id": task_id,
                "run_id": run_id,
                "scenario_id": scenario_id,
                "task_status": self._status_label(status),
            },
        )

    def _record(
        self,
        *,
        scenario_id: str,
        task_id: str,
        run_id: str,
        status: TaskStatus,
        reason: str,
        allowed: bool,
    ) -> None:
        self._records.append(
            OracleAccessRecord(
                scenario_id=scenario_id,
                task_id=task_id,
                run_id=run_id,
                allowed=allowed,
                reason=reason,
                task_status=self._status_label(status),
                read_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    def _emit_audit(
        self,
        *,
        event_type: AuditEventType,
        scenario_id: str,
        task_id: str,
        run_id: str,
        status: TaskStatus,
        reason: str,
        allowed: bool,
    ) -> None:
        """best-effort 审计；任何不可写都只告警，绝不阻断评测（S7）。"""

        event = make_event(
            event_type,
            task_id=task_id,
            run_id=run_id,
            payload={
                # 只放判定事实；expected_root_cause / injection_params /
                # truth_assertions 一律不进审计 payload（禁止项 5）。
                "scenario_id": scenario_id,
                "task_id": task_id,
                "run_id": run_id,
                "task_status": self._status_label(status),
                "reason": reason,
                "allowed": allowed,
            },
        )
        extra: dict[str, Any] = {
            "alert": ALERT_ORACLE_AUDIT_UNAVAILABLE,
            "event_type": event_type.value,
            "task_id": task_id,
            "run_id": run_id,
            "scenario_id": scenario_id,
        }
        if self._audit is None:
            logger.error("Oracle 审计端口未装配，评测不阻塞", extra=extra)
            return
        try:
            # tenant_id 由组合根装配的审计端口携带；不信任 payload、不新增
            # OracleGate 构造参数（关键签名照抄任务卡 :75-77）。
            tenant_id: str | None = getattr(self._audit, "tenant_id", None)
            if tenant_id:
                emitted = self._audit.try_emit(event, tenant_id=tenant_id)
            else:
                emitted = self._audit.try_emit(event)
        except Exception as exc:  # noqa: BLE001 - 审计不可写不得阻塞评测
            logger.error(
                "Oracle 审计写入异常，评测不阻塞",
                extra={**extra, "failure_kind": type(exc).__name__},
            )
            return
        if emitted is False:
            logger.error("Oracle 审计写入返回失败，评测不阻塞", extra=extra)

    @staticmethod
    def _status_label(status: TaskStatus) -> str:
        """把状态规范成字符串（兼容 TaskStatus 与等价字符串）。"""

        return str(getattr(status, "value", status))
