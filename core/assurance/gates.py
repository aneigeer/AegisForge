"""W11 T06：门禁清单静态守护（P0-W11-05 / AC-20）。

设计依据：``docs/w11/02-design.md`` §9、§11；裁定 W11-Q7（镜像 digest 填 ``UNKNOWN``
并登记）、W11-Q8（限制清单由 ``rc-freeze.md`` 传给 W12）。

九项 P0 门禁（``GATE_IDS``）**逐项静态判定**，全部来自"导入 + 符号存在 + 结构事实"，
不连库、不起服务、不做网络请求：

1. ``ENV_ISOLATION``：``environment_tier()`` 存在且未知/缺失一律按 PROD fail closed；
2. ``FIXED_READONLY_TOOLS``：观察器注册表内全部工具 ``side_effect == "read"``；
3. ``SERVER_SIDE_RISK``：``PolicyEngine.decide`` 存在，且模型风险提示**只升不降**；
4. ``HUMAN_APPROVAL``：``ApprovalService.consume`` 存在，且执行器对无 grant 的 R2 拒绝；
5. ``UNIQUE_TOOL_GATEWAY``：``ToolGateway(`` 的**唯一**装配点是 ``workers/stack.py``；
6. ``RECOVERY_RECONCILIATION``：``Reconciler.sweep`` / ``sweep_task`` 存在；
7. ``EVIDENCE_OWNERSHIP``：证据读取签名**强制** ``tenant_id``；
8. ``INDEPENDENT_VERIFIER``：``Verifier`` 与独立回读适配器均存在；
9. ``SECURITY_NEGATIVE_SET``：负面集冻结 29 条且清单可加载。

删除任一项 → :class:`GateRemovedError`（``code="GATE_REMOVED"``），并可直接喂给
``publish_decision``（W11 不提供"跳过门禁"的开关）。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

GATE_REMOVED = "GATE_REMOVED"
GATE_SET_VERSION = "w11-gates-v1"

#: 冻结的九项 P0 门禁（顺序即报告顺序）。**不得删除、不得放宽**。
GATE_IDS: tuple[str, ...] = (
    "ENV_ISOLATION",
    "FIXED_READONLY_TOOLS",
    "SERVER_SIDE_RISK",
    "HUMAN_APPROVAL",
    "UNIQUE_TOOL_GATEWAY",
    "RECOVERY_RECONCILIATION",
    "EVIDENCE_OWNERSHIP",
    "INDEPENDENT_VERIFIER",
    "SECURITY_NEGATIVE_SET",
)

#: ``ToolGateway`` 的**唯一**允许装配点（相对仓库根）。
ALLOWED_GATEWAY_BUILDERS: tuple[str, ...] = ("workers/stack.py",)


class GateRemovedError(RuntimeError):
    """门禁缺失/被删/被放宽（P0 红线；``GateInventory`` 校验器同码）。"""

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = GATE_REMOVED
        self.details = dict(details or {})
        super().__init__(f"{GATE_REMOVED}: {message}")


class GateInventory(BaseModel):
    """九项门禁的在场清单；任一项缺失即校验失败。"""

    model_config = ConfigDict(extra="forbid")

    version: str = GATE_SET_VERSION
    gate_ids: tuple[str, ...] = GATE_IDS
    present: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _gate_ids_are_frozen(self) -> "GateInventory":
        """``gate_ids`` 必须与 :data:`GATE_IDS` **顺序与内容全等**（L-7：禁止裁剪）。

        为什么单独一段而不是只看 ``present``：``gate_ids`` 是可传参字段，若允许裁剪，
        构造 ``GateInventory(gate_ids=("ENV_ISOLATION",), present=...)`` 就能让
        "``_all_present`` 全过"变成一句空话——门禁清单本身就成了可绕过的开关。
        """
        if tuple(self.gate_ids) != GATE_IDS:
            raise ValueError(
                f"{GATE_REMOVED}: gate_ids 必须与冻结的 GATE_IDS 完全一致"
                f"（顺序与内容，共 {len(GATE_IDS)} 项）；"
                f"实际 {len(tuple(self.gate_ids))} 项 {list(self.gate_ids)}"
            )
        return self

    @model_validator(mode="after")
    def _all_present(self) -> "GateInventory":
        missing = [gate for gate in self.gate_ids if self.present.get(gate) is not True]
        if missing:
            raise GateRemovedError(
                f"门禁缺失或被删除：{missing}",
                details={"missing": missing, "present": dict(self.present)},
            )
        extra = sorted(set(self.present) - set(self.gate_ids))
        if extra:
            raise GateRemovedError(
                f"出现未登记的门禁项：{extra}", details={"extra": extra}
            )
        return self

    def count(self) -> int:
        return sum(1 for gate in self.gate_ids if self.present.get(gate) is True)


# ---------------------------------------------------------------------------
# 九项静态检查（每项一个纯函数：只读源码/模块对象）
# ---------------------------------------------------------------------------


def _gate_env_isolation(root: Path) -> bool:
    """环境隔离：未知/缺失环境一律按 PROD（fail closed）。"""
    from core.security.models import environment_tier

    if environment_tier("LAB") != "LAB":
        return False
    # 未知 / 缺失 / 空串都必须落到 PROD（fail closed）
    for unknown in ("", None, "PROD", "STAGING", "unknown", "N/A"):
        if environment_tier(unknown) != "PROD":
            return False
    source = (root / "adapters" / "lab_assembly.py").read_text(encoding="utf-8")
    return "def is_lab_mysql_target" in source and "LAB_ENVIRONMENT_STATUS" in source


def _gate_fixed_readonly_tools(root: Path) -> bool:
    """固定只读工具集合：观察器注册表内**没有**任何写工具。"""
    from database.observers import build_observer_registry

    definitions = build_observer_registry().definitions()
    if not definitions:
        return False
    return all(item.side_effect == "read" for item in definitions)


def _gate_server_side_risk(root: Path) -> bool:
    """服务端风险解析：模型提示只升不降，且缺策略 fail closed。"""
    from core.security.models import EnvironmentContext, Principal
    from core.security.policy import PolicyEngine
    from core.security.roles import Role
    from core.tools.contracts import ToolDefinition

    engine = PolicyEngine()
    definition = ToolDefinition(
        name="w11_gate_probe",
        description="W11 门禁静态探针（不注册到任何生产注册表）",
        risk_level="R4",
        side_effect="read",
        permissions=["mysql:read"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    environment = EnvironmentContext(
        environment_id="env_w11_gate",
        tenant_id="tenant_w11_gate",
        db_engine="mysql",
        tier="LAB",
    )
    principal = Principal(
        user_id="user_w11_gate", tenant_id="tenant_w11_gate", roles=(Role.OPERATOR,)
    )
    # 伪造低风险提示不得压低 R4
    decision = engine.decide(principal, definition, environment, model_risk_hint="R0")
    if decision.effective_risk != "R4" or decision.allowed:
        return False
    if decision.reason != "R4_NOT_EXECUTABLE":
        return False
    # 缺策略（definition=None）必须 fail closed
    missing = engine.decide(principal, None, environment)  # type: ignore[arg-type]
    return missing.allowed is False and missing.reason == "POLICY_MISSING"


def _gate_human_approval(root: Path) -> bool:
    """人工审批：策略对 R2 永不放行，且执行器拒绝无 grant 的写操作。"""
    from core.approval.models import ApprovalGrant
    from core.approval.service import ApprovalService
    from core.tools.executor import ToolExecutor

    if not hasattr(ApprovalService, "consume"):
        return False
    source = inspect.getsource(ToolExecutor._precheck)
    if "approval_grant is None" not in source:
        return False
    if "APPROVAL_REQUIRED" not in source:
        return False
    return inspect.isclass(ApprovalGrant)


def _gate_unique_tool_gateway(root: Path) -> bool:
    """唯一 Gateway 装配点：全仓（除测试/文档）只有 ``workers/stack.py`` 构造它。"""
    return tuple(sorted(_gateway_builder_files(root))) == ALLOWED_GATEWAY_BUILDERS


def _gateway_builder_files(root: Path) -> list[str]:
    """AST 层找 ``ToolGateway(...)`` 的**真实调用点**（不吃注释/文档字符串的假命中）。"""
    builders: list[str] = []
    for relative in _python_sources(root):
        if relative.startswith(("tests/", "benchmarks/")):
            continue
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "ToolGateway":
                builders.append(relative)
                break
    return builders


def _gate_recovery_reconciliation(root: Path) -> bool:
    """恢复/核对：``Reconciler`` 提供 sweep 与按任务核对两个入口。"""
    from core.execution.reconciler import Reconciler

    return all(
        callable(getattr(Reconciler, name, None))
        for name in ("sweep", "sweep_detailed", "sweep_task")
    )


def _gate_evidence_ownership(root: Path) -> bool:
    """Evidence 归属：读取/统计接口**强制**携带 ``tenant_id``（无缺省旁路）。"""
    from core.evidence.repository import EvidenceRepository

    for name in ("get", "count_for_task", "list_by_task"):
        parameters = inspect.signature(getattr(EvidenceRepository, name)).parameters
        if "tenant_id" not in parameters:
            return False
        if parameters["tenant_id"].default is not inspect.Parameter.empty:
            return False  # 有默认值 = 可省略 = 归属可能被绕过
    return True


def _gate_independent_verifier(root: Path) -> bool:
    """独立 Verifier：验证编排器与**独立的只读回读**适配器都存在。"""
    from adapters.mysql.readback import MysqlReadbackAdapter
    from core.verification.verifier import Verifier

    if not inspect.isclass(Verifier) or not inspect.isclass(MysqlReadbackAdapter):
        return False
    source = (root / "workers" / "stack.py").read_text(encoding="utf-8")
    # 回读必须用独立只读 client（不能复用执行侧 rw_client）
    return "readback = MysqlReadbackAdapter(ro_client)" in source


def _gate_security_negative_set(root: Path) -> bool:
    """安全负面集：冻结 29 条且清单可加载（不复算内容，只保证不被清空）。"""
    from core.assurance.negative import NEGATIVE_CASE_COUNT, load_negative_set

    manifest_path = root / "benchmarks" / "w11" / "negative_cases.json"
    if not manifest_path.exists():
        return False
    manifest = load_negative_set(manifest_path)
    return NEGATIVE_CASE_COUNT == 29 and len(manifest.cases) == NEGATIVE_CASE_COUNT


_GATE_CHECKS: dict[str, Callable[[Path], bool]] = {
    "ENV_ISOLATION": _gate_env_isolation,
    "FIXED_READONLY_TOOLS": _gate_fixed_readonly_tools,
    "SERVER_SIDE_RISK": _gate_server_side_risk,
    "HUMAN_APPROVAL": _gate_human_approval,
    "UNIQUE_TOOL_GATEWAY": _gate_unique_tool_gateway,
    "RECOVERY_RECONCILIATION": _gate_recovery_reconciliation,
    "EVIDENCE_OWNERSHIP": _gate_evidence_ownership,
    "INDEPENDENT_VERIFIER": _gate_independent_verifier,
    "SECURITY_NEGATIVE_SET": _gate_security_negative_set,
}

_PYTHON_CACHE: dict[Path, tuple[str, ...]] = {}


def _python_sources(root: Path) -> tuple[str, ...]:
    """仓库内 Python 源文件相对路径（跳过虚拟环境与缓存）。"""
    cached = _PYTHON_CACHE.get(root)
    if cached is not None:
        return cached
    paths: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith((".venv/", "dbguardian.egg-info/")) or "__pycache__" in relative:
            continue
        paths.append(relative)
    result = tuple(paths)
    _PYTHON_CACHE[root] = result
    return result


def check_gate_inventory(root: Path) -> GateInventory:
    """逐项静态判定九项门禁；任一项不成立 → :class:`GateRemovedError`。

    Args:
        root: 仓库根目录。

    Returns:
        :class:`GateInventory`（``present`` 九项全 ``True``）。

    Raises:
        GateRemovedError: 有门禁缺失/被删（``code="GATE_REMOVED"``）。
    """
    base = Path(root)
    present: dict[str, bool] = {}
    for gate in GATE_IDS:
        present[gate] = bool(_GATE_CHECKS[gate](base))
    return GateInventory(present=present)


def audit_gate_sources(root: Path) -> dict[str, Any]:
    """门禁相关的**静态事实快照**（供 RC 清单与收口件引用，不含业务数据）。"""
    base = Path(root)
    return {
        "gate_ids": list(GATE_IDS),
        "gateway_builders": sorted(_gateway_builder_files(base)),
        "observer_tool_count": len(_observer_definitions()),
        "observer_write_tools": [
            item.name for item in _observer_definitions() if item.side_effect != "read"
        ],
    }


def _observer_definitions() -> list[Any]:
    from database.observers import build_observer_registry

    return list(build_observer_registry().definitions())


def python_ast_names(path: Path) -> set[str]:
    """（工具函数）返回一个源文件里定义的全部顶层函数/类名；供负测与审计用。"""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


__all__ = [
    "ALLOWED_GATEWAY_BUILDERS",
    "GATE_IDS",
    "GATE_REMOVED",
    "GATE_SET_VERSION",
    "GateInventory",
    "GateRemovedError",
    "audit_gate_sources",
    "check_gate_inventory",
    "python_ast_names",
]
