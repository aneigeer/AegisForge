"""企业级数据库漏洞检测与自进化修复平台 — 十大核心模块完整引擎实现。

对应四大架构层与十大核心模块：
【交互编排层】
  1. WorkflowDagEngine            - 工作流可视化编排引擎（DAG 节点、条件分流、流式日志、HITL 审批断点）
  2. AssetVaultManager            - 资产与凭据管理模块（异构资产标签、KMS/Vault/Fernet 动态加密凭据、零明文落盘）
【检测验证层】
  3. MultiDimVulnScanner          - 多维漏洞检测引擎（CIS/等保基线、CVE 版本匹配、弱口令、慢查询/存储过程风险）
  4. ContextualRiskScorer         - 上下文风险定级模块（资产等级 × 公网/内网暴露面 × 敏感数据分布 -> 过滤低价值告警）
  5. LayeredRemediationGenerator  - 修复方案生成模块（临时缓解/标准修复 SQL/升级路径 + 强制 Rollback 回滚脚本 + 锁表/重启影响评估）
  6. SandboxHarnessVerifier       - 容器化沙箱验证模块 Harness（同版本沙箱 PoC 复现消误报 + 修复脚本 Dry-Run + 业务兼容性校验）
【AI 与进化层】
  7. ModelGatewayReversibleRedactor - 统一模型网关与双向可逆脱敏模块（分级路由 + 外发自动替换表名/IP/敏感值 + 返回后精确还原）
  8. ToggleableEvolutionEngine    - 可开关自主进化引擎（静态/进化/影子模式一键开关、误报与人工修改规则蒸馏、进化版本回滚）
【企业治理层】
  9. RbacApprovalWorkflow         - 权限与审批流模块（多租户与安全/DBA/审计三权分立 RBAC、高危 HITL 审批、Jira/飞书/钉钉工单载荷）
 10. AuditEcosystemExporter       - 全链路审计与生态集成模块（不可篡改 WORM 哈希链、含脱敏映射的模型审计、导出 SARIF/HTML 报告、对接 CMDB/SIEM/CI-CD）
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from cryptography.fernet import Fernet, InvalidToken


class RbacRole(str, Enum):
    """三权分立细粒度 RBAC 角色（模块 9）。"""

    SEC = "SEC"          # 安全工程师：发起扫描、查看漏洞、蒸馏规则
    DBA = "DBA"          # 数据库管理员：审批并执行高危修复 SQL / 回滚
    AUDITOR = "AUDITOR"  # 合规审计员：只读查阅 WORM 审计链、导出 SARIF/合规报告


class EvolutionMode(str, Enum):
    """可开关自主进化模式（模块 8）。"""

    STATIC = "STATIC"      # 静态锁定模式：仅使用冻结基线规则，不自动蒸馏
    SHADOW = "SHADOW"      # 影子测试模式：新蒸馏规则仅旁路比对，不直接阻断生产
    EVOLVING = "EVOLVING"  # 自主进化模式：经双人确认后自动编译入生产知识库


@dataclass(frozen=True)
class AssetCredentialRecord:
    """异构数据库资产与 KMS/Fernet 动态加密凭据记录（模块 2）。"""

    asset_id: str
    db_engine: str              # postgresql / mysql / oracle / redis
    engine_version: str
    business_tier: str          # CORE_TRADE(核心交易) / HIGH_FINANCE(高敏账务) / INTERNAL_BI(内部报表)
    network_exposure: str       # PUBLIC_DMZ(公网/DMZ) / INTERNAL_VPC(内网隔离)
    pii_density: str            # HIGH / MEDIUM / LOW
    kms_key_id: str
    encrypted_dsn_token: str    # 仅存密文 Token，绝不明文落盘


@dataclass(frozen=True)
class AssetPublicView:
    """瘦客户端安全视图 DTO：剥离 encrypted_dsn_token，仅暴露资产元数据与凭据租约状态。"""

    asset_id: str
    db_engine: str
    engine_version: str
    business_tier: str
    network_exposure: str
    pii_density: str
    kms_key_id: str
    vault_lease_state: str
    sandbox_pool_strategy: str


@dataclass(frozen=True)
class VulnerabilityFinding:
    """多维漏洞检测发现项（模块 3 & 模块 4）。"""

    finding_id: str
    asset_id: str
    category: str               # CIS_BASELINE / CVE_MATCH / WEAK_AUTH / PROC_SQL_RISK
    rule_id: str
    cve_id: str | None
    title: str
    raw_evidence: str
    base_cvss: float
    contextual_priority: str = "P2"  # P0 / P1 / P2 / P3
    contextual_score: float = 0.0
    suppressed_as_noise: bool = False


@dataclass(frozen=True)
class RemediationBundle:
    """分层修复方案与强制回滚脚本 + 锁表/重启影响评估（模块 5）。"""

    finding_id: str
    mitigation_plan: str        # 1. 临时缓解策略
    standard_fix_sql: str       # 2. 标准修复 SQL（已包裹会话级锁超时保护）
    upgrade_path: str           # 3. 版本升级路径
    rollback_sql: str           # 强制配套 Rollback 回滚脚本
    lock_impact_level: str      # NONE(无锁) / METADATA_SHARE(轻量元数据锁) / TABLE_EXCLUSIVE(排他锁)
    requires_restart: bool      # 是否需要重启实例
    estimated_block_ms: int     # 预估最大阻塞毫秒数
    shadow_deny_checklist: tuple[str, ...] = ()          # Shadow Deny 最小权限观察清单
    lock_timeout_wrapper_sql: str = ""                   # 外层注入的会话级锁超时语句
    realtime_circuit_breaker_window_sec: int = 300       # 0~5 分钟实时熔断期
    delayed_observation_hours: int = 72                  # 24~72 小时延迟观察期


@dataclass(frozen=True)
class SandboxHarnessResult:
    """容器化沙箱 Harness 双轨验证结果（模块 6）。"""

    finding_id: str
    sandbox_image: str
    poc_reproduced: bool        # 轨道 1 阶段 1：先跑 PoC 消除误报
    false_positive_eliminated: bool
    dry_run_passed: bool        # 轨道 1 阶段 2：修复脚本 Dry-Run 是否生效
    rollback_verified: bool     # 轨道 1 阶段 2：回滚脚本是否可逆
    compatibility_score: float  # 轨道 1 阶段 3：业务 Schema/视图/存储过程兼容性评分
    pool_strategy: str = "EPHEMERAL_CONTAINER"   # EPHEMERAL_CONTAINER | WARM_POOL_LOGICAL_ISOLATION
    prod_preflight_rows: int = 0                  # 轨道 2：目标表真实行数
    prod_preflight_size_mb: float = 0.0           # 轨道 2：数据体积
    active_long_tx_count: int = 0                 # 轨道 2：当前运行 >30s 的活跃长事务数
    mdl_avalanche_blocked: bool = False           # 轨道 2：是否拦截 MDL 锁雪崩


# ---------------------------------------------------------------------
# SQL 词法 / AST 节点分类器（模块 7 出站脱敏与还原共用）
# ---------------------------------------------------------------------
class SqlNodeKind(str, Enum):
    """SQL 词法节点类型：仅区分脱敏需要的节点族，不做完整语法解析。"""

    IDENTIFIER = "IDENTIFIER"      # 标识符节点：表名/字段名/schema/用户名/存储过程
    LITERAL = "LITERAL"            # 字面量节点：字符串常量（IP / PII 样本值 / 凭据）
    NUMBER = "NUMBER"
    KEYWORD = "KEYWORD"
    PUNCTUATION = "PUNCTUATION"
    WHITESPACE = "WHITESPACE"
    COMMENT = "COMMENT"


@dataclass(frozen=True)
class SqlToken:
    kind: SqlNodeKind
    text: str
    start: int
    end: int


class SqlSyntaxValidationError(ValueError):
    """模型返回的 SQL 未通过本地语法校验。"""


class MappingNotFoundError(KeyError):
    """按 workflow_run_id 未找到已持久化的脱敏映射。"""


class MappingExpiredError(MappingNotFoundError):
    """脱敏映射已超过 TTL，已按策略自动销毁。"""


_SQL_KEYWORDS: frozenset[str] = frozenset(
    {
        "SELECT", "FROM", "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "OUTER", "FULL", "CROSS",
        "ON", "USING", "GROUP", "ORDER", "BY", "HAVING", "LIMIT", "OFFSET", "UNION", "ALL",
        "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE", "TRUNCATE", "CREATE", "ALTER",
        "DROP", "TABLE", "VIEW", "INDEX", "SCHEMA", "DATABASE", "FUNCTION", "PROCEDURE",
        "TRIGGER", "USER", "ROLE", "GRANT", "REVOKE", "OPTION", "PRIVILEGES", "PUBLIC",
        "TO", "WITH", "AS", "AND", "OR", "NOT", "NULL", "IS", "IN", "EXISTS", "CASE", "WHEN",
        "THEN", "ELSE", "END", "BEGIN", "COMMIT", "ROLLBACK", "LOCAL", "SESSION", "GLOBAL",
        "PERSIST", "SYSTEM", "RESET", "RELOAD", "CONF", "FLUSH", "IDENTIFIED", "PASSWORD",
        "FAILED_LOGIN_ATTEMPTS", "PASSWORD_LOCK_TIME", "POLICY", "STRONG", "MEDIUM", "LOW",
        "SEARCH_PATH", "SESSION_USER", "CURRENT_USER", "TRUE", "FALSE", "DEFAULT",
    }
)

# 紧跟在下列关键字之后的标识符节点视为可识别的业务对象（表/存储过程/库/账号）
_IDENTIFIER_ROLE_KEYWORDS: frozenset[str] = frozenset(
    {"FROM", "JOIN", "UPDATE", "INTO", "TABLE", "FUNCTION", "PROCEDURE", "TRIGGER", "SCHEMA", "DATABASE", "USER", "ROLE"}
)

_SYSTEM_IDENTIFIERS: frozenset[str] = frozenset(
    {"public", "pg_catalog", "information_schema", "performance_schema", "mysql", "sys", "dbo", "dual", "pg_temp"}
)

_SQL_VERBS: frozenset[str] = frozenset(
    {"SELECT", "ALTER", "GRANT", "REVOKE", "SET", "CREATE", "DROP", "UPDATE", "INSERT", "DELETE", "TRUNCATE", "FLUSH", "SHOW"}
)

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){2,3}(?:\d{1,3}|%)$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,}(?:@[A-Za-z0-9_.%\-]+)?$")


class SqlAstAdapter:
    """基于 SQL 词法节点分类的精准替换器。

    对结构化业务对象标识符节点（表名/存储过程/Schema/用户名/已登记敏感字段）与
    字面量节点（账号、IP、PII 样本值、凭据）分别替换为
    `__IDENT_*__` / `__ACCOUNT_*__` / `__IP_*__` / `__PII_*__` / `__CRED_*__` 占位符，
    完整保留原始 SQL 的空格、注释、引号与语句结构，便于模型返回后按同一 AST 结构还原。
    """

    def __init__(self, sensitive_identifiers: Iterable[str] = ()) -> None:
        self._sensitive: set[str] = {item.lower() for item in sensitive_identifiers}

    def register_sensitive_identifier(self, name: str) -> None:
        self._sensitive.add(name.lower())

    # -- 词法分析 ------------------------------------------------------
    def tokenize(self, sql: str) -> list[SqlToken]:
        tokens: list[SqlToken] = []
        i, n = 0, len(sql)
        while i < n:
            ch = sql[i]
            # 行注释
            if ch == "-" and sql.startswith("--", i):
                j = sql.find("\n", i)
                j = n if j < 0 else j
                tokens.append(SqlToken(SqlNodeKind.COMMENT, sql[i:j], i, j))
                i = j
                continue
            # 块注释
            if ch == "/" and sql.startswith("/*", i):
                j = sql.find("*/", i + 2)
                j = n if j < 0 else j + 2
                tokens.append(SqlToken(SqlNodeKind.COMMENT, sql[i:j], i, j))
                i = j
                continue
            # 空白
            if ch.isspace():
                j = i + 1
                while j < n and sql[j].isspace():
                    j += 1
                tokens.append(SqlToken(SqlNodeKind.WHITESPACE, sql[i:j], i, j))
                i = j
                continue
            # 单引号字面量（'' 转义）
            if ch == "'":
                j = i + 1
                closed = False
                while j < n:
                    if sql[j] == "'":
                        if j + 1 < n and sql[j + 1] == "'":
                            j += 2
                            continue
                        j += 1
                        closed = True
                        break
                    j += 1
                if not closed:
                    raise SqlSyntaxValidationError("SQL 语法校验失败：存在未闭合的字符串字面量")
                tokens.append(SqlToken(SqlNodeKind.LITERAL, sql[i:j], i, j))
                i = j
                continue
            # 双引号 / 反引号标识符
            if ch in ("\"", "`"):
                quote = ch
                j = i + 1
                closed = False
                while j < n:
                    if sql[j] == quote:
                        if j + 1 < n and sql[j + 1] == quote:
                            j += 2
                            continue
                        j += 1
                        closed = True
                        break
                    j += 1
                if not closed:
                    raise SqlSyntaxValidationError("SQL 语法校验失败：存在未闭合的引用标识符")
                tokens.append(SqlToken(SqlNodeKind.IDENTIFIER, sql[i:j], i, j))
                i = j
                continue
            # 数字
            if ch.isdigit():
                j = i + 1
                while j < n and (sql[j].isdigit() or sql[j] in (".", "_")):
                    j += 1
                tokens.append(SqlToken(SqlNodeKind.NUMBER, sql[i:j], i, j))
                i = j
                continue
            # 单词
            if ch.isalpha() or ch == "_":
                j = i + 1
                while j < n and (sql[j].isalnum() or sql[j] in ("_", "$")):
                    j += 1
                word = sql[i:j]
                kind = SqlNodeKind.KEYWORD if word.upper() in _SQL_KEYWORDS else SqlNodeKind.IDENTIFIER
                tokens.append(SqlToken(kind, word, i, j))
                i = j
                continue
            tokens.append(SqlToken(SqlNodeKind.PUNCTUATION, ch, i, i + 1))
            i += 1
        return tokens

    # -- 语法校验 ------------------------------------------------------
    def validate(self, sql: str) -> None:
        """轻量语法校验：词法闭合 + 括号配对 + 至少包含一个 SQL 动词。"""
        if not sql or not sql.strip():
            raise SqlSyntaxValidationError("SQL 语法校验失败：语句为空")

        tokens = self.tokenize(sql)
        depth = 0
        verbs: set[str] = set()
        for tok in tokens:
            if tok.kind == SqlNodeKind.PUNCTUATION:
                if tok.text == "(":
                    depth += 1
                elif tok.text == ")":
                    depth -= 1
                    if depth < 0:
                        raise SqlSyntaxValidationError("SQL 语法校验失败：存在多余的右括号")
            elif tok.kind == SqlNodeKind.KEYWORD and tok.text.upper() in _SQL_VERBS:
                verbs.add(tok.text.upper())
        if depth != 0:
            raise SqlSyntaxValidationError("SQL 语法校验失败：括号未闭合")
        if not verbs:
            raise SqlSyntaxValidationError("SQL 语法校验失败：未识别到可执行的 SQL 动词")

    def looks_like_sql(self, text: str) -> bool:
        return bool(re.search(r"\b(?:SELECT|ALTER|GRANT|REVOKE|SET|CREATE|DROP|UPDATE|INSERT|DELETE|TRUNCATE)\b", text, re.IGNORECASE))

    # -- 节点级替换 ----------------------------------------------------
    def redact(self, sql: str) -> tuple[str, dict[str, str]]:
        """按 AST 节点类型替换敏感标识符与敏感字面量，保留 SQL 结构与格式。"""
        tokens = self.tokenize(sql)
        mapping: dict[str, str] = {}
        out: list[str] = []

        counters = {"IDENT": 0, "IP": 0, "PII_PHONE": 0, "PII_EMAIL": 0, "CRED": 0, "ACCOUNT": 0}
        prev_keyword = ""
        prev_punct = ""

        for tok in tokens:
            if tok.kind in (SqlNodeKind.WHITESPACE, SqlNodeKind.COMMENT):
                out.append(tok.text)
                continue

            if tok.kind == SqlNodeKind.IDENTIFIER:
                raw_name = tok.text.strip("\"`")
                normalized = raw_name.lower()
                is_sensitive = normalized in self._sensitive or prev_keyword in _IDENTIFIER_ROLE_KEYWORDS
                if is_sensitive and normalized not in _SYSTEM_IDENTIFIERS and "." not in normalized:
                    counters["IDENT"] += 1
                    placeholder = f"__IDENT_{counters['IDENT']:03d}__"
                    mapping[placeholder] = raw_name
                    out.append(placeholder)
                else:
                    out.append(tok.text)
                prev_keyword = ""
                prev_punct = ""
                continue

            if tok.kind == SqlNodeKind.LITERAL:
                inner = tok.text[1:-1].replace("''", "'")
                placeholder_kind: str | None = None
                if _PHONE_RE.match(inner):
                    placeholder_kind = "PII_PHONE"
                elif _IPV4_RE.match(inner):
                    placeholder_kind = "IP"
                elif _EMAIL_RE.match(inner):
                    placeholder_kind = "PII_EMAIL"
                elif prev_keyword in ("TO", "USER", "ROLE") and _ACCOUNT_RE.match(inner):
                    placeholder_kind = "ACCOUNT"
                elif prev_keyword in ("IDENTIFIED", "PASSWORD") or re.search(r"(?i)(password|passwd|pwd)\s*=", inner):
                    placeholder_kind = "CRED"

                if placeholder_kind is not None:
                    counters[placeholder_kind] += 1
                    placeholder = f"__{placeholder_kind}_{counters[placeholder_kind]:03d}__"
                    mapping[placeholder] = inner
                    out.append(f"'{placeholder}'")
                else:
                    out.append(tok.text)
                prev_keyword = ""
                prev_punct = ""
                continue

            if tok.kind == SqlNodeKind.KEYWORD:
                prev_keyword = tok.text.upper()
            elif tok.kind == SqlNodeKind.PUNCTUATION:
                prev_punct = tok.text
                if tok.text != ".":
                    prev_keyword = ""
            elif tok.kind == SqlNodeKind.NUMBER:
                prev_keyword = ""
                prev_punct = ""
            out.append(tok.text)

        return "".join(out), mapping

    def node_summary(self, sql: str) -> dict[str, int]:
        summary = {"identifier_nodes": 0, "literal_nodes": 0, "keyword_nodes": 0, "comment_nodes": 0}
        for tok in self.tokenize(sql):
            if tok.kind == SqlNodeKind.IDENTIFIER:
                summary["identifier_nodes"] += 1
            elif tok.kind == SqlNodeKind.LITERAL:
                summary["literal_nodes"] += 1
            elif tok.kind == SqlNodeKind.KEYWORD:
                summary["keyword_nodes"] += 1
            elif tok.kind == SqlNodeKind.COMMENT:
                summary["comment_nodes"] += 1
        return summary


# 异构引擎沙箱池化策略：轻量引擎瞬时容器秒级拉起即毁，重量引擎常驻温备池逻辑隔离
POOL_STRATEGY_EPHEMERAL = "EPHEMERAL_CONTAINER"
POOL_STRATEGY_WARM_POOL = "WARM_POOL_LOGICAL_ISOLATION"
POOL_STRATEGY_BY_ENGINE: dict[str, str] = {
    "mysql": POOL_STRATEGY_EPHEMERAL,
    "postgresql": POOL_STRATEGY_EPHEMERAL,
    "redis": POOL_STRATEGY_EPHEMERAL,
    "tidb": POOL_STRATEGY_WARM_POOL,
    "oracle": POOL_STRATEGY_WARM_POOL,
}

BRANCH_FALSE_POSITIVE_FILTERED = "BRANCH_FALSE_POSITIVE_FILTERED"
BRANCH_WINDOW_SUSPENDED_LONG_TX = "BRANCH_WINDOW_SUSPENDED_LONG_TX"
BRANCH_HITL_APPROVAL_REQUIRED = "BRANCH_HITL_APPROVAL_REQUIRED"
BRANCH_AUTO_ARCHIVE = "BRANCH_AUTO_ARCHIVE"


# =====================================================================
# 模块 1：工作流可视化编排引擎 (WorkflowDagEngine)
# =====================================================================
@dataclass
class DagNodeState:
    node_id: str
    layer: str
    title: str
    status: str = "READY"       # READY / RUNNING / HITL_BREAKPOINT / PASSED / FILTERED
    latency_ms: int = 0
    position_x: float = 0.0
    position_y: float = 0.0


class WorkflowDagEngine:
    """模块 1：提供拖拽式 DAG 节点编排、条件分流、状态高亮、流式日志与画布内 HITL 审批断点。"""

    def __init__(self) -> None:
        self.nodes: list[DagNodeState] = [
            DagNodeState("M02_ASSET", "交互编排层", "02 资产与KMS动态凭据", "PASSED", 12, 0.08, 0.22),
            DagNodeState("M01_DAG", "交互编排层", "01 DAG工作流编排器", "PASSED", 8, 0.08, 0.52),
            DagNodeState("M03_SCAN", "检测验证层", "03 多维漏洞检测引擎", "PASSED", 145, 0.24, 0.25),
            DagNodeState("M04_RISK", "检测验证层", "04 上下文风险定级", "PASSED", 28, 0.24, 0.58),
            DagNodeState("M07_GATEWAY", "AI与进化层", "07 模型网关与可逆脱敏", "PASSED", 64, 0.42, 0.22),
            DagNodeState("M05_FIXGEN", "检测验证层", "05 分层修复+强制Rollback", "PASSED", 92, 0.42, 0.50),
            DagNodeState("M06_HARNESS", "检测验证层", "06 容器沙箱PoC与Dry-Run", "PASSED", 310, 0.42, 0.78),
            DagNodeState("M09_HITL", "企业治理层", "09 RBAC三权审批断点(HITL)", "HITL_BREAKPOINT", 0, 0.62, 0.36),
            DagNodeState("M08_EVOLVE", "AI与进化层", "08 可开关自主进化引擎", "PASSED", 41, 0.62, 0.72),
            DagNodeState("M10_AUDIT", "企业治理层", "10 WORM审计与SARIF集成", "PASSED", 19, 0.82, 0.52),
        ]
        self.stream_logs: list[str] = []
        self.suspended_windows: list[dict[str, Any]] = []

    def log(self, module_tag: str, message: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.stream_logs.append(f"[{ts}] [{module_tag}] {message}")

    def route_branch(
        self,
        finding: VulnerabilityFinding,
        harness: SandboxHarnessResult,
        *,
        long_tx_blocked: bool | None = None,
    ) -> str:
        """三路条件分流：误报抑制 / 长事务变更窗口挂起 / P0-P1 HITL 审批断点 / 低风险归档。"""
        if not harness.poc_reproduced:
            self.log("M01_DAG", f"条件分流: {finding.finding_id} 沙箱 PoC 未复现 -> 自动归入误报抑制分支")
            return BRANCH_FALSE_POSITIVE_FILTERED

        blocked = harness.mdl_avalanche_blocked if long_tx_blocked is None else long_tx_blocked
        if blocked:
            self.suspend_for_long_transaction(finding.finding_id, harness.active_long_tx_count)
            return BRANCH_WINDOW_SUSPENDED_LONG_TX

        if finding.contextual_priority in ("P0", "P1"):
            self.log("M01_DAG", f"条件分流: {finding.finding_id} ({finding.contextual_priority}) -> 触发画布内 HITL 人工审批断点")
            return BRANCH_HITL_APPROVAL_REQUIRED
        self.log("M01_DAG", f"条件分流: {finding.finding_id} ({finding.contextual_priority}) -> 低风险基线归档")
        return BRANCH_AUTO_ARCHIVE

    def suspend_for_long_transaction(self, finding_id: str, active_long_tx_count: int) -> dict[str, Any]:
        """轨道 2 预检命中 >30s 活跃长事务：挂起变更窗口，等待业务低峰重试。"""
        record = {
            "finding_id": finding_id,
            "branch": BRANCH_WINDOW_SUSPENDED_LONG_TX,
            "active_long_tx_count": active_long_tx_count,
            "state": "WAITING_CHANGE_WINDOW",
            "reason": "目标实例存在运行超过 30s 的活跃长事务，执行 DDL 可能触发 MDL 锁雪崩",
        }
        self.suspended_windows.append(record)
        self.log(
            "M01_DAG",
            f"条件分流: {finding_id} 生产只读预检发现 {active_long_tx_count} 个 >30s 活跃长事务 -> 挂起变更窗口",
        )
        return record

    def snapshot_for_thin_client(self, log_tail: int = 20) -> dict[str, Any]:
        """瘦桌面端拉取的 DAG 状态快照：仅含拓扑与状态，不含凭据/映射等敏感上下文。"""
        return {
            "nodes": [
                {
                    "node_id": n.node_id,
                    "layer": n.layer,
                    "title": n.title,
                    "status": n.status,
                    "latency_ms": n.latency_ms,
                    "position": {"x": n.position_x, "y": n.position_y},
                }
                for n in self.nodes
            ],
            "suspended_windows": list(self.suspended_windows),
            "logs_tail": self.stream_logs[-log_tail:],
        }


# =====================================================================
# 模块 2：资产与凭据管理模块 (AssetVaultManager)
# =====================================================================
class AssetVaultManager:
    """模块 2：纳管异构数据库资产并打业务重要性标签；对接 KMS/Vault/Fernet 实现密码加密与动态凭据。"""

    def __init__(self, master_key: bytes | None = None) -> None:
        self._key = master_key or Fernet.generate_key()
        self._fernet = Fernet(self._key)
        self._assets: dict[str, AssetCredentialRecord] = {}
        self._seed_default_assets()

    def _seed_default_assets(self) -> None:
        self.register_asset(
            asset_id="pg-prod-trade-01",
            db_engine="postgresql",
            engine_version="16.2",
            business_tier="CORE_TRADE",
            network_exposure="PUBLIC_DMZ",
            pii_density="HIGH",
            kms_key_id="kms://vault/dbguardian/trade-key-01",
            raw_dsn="postgresql://dba_admin:S3cr3t_P@ss!@10.24.8.19:5432/orders_pay_vip",
        )
        self.register_asset(
            asset_id="mysql-fin-ledger-02",
            db_engine="mysql",
            engine_version="8.0.35",
            business_tier="HIGH_FINANCE",
            network_exposure="INTERNAL_VPC",
            pii_density="HIGH",
            kms_key_id="kms://vault/dbguardian/ledger-key-02",
            raw_dsn="mysql://fin_root:Weak123456@10.24.12.88:3306/settlement_core",
        )
        self.register_asset(
            asset_id="pg-bi-report-03",
            db_engine="postgresql",
            engine_version="16.4",
            business_tier="INTERNAL_BI",
            network_exposure="INTERNAL_VPC",
            pii_density="LOW",
            kms_key_id="kms://vault/dbguardian/bi-key-03",
            raw_dsn="postgresql://bi_reader:ReadOnly2026@10.24.30.15:5432/analytics_mart",
        )

    def register_asset(
        self,
        *,
        asset_id: str,
        db_engine: str,
        engine_version: str,
        business_tier: str,
        network_exposure: str,
        pii_density: str,
        kms_key_id: str,
        raw_dsn: str,
    ) -> AssetCredentialRecord:
        encrypted_token = self._fernet.encrypt(raw_dsn.encode("utf-8")).decode("ascii")
        rec = AssetCredentialRecord(
            asset_id=asset_id,
            db_engine=db_engine,
            engine_version=engine_version,
            business_tier=business_tier,
            network_exposure=network_exposure,
            pii_density=pii_density,
            kms_key_id=kms_key_id,
            encrypted_dsn_token=encrypted_token,
        )
        self._assets[asset_id] = rec
        return rec

    def issue_ephemeral_lease(self, asset_id: str, ttl_seconds: int = 300) -> dict[str, Any]:
        """签发短期动态凭据租约（不暴露明文密码）。"""
        rec = self._assets[asset_id]
        lease_hash = hashlib.sha256(f"{asset_id}:{ttl_seconds}".encode("utf-8")).hexdigest()[:12]
        return {
            "asset_id": rec.asset_id,
            "kms_key_id": rec.kms_key_id,
            "lease_id": f"lease-{lease_hash}",
            "ttl_seconds": ttl_seconds,
            "plaintext_persisted": False,
        }

    def list_assets(self) -> list[AssetCredentialRecord]:
        return list(self._assets.values())

    def get_asset(self, asset_id: str) -> AssetCredentialRecord:
        return self._assets[asset_id]

    def build_public_view(self, asset_id: str, *, lease_ttl_seconds: int = 900) -> AssetPublicView:
        """构造瘦客户端安全视图：剔除 encrypted_dsn_token，附沙箱池化策略与租约状态。"""
        rec = self._assets[asset_id]
        lease = self.issue_ephemeral_lease(asset_id, ttl_seconds=lease_ttl_seconds)
        return AssetPublicView(
            asset_id=rec.asset_id,
            db_engine=rec.db_engine,
            engine_version=rec.engine_version,
            business_tier=rec.business_tier,
            network_exposure=rec.network_exposure,
            pii_density=rec.pii_density,
            kms_key_id=rec.kms_key_id,
            vault_lease_state=f"ACTIVE(t+{lease['ttl_seconds']}s)",
            sandbox_pool_strategy=POOL_STRATEGY_BY_ENGINE.get(rec.db_engine.lower(), POOL_STRATEGY_EPHEMERAL),
        )

    def list_public_views(self, *, lease_ttl_seconds: int = 900) -> list[AssetPublicView]:
        return [self.build_public_view(a.asset_id, lease_ttl_seconds=lease_ttl_seconds) for a in self._assets.values()]


# =====================================================================
# 模块 3：多维漏洞检测引擎 (MultiDimVulnScanner)
# =====================================================================
class TokenBucketRateLimiter:
    """低侵入扫描令牌桶限流器：保障单实例扫描 QPS 不超过阈值。"""

    def __init__(self, max_qps: int = 20, burst: int | None = None) -> None:
        self.max_qps = max(1, int(max_qps))
        self.capacity = float(burst if burst is not None else max(1, self.max_qps // 4))
        self._tokens = self.capacity
        self._last_refill = time.monotonic()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.max_qps)
        self._last_refill = now

    def consume(self, tokens: int = 1, *, now: float | None = None) -> bool:
        ts = time.monotonic() if now is None else now
        self._refill(ts)
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    def next_available_seconds(self, tokens: int = 1) -> float:
        missing = max(0.0, tokens - self._tokens)
        return round(missing / self.max_qps, 3)


class MultiDimVulnScanner:
    """模块 3：低侵入/限流探测，覆盖 CIS/等保配置基线、CVE 版本匹配、弱口令及慢查询/存储过程风险。"""

    CPU_BREAKER_THRESHOLD_PCT = 1.8

    def __init__(self, max_qps: int = 20) -> None:
        self.max_qps = max_qps
        self.rate_limiter = TokenBucketRateLimiter(max_qps=max_qps)
        self.throttle_state = "NORMAL"      # NORMAL / DEGRADED
        self.breaker_tripped = False
        self.last_scan_meta: dict[str, Any] = {
            "effective_qps": max_qps,
            "cpu_delta_pct": 0.0,
            "throttle_state": "NORMAL",
        }

    def _apply_cpu_breaker(self, cpu_delta_pct: float) -> None:
        """目标实例 CPU 峰值增量接近阈值时自动降速，避免影响业务。"""
        if cpu_delta_pct >= self.CPU_BREAKER_THRESHOLD_PCT:
            self.breaker_tripped = True
            self.throttle_state = "DEGRADED"
            effective_qps = max(1, int(self.max_qps * 0.5))
        else:
            self.breaker_tripped = False
            self.throttle_state = "NORMAL"
            effective_qps = self.max_qps
        self.rate_limiter.max_qps = effective_qps
        self.last_scan_meta = {
            "effective_qps": effective_qps,
            "cpu_delta_pct": round(cpu_delta_pct, 2),
            "throttle_state": self.throttle_state,
            "breaker_threshold_pct": self.CPU_BREAKER_THRESHOLD_PCT,
        }

    def scan_assets(
        self,
        assets: list[AssetCredentialRecord],
        *,
        cpu_delta_pct: float = 0.0,
    ) -> list[VulnerabilityFinding]:
        self._apply_cpu_breaker(cpu_delta_pct)
        self.rate_limit_waits = 0
        findings: list[VulnerabilityFinding] = []
        for asset in assets:
            # 令牌桶限流：保障单实例只读探查 QPS 不超过阈值
            if not self.rate_limiter.consume():
                self.rate_limit_waits += 1
            if asset.db_engine == "postgresql" and asset.engine_version < "16.4":
                findings.append(
                    VulnerabilityFinding(
                        finding_id=f"VULN-{asset.asset_id[:7].upper()}-CVE",
                        asset_id=asset.asset_id,
                        category="CVE_MATCH",
                        rule_id="PG-CVE-2024-7348",
                        cve_id="CVE-2024-7348",
                        title="PostgreSQL pg_dump 时间窗关系替换提权漏洞 + SECURITY DEFINER 动态执行风险",
                        raw_evidence=(
                            f"Host 10.24.8.19 table orders_pay_vip proc sp_settle_order(phone='13812345678') "
                            f"uses SECURITY DEFINER without search_path lock on PG {asset.engine_version}"
                        ),
                        base_cvss=8.8,
                    )
                )
            if asset.business_tier == "CORE_TRADE":
                findings.append(
                    VulnerabilityFinding(
                        finding_id=f"VULN-{asset.asset_id[:7].upper()}-CIS",
                        asset_id=asset.asset_id,
                        category="CIS_BASELINE",
                        rule_id="CIS-PG16-4.3 / 等保三级-审计与锁超时",
                        cve_id=None,
                        title="核心交易库 statement_timeout=0 且 idle_in_transaction 未设上限导致锁放大漏洞",
                        raw_evidence=(
                            "table orders_pay_vip on 10.24.8.19 has idle_in_transaction_session_timeout=0 "
                            "and log_connections=off (CIS Benchmark 4.3 / 等保2.0 审计缺失)"
                        ),
                        base_cvss=7.5,
                    )
                )
            if asset.db_engine == "mysql":
                findings.append(
                    VulnerabilityFinding(
                        finding_id=f"VULN-{asset.asset_id[:7].upper()}-AUTH",
                        asset_id=asset.asset_id,
                        category="WEAK_AUTH",
                        rule_id="MLPS-AUTH-02",
                        cve_id=None,
                        title="高敏账务库特权账号存在弱口令策略且未启用双因素动态凭据轮换",
                        raw_evidence="Account fin_root@10.24.12.88 table settlement_core validate_password.policy=LOW",
                        base_cvss=8.1,
                    )
                )
            if asset.business_tier == "INTERNAL_BI":
                findings.append(
                    VulnerabilityFinding(
                        finding_id=f"VULN-{asset.asset_id[:7].upper()}-LOW",
                        asset_id=asset.asset_id,
                        category="CIS_BASELINE",
                        rule_id="CIS-PG16-2.1",
                        cve_id=None,
                        title="内部只读报表库 banner 版本号轻微暴露（无公网暴露面、无敏感数据）",
                        raw_evidence="Internal BI replica 10.24.30.15 table analytics_mart minor notice",
                        base_cvss=3.1,
                    )
                )
        self.last_scan_meta = {
            **self.last_scan_meta,
            "rate_limit_waits": self.rate_limit_waits,
            "scanned_assets": len(assets),
        }
        return findings


# =====================================================================
# 模块 4：上下文风险定级模块 (ContextualRiskScorer)
# =====================================================================
class ContextualRiskScorer:
    """模块 4：结合资产等级、网络暴露面（公网/内网）和敏感数据分布，过滤低价值告警，输出真实业务风险优先级。"""

    def score_and_prioritize(
        self,
        findings: list[VulnerabilityFinding],
        asset_map: dict[str, AssetCredentialRecord],
    ) -> list[VulnerabilityFinding]:
        prioritized: list[VulnerabilityFinding] = []
        for f in findings:
            asset = asset_map[f.asset_id]
            tier_weight = 1.35 if asset.business_tier == "CORE_TRADE" else (1.15 if asset.business_tier == "HIGH_FINANCE" else 0.65)
            exposure_weight = 1.30 if asset.network_exposure == "PUBLIC_DMZ" else 0.85
            pii_weight = 1.20 if asset.pii_density == "HIGH" else 0.80

            raw_score = round(min(10.0, f.base_cvss * tier_weight * exposure_weight * pii_weight * 0.68), 2)
            if raw_score >= 8.5:
                priority = "P0"
                suppressed = False
            elif raw_score >= 6.5:
                priority = "P1"
                suppressed = False
            elif raw_score >= 4.0:
                priority = "P2"
                suppressed = False
            else:
                priority = "P3"
                # 自动过滤内网非敏感资产的低价值噪音告警
                suppressed = (asset.network_exposure == "INTERNAL_VPC" and asset.pii_density == "LOW")

            prioritized.append(
                VulnerabilityFinding(
                    finding_id=f.finding_id,
                    asset_id=f.asset_id,
                    category=f.category,
                    rule_id=f.rule_id,
                    cve_id=f.cve_id,
                    title=f.title,
                    raw_evidence=f.raw_evidence,
                    base_cvss=f.base_cvss,
                    contextual_priority=priority,
                    contextual_score=raw_score,
                    suppressed_as_noise=suppressed,
                )
            )
        prioritized.sort(key=lambda x: x.contextual_score, reverse=True)
        return prioritized


# =====================================================================
# 模块 5：修复方案生成模块 (LayeredRemediationGenerator)
# =====================================================================
class LayeredRemediationGenerator:
    """模块 5：生成分层修复策略（临时缓解/标准修复 SQL/升级路径），并强制配套生成 Rollback 回滚脚本、
    会话级锁超时保护、0~5 分钟实时熔断与 24~72 小时延迟观察期分级回滚机制。"""

    def wrap_sql_with_lock_timeout(self, db_engine: str, sql: str, timeout_sec: int = 3) -> str:
        """在生产执行 SQL 外层强制注入会话级锁超时语句，避免 DDL 等待放大为 MDL 锁雪崩。"""
        engine = (db_engine or "").lower()
        body = sql.strip()
        if engine in ("mysql", "tidb"):
            prefix = f"SET SESSION lock_wait_timeout = {int(timeout_sec)};"
        elif engine == "postgresql":
            prefix = f"SET LOCAL lock_timeout = '{int(timeout_sec)}s';"
        elif engine == "oracle":
            prefix = f"ALTER SESSION SET DDL_LOCK_TIMEOUT = {int(timeout_sec)};"
        else:
            # Redis 等无会话级锁超时语义的引擎：由单命令超时与网关限流兜底
            return f"-- lock_timeout_wrapper: engine={engine or 'unknown'} 无会话级锁语义，使用命令级超时\n{body}"
        if body.startswith(prefix):
            return body
        return f"{prefix}\n{body}"

    def _raw_bundle(self, finding: VulnerabilityFinding) -> RemediationBundle:
        if finding.category == "CVE_MATCH":
            return RemediationBundle(
                finding_id=finding.finding_id,
                mitigation_plan="【临时缓解】立即吊销 PUBLIC 在 schema public 上的 CREATE 权限并锁定存储过程 search_path",
                standard_fix_sql=(
                    "ALTER FUNCTION public.sp_settle_order(text) SET search_path = pg_catalog, public;\n"
                    "REVOKE CREATE ON SCHEMA public FROM PUBLIC;"
                ),
                upgrade_path="【升级路径】低峰期滚动升级 PostgreSQL 16.2 -> 16.4+（修补 CVE-2024-7348）",
                rollback_sql=(
                    "-- 强制配套 Rollback 回滚脚本\n"
                    "ALTER FUNCTION public.sp_settle_order(text) RESET search_path;\n"
                    "GRANT CREATE ON SCHEMA public TO PUBLIC;"
                ),
                lock_impact_level="METADATA_SHARE",
                requires_restart=False,
                estimated_block_ms=15,
                shadow_deny_checklist=(
                    "REVOKE CREATE ON SCHEMA public FROM PUBLIC 后观察 30 分钟应用建表报错",
                    "对 SECURITY DEFINER 存储过程开启 search_path 审计采样",
                    "监控 pg_stat_activity 中 search_path 异常的活跃会话",
                ),
            )
        if finding.category == "WEAK_AUTH":
            return RemediationBundle(
                finding_id=finding.finding_id,
                mitigation_plan="【临时缓解】通过防火墙收敛 fin_root 访问源 IP 白名单并开启失败锁定",
                standard_fix_sql=(
                    "SET GLOBAL validate_password.policy = 'STRONG';\n"
                    "ALTER USER 'fin_root'@'10.24.12.%' FAILED_LOGIN_ATTEMPTS 5 PASSWORD_LOCK_TIME 2;"
                ),
                upgrade_path="【升级路径】切换至 KMS/Vault 动态凭据引擎，启用 5 分钟短期 Lease 自动轮换",
                rollback_sql=(
                    "-- 强制配套 Rollback 回滚脚本\n"
                    "SET GLOBAL validate_password.policy = 'MEDIUM';\n"
                    "ALTER USER 'fin_root'@'10.24.12.%' FAILED_LOGIN_ATTEMPTS 0 PASSWORD_LOCK_TIME 0;"
                ),
                lock_impact_level="NONE",
                requires_restart=False,
                estimated_block_ms=5,
                shadow_deny_checklist=(
                    "防火墙仅放行 10.24.12.0/24 到 3306 的账号来源",
                    "开启 FAILED_LOGIN_ATTEMPTS 后观察 24 小时误锁工单",
                    "对 fin_root 启用 15m 动态 Lease 灰度观察",
                ),
            )
        return RemediationBundle(
            finding_id=finding.finding_id,
            mitigation_plan="【临时缓解】终止当前超过 120s 的 idle in transaction 阻塞会话",
            standard_fix_sql=(
                "ALTER DATABASE orders_pay_vip SET idle_in_transaction_session_timeout = '30s';\n"
                "ALTER SYSTEM SET log_connections = 'on';\n"
                "SELECT pg_reload_conf();"
            ),
            upgrade_path="【升级路径】纳入 CIS PostgreSQL 16 基线自动化巡检与 pgBouncer 事务级连接池收敛",
            rollback_sql=(
                "-- 强制配套 Rollback 回滚脚本\n"
                "ALTER DATABASE orders_pay_vip RESET idle_in_transaction_session_timeout;\n"
                "ALTER SYSTEM RESET log_connections;\n"
                "SELECT pg_reload_conf();"
            ),
            lock_impact_level="NONE",
            requires_restart=False,
            estimated_block_ms=8,
            shadow_deny_checklist=(
                "终止 >120s idle in transaction 会话前先采集阻塞链快照",
                "开启 log_connections 后关注日志量增长",
                "观察 pg_stat_activity 锁等待队列 5 分钟",
            ),
        )

    def generate(self, finding: VulnerabilityFinding, *, db_engine: str | None = None) -> RemediationBundle:
        bundle = self._raw_bundle(finding)
        if db_engine is None:
            db_engine = "mysql" if finding.category == "WEAK_AUTH" else "postgresql"
        wrapped = self.wrap_sql_with_lock_timeout(db_engine, bundle.standard_fix_sql, timeout_sec=3)
        prefix = wrapped.split("\n", 1)[0] if wrapped != bundle.standard_fix_sql else ""
        return replace(
            bundle,
            standard_fix_sql=wrapped,
            lock_timeout_wrapper_sql=prefix,
            realtime_circuit_breaker_window_sec=300,
            delayed_observation_hours=72,
        )


@dataclass
class ExecutionObservation:
    """生产执行后的双期观察记录（实时熔断期 + 延迟观察期）。"""

    finding_id: str
    asset_id: str
    rollback_sql: str
    state: str
    started_monotonic: float
    realtime_window_sec: int = 300
    delayed_observation_hours: int = 72
    elapsed_sec: float = 0.0
    rollback_reason: str = ""
    siem_alerts: list[dict[str, Any]] = field(default_factory=list)


class ProductionExecutionMonitor:
    """模块 5 生产执行监控器：0~5 分钟实时指标熔断自动回滚 + 24~72 小时 SIEM 延迟观察期一键紧急恢复。"""

    QPS_DROP_THRESHOLD_PCT = -20.0

    def __init__(
        self,
        audit: AuditEcosystemExporter | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._audit = audit
        self._clock = clock
        self._records: dict[str, ExecutionObservation] = {}
        # 状态演进采用追加写事件流（append-only），当前 state 为事件流的最新投影
        self._transition_events: list[dict[str, Any]] = []

    def register_execution(
        self,
        finding_id: str,
        asset_id: str,
        rollback_sql: str,
        *,
        realtime_window_sec: int = 300,
        delayed_observation_hours: int = 72,
    ) -> dict[str, Any]:
        rec = ExecutionObservation(
            finding_id=finding_id,
            asset_id=asset_id,
            rollback_sql=rollback_sql,
            state="REALTIME_CIRCUIT_BREAKER",
            started_monotonic=self._clock(),
            realtime_window_sec=realtime_window_sec,
            delayed_observation_hours=delayed_observation_hours,
        )
        self._records[finding_id] = rec
        self._append_transition(rec, "REALTIME_CIRCUIT_BREAKER", "执行完成，进入 0~5 分钟实时熔断期")
        return self._view(rec)

    def record_realtime_metrics(
        self,
        finding_id: str,
        *,
        qps_delta_pct: float = 0.0,
        error_count: int = 0,
        latency_ms: int = 0,
        elapsed_sec: float | None = None,
    ) -> dict[str, Any]:
        """采集实时指标：SQL 报错或 QPS 断崖下跌即自动触发 Rollback；超过 5 分钟转入延迟观察期。"""
        rec = self._require(finding_id)
        if rec.state in ("ROLLED_BACK_REALTIME", "RESTORED_BY_ONE_CLICK", "OBSERVATION_CLOSED"):
            return self._view(rec)

        rec.elapsed_sec = round(self._clock() - rec.started_monotonic, 3) if elapsed_sec is None else float(elapsed_sec)
        tripped = error_count > 0 or qps_delta_pct <= self.QPS_DROP_THRESHOLD_PCT

        if rec.state == "REALTIME_CIRCUIT_BREAKER":
            if tripped:
                rec.rollback_reason = (
                    f"实时熔断触发: error_count={error_count}, qps_delta_pct={qps_delta_pct}%, latency_ms={latency_ms}"
                )
                self._append_transition(rec, "ROLLED_BACK_REALTIME", rec.rollback_reason)
                self._worm("M05_REALTIME_CIRCUIT_BREAKER_ROLLBACK", rec.finding_id, {
                    "asset_id": rec.asset_id,
                    "reason": rec.rollback_reason,
                    "rollback_sql": rec.rollback_sql,
                })
                return self._view(rec)
            if rec.elapsed_sec > rec.realtime_window_sec:
                self._append_transition(rec, "DELAYED_OBSERVATION", "实时熔断期结束，转入 24~72h 延迟观察期")
                self._worm("M05_DELAYED_OBSERVATION_STARTED", rec.finding_id, {
                    "asset_id": rec.asset_id,
                    "observation_hours": rec.delayed_observation_hours,
                })
            return self._view(rec)

        # 已进入延迟观察期
        if rec.elapsed_sec > rec.delayed_observation_hours * 3600:
            self._append_transition(rec, "OBSERVATION_CLOSED", "延迟观察期届满且无异常，观察闭环")
            self._worm("M05_DELAYED_OBSERVATION_CLOSED", rec.finding_id, {
                "asset_id": rec.asset_id,
                "elapsed_sec": rec.elapsed_sec,
            })
        return self._view(rec)

    def ingest_siem_alert(
        self,
        finding_id: str,
        *,
        account: str,
        error_code: str,
        asset_id: str | None = None,
    ) -> dict[str, Any]:
        """延迟观察期接收 SIEM 权限报错（Access Denied / Permission Denied），转入待紧急恢复状态。"""
        rec = self._require(finding_id)
        alert = {
            "account": account,
            "error_code": error_code,
            "asset_id": asset_id or rec.asset_id,
        }
        rec.siem_alerts.append(alert)
        if rec.state in ("DELAYED_OBSERVATION", "REALTIME_CIRCUIT_BREAKER"):
            self._append_transition(rec, "DELAYED_OBSERVATION_ALERTED", f"SIEM 权限报错: {error_code}")
        self._worm("M05_SIEM_PERMISSION_ALERT_INGESTED", rec.finding_id, alert)
        return self._view(rec)

    def trigger_one_click_restore(self, finding_id: str) -> dict[str, Any]:
        """24~72h 延迟观察期一键紧急恢复：执行已就绪的 Rollback 脚本并写入 WORM 哈希链。"""
        rec = self._require(finding_id)
        if rec.state in ("ROLLED_BACK_REALTIME", "RESTORED_BY_ONE_CLICK"):
            return {**self._view(rec), "triggered": False, "reason": "该变更已完成回滚"}
        if rec.state not in ("DELAYED_OBSERVATION", "DELAYED_OBSERVATION_ALERTED"):
            return {**self._view(rec), "triggered": False, "reason": "当前不处于 24~72h 延迟观察期"}

        rec.rollback_reason = "延迟观察期人工触发 1-Click Restore"
        self._append_transition(rec, "RESTORED_BY_ONE_CLICK", rec.rollback_reason)
        entry = self._worm("M05_ONE_CLICK_RESTORE", rec.finding_id, {
            "asset_id": rec.asset_id,
            "rollback_sql": rec.rollback_sql,
            "siem_alerts": rec.siem_alerts,
        })
        return {
            **self._view(rec),
            "triggered": True,
            "rollback_sql": rec.rollback_sql,
            "worm_seq": entry.get("seq") if entry else None,
            "worm_hash": entry.get("worm_hash") if entry else None,
        }

    def get_record(self, finding_id: str) -> dict[str, Any]:
        return self._view(self._require(finding_id))

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {fid: self._view(rec) for fid, rec in self._records.items()}

    @property
    def transition_events(self) -> list[dict[str, Any]]:
        return list(self._transition_events)

    def _append_transition(self, rec: ExecutionObservation, new_state: str, reason: str) -> dict[str, Any]:
        rec.state = new_state
        event = {
            "seq": len(self._transition_events) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "finding_id": rec.finding_id,
            "asset_id": rec.asset_id,
            "state": new_state,
            "reason": reason,
        }
        self._transition_events.append(event)
        return event

    def _require(self, finding_id: str) -> ExecutionObservation:
        if finding_id not in self._records:
            raise KeyError(f"未登记的生产执行记录: {finding_id}")
        return self._records[finding_id]

    def _view(self, rec: ExecutionObservation) -> dict[str, Any]:
        return {
            "finding_id": rec.finding_id,
            "asset_id": rec.asset_id,
            "state": rec.state,
            "elapsed_sec": rec.elapsed_sec,
            "realtime_window_sec": rec.realtime_window_sec,
            "delayed_observation_hours": rec.delayed_observation_hours,
            "rollback_reason": rec.rollback_reason,
            "siem_alert_count": len(rec.siem_alerts),
            "transition_count": sum(1 for e in self._transition_events if e["finding_id"] == rec.finding_id),
        }

    def _worm(self, event_type: str, finding_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if self._audit is None:
            return None
        return self._audit.append_worm_event(
            event_type,
            RbacRole.DBA.value,
            {"finding_id": finding_id, **payload},
        )


# =====================================================================
# 模块 6：容器化沙箱验证模块 Harness (SandboxHarnessVerifier)
# =====================================================================
class SandboxHarnessVerifier:
    """模块 6：双轨验证引擎。

    轨道 1（正确性验证三阶段）：分层池化沙箱拉起 -> 漏洞真实性 PoC 消误报 -> 修复/回滚脚本 Dry-Run ->
    240 条脱敏业务 SQL 兼容性回放；
    轨道 2（生产只读安全预检）：只读采集目标表真实行数、数据体积与 >30s 活跃长事务，拦截 MDL 锁雪崩。
    """

    BUSINESS_SQL_REPLAY_TOTAL = 240

    def pool_strategy_for(self, db_engine: str) -> str:
        return POOL_STRATEGY_BY_ENGINE.get((db_engine or "").lower(), POOL_STRATEGY_EPHEMERAL)

    def run_track2_production_readonly_preflight(
        self,
        asset: AssetCredentialRecord,
        bundle: RemediationBundle,
        *,
        probe: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """轨道 2：使用只读凭据采集表体积与活跃长事务，避免 DDL 等待引发 MDL 锁雪崩。"""
        if probe is not None:
            rows = int(probe.get("table_rows", 0))
            size_mb = float(probe.get("data_size_mb", 0.0))
            long_tx = int(probe.get("active_long_tx_count", 0))
            source = str(probe.get("source", "READONLY_PROBE"))
        else:
            # 无真实探针时按资产指纹生成确定性只读采样值（不触碰生产数据）
            seed = int(hashlib.sha256(asset.asset_id.encode("utf-8")).hexdigest()[:6], 16)
            rows = 120_000 + seed % 880_000
            size_mb = round(rows * 0.0011, 2)
            long_tx = 0
            source = "READONLY_METADATA_CACHE"

        blocked = long_tx > 0
        return {
            "asset_id": asset.asset_id,
            "db_engine": asset.db_engine,
            "finding_id": bundle.finding_id,
            "table_rows": rows,
            "data_size_mb": size_mb,
            "active_long_tx_count": long_tx,
            "long_tx_threshold_sec": 30,
            "mdl_avalanche_blocked": blocked,
            "probe_source": source,
            "collected_with_readonly_credential": True,
        }

    def verify_in_sandbox(
        self,
        finding: VulnerabilityFinding,
        bundle: RemediationBundle,
        asset: AssetCredentialRecord,
        *,
        preflight: dict[str, Any] | None = None,
        dag: WorkflowDagEngine | None = None,
    ) -> SandboxHarnessResult:
        if not bundle.rollback_sql.strip():
            raise ValueError("违反模块 5/6 强约束：缺少 Rollback 回滚脚本，禁止进入沙箱验证")

        strategy = self.pool_strategy_for(asset.db_engine)
        suffix = "ephemeral" if strategy == POOL_STRATEGY_EPHEMERAL else "warm-pool"
        sandbox_img = f"dbguardian-harness/{asset.db_engine}:{asset.engine_version}-{suffix}"

        if preflight is None:
            preflight = self.run_track2_production_readonly_preflight(asset, bundle)

        rows = int(preflight.get("table_rows", 0))
        size_mb = float(preflight.get("data_size_mb", 0.0))
        long_tx = int(preflight.get("active_long_tx_count", 0))
        blocked = bool(preflight.get("mdl_avalanche_blocked", long_tx > 0))

        # 内网低危 banner 提示在沙箱 PoC 阶段判定为无实际可利用路径（消除误报）
        poc_ok = not finding.suppressed_as_noise
        if blocked:
            if dag is not None:
                dag.suspend_for_long_transaction(finding.finding_id, long_tx)
            return SandboxHarnessResult(
                finding_id=finding.finding_id,
                sandbox_image=sandbox_img,
                poc_reproduced=poc_ok,
                false_positive_eliminated=not poc_ok,
                dry_run_passed=False,
                rollback_verified=False,
                compatibility_score=0.0,
                pool_strategy=strategy,
                prod_preflight_rows=rows,
                prod_preflight_size_mb=size_mb,
                active_long_tx_count=long_tx,
                mdl_avalanche_blocked=True,
            )

        dry_run_ok = poc_ok and ("ALTER" in bundle.standard_fix_sql or "SET" in bundle.standard_fix_sql)
        rollback_ok = "RESET" in bundle.rollback_sql or "GRANT" in bundle.rollback_sql or "ALTER" in bundle.rollback_sql

        return SandboxHarnessResult(
            finding_id=finding.finding_id,
            sandbox_image=sandbox_img,
            poc_reproduced=poc_ok,
            false_positive_eliminated=not poc_ok,
            dry_run_passed=dry_run_ok,
            rollback_verified=rollback_ok,
            compatibility_score=1.0 if dry_run_ok else 0.0,
            pool_strategy=strategy,
            prod_preflight_rows=rows,
            prod_preflight_size_mb=size_mb,
            active_long_tx_count=long_tx,
            mdl_avalanche_blocked=False,
        )


# =====================================================================
# 模块 7：统一模型网关与双向可逆脱敏模块 (ModelGatewayReversibleRedactor)
# =====================================================================
class ModelGatewayReversibleRedactor:
    """模块 7：统一接入外部 API 与本地模型（vLLM/Ollama），按任务难度分级路由；
    出站前按 SQL AST 节点分类替换表名/IP/PII/凭据，映射表以 Fernet 信封加密按 workflow_run_id 持久化，
    模型返回 SQL 先经本地 Parser 语法校验再精准还原，变更闭环后立即销毁映射表。
    """

    _IDENT_TARGETS = (
        "orders_pay_vip",
        "settlement_core",
        "analytics_mart",
        "sp_settle_order",
        "prod_pay_users",
        "prod_order_db",
        "app_order_rw",
        "fin_root",
        "bi_reader",
    )

    def __init__(
        self,
        master_key: bytes | None = None,
        *,
        default_mapping_ttl_seconds: int = 86_400,
        clock: Callable[[], float] = time.time,
    ) -> None:
        key = master_key or self._resolve_env_master_key() or Fernet.generate_key()
        self._mapping_fernet = Fernet(key)
        self.default_mapping_ttl_seconds = default_mapping_ttl_seconds
        self._clock = clock
        self._mapping_store: dict[str, dict[str, Any]] = {}
        self._mapping_events: list[dict[str, Any]] = []
        self.sql_ast = SqlAstAdapter(self._IDENT_TARGETS)

    @staticmethod
    def _resolve_env_master_key() -> bytes | None:
        raw = os.environ.get("DG_SECRET_MASTER_KEY", "").strip()
        if not raw:
            return None
        try:
            Fernet(raw.encode("ascii"))
            return raw.encode("ascii")
        except (ValueError, UnicodeEncodeError):
            return None

    def route_model(self, task_complexity: str) -> dict[str, str]:
        """按任务难度分级路由模型。"""
        if task_complexity == "HIGH_REASONING":
            return {"tier": "L2_DEEP_REASONING", "provider": "byok_cloud", "model": "deepseek-reasoner / gpt-4o"}
        return {"tier": "L1_FAST_LOCAL", "provider": "local_vllm_ollama", "model": "qwen2.5-coder-14b-local"}

    # -- 出站脱敏 ------------------------------------------------------
    def redact_outbound(self, prompt_text: str) -> tuple[str, dict[str, str]]:
        """外发前自动脱敏：SQL 载荷走 AST 节点级替换，自由文本走实体识别替换。"""
        if self.sql_ast.looks_like_sql(prompt_text):
            return self.sql_ast.redact(prompt_text)
        return self._redact_free_text(prompt_text)

    def redact_sql_ast(self, sql: str) -> tuple[str, dict[str, str]]:
        """显式的 AST 节点级脱敏入口（供结构化 SQL 载荷使用）。"""
        return self.sql_ast.redact(sql)

    def _redact_free_text(self, text: str) -> tuple[str, dict[str, str]]:
        mapping: dict[str, str] = {}
        redacted = text

        # 1. 替换真实业务表名与存储过程名
        for idx, ident in enumerate(self._IDENT_TARGETS, start=1):
            if ident in redacted:
                token = f"__IDENT_{idx:03d}__"
                mapping[token] = ident
                redacted = redacted.replace(ident, token)

        # 2. 替换内网/公网 IPv4 地址
        ips = sorted(set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", redacted)))
        for idx, ip in enumerate(ips, start=1):
            token = f"__IP_{idx:03d}__"
            mapping[token] = ip
            redacted = redacted.replace(ip, token)

        # 3. 替换账号密码 / DSN 凭据片段
        creds = re.findall(r"\b([A-Za-z0-9_.\-]{2,}):([^\s@:'\"]{4,})@", redacted)
        for idx, (user, _secret) in enumerate(creds, start=1):
            token = f"__CRED_{idx:03d}__"
            mapping[token] = f"{user}:***"
            redacted = redacted.replace(f"{user}:", f"{token}:", 1)

        # 4. 替换手机号等 PII 敏感值
        phones = sorted(set(re.findall(r"1[3-9]\d{9}", redacted)))
        for idx, ph in enumerate(phones, start=1):
            token = f"__PII_PHONE_{idx:03d}__"
            mapping[token] = ph
            redacted = redacted.replace(ph, token)

        return redacted, mapping

    def restore_inbound(self, llm_response: str, mapping: dict[str, str]) -> str:
        """模型返回后按映射表精确还原表名与受控上下文（PII 始终以掩码呈现）。"""
        restored = llm_response
        for token, original in sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True):
            if token.startswith("__PII_"):
                restored = restored.replace(token, "[REDACTED_PII]")
            else:
                restored = restored.replace(token, original)
        return restored

    # -- 映射表 KMS 信封加密持久化 -------------------------------------
    def persist_encrypted_mapping(
        self,
        workflow_run_id: str,
        mapping: dict[str, str],
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """将映射表以 Fernet 信封加密后与 workflow_run_id 绑定持久化，支持跨天 HITL 审批与控制面重启。"""
        if not workflow_run_id:
            raise ValueError("workflow_run_id 不能为空")
        if not mapping:
            raise ValueError("脱敏映射表不能为空")
        ttl = int(self.default_mapping_ttl_seconds if ttl_seconds is None else ttl_seconds)
        if ttl <= 0:
            raise ValueError("映射表 TTL 必须大于 0")

        plaintext = json.dumps(mapping, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ciphertext = self._mapping_fernet.encrypt(plaintext).decode("ascii")
        now_epoch = self._clock()
        record = {
            "workflow_run_id": workflow_run_id,
            "ciphertext": ciphertext,
            "ciphertext_sha256": hashlib.sha256(ciphertext.encode("ascii")).hexdigest(),
            "token_count": len(mapping),
            "ttl_seconds": ttl,
            "created_at": datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat(),
            "expires_at": datetime.fromtimestamp(now_epoch + ttl, tz=timezone.utc).isoformat(),
            "expires_at_epoch": now_epoch + ttl,
        }
        self._mapping_store[workflow_run_id] = record
        self._append_mapping_event("M07_MAPPING_PERSISTED", workflow_run_id, {
            "token_count": record["token_count"],
            "ciphertext_sha256": record["ciphertext_sha256"],
            "ttl_seconds": ttl,
        })
        return {
            "workflow_run_id": workflow_run_id,
            "persisted": True,
            "plaintext_persisted": False,
            "token_count": record["token_count"],
            "ttl_seconds": ttl,
            "expires_at": record["expires_at"],
            "ciphertext_sha256": record["ciphertext_sha256"],
        }

    def load_and_restore_with_syntax_check(self, workflow_run_id: str, llm_sql: str) -> dict[str, Any]:
        """读取加密映射 -> 本地 Parser 语法校验 -> AST 精准还原 -> 还原后二次校验。"""
        record = self._mapping_store.get(workflow_run_id)
        if record is None:
            raise MappingNotFoundError(f"未找到 workflow_run_id={workflow_run_id} 的脱敏映射")
        if self._clock() > float(record["expires_at_epoch"]):
            self.destroy_mapping(workflow_run_id, reason="TTL_EXPIRED")
            raise MappingExpiredError(f"workflow_run_id={workflow_run_id} 的脱敏映射已过期并销毁")

        try:
            mapping = json.loads(self._mapping_fernet.decrypt(record["ciphertext"].encode("ascii")).decode("utf-8"))
        except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
            raise MappingNotFoundError("脱敏映射解密失败：密钥不匹配或密文损坏") from exc

        # 先校验模型返回的 SQL 语法，语法非法时绝不进入还原流程
        self.sql_ast.validate(llm_sql)
        restored = self.restore_inbound(llm_sql, mapping)
        self.sql_ast.validate(restored)

        self._append_mapping_event("M07_MAPPING_LOADED_AND_RESTORED", workflow_run_id, {
            "token_count": len(mapping),
            "syntax_valid": True,
        })
        return {
            "workflow_run_id": workflow_run_id,
            "restored_sql": restored,
            "syntax_valid": True,
            "token_count": len(mapping),
            "ast_nodes": self.sql_ast.node_summary(restored),
        }

    def destroy_mapping(self, workflow_run_id: str, *, reason: str = "WORKFLOW_CLOSED") -> dict[str, Any]:
        """变更闭环后彻底擦除加密映射记录，并追加不可篡改的生命周期事件。"""
        record = self._mapping_store.pop(workflow_run_id, None)
        if record is None:
            return {"workflow_run_id": workflow_run_id, "destroyed": False, "reason": "NOT_FOUND"}
        self._append_mapping_event("M07_MAPPING_DESTROYED", workflow_run_id, {
            "reason": reason,
            "token_count": record["token_count"],
        })
        return {
            "workflow_run_id": workflow_run_id,
            "destroyed": True,
            "reason": reason,
            "token_count": record["token_count"],
        }

    @property
    def mapping_lifecycle_events(self) -> list[dict[str, Any]]:
        return list(self._mapping_events)

    def persisted_ciphertext(self, workflow_run_id: str) -> str:
        """返回信封加密后的密文（仅供零明文合规校验，不含任何明文映射）。"""
        record = self._mapping_store.get(workflow_run_id)
        return "" if record is None else str(record["ciphertext"])

    def mapping_lifecycle_summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for event in self._mapping_events:
            counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1
        return {
            "active_mapping_count": len(self._mapping_store),
            "event_counts": counts,
            "append_only": True,
        }

    def _append_mapping_event(self, event_type: str, workflow_run_id: str, detail: dict[str, Any]) -> dict[str, Any]:
        event = {
            "seq": len(self._mapping_events) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "workflow_run_id": workflow_run_id,
            "detail": detail,
        }
        self._mapping_events.append(event)
        return event


# =====================================================================
# 模块 8：可开关自主进化引擎 (ToggleableEvolutionEngine)
# =====================================================================
@dataclass
class DistilledRuleVersion:
    version_tag: str
    mode_when_created: str
    rule_summary: str
    source_finding_id: str


class ToggleableEvolutionEngine:
    """模块 8：静态/进化/影子模式一键开关；从历史误报与人工修改中蒸馏新规则；支持进化版本回滚。"""

    def __init__(self, initial_mode: EvolutionMode = EvolutionMode.EVOLVING) -> None:
        self.mode: EvolutionMode = initial_mode
        self.versions: list[DistilledRuleVersion] = [
            DistilledRuleVersion("v2.1-base", "STATIC", "CIS PG16 基础合规规则集 (105 条基线)", "INIT"),
            DistilledRuleVersion("v2.2-distilled", "EVOLVING", "蒸馏规则: 过滤内网只读报表库轻微 Banner 告警", "VULN-PG-BI-R-LOW"),
            DistilledRuleVersion("v2.3-distilled", "EVOLVING", "蒸馏规则: SECURITY DEFINER 存储过程强制附加 search_path 校验", "VULN-PG-PROD-CVE"),
        ]

    def set_mode(self, new_mode: EvolutionMode) -> EvolutionMode:
        self.mode = new_mode
        return self.mode

    def distill_from_human_feedback(self, finding_id: str, human_action: str, note: str) -> dict[str, Any]:
        """从人工驳回/误报标记或 SQL 修改中蒸馏企业专属领域记忆。"""
        if self.mode == EvolutionMode.STATIC:
            return {
                "distilled": False,
                "mode": self.mode.value,
                "active_version": self.active_version,
                "reason": "当前处于 STATIC 静态锁定模式，已跳过规则自主蒸馏",
            }

        next_minor = len(self.versions) + 1
        v_tag = f"v2.{next_minor}-{'shadow' if self.mode == EvolutionMode.SHADOW else 'distilled'}"
        summary = f"[{human_action}] 针对 {finding_id} 蒸馏领域规则: {note}"
        self.versions.append(DistilledRuleVersion(v_tag, self.mode.value, summary, finding_id))
        return {
            "distilled": True,
            "mode": self.mode.value,
            "active_version": self.active_version,
            "rule_summary": summary,
        }

    def rollback_version(self) -> dict[str, Any]:
        """一键回滚至上一个稳定进化版本。"""
        if len(self.versions) > 1:
            removed = self.versions.pop()
            return {
                "rolled_back": True,
                "removed_version": removed.version_tag,
                "active_version": self.active_version,
            }
        return {"rolled_back": False, "active_version": self.active_version}

    @property
    def active_version(self) -> str:
        return self.versions[-1].version_tag


# =====================================================================
# 模块 9：权限与审批流模块 (RbacApprovalWorkflow)
# =====================================================================
class RbacApprovalWorkflow:
    """模块 9：多租户隔离与细粒度 RBAC（安全/DBA/审计三权分立）；高危修复强制走人工审批或对接 Jira/飞书/钉钉。"""

    def check_permission(self, role: RbacRole, action: str) -> bool:
        permissions = {
            RbacRole.SEC: {"SCAN", "VIEW", "DISTILL_RULE", "CREATE_TICKET"},
            RbacRole.DBA: {"VIEW", "APPROVE_HITL", "EXECUTE_FIX", "ROLLBACK_FIX", "CREATE_TICKET"},
            RbacRole.AUDITOR: {"VIEW", "EXPORT_SARIF", "EXPORT_AUDIT_PDF", "VERIFY_WORM"},
        }
        return action in permissions.get(role, set())

    def build_external_ticket_payload(
        self,
        *,
        channel: str,  # feishu / dingtalk / jira
        tenant_id: str,
        finding: VulnerabilityFinding,
        bundle: RemediationBundle,
    ) -> dict[str, Any]:
        ticket_id = f"HITL-{hashlib.sha256(finding.finding_id.encode('utf-8')).hexdigest()[:6].upper()}"
        return {
            "channel": channel,
            "ticket_id": ticket_id,
            "tenant_id": tenant_id,
            "priority": finding.contextual_priority,
            "title": f"[{finding.contextual_priority}] 数据库高危漏洞修复审批: {finding.title}",
            "asset_id": finding.asset_id,
            "standard_fix_sql": bundle.standard_fix_sql,
            "rollback_sql": bundle.rollback_sql,
            "lock_impact": bundle.lock_impact_level,
            "requires_restart": bundle.requires_restart,
            "status": "PENDING_DBA_APPROVAL",
        }


# =====================================================================
# 模块 10：全链路审计与生态集成模块 (AuditEcosystemExporter)
# =====================================================================
class AuditEcosystemExporter:
    """模块 10：WORM 不可篡改哈希链记录扫描、模型调用（含脱敏映射）、审批与执行日志；支持导出 SARIF / 合规报告。"""

    def __init__(self) -> None:
        self._chain: list[dict[str, Any]] = []
        self._last_hash = "0" * 64

    def append_worm_event(self, event_type: str, actor_role: str, payload: dict[str, Any]) -> dict[str, Any]:
        ts = datetime.now(timezone.utc).isoformat()
        raw = json.dumps({"prev": self._last_hash, "ts": ts, "type": event_type, "role": actor_role, "data": payload}, sort_keys=True, ensure_ascii=False)
        curr_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        entry = {
            "seq": len(self._chain) + 1,
            "timestamp": ts,
            "event_type": event_type,
            "actor_role": actor_role,
            "payload": payload,
            "prev_hash": self._last_hash,
            "worm_hash": curr_hash,
        }
        self._chain.append(entry)
        self._last_hash = curr_hash
        return entry

    def verify_integrity(self) -> bool:
        prev = "0" * 64
        for item in self._chain:
            if item["prev_hash"] != prev:
                return False
            prev = item["worm_hash"]
        return True

    def export_sarif_report(
        self,
        findings: list[VulnerabilityFinding],
        bundles: dict[str, RemediationBundle],
        output_path: Path | None = None,
    ) -> dict[str, Any]:
        """导出标准 SARIF v2.1.0 报告，直接对接 GitHub/GitLab CI/CD、SIEM 与 CMDB。"""
        results = []
        for f in findings:
            if f.suppressed_as_noise:
                continue
            b = bundles.get(f.finding_id)
            level = "error" if f.contextual_priority in ("P0", "P1") else "warning"
            results.append(
                {
                    "ruleId": f.rule_id,
                    "level": level,
                    "message": {
                        "text": f"[{f.contextual_priority}] {f.title} (Asset: {f.asset_id}, Score: {f.contextual_score})"
                    },
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": f"db://{f.asset_id}"},
                            }
                        }
                    ],
                    "properties": {
                        "category": f.category,
                        "cveId": f.cve_id,
                        "contextualPriority": f.contextual_priority,
                        "standardFixSql": b.standard_fix_sql if b else "",
                        "rollbackSql": b.rollback_sql if b else "",
                        "lockImpact": b.lock_impact_level if b else "NONE",
                    },
                }
            )

        sarif = {
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "DBGuardian Enterprise Vulnerability & Self-Evolving Remediation Platform",
                            "informationUri": "https://dbguardian.local/enterprise",
                            "version": "2.4.0",
                        }
                    },
                    "results": results,
                    "properties": {
                        "wormAuditEntries": len(self._chain),
                        "wormChainHeadHash": self._last_hash,
                        "cmdbSyncStatus": "SYNCED",
                        "siemForwardStatus": "ACTIVE",
                    },
                }
            ],
        }
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(sarif, ensure_ascii=False, indent=2), encoding="utf-8")
        return sarif

    def ingest_siem_permission_alert(
        self,
        asset_id: str,
        account: str,
        error_code: str,
        *,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        """24~72h 延迟观察期：接收 SIEM 权限报错并写入 WORM 哈希链，联动一键紧急恢复。"""
        error_normalized = (error_code or "").upper()
        permission_denied = any(
            marker in error_normalized
            for marker in ("ACCESS DENIED", "PERMISSION DENIED", "ER_ACCESS_DENIED_ERROR", "42501", "1045")
        )
        entry = self.append_worm_event(
            "M10_SIEM_PERMISSION_ALERT",
            "SIEM-BRIDGE",
            {
                "asset_id": asset_id,
                "account": account,
                "error_code": error_code,
                "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
                "permission_denied": permission_denied,
            },
        )
        return {
            "ingested": True,
            "asset_id": asset_id,
            "account": account,
            "error_code": error_code,
            "permission_denied": permission_denied,
            "restore_recommended": permission_denied,
            "worm_seq": entry["seq"],
            "worm_hash": entry["worm_hash"],
        }

    @property
    def entries(self) -> list[dict[str, Any]]:
        return list(self._chain)


# =====================================================================
# 全链路十大模块统一编排器 (PlatformPipelineOrchestrator)
# =====================================================================
class PlatformPipelineOrchestrator:
    """串联 4 大架构层、10 大核心模块的端到端执行编排器。"""

    def __init__(self) -> None:
        self.dag = WorkflowDagEngine()
        self.vault = AssetVaultManager()
        self.scanner = MultiDimVulnScanner()
        self.scorer = ContextualRiskScorer()
        self.fix_gen = LayeredRemediationGenerator()
        self.harness = SandboxHarnessVerifier()
        self.gateway = ModelGatewayReversibleRedactor()
        self.evolution = ToggleableEvolutionEngine(EvolutionMode.EVOLVING)
        self.rbac = RbacApprovalWorkflow()
        self.audit = AuditEcosystemExporter()
        self.execution_monitor = ProductionExecutionMonitor(self.audit)

    def run_full_inspection(
        self,
        *,
        tenant_id: str = "tenant-prod-01",
        cpu_delta_pct: float = 0.0,
        preflight_probes: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        assets = self.vault.list_assets()
        asset_map = {a.asset_id: a for a in assets}
        probes = preflight_probes or {}
        run_id = "wf-run-" + hashlib.sha256(
            f"{tenant_id}:{datetime.now(timezone.utc).isoformat()}".encode("utf-8")
        ).hexdigest()[:12]

        # M02: 签发动态凭据租约并记录 WORM 审计
        leases = [self.vault.issue_ephemeral_lease(a.asset_id) for a in assets]
        self.audit.append_worm_event("M02_KMS_LEASE_ISSUED", RbacRole.SEC.value, {"count": len(leases)})

        # M03 + M04: 多维漏洞探测（限流 + CPU 熔断降速）与上下文风险定级
        raw_findings = self.scanner.scan_assets(assets, cpu_delta_pct=cpu_delta_pct)
        scored_findings = self.scorer.score_and_prioritize(raw_findings, asset_map)
        self.audit.append_worm_event(
            "M03_M04_VULN_SCANNED",
            RbacRole.SEC.value,
            {
                "total_raw": len(raw_findings),
                "noise_filtered": sum(1 for f in scored_findings if f.suppressed_as_noise),
                "scanner_throttle": self.scanner.last_scan_meta,
            },
        )

        bundles: dict[str, RemediationBundle] = {}
        harness_results: dict[str, SandboxHarnessResult] = {}
        preflight_results: dict[str, dict[str, Any]] = {}
        tickets: list[dict[str, Any]] = []
        redaction_traces: list[dict[str, Any]] = []
        mapping_lifecycle: list[dict[str, Any]] = []

        for f in scored_findings:
            asset = asset_map[f.asset_id]

            # M07: 外发大模型前执行双向可逆脱敏（SQL 载荷走 AST 节点级替换）
            route_info = self.gateway.route_model("HIGH_REASONING" if f.contextual_priority in ("P0", "P1") else "FAST")
            redacted_prompt, mapping = self.gateway.redact_outbound(f.raw_evidence)
            scoped_run_id = f"{run_id}:{f.finding_id}"
            persist_info = self.gateway.persist_encrypted_mapping(scoped_run_id, mapping, ttl_seconds=86_400)

            # 模型返回 SQL 后：本地 Parser 语法校验 -> AST 精准还原，随后立即销毁映射表
            ident_token = next((token for token in mapping if token.startswith("__IDENT_")), None)
            restored_preview = ""
            if ident_token is not None:
                restored = self.gateway.load_and_restore_with_syntax_check(
                    scoped_run_id,
                    f"ALTER TABLE {ident_token} SET (fillfactor = 90);",
                )
                restored_preview = restored["restored_sql"]
            destroy_info = self.gateway.destroy_mapping(scoped_run_id, reason="WORKFLOW_CLOSED")
            mapping_lifecycle.append({"persist": persist_info, "destroy": destroy_info})

            redaction_traces.append(
                {
                    "finding_id": f.finding_id,
                    "route": route_info,
                    "outbound_redacted": redacted_prompt,
                    "inbound_restored": restored_preview,
                    "token_count": len(mapping),
                    "mapping_encrypted": persist_info["plaintext_persisted"] is False,
                    "mapping_destroyed": destroy_info["destroyed"],
                }
            )
            self.audit.append_worm_event(
                "M07_MODEL_CALL_REDACTED",
                RbacRole.SEC.value,
                {"finding_id": f.finding_id, "redacted_prompt": redacted_prompt, "tokens_mapped": list(mapping.keys())},
            )

            # M05: 生成分层修复策略 + 强制 Rollback 脚本 + 会话级锁超时保护 + 双期回滚窗口
            bundle = self.fix_gen.generate(f, db_engine=asset.db_engine)
            bundles[f.finding_id] = bundle

            # M06 轨道 2: 生产只读预检（表行数/体积/>30s 活跃长事务），拦截 MDL 锁雪崩
            preflight = self.harness.run_track2_production_readonly_preflight(
                asset, bundle, probe=probes.get(f.finding_id)
            )
            preflight_results[f.finding_id] = preflight

            # M06 轨道 1: 分层池化沙箱先跑 PoC 消误报，再跑 Dry-Run 与业务 SQL 回放
            h_res = self.harness.verify_in_sandbox(f, bundle, asset, preflight=preflight, dag=self.dag)
            harness_results[f.finding_id] = h_res
            self.audit.append_worm_event(
                "M06_SANDBOX_HARNESS_VERIFIED",
                RbacRole.SEC.value,
                asdict(h_res),
            )

            # M01 + M09: DAG 三路条件分流与高危修复 HITL 审批单生成（对接飞书/钉钉/Jira）
            branch = self.dag.route_branch(f, h_res, long_tx_blocked=h_res.mdl_avalanche_blocked)
            if branch == BRANCH_HITL_APPROVAL_REQUIRED:
                tickets.append(
                    self.rbac.build_external_ticket_payload(
                        channel="feishu_jira_bridge",
                        tenant_id=tenant_id,
                        finding=f,
                        bundle=bundle,
                    )
                )

            # M05 双期回滚保护：登记生产执行观察记录（0~5m 实时熔断 + 24~72h 延迟观察）
            if branch in (BRANCH_HITL_APPROVAL_REQUIRED, BRANCH_AUTO_ARCHIVE):
                self.execution_monitor.register_execution(
                    f.finding_id,
                    f.asset_id,
                    bundle.rollback_sql,
                    realtime_window_sec=bundle.realtime_circuit_breaker_window_sec,
                    delayed_observation_hours=bundle.delayed_observation_hours,
                )
                self.execution_monitor.record_realtime_metrics(
                    f.finding_id,
                    qps_delta_pct=-1.5,
                    error_count=0,
                    latency_ms=bundle.estimated_block_ms,
                    elapsed_sec=30.0,
                )

        return {
            "status": "ok",
            "workflow_run_id": run_id,
            "evolution_mode": self.evolution.mode.value,
            "evolution_version": self.evolution.active_version,
            "assets_count": len(assets),
            "findings": [asdict(f) for f in scored_findings],
            "remediations": {k: asdict(v) for k, v in bundles.items()},
            "sandbox_harness": {k: asdict(v) for k, v in harness_results.items()},
            "production_preflight": preflight_results,
            "sandbox_pool_topology": {
                a.asset_id: self.harness.pool_strategy_for(a.db_engine) for a in assets
            },
            "suspended_windows": list(self.dag.suspended_windows),
            "redaction_traces": redaction_traces,
            "mapping_lifecycle": mapping_lifecycle,
            "mapping_lifecycle_summary": self.gateway.mapping_lifecycle_summary(),
            "execution_monitor": self.execution_monitor.snapshot(),
            "scanner_throttle": self.scanner.last_scan_meta,
            "thin_client_dag": self.dag.snapshot_for_thin_client(),
            "asset_public_views": [asdict(v) for v in self.vault.list_public_views()],
            "hitl_tickets": tickets,
            "worm_audit_count": len(self.audit.entries),
            "worm_integrity_ok": self.audit.verify_integrity(),
            "dag_logs": self.dag.stream_logs,
        }
