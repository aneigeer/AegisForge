"""跨领域共享记忆库（UPG-03，Enterprise SecOps Memory）。

在 V2.0 ``core/memory/wiki`` 编译与 8-gate 检索分层之上，补齐**跨资产域**的共享记忆：

- 按租户隔离（RBAC 视角：SEC/DBA 可写，AUDITOR 只读）；
- 沉淀资产拓扑关系、历史变更窗口、业务峰值容忍度与跨栈误报特征；
- 检索为确定性打分（精确子串 > 词元重叠 > 类型权重 > 新近度），
  不引入随机性，便于回放与单测；
- **入库即拒密**：含 ``password: xxx`` / ``token=xxx`` 形态的明文一律拒绝，
  避免"记忆库"变成第二个凭据泄漏面。

与 8-gate Hybrid RAG 的关系：本模块只负责"存什么、怎么取"，不替代其网关校验与
证据闸门；写入前的内容仍应来自已脱敏的证据链。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from core.platform.ten_modules import RbacRole

#: 可写角色（审计员只读）。
_WRITE_ROLES = frozenset({RbacRole.SEC.value, RbacRole.DBA.value})

#: 明文凭据特征：命中即拒绝入库。
_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:password|passwd|pwd|secret|token|dsn|api[_-]?key|private[_-]?key|access[_-]?key)\b\s*[:=]\s*\S+"
)

#: 记忆类型权重（模板 > 人工升级特征 > 误报特征）。
_KIND_WEIGHT: dict[str, float] = {
    "REMEDIATION_TEMPLATE": 0.16,
    "HUMAN_ESCALATION_PATTERN": 0.13,
    "FALSE_POSITIVE_PATTERN": 0.10,
    "CHANGE_WINDOW": 0.08,
    "ASSET_TOPOLOGY": 0.06,
}


class MemoryPolicyError(ValueError):
    """违反记忆库安全策略（明文凭据 / 越权写入 / 租户缺失）。"""


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    tenant_id: str
    domain: str
    kind: str
    text: str
    workflow_run_id: str = ""
    tags: tuple[str, ...] = ()
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tags"] = list(self.tags)
        return data


@dataclass
class _TenantStore:
    records: list[MemoryRecord] = field(default_factory=list)
    topology: dict[str, dict[str, Any]] = field(default_factory=dict)
    change_windows: dict[str, dict[str, Any]] = field(default_factory=dict)
    rejected_total: int = 0


class SecOpsMemory:
    """跨领域共享记忆库（进程内、追加写、租户隔离）。"""

    def __init__(self, *, max_records_per_tenant: int = 500, clock: Any | None = None) -> None:
        self.max_records_per_tenant = max_records_per_tenant
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self._tenants: dict[str, _TenantStore] = {}
        self._seq = 0

    # -- 写入 ----------------------------------------------------------
    def remember(
        self,
        *,
        tenant_id: str,
        domain: str,
        kind: str,
        text: str,
        workflow_run_id: str = "",
        tags: Iterable[str] = (),
        role: str = RbacRole.SEC.value,
    ) -> bool:
        """写入一条记忆；不安全或越权时**返回 False**（不抛异常打断工作流）。"""
        try:
            self._assert_writable(tenant_id, text, role)
        except MemoryPolicyError:
            self._tenants.setdefault(tenant_id, _TenantStore()).rejected_total += 1
            return False

        store = self._tenants.setdefault(tenant_id, _TenantStore())
        if len(store.records) >= self.max_records_per_tenant:
            store.rejected_total += 1
            return False

        self._seq += 1
        record = MemoryRecord(
            memory_id=f"mem-{self._seq:05d}",
            tenant_id=tenant_id,
            domain=domain,
            kind=kind,
            text=text.strip(),
            workflow_run_id=workflow_run_id,
            tags=tuple(str(tag) for tag in tags),
            created_at=str(self._clock()),
        )
        store.records.append(record)
        return True

    def register_asset(
        self,
        *,
        tenant_id: str,
        asset_id: str,
        domain: str,
        relations: Iterable[dict[str, str]] = (),
        role: str = RbacRole.SEC.value,
    ) -> bool:
        """登记资产拓扑关系（例如 Web API -> DB 实例）。"""
        if role not in _WRITE_ROLES:
            return False
        store = self._tenants.setdefault(tenant_id, _TenantStore())
        store.topology[asset_id] = {
            "asset_id": asset_id,
            "domain": domain,
            "relations": [dict(item) for item in relations],
            "updated_at": str(self._clock()),
        }
        return True

    def record_change_window(
        self,
        *,
        tenant_id: str,
        asset_id: str,
        window: str,
        tolerance_pct: float,
        role: str = RbacRole.SEC.value,
    ) -> bool:
        """沉淀业务峰值容忍度（供后续定级与变更窗口选择复用）。"""
        if role not in _WRITE_ROLES:
            return False
        if not 0 <= tolerance_pct <= 100:
            raise ValueError("tolerance_pct 必须在 0~100 之间")
        store = self._tenants.setdefault(tenant_id, _TenantStore())
        store.change_windows[asset_id] = {
            "asset_id": asset_id,
            "window": window,
            "tolerance_pct": float(tolerance_pct),
            "updated_at": str(self._clock()),
        }
        return True

    # -- 读取 ----------------------------------------------------------
    def recall(
        self,
        *,
        tenant_id: str,
        domain: str | None = None,
        kind: str | None = None,
        query: str = "",
        limit: int = 10,
        role: str = RbacRole.SEC.value,
    ) -> list[dict[str, Any]]:
        """按租户检索；``domain=None`` 表示跨域共享检索。"""
        if role not in {item.value for item in RbacRole}:
            raise MemoryPolicyError(f"未知角色: {role}")
        store = self._tenants.get(tenant_id)
        if store is None:
            return []

        candidates = [
            record
            for record in store.records
            if (domain is None or record.domain == domain) and (kind is None or record.kind == kind)
        ]
        scored: list[tuple[float, int, MemoryRecord]] = []
        for index, record in enumerate(candidates):
            score = self._score(record, query)
            if query and score <= 0:
                continue
            scored.append((score, -index, record))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [
            {**record.to_dict(), "score": round(score, 4)}
            for score, _, record in scored[: max(0, limit)]
        ]

    def topology(self, tenant_id: str) -> dict[str, dict[str, Any]]:
        store = self._tenants.get(tenant_id)
        return {} if store is None else {key: dict(value) for key, value in store.topology.items()}

    def change_windows(self, tenant_id: str) -> dict[str, dict[str, Any]]:
        store = self._tenants.get(tenant_id)
        return {} if store is None else {key: dict(value) for key, value in store.change_windows.items()}

    def snapshot(self) -> dict[str, Any]:
        by_domain: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        rejected = 0
        total = 0
        for store in self._tenants.values():
            rejected += store.rejected_total
            total += len(store.records)
            for record in store.records:
                by_domain[record.domain] = by_domain.get(record.domain, 0) + 1
                by_kind[record.kind] = by_kind.get(record.kind, 0) + 1
        return {
            "tenants": len(self._tenants),
            "records": total,
            "rejected": rejected,
            "by_domain": by_domain,
            "by_kind": by_kind,
            "tenant_ids": sorted(self._tenants),
        }

    # -- 内部 ----------------------------------------------------------
    def _assert_writable(self, tenant_id: str, text: str, role: str) -> None:
        if role not in _WRITE_ROLES:
            raise MemoryPolicyError(f"角色 {role} 无权写入共享记忆库")
        if not tenant_id:
            raise MemoryPolicyError("tenant_id 不能为空")
        if not text or not text.strip():
            raise MemoryPolicyError("记忆内容不能为空")
        if _SECRET_PATTERN.search(text):
            raise MemoryPolicyError("记忆内容疑似包含明文凭据，拒绝入库")

    @staticmethod
    def _score(record: MemoryRecord, query: str) -> float:
        score = _KIND_WEIGHT.get(record.kind, 0.05)
        if not query:
            return score
        needle = query.strip().lower()
        haystack = record.text.lower()
        if needle and needle in haystack:
            score += 0.5
        query_tokens = {token for token in needle.split() if token}
        if query_tokens:
            overlap = query_tokens & {token for token in haystack.replace("/", " ").split()}
            score += min(0.4, 0.1 * len(overlap))
        if record.domain and record.domain.lower() in needle:
            score += 0.05
        return score


__all__ = ["MemoryPolicyError", "MemoryRecord", "SecOpsMemory"]
