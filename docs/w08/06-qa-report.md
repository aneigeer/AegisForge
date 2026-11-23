# W08 最终独立回归与 AC 逐条验收（QA 复核报告）

> 产出：software-qa-engineer
> 观测窗口：2026-11-12 23:52 – 2026-11-13 14:41 +0800（跨夜）
> 约束：**只读复核**——未改任何代码/测试、未 `git commit`、未跑迁移；所有可能挂起的命令均以 `timeout` 包住
> 环境：`D:\DBGuaredian`，仅用 `.venv\Scripts\python.exe`（Python 3.14.7），`DG_RECOVER_ON_STARTUP=false` 用于所有集成跑
> 口径：本文数字全部来自实跑输出；**未跑完的部分明确标「未跑完」，不计为通过**；实现者自述只作核对线索，不作正确性证据

---

## 0. 结论摘要

| 项 | 结果 |
|---|---|
| `pytest tests/unit` | **891 passed / 0 failed**（4.46s，PIPE=0），与实现者声称一致 |
| `pytest tests/integration` 全量（`timeout 900`） | **未跑完**：369 items 收集正常，900s 内只推进到 77%，日志无 pytest 汇总行，最后停在 `test_task_evidence_chain.py::test_recover_executing_task_requires_reconciliation`；24 个文件产生结果行、13 个文件未触达 |
| 按文件补齐（`timeout 180`/文件） | 13 个未触达文件全部补齐：6 个 W08 文件 **23 passed / 0 failed**，7 个既有文件数字见 §1.4 |
| AC-01…AC-21 | **21 / 21 通过**（其中 AC-20 含静态代码核对，见 §3） |
| 是否有一条失败可归因到 W08 新增代码 | **没有**（证据见 §2.4） |
| 一句话结论 | 见 §6 |

---

## 1. 全量回归总表

### 1.1 单元测试（跑完）

```
=== 任务A-1 pytest tests/unit (timeout 180) ===
........................................................................ [100%]
891 passed in 4.46s
=== PIPE=0 ===
```

命令：`timeout 180 .venv/Scripts/python.exe -m pytest tests/unit -o addopts="" -q -p no:cacheprovider`

### 1.2 集成全量（未跑完，不计通过）

命令：`DG_RECOVER_ON_STARTUP=false timeout 900 .venv/Scripts/python.exe -m pytest tests/integration -o addopts="" -v -p no:cacheprovider`

日志 `PCl7j3.stdout.log` 只读分析结果：

```
TOTAL_LINES = 408
FILES_WITH_RESULTS = 24   TOTAL_RESULT_LINES = 386
ALL_FILES = 37
NO_RESULT_FILES = ['test_w03_integrity.py', 'test_w06_acceptance.py',
  'test_w07_disclosure_integrity.py', 'test_w07_rejection_matrix.py',
  'test_w07_repair_e2e.py', 'test_w08_compensation.py',
  'test_w08_crash_matrix.py', 'test_w08_recovery_e2e.py',
  'test_w08_verifier_readback.py', 'test_w08_verifier_rejects.py',
  'test_w08_worker_process.py', 'test_worker_gateway.py',
  'test_worker_process.py']
结尾 12 行（最后一行为用例名、无结果后缀）:
  tests/integration/test_task_evidence_chain.py::test_cancel_semantics ERROR [ 77%]
  tests/integration/test_task_evidence_chain.py::test_recover_executing_task_requires_reconciliation
```

→ **900s 超时挂断，没有 `N passed in Xs` 汇总行，不能算通过**。369 items 中约 77% 被触达，13 个文件从未执行。

### 1.3 全量 900s 已触达的 24 个文件（`-v` 结果行统计，P/F/E/S）

> `ERROR` 大多是与 `PASSED`/`FAILED` **同一用例**的 fixture teardown 报错，不是独立用例；下表的"结果行数"不等于用例数。这些数字只代表该进程跑到 77% 时的快照，**不代表文件跑完**。

```
文件                                      PASSED FAILED ERROR SKIPPED
test_adapter_api.py                          1      1      0      0
test_adapter_registry.py                     1      1      0      0
test_agent_loop.py                          11     0     11      0
test_api_health.py                           1      2      0      0
test_api_v1.py                               6      1      4      0
test_approval_api.py                         0      0     12      0
test_approval_consume.py                    14      0     14      0
test_approval_service.py                    28      1     29      0
test_audit_service.py                       13      4     11      0
test_auth_api.py                            10      3      3      0
test_auth_service.py                        14      0     10      0
test_db_connection.py                        5      0      0      0
test_db_permissions.py                       5      0      0      0
test_diagnosis_flows.py                      4      5      4      0
test_evidence_service.py                     6      0      6      0
test_executor.py                            18      0      0      0
test_fault_injection.py                      4      0      0      4
test_mysql_adapter.py                       14      0      0      0
test_observers.py                           29      0      0      0
test_pg_connection.py                        1      5      0      0
test_postgres_mcp.py                         4      8      0      0
test_rbac_api.py                             0      2      6      0
test_step_prepared.py                       15      5     20      0
test_task_evidence_chain.py                  5      0      5      0
FILES_WITH_RESULTS = 24   TOTAL_RESULT_LINES = 386
FAILED_TOTAL(去重用例) = 38   ERROR_TOTAL(去重用例) = 135
```

### 1.4 按文件补齐 13 个未触达文件（每文件 `timeout 180`，全部跑完）

**A. W08 6 个文件（任务 C 独立复跑，`DG_RECOVER_ON_STARTUP=false`、`-v --tb=short`）**

```
test_w08_crash_matrix.py       7 passed in 117.43s          EXIT=0
test_w08_verifier_rejects.py   5 passed in 11.18s           EXIT=0
test_w08_verifier_readback.py  5 passed in 0.24s            EXIT=0
test_w08_compensation.py       2 passed in 10.23s           EXIT=0
test_w08_worker_process.py     2 passed, 2 warnings in 38.78s EXIT=0
test_w08_recovery_e2e.py       2 passed in 4.35s            EXIT=0
合计 23 passed / 0 failed / 0 errors
```
`test_w08_worker_process.py` 的 2 条 warning 为「控制库只追加事实未清理（需可用的 DG_ADMIN_DSN）」，是环境阻塞不是失败。

**B. 另外 7 个既有文件（`-q --tb=short -ra`）**

```
test_w03_integrity.py             1 failed, 2 passed, 3 errors in 2.77s        PIPE=1
test_w06_acceptance.py            2 failed, 23 passed, 26 warnings in 63.82s   PIPE=1
test_w07_disclosure_integrity.py  5 passed in 9.76s                            PIPE=0
test_w07_rejection_matrix.py      6 passed in 14.08s                           PIPE=0
test_w07_repair_e2e.py            3 passed in 5.60s                            PIPE=0
test_worker_gateway.py            2 failed, 11 passed, 12 errors in 7.38s      PIPE=1
test_worker_process.py            4 passed, 2 warnings in 24.73s               PIPE=0
```

### 1.5 环境阻塞清单

| 环境事实 | 实测证据 | 影响范围 |
|---|---|---|
| PostgreSQL 5432 不可达 | `('127.0.0.1', 5432) FAILED ConnectionRefusedError [WinError 10061] ... elapsed= 2.04`（上一轮 A6 实跑）；本轮 PG 相关用例全 FAILED | `test_pg_connection.py` 5 FAILED、`test_postgres_mcp.py` 8 FAILED、`test_diagnosis_flows.py` PG 参数化 5 FAILED、`test_adapter_api.py` / `test_adapter_registry.py` / `test_api_health.py` 的 PG 用例 FAILED；`test_fault_injection.py` 4 例 SKIPPED |
| `DG_ADMIN_DSN`（root）1045 不可用 | 针对性复跑 4 例错误栈：`admin_client.execute/fetch_all` → `database/mysql/connection.py:156 connect` → `:60 _raise_connect_error` → `E core.errors.DatabaseAuthError: 数据库认证失败，请检查账号配置`；`docs/w07/worker-residue.md` 已记录同一事实 | `test_w03_integrity.py` 1 failed + 3 errors、`test_worker_gateway.py` 2 failed + 12 errors、`test_w06_acceptance.py` 26 warnings（清理降级）、`test_w08_worker_process.py` / `test_worker_process.py` warnings；全量日志中 135 条 fixture teardown ERROR 同源 |
| MySQL 3306 控制库/LAB 可达 | `('127.0.0.1', 3306) CONNECTED elapsed= 0.0`（上一轮 A6）；本轮 LAB 相关用例能跑 | 非阻塞 |

---

## 2. 失败三分类与逐条归因

### 2.1 环境类（明确、可复现）

**PG 5432（21 条 FAILED，全部既有文件）**
- `test_adapter_api.py::test_postgres_tools_list_and_call` — `AssertionError: assert 'error' in {'partial', 'success'}`（PG MCP 探针返回 error）
- `test_adapter_registry.py::test_postgres_adapter_tools_run_through_executor`
- `test_api_health.py::test_healthz_pg_returns_readonly_identity`
- `test_diagnosis_flows.py`：`[pg_slow_query-postgresql]`、`[pg_connection_pressure-postgresql]`、`[pg_lock_wait-postgresql]`、`test_pg_lock_wait_edge_and_transaction_age_are_observed`、`test_pg_query_stats_missing_is_not_fabricated`
- `test_pg_connection.py`：`test_identity_and_readonly_session`、`test_read_seed_tables_and_row_limit`、`test_readonly_account_cannot_write`、`test_wrong_password_maps_to_auth_error`、`test_capability_report_is_explicit`
- `test_postgres_mcp.py`：`test_get_connection_snapshot`、`test_get_lock_wait_edges_and_details`、`test_get_transaction_metadata`、`test_get_table_stats`、`test_get_query_stats_is_explicit_on_pg12`、`test_explain_query_returns_json_plan`、`test_explain_query_rejects_write_and_analyze_without_execution`、`test_probe_capabilities_reports_required_fields`

**admin 1045（已逐条取栈验证 3 条）**
- `test_worker_gateway.py::test_prod_environment_is_denied_before_execution` — `test_worker_gateway.py:506 admin_client.execute` → `DatabaseAuthError: 数据库认证失败，请检查账号配置`（`1 failed in 0.26s`）
- `test_worker_gateway.py::test_expired_lease_blocks_heartbeat_and_new_worker_gets_bigger_token` — `:400 admin_client.execute` → 同一 `DatabaseAuthError`（`1 failed, 1 error in 0.58s`）
- `test_w03_integrity.py::test_events_and_evidence_are_append_only_for_control_account` — `:43 admin_client.fetch_all` → 同一 `DatabaseAuthError`（`1 failed, 1 error in 0.43s`）
- 全量日志 135 条 ERROR 与分组补齐中 `test_w03_integrity.py` 3 errors、`test_worker_gateway.py` 12 errors 同源（fixture teardown 清理失败），**未逐条取栈验证**，但用例名、栈模式与上面 3 条一致。

### 2.2 既有失败（W06/W07 遗留，非 W08 引入）

- `test_w06_acceptance.py::test_p03b_client_supplied_target_objects_cannot_override_extraction`
  `tests/integration/test_w06_acceptance.py:1255: assert names == {"orders"}`，实际 `E AssertionError: ... 实际={'orders_archive'}`（`1 failed, 2 warnings in 1.81s`）。
  非 W08 依据：① `orders_archive` 全局仅出现在 `docs/w06/05-freeze.md:147`、`docs/adr/ADR-0005-...:182`、`tests/unit/test_action_digest.py`、`tests/integration/test_approval_service.py:449`、`test_w06_acceptance.py:1233/1245`（测试自身注入值），**W08 新增文件无命中**；② W08 对 `core/approval/{service,repository}.py` 的 diff 只新增 `compensation_of` 列（diff 中 5 处 `target_objects` 全是 `target_objects_digest` 列名），**未触及 `extract_target_objects` 抽取路径**（`core/approval/digest.py` 未被修改）；③ 上一轮 #4 报告表 D（T02–T06 落地前）已记录 `test_w06_acceptance.py 23 passed / 2 failed`，本轮仍为 2 failed，未新增。
- `test_w06_acceptance.py::test_t11_no_exactly_once_in_code_comments_only`
  `assert not offenders`，offenders 3 条：`docs/w07/01-prd.md:188`、`test_w06_acceptance.py:1400`、`test_w06_acceptance.py:1427`（测试自身的 pattern 定义）。均为既有文件，**W08 新增文件无命中**。
- 上一轮 #4 报告表 D 与本轮全量日志对相同文件的 F/E 分布一致（`test_audit_service.py` 4 FAILED/`test_auth_api.py` 3 FAILED/`test_rbac_api.py` 2 FAILED/`test_pg_connection.py` 5 FAILED/`test_postgres_mcp.py` 8 FAILED 等），佐证这些失败在 W08 实现落地前已存在。

**未逐条取栈验证的既有 FAILED（17 条）**：`test_api_health.py::test_healthz_db_returns_readonly_identity`、`test_api_v1.py::test_cross_tenant_access_returns_404_without_leaking`、`test_approval_service.py::test_expire_due_marks_pending_and_approved`、`test_audit_service.py` 4 条、`test_auth_api.py::test_login_principal_roles_come_from_db[Viewer/Operator/Approver]`、`test_rbac_api.py` 2 条、`test_step_prepared.py` 5 条。这些全部位于 W06/W07 既有测试文件、W08 新增/修改路径之外；**本报告不声称已逐条证明其根因**，列入「风险与未验证项」§5。

### 2.3 W08 代码失败

**0 条。** 全量日志的 38 FAILED / 135 ERROR 用例名清单中**无任何 `test_w08_*` 文件**（它们排在字母序末尾，全量 900s 未触达）；6 个 W08 集成文件在独立复跑中 23 passed / 0 failed；9 个 W08 新增单测文件包含在 891 passed / 0 failed 内。

### 2.4 「没有 W08 代码失败」的独立复跑证据

```
=== FILE test_w08_crash_matrix.py ===        7 passed in 117.43s   EXIT=0
=== FILE test_w08_verifier_rejects.py ===    5 passed in 11.18s    EXIT=0
=== FILE test_w08_verifier_readback.py ===   5 passed in 0.24s     EXIT=0
=== FILE test_w08_compensation.py ===        2 passed in 10.23s    EXIT=0
=== FILE test_w08_worker_process.py ===      2 passed, 2 warnings in 38.78s  EXIT=0
=== FILE test_w08_recovery_e2e.py ===        2 passed in 4.35s     EXIT=0
=== ALL W08 FILES DONE ===
```
外加：`pytest tests/unit` = `891 passed in 4.46s`；AC-20 离线 `-c` 实跑 PASS（§3）。

---

## 3. AC-01…AC-21 逐条判定

> 判定依据均为实跑输出或实测断言行号，不采信实现者自述。unit 891 全绿覆盖 9 个 W08 新增单测文件；W08 6 个集成文件独立复跑 23 passed / 0 failed 见 §1.4 A。

| # | 结论 | 证据（测试文件::用例） | 判定依据（实测） |
|---|---|---|---|
| AC-01 | **通过** | `tests/unit/test_verification_plan.py::test_from_disclosure_exposes_three_frozen_assertions`、`::test_evaluate_plan_three_assertions_all_pass_with_observed` | `:95-97` `[a.assertion_id ...] == ["affected_rows","target_note_final","snapshot_comparable"]`；`:110` `verdict ∈ {PASS,FAIL,UNKNOWN}`；`:113` 三条全 PASS；unit 891 全绿 |
| AC-02 | **通过** | 同上 `::test_resolve_expected_closed_grammar`、`::test_unknown_placeholder_is_unknown_and_never_raises`、`::test_expected_is_never_evaluated` | `:119-120` `$params.note`→`"repaired"`、`$params.expected_note`→`"broken"`；`:137-139` 未知占位→`UNKNOWN` / `EXPECTED_UNSUPPORTED`；`:151` 封闭文法不 eval |
| AC-03 | **通过** | 同上 `::test_aggregate_worst_case_never_inflates`、`::test_aggregate_is_order_independent` | `:156-160` `{P,P,U}→UNKNOWN`、`{P,F,U}→FAIL`、`{U,U,U}→UNKNOWN`、`{P,P,P}→PASS`、`[]→UNKNOWN`；`:165` 顺序无关 |
| AC-04 | **通过** | `tests/unit/test_verification_plan.py::test_plan_extra_forbid_blocks_model_text` + `tests/integration/test_w08_verifier_readback.py::test_verify_signature_has_no_model_text_channel` | `:169` 注入模型文本字段被拒（`extra="forbid"`）；readback `:319` `not names & {"model_output","llm","narrative","explanation"}`、`:333` `not init_names & {...}`；后者独立复跑 PASSED |
| AC-05 | **通过** | `tests/integration/test_w08_verifier_readback.py` 全 5 例 | 独立复跑 `5 passed in 0.24s`；`:222` `read_only is True`、`:225` `client_identity()==id(ro_client)`、`:226-227` `!= id(rw_client)` / `!= id(control_client)`；`:281` `honest_target.source == "target_readback"`；`:300` `ReadbackPort` 只接受 TargetObject |
| AC-06 | **通过** | `tests/unit/test_verification_perf.py::test_p50_p95_of_fixed_sequence`、`::test_percentile_n1_degenerates`、`::test_percentile_n0_raises_request_invalid` | unit 891 全绿；`[10,20,30,40,50]` P50==30 / P95==50；N=1 退化；N=0 抛 `REQUEST_INVALID` |
| AC-07 | **通过** | 同上 `::test_gain_insufficient_never_claims_success`、`::test_perf_report_with_gain_insufficient_true_is_deterministic`、`::test_no_baseline_is_unknown_not_pass` | `performance_gain_insufficient=True` → `verdict != PASS` 且 `claimed_success is False` |
| AC-08 | **通过** | `tests/integration/test_w08_verifier_rejects.py` 全 5 例 | 独立复跑 `5 passed in 11.18s`；`:273-274` FAIL 用例 `status is not COMPLETED` 且 `is FAILED`、`:297` `VERIFICATION_FAILED.verdict=="FAIL"`；`:340-341` UNKNOWN 用例不 COMPLETED 且落 `NEEDS_RECONCILIATION`/`MANUAL_INTERVENTION`；`:391` Verifier 异常不停在 EXECUTING |
| AC-09 | **通过** | `tests/unit/test_verification_plan.py::test_missing_step_observed_yields_unknown_overall` | `:183-187` 删除 step_observed → `affected_rows` 与 `snapshot_comparable` 均 `UNKNOWN`（`STEP_OBSERVED_MISSING`），整体 `aggregate(...) == "UNKNOWN"` |
| AC-10 | **通过** | `tests/integration/test_w08_compensation.py` 2 例 + `tests/unit/test_compensation_contract.py` 9 例 | 独立复跑 `2 passed in 10.23s`；`:240` `compensation.approval_id != original.approval_id`、`:242` `action_digest != original.action_digest`、`:249-250` note/expected_note 互换、`:267` 补偿后 note 回到 before、`:286` `COMPENSATION_DENIED` 1 条、`:327` 复用原审批→`ToolStatus.DENIED`、`:330` 目标行不变 |
| AC-11 | **通过** | `tests/unit/test_compensation_contract.py::test_public_view_is_not_rollback_and_has_no_forbidden_word` + 上一轮 B3 `-c` 实跑 | `to_public_dict()` → `{'label': '补偿方案', ...}`，label/description 均不含 rollback（大小写不敏感）；`ROLLBACK the row`/`rollBack 回滚`/`auto-rollback plan`/`please Rollback now`/`xrollbacky` 5 种全部 `value_error`；`VerificationResult` 字段集无自然语言字段、注入 `model_output` 被 `extra_forbidden` 拒 |
| AC-12 | **通过** | `tests/unit/test_disposition_policy.py` 6 例 | `test_nine_grid_cell_by_cell` 9 格逐格；`test_unknown_safe_to_compensate_raises_policy_denied` / `test_resolve_disposition_unknown_with_sufficient_evidence_raises` 抛 `POLICY_DENIED`；`test_every_safe_to_compensate_requires_approval` 必带 `approval_required=True`；`test_enums_are_closed` |
| AC-13 | **通过** | `tests/unit/test_recovery_controller.py::test_loop_boundary_first_call_continues_second_stops`、`::test_same_evidence_twice_stops_with_no_progress`、`::test_every_action_has_non_empty_explanation`、`::test_loop_takeover_emits_started_once` | 同一 `call_key` 第 1 次不触发/第 2 次 `AGENT_LOOP_DETECTED`；连续 2 次相同 digest → `AGENT_NO_PROGRESS` 且 `explanation` 非空 |
| AC-14 | **通过** | 同上 `::test_invalid_output_retries_exactly_once_then_stops`、`::test_further_invalid_output_never_retries_again` | 非法输出第 1 次触发 1 次重试（`retry_count == 1`）、第 2 次终止 `MODEL_OUTPUT_INVALID`、后续不再重试 |
| AC-15 | **通过** | 同上 `::test_refute_marks_refuted_and_bumps_plan_version`、`::test_two_refutations_bump_plan_version_by_two_and_keep_history` | `Hypothesis.status == REFUTED`、`plan_version` 递增 1、原 hypothesis 仍可查（不物理删除） |
| AC-16 | **通过** | `tests/integration/test_w08_recovery_e2e.py` 2 例 | 独立复跑 `2 passed in 4.35s`；`:128-132` 证据充分→`NEEDS_RECONCILIATION`、`:92-93` 恢复前后 `tool_calls` 相等且 `tool_calls_delta==0`；`:159-164` 证据不足→`MANUAL_INTERVENTION` |
| AC-17 | **通过（两条都有）** | 离线 `tests/unit/test_worker_scheduler.py`（18 例）+ 真实子进程 `tests/integration/test_w08_worker_process.py`（2 例） | 离线：`:245` `len(reconciler.calls) == 1`、`:246` 入参精确、`:400-403` 抛错时 `executed==1`/`clean is False`/`failed==1`、`:557` 顺序 `["sweep","discover"]`、`:367` 两类调度事件同一伪任务；端到端独立复跑 `2 passed, 2 warnings in 38.78s`，`:200` 任务转 `NEEDS_RECONCILIATION`、`:203` 本任务 `tool_calls==0`、`:214` 无 `TOOL_CALL_RETRIED`、`:223` `RECONCILIATION_SWEPT` 落库、`:230-231` 四个冻结字段齐全 |
| AC-18 | **通过** | `tests/integration/test_w08_crash_matrix.py` 7 例（`test_01`…`test_07`） | 独立复跑 `7 passed in 117.43s`，7 类注入各自独立可 `-k` 定位；统一硬断言 `_assert_no_duplicate_effect`（`:143-155`）：`tool_calls ≤ max`、`:147` `seq == list(range(len(seq)))`、`:150` 无 `TOOL_CALL_RETRIED`、`:153` note ∈ {before, target}、`:155` `step_observed ≤1`；`test_04` 断言 `FENCING_TOKEN_STALE`（`:454`） |
| AC-19 | **通过** | `tests/integration/test_w08_crash_matrix.py::test_07_pause_and_cancel_race_with_in_flight_sql` | `:566-567` PAUSE 后 `status is EXECUTING`（不伪造「已停止」）、`:574` `TASK_PAUSE_REQUESTED`；`:580-591` CANCEL 经 `CANCEL_REQUESTED`/`TASK_CANCEL_REQUESTED` 落 `CANCELLED` 或 `NEEDS_RECONCILIATION`，`:587` `executed == 0`、`:593` note 不变 |
| AC-20 | **通过（含静态核对）** | `-c` 实跑 + `tests/unit/test_tools_contracts.py::test_registry_has_exactly_the_ten_observer_tools` + `tests/unit/test_write_tool_gate.py::test_production_registry_write_tools_declare_target_objects` | `-c` 输出：`[AC-20] PG 工具数 = 7`、`side_effect 集合 = ['read']`、`name 列表 = ['explain_query','get_connection_snapshot','get_lock_details','get_lock_wait_edges','get_query_stats','get_table_stats','get_transaction_metadata']`，断言 `len(defs)==7` 且 `{side_effect}=={'read'}` → `PASS`；静态：`adapters/lab_assembly.py:66-68` 仅在 `is_lab_mysql_target(...)` 为真时注册唯一写工具 `LabOrderNoteUpdateTool`，否则写集合为空，`workers/stack.py:226-233` 引用同一唯一装配点，不存在第二个写动作；两个 unit 测试在 891 全绿内。**未实跑构造生产 registry 断言**（需连库），以静态代码 + unit 测试为依据 |
| AC-21 | **通过** | `tests/unit/test_verification_determinism.py` 6 例 | `:225-228` 10 轮 `verdicts` 集合大小 `==1` 且 `=={"PASS"}`；`:234` plan_digest、`:249` 断言三元组、`:259` disposition 均集合大小 1；`:274-275` `plan_source.calls == ROUNDS`、`readback.calls == ROUNDS`（确定性来自同输入，非缓存首次结果）；`:253` `sources == {"step_observed","target_readback"}` |

**AC 通过数：21 / 21**（0 未通过、0 未覆盖）。

---

## 4. 4 个「实现者自述偏差」的独立认定

| # | 偏差 | 代码/文档实证 | 独立认定 |
|---|---|---|---|
| 1 | `COMPENSATION_DENIED` 实际只覆盖「重复发起补偿」「原审批未消费」；复用原审批由 Gateway 发 `APPROVAL_ALREADY_CONSUMED` | `core/verification/compensation.py:36-39` 定义 4 个 reason（`COMPENSATION_REUSES_ORIGINAL_APPROVAL` / `COMPENSATION_DIGEST_NOT_INDEPENDENT` / `COMPENSATION_ORIGINAL_NOT_CONSUMED` / `COMPENSATION_ALREADY_REQUESTED`），`:184-190` 复用原审批在 `request()` 层被本服务拒、`:294-305` 发 `COMPENSATION_DENIED`；但**执行阶段**用原 approval 驱动由 Gateway 拒（`:327` `reused.status is ToolStatus.DENIED`）。设计 `02-design.md:434` 冻结表要求 `COMPENSATION_DENIED`「含复用原审批、digest 不符、未审批」，`:370` 时序图 `APPROVAL_ALREADY_CONSUMED → emit COMPENSATION_DENIED`；`01-prd.md:83` 允许 `APPROVAL_ALREADY_CONSUMED` 或 `APPROVAL_DIGEST_MISMATCH` | **不影响 AC-10，但登记口径与设计 §5 第 7 行不完全一致**：AC-10 只要求「复用原 approval_id → 拒绝且目标行不变」，`test_driving_compensation_with_original_approval_is_denied` 已通过（`ToolStatus.DENIED` + note 不变 + 无 compensation_of 关联）；但执行阶段该路径**未发 `COMPENSATION_DENIED` 事件**，与设计时序图 :370 不符。team-lead 自述「只覆盖重复发起、原审批未消费」也不完整（代码另覆盖 digest 不独立、request 层复用）。建议：补登记到门禁件并明确"执行阶段拒绝由 Gateway 承担、不补发 DENIED"，或按设计补发事件（**不改 AC 判定**）。 |
| 2 | `sweep` 失败计入 `failed`（`RunReport` 无专用计数位） | `workers/runner.py:285` `failed=counters[OUTCOME_FAILED] + (1 if sweep_failed else 0)`；`test_worker_scheduler.py::test_sweep_failure_does_not_stop_candidate_execution` 断言 `failed == 1`、`clean is False`、`executed == 1`；`docs/w08/08-drill-report.md` §1.3 #2 **已登记** | **已登记且不影响 AC-17**：AC-17 要求「`sweep` 抛错时 `clean=False` 且循环不中断」，实测满足；权威证据是 `RECONCILIATION_FAILED` 事件（`:413`）。风险：退出码非 0 可能被误读为"执行失败"；`RunReport` 字段集已冻结，加专用计数位需升版本，建议 W09 处理。 |
| 3 | `RECONCILIATION_SWEPT.scanned` = 产出结论条数（非全表扫描行数）；`conclusions` 超 50 条被截断 | `workers/runner.py:351-356` 注释明确口径 + `"scanned": len(results)`；截断在审计层 `core/audit/events.py:38 MAX_AUDIT_ITEMS = 50`、`:144-146` `value[:MAX_AUDIT_ITEMS]` + `<truncated N items>`；`docs/w08/08-drill-report.md` §1.3 #4/#5 **已登记**（含"实测 72 条即如此"） | **已登记且不影响 AC**：AC-17 只要求 sweep 恰好 1 次、失败不中断、SWEPT 落库；`test_w08_worker_process.py:230-231` 仅断言 4 个冻结字段存在（实测通过）。风险：`scanned` 语义易被误读为全表扫描行数；>50 结论时事件丢明细但 `scanned`/`converged` 完整。 |
| 4 | 调度事件挂伪任务 `system:worker_scheduler`，`AuditService.query` 查不到 | `workers/runner.py:64 SCHEDULER_TASK_ID = "system:worker_scheduler"`、`:423` `task_id=SCHEDULER_TASK_ID`、`:428` `actor_user_id="system:worker_scheduler"`；`docs/w08/08-drill-report.md` §1.3 #1 **已登记**（明确 `tenant_id` 过滤列为 NULL、`AuditService.query(tenant_id=...)` 查不到）；测试用 `WHERE task_id=SCHEDULER_TASK_ID` 直查事件表（`test_worker_scheduler.py::test_swept_and_failed_share_scheduler_task_id`、`test_w08_crash_matrix.py::test_03`、`test_w08_worker_process.py:217-223`） | **已登记且不影响 AC-17**（相关测试全绿），但影响"通过审计 API 按租户检索调度事件"的可观测性 → **W09 风险项**，需在设计里明确检索方式或补租户归属。 |

**额外发现（team-lead 未列，但同一份演练报告已登记）**：`docs/w08/08-drill-report.md` §1.3 #3 —— `reconcile_interval_s`（`core/config.py` 的 W08 新配置）**只被校验、未被读取**；调度节奏由 `worker_poll_interval_s` / 调用方决定。判定：**已登记且不影响 AC-17**，但属于"配置项声明与实现不一致"，建议 W09 明确该配置的语义或删除。

---

## 5. 风险与未验证项（不写"应该没问题"）

1. **全量 `pytest -q` 仍未跑完**：`DG_RECOVER_ON_STARTUP=false timeout 900 pytest tests/integration` 900s 只推进 77%，13 个文件未触达；本报告的文件级数字由「1 次未跑完的全量 + 13 个文件独立补齐」拼成，**不是单次全量进程的完整结果**。全量回归"通过"状态在本机环境**不可判定**。这是上一轮 #4 已定位的启动恢复根因（763 个历史未完成任务 × 每条 SQL 新建连接）叠加 PG/admin 环境不可用后的延续；本轮已按 `DG_RECOVER_ON_STARTUP=false` 绕开恢复循环，但 369 个用例仍因 PG 探针与真实库往返在 900s 内跑不完。
2. **38 FAILED / 135 ERROR 中，17 条 FAILED 与 135 条 ERROR 未逐条取栈验证归因**（§2.2 已列名单）。已逐条取栈的只有 4 条（3 条 admin + 1 条既有）。这些未验证项全部位于 W06/W07 既有测试文件、W08 新增/修改路径之外，且与上一轮 #4（T02–T06 落地前）的 F/E 分布一致；**但本报告不声称已证明每一条的根因**。
3. **PG 5432 与 `DG_ADMIN_DSN` root 1045 不可用**是环境阻塞，不是 W08 代码缺陷；但它意味着本机**永远无法给出一次全绿的 integration 全量回归**，门禁复核必须依赖"分层证据"（unit 891 + W08 6 文件 23 + AC 逐条）而非一次全量绿灯。
4. **AC-20 的生产写工具集合**是静态代码核对（`adapters/lab_assembly.py:66-68`、`workers/stack.py:226-233`）+ unit 测试全绿，**未实跑构造生产 registry 断言**（构造需连库）。
5. **W08 T02 迁移 008 是否已在本机数据库执行未验证**：`database/migrations/008_w08_verification.sql` 与 rollback 脚本已落盘（git status 可见），但本次只读复核未检查 `information_schema`；两次观测（全量与 W08 独立复跑）未见因 schema 变化导致的结果前后矛盾。
6. **集成测试写库残留**：本轮复跑的 W08/W06/W07 集成用例按其设计写入控制库 append-only 表、审批与 LAB 目标行；`DG_ADMIN_DSN` 不可用时清理降级为 `UserWarning`（`test_w08_worker_process.py` 2 条、`test_worker_process.py` 2 条、`test_w06_acceptance.py` 26 条），残留未清理，与 `docs/w07/worker-residue.md` 同类。
7. **偏差 #1 的登记位置**：`docs/w08/08-drill-report.md` §1.3 只登记 T06 的 5 条边界；`COMPENSATION_DENIED` 与设计 §5 冻结表的口径差异（执行阶段不补发 DENIED）未在演练报告中登记，需确认是否在其它门禁件里（本次未找到，`docs/w08/` 内仅 `01-prd.md:83`、`02-design.md:105/370/434` 有相关表述）。
8. **时间点一致性**：观测跨夜（2026-11-12 23:52 → 2026-11-13 14:41 +0800）。期间其它工程师在写 W09 文档（`?? docs/w09/`），`git status` 中 W08 实现文件与测试文件清单在前后两次检查间一致，未发现 W08 代码在复核期间变动。

---

## 6. 一句话结论

**不可**：W08 已具备「AC-01…21 逐条通过 + unit 891 全绿 + W08 6 个集成文件 23 passed / 0 failed + 无一条失败归因到 W08 新增代码」的**分层证据**，但**缺一次跑完的全量集成回归**（900s 只到 77%、13 个文件未触达，PG 5432 与 admin 1045 环境阻塞导致既有文件 38 FAILED / 135 ERROR 无法全绿），且偏差 #1 与设计 §5 冻结事件口径不一致尚未补登记；按现有门禁口径（要求全量回归可判可信）**不具备门禁复核条件**，除非门禁接受"分层证据 + 环境阻塞豁免 + 偏差补登记"作为替代。
