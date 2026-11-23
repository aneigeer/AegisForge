# W05 安全测试证据

- 适用：DBGuardian V2.2 W05 身份认证与策略底座
- 日期：2026-11-08
- 运行环境：Python 3.14.7；MySQL 8.0.46（127.0.0.1:3306）；PostgreSQL 12.18（临时启动于 127.0.0.1:55432）

## 1. 全量基线

```powershell
.\.venv\Scripts\python.exe -m pytest -q --assert=plain
```

结果：

```
654 passed, 1 warning
```

- 1 warning 为 `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead`。
- 该基线无 skip、无 xfail 报告。
- W05 专项共 **146** 项（含参数化展开），覆盖下列 12 个文件；W05 专项内无 skip/xfail。
- 本机未运行 Docker，网络与容器运行态不纳入本基线结论。

## 2. W05 专项用例分布

| 文件 | 收集用例数（含参数化） | 覆盖 |
| --- | --- | --- |
| `tests/unit/test_auth_passwords.py` | 15 | scrypt 格式、随机盐、错密码、篡改摘要、非法格式、空密码、非字符串输入 |
| `tests/unit/test_auth_tokens.py` | 7 | Token 形状、唯一性、SHA-256 确定性、指纹不泄露完整 Token、空 Token 拒绝 |
| `tests/unit/test_api_principal.py` | 7 | API Principal 依赖：dev_local 忽略 Authorization、Bearer 解析、缺失/非法 Token、权限依赖 |
| `tests/unit/test_authz.py` | 15 | 角色权限矩阵、Administrator 无审批权、租户隔离、任务所有者控制 |
| `tests/unit/test_policy_engine.py` | 23 | R0–R4、PROD、模型提示只升不降、风险下限、环境白名单、未知类别 fail closed |
| `tests/unit/test_envelope.py` | 26 | 参数摘要、时间规范化、签名、逐字段篡改、过期、未来签发、空 nonce |
| `tests/unit/test_worker_isolation.py` | 2 | Worker 源码不得引用数据库/驱动；构造函数不得出现 DSN |
| `tests/unit/test_model_isolation.py` | 2 | 模型包不得引用数据库/DSN；`model_service` 不得加入 `lab_net` |
| `tests/integration/test_auth_service.py` | 14 | 登录/认证/登出/轮换、用户与成员关系变化、dev_local 主体 |
| `tests/integration/test_auth_api.py` | 16 | Bearer 解析、登录/me/logout/refresh、客户端角色拒绝、dev_local 回归 |
| `tests/integration/test_rbac_api.py` | 8 | 角色读写边界、租户忽略、跨租户 404、R2 拒绝、dev_local 回归 |
| `tests/integration/test_worker_gateway.py` | 11 | 正常执行链、nonce 重放、租约/fencing、过期信封、参数篡改、R3/R4 拒绝 |

## 3. 任务书验收项映射

| 验收项 | 对应测试 | 实测结果 |
| --- | --- | --- |
| 本地认证与 Principal | `test_auth_service.py::test_login_success_roles_come_from_database`、`test_login_single_tenant_is_auto_selected`、`test_login_multi_tenant_requires_explicit_tenant`、`test_login_wrong_password_is_invalid`、`test_login_unknown_user_is_invalid`、`test_login_inactive_user_is_invalid`；`test_auth_api.py::test_login_principal_roles_come_from_db`、`test_login_me_logout_flow`、`test_missing_authorization_returns_auth_required`、`test_invalid_token_returns_auth_invalid`、`test_non_bearer_authorization_is_auth_invalid`、`test_dev_local_me_returns_local_administrator`；`test_api_principal.py::test_dev_local_returns_server_side_principal`、`test_token_mode_missing_authorization_is_auth_required`、`test_token_mode_non_bearer_is_auth_invalid`、`test_token_mode_invalid_token_is_auth_invalid`、`test_token_mode_valid_token_returns_principal`、`test_require_permission_checks_rbac`、`test_extract_bearer_token_trims_value`；`test_auth_passwords.py` 全部；`test_auth_tokens.py` 全部 | 通过（全量基线内） |
| Token 会话生命周期 | `test_auth_service.py::test_authenticate_success_and_tampered_token`、`test_authenticate_rejects_disabled_user_and_removed_membership`、`test_authenticate_expired_session`、`test_logout_is_idempotent_and_revokes_session`、`test_rotate_invalidates_old_token`；`test_auth_api.py::test_refresh_invalidates_old_token`、`test_refresh_requires_bearer`、`test_logout_without_token_is_auth_required` | 通过（全量基线内） |
| RBAC 角色权限矩阵 | `test_authz.py::test_read_only_roles_lack_adapter_invoke`、`test_operator_and_administrator_have_adapter_invoke`、`test_administrator_has_no_approval_decide_permission`、`test_viewer_can_read_tasks`、`test_approver_can_read_task_but_cannot_control_it`；`test_rbac_api.py::test_viewer_can_read_but_cannot_create`、`test_auditor_can_read_but_cannot_create`、`test_approver_cannot_create_task` | 通过（全量基线内） |
| 租户与对象级授权 | `test_authz.py::test_ensure_tenant_rejects_mismatch_with_safe_details`、`test_ensure_tenant_accepts_same_tenant`、`test_cross_tenant_task_is_hidden_as_not_found`、`test_cross_tenant_task_not_found_even_for_administrator`、`test_task_owner_can_control_own_task`、`test_non_owner_operator_cannot_control_task`、`test_administrator_can_control_task_without_ownership`；`test_rbac_api.py::test_operator_create_ignores_client_tenant_and_controls_own_task`、`test_client_supplied_role_and_approver_are_rejected`、`test_cross_tenant_task_is_404_for_get_and_pause` | 通过（全量基线内） |
| Risk Resolver / Policy Engine | `test_policy_engine.py::test_r0_is_allowed_for_operator_in_lab`、`test_r1_is_allowed_for_operator_and_admin_in_lab`、`test_prod_target_is_denied_with_risk_at_least_r2`、`test_r3_is_forbidden_for_agent_execution`、`test_r4_is_never_executable`、`test_model_hint_cannot_lower_r2_to_r0`、`test_model_hint_can_raise_r0_to_r4_and_deny`、`test_invalid_model_hint_is_ignored_and_noted`、`test_roles_without_adapter_invoke_are_denied`、`test_risk_floor_raises_effective_risk_to_r2`、`test_write_side_effect_raises_risk_to_r2`、`test_environment_allow_list_blocks_non_matching_tier`、`test_unknown_operation_class_fails_closed`、`test_missing_definition_fails_closed`、`test_policy_version_is_returned_verbatim` | 通过（全量基线内） |
| Worker lease / fencing / ExecutionEnvelope | `test_envelope.py::test_args_digest_is_order_independent`、`test_canonical_payload_normalizes_datetime_timezone`、`test_signed_envelope_verifies`、`test_tampering_any_field_breaks_signature`、`test_expired_envelope_is_rejected`、`test_future_issued_at_is_rejected`、`test_empty_nonce_is_rejected_even_with_valid_signature`；`test_worker_gateway.py::test_readonly_tool_executes_and_writes_audit_chain`、`test_nonce_replay_is_rejected`、`test_old_lease_after_release_is_rejected`、`test_stale_fencing_token_is_rejected`、`test_second_worker_conflicts_while_lease_active`、`test_expired_lease_blocks_heartbeat_and_new_worker_gets_bigger_token`、`test_expired_envelope_is_rejected`、`test_tampered_params_are_rejected`、`test_unregistered_tool_is_rejected` | 通过（全量基线内） |
| 唯一执行链与静态隔离负测 | `test_worker_isolation.py::test_worker_sources_do_not_reference_database_or_drivers`、`test_worker_constructor_has_no_dsn_parameter`；`test_model_isolation.py::test_model_package_never_imports_database_or_dsn`、`test_model_service_is_not_attached_to_lab_net`；`test_worker_gateway.py` 全部 | 通过（全量基线内；静态检查，不代表容器运行态已验证） |
| R2 保持关闭 | `test_policy_engine.py::test_r2_always_requires_approval_and_is_not_allowed`、`test_model_hint_cannot_lower_r2_to_r0`；`test_rbac_api.py::test_r2_tool_call_is_denied_by_policy`；`test_worker_gateway.py::test_high_risk_tools_are_denied_without_tool_calls`（R3/R4 同链路的拒绝与审计） | 通过（全量基线内） |

关键断言：

- R2 拒绝时 `reason=R2_REQUIRES_APPROVAL_W06_CLOSED`、`required_approval=true`、`deny_code=POLICY_DENIED`；R3/R4 拒绝且不产生 `tool_calls`，同时写 `POLICY_DECISION` 与 `GATEWAY_EXECUTION_DENIED`。
- nonce 重放返回 `NONCE_REPLAY`；释放后的旧租约被拒；旧/不一致 fencing token 返回 `FENCING_TOKEN_STALE`；同一任务第二个 Worker 返回 `WORKER_CONFLICT`。
- 参数篡改导致 `args_digest` 不匹配，返回 `ENVELOPE_INVALID`；未注册工具返回 `TOOL_NOT_FOUND`。
- 跨租户查询、事件、证据与暂停均返回 404 `TASK_NOT_FOUND`；同租户非所有者暂停返回 403 `FORBIDDEN`。
- 客户端提交 `role` 或 `approver_id` 返回 422 `REQUEST_INVALID`；客户端 `tenant_id` 被忽略，任务归属仍为 Principal 租户。

## 4. 未验证项（NOT_VERIFIED）

以下项没有运行态证据，不得视为通过：

| 项 | 状态 | 原因 |
| --- | --- | --- |
| Docker 网络拓扑与容器运行态 | `NOT_VERIFIED` | 本机 Docker daemon 不可用；仅执行 `docker compose config` 静态校验 |
| 模型服务实际不可达 `lab_net` 的容器级证据 | `NOT_VERIFIED` | 仅有 `docker-compose.yml` 静态断言（`test_model_service_is_not_attached_to_lab_net`），未启动容器验证 DNS/连通性 |
| API 移除 `lab_net` 后的运行态复测 | `NOT_VERIFIED` | `docker-compose.yml` 中 `api` 仍保留 `lab_net` 过渡挂载 |
| 独立 Worker 容器/独立 Gateway 进程 | `NOT_VERIFIED` | W05 的 Gateway 与租约服务当前为进程内组件，未做容器级部署 |
| 审批事务与 WORM 审计 | 未实现 | 留待 W06；R2 写操作保持关闭 |

静态负测结论只覆盖源码依赖与 compose 网络声明，不覆盖容器实际网络命名空间、DNS 解析或跨网络连通性。
