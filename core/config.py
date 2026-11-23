"""应用配置：环境变量 / .env，密钥使用 SecretStr。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

import secrets

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.errors import ConfigError
from database.mysql.dsn import DsnParseError, MysqlDsn, parse_mysql_dsn
from database.postgres.dsn import PgDsn, PgDsnParseError, parse_pg_dsn

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
# 仅开发环境 fallback：进程内临时签名密钥，重启/多进程间不共享。生产必须显式配置。
_DEV_GATEWAY_KEY = secrets.token_bytes(32)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DG_",
        case_sensitive=False,
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"
    app_name: str = "DBGuardian"
    log_level: str = "INFO"
    log_json: bool = True

    database_id: str = Field(default="lab_mysql_01", min_length=1)

    # 只读诊断账号 DSN
    ro_dsn: SecretStr
    # 隔离沙箱写账号 DSN（仅故障注入/修复路径使用）
    rw_dsn: SecretStr

    # PostgreSQL LAB（W01 双引擎）；未配置时 PG 功能显式不可用。
    pg_database_id: str = Field(default="lab_pg_01", min_length=1)
    pg_ro_dsn: SecretStr | None = None
    pg_rw_dsn: SecretStr | None = None

    # 控制库（任务/证据/审计）独立账号；未配置时回退到 rw_dsn（仅兼容旧测试）。
    control_dsn: SecretStr | None = None

    # 仅初始化/测试清理使用；应用运行时不读取。
    admin_dsn: SecretStr | None = None

    # W03 租户/环境（W05 接入 Principal 前的服务端默认归属，不接受客户端指定）
    default_tenant_id: str = Field(default="tenant_local", min_length=1)
    default_environment_id: str = Field(default="env_mysql_local", min_length=1)
    default_pg_environment_id: str = Field(default="env_pg_local", min_length=1)
    resume_evidence_max_age_s: float = Field(default=3600.0, gt=0, le=86_400)

    connect_timeout_s: float = Field(default=5.0, gt=0, le=60)
    statement_timeout_ms: int = Field(default=30_000, ge=100, le=600_000)
    max_result_rows: int = Field(default=500, ge=1, le=100_000)

    # ---- Model Gateway（W04）-----------------------------------------
    # fake：确定性桩适配器，用于测试与本地演示；openai_compat：OpenAI 兼容 HTTP 服务。
    model_provider: Literal["fake", "openai_compat", "disabled"] = "fake"
    model_name: str = "fake-diagnostic-1"
    model_base_url: str | None = None
    model_api_key: SecretStr | None = None
    model_timeout_s: float = Field(default=30.0, gt=0, le=300)
    model_max_tokens: int = Field(default=1024, ge=1, le=32_768)
    # P1-T1 角色路由：{"planner": "...", "classifier": "...", "evaluator": "..."}。
    # 未列出的角色回落到 `model_name`；`embedding` 暂无 HTTP 传输实现，映射即报错。
    model_role_models: dict[str, str] = Field(default_factory=dict)
    # 计价（每 1K token，美元）。未配置 → 成本恒 None（**不估算**，INV-06）。
    model_input_price_per_1k: float | None = Field(default=None, ge=0)
    model_output_price_per_1k: float | None = Field(default=None, ge=0)
    # 结构化输出是否下发 `json_schema`（严格模式）。端点不支持时置 false 退回
    # `json_object`；Pydantic 校验在两种模式下都生效。
    @field_validator("secret_master_key", mode="before")
    @classmethod
    def _blank_master_key_is_none(cls, value: object) -> object:
        """空串视为"未配置"：避免 BYOK 路径以空主密钥启动（fail closed 更早）。"""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    model_use_json_schema: bool = True
    # P2-T1：流式读取。本地单 worker 端点上，非流式请求在超时后仍会继续生成并阻塞队列；
    # 评测/长输出场景建议开启（超时即断连、端点中止生成）。
    model_stream: bool = False

    # ---- P1-BYOK：租户级供应商 --------------------------------
    # 主密钥用于 Fernet 加密 BYOK api_key；未配置时供应商管理端点返回 SECRET_KEY_MISSING。
    secret_master_key: SecretStr | None = None
    # SSRF：允许 http（仅 dev）与显式 allowlist 主机（运维声明，不自动放行私网）。
    byok_allow_http: bool = False
    byok_allowed_hosts: tuple[str, ...] = ()
    # 每租户默认配额（None = 不限；可按租户在供应商记录外另行覆盖，当前取全局默认）。
    byok_max_calls_per_task: int | None = None
    byok_max_tokens_per_task: int | None = None

    # ---- P3-T2：任务后知识沉淀 --------------------------------
    # 默认关闭：开启后每次诊断结束会生成一条 PROPOSED 草稿（仍需两级人工确认才可检索）。
    memory_auto_draft: bool = False

    # ---- Agent Loop 预算（W04）---------------------------------------
    # P6-T1：inline=进程内只读连接（开发/单测默认）；remote_worker=API 只写控制库派发队列，
    # 由独立 Worker 在 lab_net 执行只读工具并脱敏回传，实现 API 零 lab_net 网络隔离。
    agent_execution_mode: Literal["inline", "remote_worker"] = "inline"
    agent_dispatch_timeout_s: float = Field(default=30.0, gt=0, le=300)
    trajectory_dir: str = "runs"
    agent_max_steps: int = Field(default=8, ge=1, le=50)
    agent_max_tool_calls: int = Field(default=10, ge=1, le=50)
    agent_max_model_calls: int = Field(default=20, ge=1, le=100)
    agent_max_tokens: int = Field(default=20_000, ge=0)
    agent_max_seconds: float = Field(default=300.0, gt=0, le=3_600)
    agent_max_duplicate_calls: int = Field(default=2, ge=1, le=10)
    agent_no_progress_threshold: int = Field(default=2, ge=1, le=10)
    recover_on_startup: bool = True
    # 启动恢复护栏：单次启动最多恢复多少任务、最多花多少秒（防止历史积压拖垮启动）。
    recover_max_tasks: int = Field(default=100, ge=1, le=10_000)
    recover_budget_s: float = Field(default=10.0, gt=0, le=300)

    # ---- W05 身份认证与策略底座 -------------------------------------
    # dev_local：仅开发/测试，服务端固定本地主体；生产环境禁止。
    # token：Bearer Token（本地账户登录签发），生产环境必须使用。
    auth_mode: Literal["dev_local", "token", "oidc"] = "dev_local"
    # ---- P4-T1：OIDC / SSO --------------------------------------
    # 授权仍只来自 DB（memberships）；以下仅决定"令牌怎么验"。
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_uri: str | None = None
    oidc_leeway_s: int = Field(default=60, ge=0, le=600)
    oidc_jwks_ttl_s: int = Field(default=300, ge=30, le=3600)
    auth_token_ttl_s: float = Field(default=3600.0, gt=0, le=604_800)
    auth_token_max_ttl_s: float = Field(default=86_400.0, gt=0, le=604_800)
    # Gateway 执行信封 HMAC 密钥；生产环境必填，开发环境可临时生成进程内密钥。
    gateway_signing_key: SecretStr | None = None
    gateway_envelope_ttl_s: float = Field(default=60.0, gt=0, le=600)
    worker_lease_ttl_s: float = Field(default=30.0, gt=0, le=600)
    worker_heartbeat_interval_s: float = Field(default=10.0, gt=0, le=300)
    policy_version: str = Field(default="w06-v1", min_length=1, max_length=64)

    # ---- W06 审批事务与追加式审计 -------------------------------------
    # 审批有效期：默认 30 分钟、上限 120 分钟（LAB 阶段决策）。
    # 有效期比较只使用服务端时间，不接受客户端时间。
    approval_ttl_s: float = Field(default=1800.0, gt=0, le=7200)
    approval_max_ttl_s: float = Field(default=7200.0, gt=0, le=7200)
    # 操作摘要算法版本；变更视为不兼容变更（旧审批全部失效）。
    action_digest_version: str = Field(default="w06-v1", min_length=1, max_length=64)

    # ---- W07 收口：独立 Worker 载体（process carrier）------------------
    # Worker 身份用于租约归属与审计；多副本部署时必须保证全局唯一。
    worker_id: str = Field(default="worker_local_01", min_length=1, max_length=128)
    # 空闲轮询间隔；只有"没有候选"时才等待这么久，不会推迟已就绪的执行。
    worker_poll_interval_s: float = Field(default=5.0, gt=0, le=300)
    # 优雅停机窗口：收到 SIGTERM 后等待在途工具返回的上限。
    # 必须 >= 单次工具 timeout（写工具 timeout_ms=5000，即 5s），
    # 否则"优雅"停机可能在写到一半时切断连接，制造未知结果。
    worker_shutdown_grace_s: float = Field(default=30.0, gt=0, le=300)
    # 本轮**固定为 1**：跨任务并发会放大 R2 写风险且缺少并发证据链验证，
    # 多并发留给 W08 之后的独立评审。取值范围有意写成 ge=1 le=1。
    worker_max_concurrency: int = Field(default=1, ge=1, le=1)
    # 执行主体（写入信封的 `actor_user_id`）：默认取审批的 `requested_by`。
    # 本项仅作显式覆盖/兜底（审批申请人字段为空时），**不用于**"换个主体重试"——
    # 审批消费用的是服务端构造的 `worker:<id>` 主体，与 actor 无关。
    worker_actor_user_id: str = Field(
        default="system:worker", min_length=1, max_length=64
    )

    # ---- W08 独立验证与恢复：核对调度与性能采样 ------------------------
    # Reconciler 扫描间隔：Worker 每周期调用一次 sweep，不推迟已就绪执行。
    reconcile_interval_s: float = Field(default=60.0, gt=0, le=3600)
    # 只核对「早于该窗口」的未知结果任务，避免把刚写入的在途任务误判为 UNKNOWN。
    reconcile_older_than_s: float = Field(default=60.0, gt=0, le=3600)
    # 性能统计采样数（P50/P95 的 N）。ge=1 保证配置层不出现 N=0；
    # 运行期 percentile([]) 的「N=0 抛 REQUEST_INVALID」由调用侧（T02 perf）校验。
    verification_perf_samples: int = Field(default=5, ge=1, le=50)

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in _LOG_LEVELS:
            raise ValueError("log_level 必须是 DEBUG/INFO/WARNING/ERROR/CRITICAL")
        return upper

    @field_validator("ro_dsn", "rw_dsn")
    @classmethod
    def _validate_dsn(cls, value: SecretStr) -> SecretStr:
        try:
            parse_mysql_dsn(value.get_secret_value())
        except DsnParseError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("pg_ro_dsn", "pg_rw_dsn")
    @classmethod
    def _validate_pg_dsn(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        try:
            parse_pg_dsn(value.get_secret_value())
        except PgDsnParseError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("control_dsn", "admin_dsn")
    @classmethod
    def _validate_control_dsn(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        try:
            parse_mysql_dsn(value.get_secret_value())
        except DsnParseError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @model_validator(mode="after")
    def _validate_security_mode(self) -> "Settings":
        if self.auth_token_ttl_s > self.auth_token_max_ttl_s:
            raise ValueError("auth_token_ttl_s 不能超过 auth_token_max_ttl_s")
        if self.approval_ttl_s > self.approval_max_ttl_s:
            raise ValueError("approval_ttl_s 不能超过 approval_max_ttl_s")
        if self.auth_mode == "oidc":
            missing = [
                name
                for name, value in (
                    ("oidc_issuer", self.oidc_issuer),
                    ("oidc_audience", self.oidc_audience),
                    ("oidc_jwks_uri", self.oidc_jwks_uri),
                )
                if not str(value or "").strip()
            ]
            if missing:
                raise ValueError(f"auth_mode=oidc 需要配置：{', '.join(missing)}")
        if self.env == "prod":
            if self.auth_mode not in {"token", "oidc"}:
                raise ValueError("生产环境禁止 dev_local 认证绕过")
            if self.gateway_signing_key is None or not self.gateway_signing_key.get_secret_value().strip():
                raise ValueError("生产环境必须配置 gateway_signing_key")
        return self

    def gateway_key(self) -> bytes:
        """返回 Gateway 信封签名密钥；开发环境未配置时使用进程内临时密钥。"""
        if self.gateway_signing_key is not None:
            value = self.gateway_signing_key.get_secret_value().strip()
            if value:
                return value.encode("utf-8")
        if self.auth_mode == "oidc":
            missing = [
                name
                for name, value in (
                    ("oidc_issuer", self.oidc_issuer),
                    ("oidc_audience", self.oidc_audience),
                    ("oidc_jwks_uri", self.oidc_jwks_uri),
                )
                if not str(value or "").strip()
            ]
            if missing:
                raise ValueError(f"auth_mode=oidc 需要配置：{', '.join(missing)}")
        if self.env == "prod":
            raise ConfigError(
                "生产环境缺少 Gateway 签名密钥", details={"field": "gateway_signing_key"}
            )
        return _DEV_GATEWAY_KEY

    def ro_mysql_dsn(self) -> MysqlDsn:
        return parse_mysql_dsn(self.ro_dsn.get_secret_value())

    def rw_mysql_dsn(self) -> MysqlDsn:
        return parse_mysql_dsn(self.rw_dsn.get_secret_value())

    def pg_ro_pg_dsn(self) -> PgDsn | None:
        if self.pg_ro_dsn is None:
            return None
        return parse_pg_dsn(self.pg_ro_dsn.get_secret_value())

    def pg_rw_pg_dsn(self) -> PgDsn | None:
        if self.pg_rw_dsn is None:
            return None
        return parse_pg_dsn(self.pg_rw_dsn.get_secret_value())

    def control_mysql_dsn(self) -> MysqlDsn:
        if self.control_dsn is None:
            raise ConfigError(
                "未配置控制库账号，禁止回退到沙箱写账号",
                details={"field": "control_dsn"},
            )
        return parse_mysql_dsn(self.control_dsn.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # 只暴露字段名，绝不回显字段取值（可能包含 DSN 密码）。
        fields = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
        raise ConfigError(
            "环境配置缺失或非法；请复制 .env.example 为 .env 并填写",
            details={"fields": fields},
        ) from None
