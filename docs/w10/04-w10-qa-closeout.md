# W10 定向验收 QA 收口报告

| 项 | 值 |
|---|---|
| 报告编号 | `docs/w10/04-w10-qa-closeout.md` |
| 产出人 | 严过关（Yan）｜QA 工程师（软件 QA 独立定向验收与收口） |
| 团队 | `software-dbguardian-w10-resume` |
| 时点锚 | `HEAD = 2850eca`；全部改动**未提交** |
| 工作区 | `D:/DBGuaredian`（Windows / Git Bash）；解释器 `.venv/Scripts/python.exe` = Python 3.14.7 |
| 权威口径 | `docs/w10/03-task-cards.md`（AC-01…AC-21，验收总表 `:551-577`）、`docs/w10/02-design.md`、`docs/w10/01-prd.md`、`docs/w10-w12-lead-rulings.md`（§2 W10-Q1..Q8、§5 执行纪律、§6 `ENV-*` 编号、§7 Q-W10-A..F、§10 W10-CORR-01/02） |
| 上游输入 | T01–T06 各卡 `IS_PASS: YES`；全局一致性审查 28/28；工程师 W10 定向验收总门禁 `IS_PASS: YES` |
| 本报告性质 | **QA 独立定向验收结论**，不复核「文件存在」、不重跑工程师命令即签字；含独立构造错误路径 51 项、AC 逐条覆盖矩阵、串行复跑真实数字、冻结边界与遗留登记 |
| 门禁状态 | **W10 门禁：未评**。本报告只给「定向验收通过 / 不通过」，不替主理人签门禁 |

---

## 一、结论与智能路由判定

| 项 | 结论 |
|---|---|
| **W10 定向验收** | **通过** |
| 独立错误路径验证 | **51 / 51 PASS，exit 0**（脚本 `D:/qa_w10_qa_tmp/qa_w10_ep.py`，仓库外临时目录，对仓库只读） |
| 串行复跑定向子集 | `pytest tests/unit` 全量 **1582 passed / 0 failed / exit 0**；另 11 组定向命令 + 2 项非 pytest 复核**全部 0 failed** |
| AC 覆盖 | **21 / 21 已覆盖**（0 部分、0 未覆盖） |
| **智能路由判定** | **`NoOne`** |
| 路由理由 | 独立构造的 51 项错误路径全部按 PRD / 设计预期被拒或返回正确布尔值，未发现 W10 生产代码 bug；未发现测试代码自身 bug（无需 QA 自修）；串行复跑无失败用例。唯一异常事件为 CPython 3.14.7 / pytest assertion-rewrite 层偶发段错误（见 §七 L-3），单独隔离重跑同命令即恢复，**不构成 `Engineer` 或 `QA` 路由** |
| 测试轮次 | 第 1 轮完成（独立验证 + 串行复跑 + 隔离重跑）；无源码 / 测试改动，未触发第 2 轮回归；2 轮硬上限未触及 |
| 已知问题 | **3 项实质遗留**（W09 存量 `git diff --check` 告警、设计 §10.3 事实偏差、C1 段错误环境事件）+ **2 项口径事实登记**（`cases.jsonl` 为运行期产物、`fault_seed` 公开 data_seed 边界）+ **5 项 `ENV-*` 环境阻塞**；均不判 FAIL、不冒充通过、不修环境 |
| git 操作 | **未执行 `git commit` / `add` / `checkout` / `reset`**；未碰 `.env` / `DG_ADMIN_DSN` / `dg_ctl`；未 DELETE / UPDATE / TRUNCATE 历史残留 |

> 本报告严格遵守 §九 的 C-7 表述禁令自查；W10 门禁未评，上游口径见 §九。

---

## 二、AC-01…AC-21 覆盖矩阵

判据要点以 `03-task-cards.md:551-577` 验收总表为准。「独立数字」列为本次 QA 实跑命令编号（见 §六）。用例名取自各测试文件静态枚举（`grep -nE '^def test_'`），「已覆盖」判定同时要求：文件真实落盘、用例名存在、且被本次串行复跑真实收集执行。

| AC | 判据要点 | 卡 | 证据文件 | 具体用例名 | 覆盖 | 独立数字 |
|---|---|---|---|---|---|---|
| AC-01 | Manifest 字段完整（缺 `seed`/`env_image_digest`/`budget` → `ValidationError`；版本与 `baseline_manifest.json` 相等） | T02 | `tests/unit/test_w10_run_manifest.py` | `test_ac01_missing_required_field_raises_validation_error`、`test_ac01_from_baseline_versions_equal_frozen_assets`、`test_ac01_from_baseline_respects_explicit_digest_and_overrides`、`test_ac01_from_baseline_rejects_conflicting_frozen_assets` | 已覆盖 | C1 / C13 |
| AC-02 | Manifest 封闭（`extra="forbid"`） | T02 | 同上 | `test_ac02_extra_fields_are_forbidden` | 已覆盖 | C1 / C13 |
| AC-03 | Runner 可重放（`{case_ids}`/`{seeds}`/`{tool_sequence_digest}` 各 == 1） | T02 | `tests/integration/test_w10_runner_replay.py` | `test_ac03_two_runs_have_single_valued_case_seed_and_digest_sets`、`test_ac03_full_frozen_suite_replays_case_seed_and_digest_sets` | 已覆盖 | C6 |
| AC-04 | 取消与失败原因非空 | T02 | 同上 | `test_ac04_cancel_sets_status_and_reason`、`test_ac04_cancel_token_sets_status_and_reason`、`test_ac04_case_exception_records_failure_reason_and_fails_run` | 已覆盖 | C6 |
| AC-05 | Runner 只读（签名无 `ToolGateway`/`ApprovalService`；写工具集为空） | T02 | `test_w10_run_manifest.py` + `test_w10_runner_replay.py` | `test_ac05_constructor_signature_has_no_gateway_approval_or_worker`、`test_ac05_write_tool_registry_is_rejected`、`test_ac05_readonly_registry_assembles_and_observes_seven_tools`、`test_ac05_registry_has_no_write_tools_and_signature_is_readonly` | 已覆盖 | C6 / C13 |
| AC-06 | 指标分母与排除规则逐值相等 | T03 | `tests/unit/test_w10_metrics.py` | `test_ac06_ten_cases_denominators_match_frozen_expectations`、`test_ac06_environment_cases_are_in_d_env`、`test_zero_denominator_yields_none_never_zero` | 已覆盖 | C1 / C5；C12 独立核对 14 分母全 0 |
| AC-07 | 重复 seed 不独立计数 | T03 | 同上 | `test_ac07_repeated_seed_increments_effective_samples_only_once` | 已覆盖 | C1 / C5 |
| AC-08 | 不编造指标（`NOT_RUN`/`RUNNING` → `scores`/`metrics` 为 `None`） | T03 | 同上 + `test_w10_run_manifest.py` | `test_ac08_run_result_scores_and_metrics_none_before_terminal`、`test_ac08_metrics_are_not_fabricated_when_no_cases`、`test_run_result_scores_and_metrics_are_none_before_terminal` | 已覆盖 | C1 / C5 / C13；EP-7 独立核对 `metrics/ci/cost=None` |
| AC-09 | 四类归因之和 == 总 case 数 | T03 | 同上 | `test_ac09_attribution_counts_sum_to_total_cases`、`test_attribute_four_step_decision_tree` | 已覆盖 | C1 / C5；C12 独立核对 `attr_sum=0 == total_cases=0` |
| AC-10 | Bootstrap 可复现 + `percentile([])` → `REQUEST_INVALID` | T03 | `tests/unit/test_w10_bootstrap.py` | `test_ac10_same_seed_two_runs_are_bitwise_equal`、`test_default_resamples_and_seed_are_deterministic`、`test_percentile_empty_samples_raises_request_invalid`、`test_percentile_reused_for_ci_endpoints` | 已覆盖 | C1 / C13 |
| AC-11 | 遥测字段白名单（6 项；`request_body`/`user_id` 被剔除） | T05 | `tests/unit/test_w10_telemetry_adapter.py` | `test_allowlist_is_exactly_six_logical_keys`、`test_ac11_parse_output_has_exactly_six_metric_fields`、`test_ac11_forbidden_fields_are_dropped_and_recorded`、`test_ac11_dropped_fields_are_sorted_and_deterministic`、`test_ac11_pool_connections_composite_key_is_validated` | 已覆盖 | C1 / C13；EP-7 独立核对 `metric_payload()` 6 键全 `None` |
| AC-12 | 池状态缺失不冒充（`UNKNOWN` +「应用池状态未确认」；DB 连接数冒充 → `ValidationError`） | T05 | 同上 | `test_ac12_none_raw_is_unknown_with_six_none_metrics`、`test_ac12_database_connection_count_impersonation_is_rejected`、`test_ac12_source_kind_outside_application_pool_is_rejected`、`test_ac13_unknown_collection_status_never_uses_high_quality` | 已覆盖 | C1 / C13；EP-7 独立运行时验证 `parse(None)` |
| AC-13 | 遥测 Evidence 字段非空且经 W09 脱敏 | T05 | `tests/integration/test_w10_telemetry_evidence.py` + `test_w10_telemetry_adapter.py` | `test_ac13_telemetry_evidence_is_persisted_via_evidence_service`、`test_w09_high_quality_guard_is_not_bypassed`、`test_ac13_to_evidence_request_produces_well_formed_evidence_create`、`test_ac13_w09_for_evidence_sets_redaction_version_and_contains_no_raw` | 已覆盖 | C8（DB 落库，非 skip）/ C13 |
| AC-14 | group split 无泄漏（交集空 + `leakage_count == 0`；许可不明 `QUARANTINED`） | T04 | `tests/unit/test_w10_group_split.py` | `test_ac14_homologous_templates_have_empty_intersections_and_zero_leakage`、`test_ac14_full_reference_split_is_deterministic_without_rng`、`test_ac14_leakage_count_is_measured_not_a_constant`、`test_ac14_unknown_license_is_quarantined_and_excluded`、`test_assert_no_group_leakage_detects_tampered_manifest` | 已覆盖 | C10 / C1 |
| AC-15 | Oracle 不入训练样本（`oracle_in_training_samples is False`；文本无 `expected_root_cause`/`fault_seed`） | T04 + T01 | `test_w10_group_split.py` + `tests/unit/test_w10_oracle_isolation.py` | `test_ac15_training_texts_have_no_oracle_truth_field_names`、`test_oracle_leak_sample_is_rejected_by_t01_assert`、`test_dataset_module_reuses_t01_oracle_assert`、`test_s3_leaked_sample_with_expected_root_cause_is_detected`、`test_s3_frozen_prompt_and_clean_training_samples_are_clear`、`test_s6_serialized_run_artifact_contains_no_truth_fields` | 已覆盖 | C10 / C1；EP-6 独立 11 项 |
| AC-16 | token 统计不臆造（P50/P95 逐值；未冻结 → `UNKNOWN` + `null`） | T04 | `tests/unit/test_w10_token_stats.py` | `test_ac16_fixed_samples_stats_match_written_down_values`、`test_ac16_stats_are_deterministic`、`test_ac16_no_tokenizer_means_all_statistical_fields_are_none`、`test_ac16_no_tokenizer_never_reports_fabricated_numbers`、`test_token_stats_without_gate_is_pure_statistics` | 已覆盖 | C1 / C13；EP-7 `tokenizer_commit=None` |
| AC-17 | Baseline 报告诚实（`model_commit is None` → `metrics None` + `blocked_reason` 非空；环境故障不计入 `false_diagnosis_rate`） | T06 | `tests/unit/test_w10_baseline_report.py` | `test_ac17_no_model_means_no_metrics_and_blocked_reason`、`test_ac17_validator_rejects_metrics_when_model_absent`、`test_ac17_validator_rejects_empty_blocked_reason`、`test_assert_no_fabricated_metrics_catches_model_construct_bypass`、`test_ac17_attribution_counts_sum_to_total_cases`、`test_ac17_build_rejects_inconsistent_attribution_counts`、`test_ac17_environment_faults_are_attributed_and_excluded`、`test_baseline_report_json_is_honest_and_valid`、`test_baseline_report_json_has_no_fabricated_or_forbidden_text` | 已覆盖 | C3 / C1；C12 自检；EP-1 / EP-2 / EP-7 |
| AC-18 | SFT 条件闸门（`shortcoming_located False` → `False`；`10..50`；版本相等；`dpo`/`rl`/`14b` 恒 `False`） | T06 | `tests/unit/test_w10_sft_gate.py` | `test_ac18_shortcoming_not_located_disallows_sft`、`test_ac18_sample_count_bounds_return_false_not_raise`、`test_ac18_harness_version_must_match_baseline_verbatim`、`test_ac18_testset_version_must_match_baseline_verbatim`、`test_ac18_classvars_are_false_and_properties_always_false`、`test_ac18_instance_assignment_cannot_change_switches`、`test_ac18_dict_injection_is_rejected_by_extra_forbid`、`test_ac18_model_copy_injection_cannot_change_readonly_properties`、`test_w10_q7_sft_gate_is_record_only` | 已覆盖 | C3 / C1；EP-3 / EP-4 独立注入 |
| AC-19 | Oracle 时序隔离（未终态 → `ORACLE_ACCESS_DENIED`；终态可读 1 次 + 审计） | T01 | `tests/integration/test_w10_oracle_timing.py` + `test_w10_oracle_isolation.py` | `test_oracle_timing_three_states_and_task_events_rows`、`test_oracle_timing_three_states_with_in_memory_state_machine`、`test_s5_read_queries_status_reader_itself_and_denies_non_terminal`、`test_s5_first_read_on_terminal_status_is_allowed`、`test_s6_second_read_is_denied_with_already_read`、`test_s7_audit_emitted_for_every_allow_and_deny`、`test_s8_non_terminal_states_are_denied` | 已覆盖 | C7（DB 非 skip）/ C10 |
| AC-20 | 写面与引擎不回退 | T01–T06 全卡回归 | **既存** `tests/unit/test_write_tool_gate.py` + `tests/unit/test_tools_contracts.py` | `test_production_registry_write_tools_declare_target_objects`（`:119` `all(side_effect != "write")`，D-02 等价断言）、`test_registry_cannot_hold_write_tool_without_target_objects`、`test_registry_has_exactly_the_ten_observer_tools`（`:23-41` 10 工具全 `R0`/`read`，D-03 第二工具面）、`test_tool_input_validation_returns_structured_error` | 已覆盖（既存，未被替代） | **C2 11 passed**；C1 亦含；`TRACKED@HEAD` |
| AC-21 | W09 脱敏不被绕过 | T04 + T01 | **既存** `tests/integration/test_w09_redaction_pipeline.py` + `tests/unit/test_w10_group_split.py` | 既存 4 条：`test_redacted_evidence_is_persisted_with_matching_audit_version`、`test_blocked_payload_raises_and_writes_zero_rows`、`test_review_required_writes_quarantine_without_raw_content`、`test_clean_payload_hash_invariant_is_unchanged`（`PHONE_RAW`/`CONNECTION_RAW`/`NAME_RAW` 不落盘、`redaction-v2`）；T04 复用门：`test_ac21_not_passable_statuses_are_rejected`、`test_ac21_blocked_exception_path_is_rejected`、`test_ac21_real_w09_gate_blocks_connection_string`、`test_ac21_clean_and_redacted_samples_are_passable`、`test_dataset_module_does_not_rewrite_w09_gate` | 已覆盖（既存，未被替代） | **C9 4 passed**（DB 非 skip）/ C10；`TRACKED@HEAD` |

**统计**：21 条全部「已覆盖」（21/21），0 部分、0 未覆盖。

---

## 三、AC-15 / AC-20 / AC-21 既存用例专项核对

任务要求特别核对「既存用例是否真被引用、有没有被新建替代用例绕过」。QA 独立核对结果：

| 核对项 | 方法 | 结果 |
|---|---|---|
| 既存用例真实性 | `git ls-files --error-unmatch` | `test_write_tool_gate.py`、`test_tools_contracts.py`、`test_w09_redaction_pipeline.py` 三者均为 **TRACKED@HEAD**（W10 之前已存在），非 W10 新建 |
| 是否存在新建替代文件 | `ls tests/unit/test_w10* tests/integration/test_w10* \| grep -iE 'write\|redact\|gate\|contract'` | 仅命中 `test_w10_sft_gate.py`，那是 T06 **SFT 条件闸门**，与写工具门禁 / W09 脱敏无关；**W10 未新建 write/redaction/gate/contract 类替代用例文件** |
| 既存用例是否真被执行 | 单独定向实跑 | C2 = `11 passed`（6 + 5）；C9 = `4 passed`（真实控制库落库，非 skip）；C1 全量 1582 亦收集两个 unit 既存文件 |
| 断言形态是否与设计 D-02 / D-03 一致 | 读源码断言行 | `test_write_tool_gate.py:119` `assert all(item.side_effect != "write" ...)` 对应 D-02 等价断言；`test_tools_contracts.py:23-41` 断言「10 个 observer 工具全 `R0`/`read`」对应 D-03 第二工具面，未与 7 个固定只读工具面混写 |
| AC-15 是否被替代 | 用例名 + `TRACKED` 状态 | `test_w10_group_split.py` 为 T04 本卡用例（验收总表本就列为 AC-15/AC-21 落点），其 `test_ac21_*` 系列显式**复用** W09 门（`test_ac21_real_w09_gate_blocks_connection_string`、`test_dataset_module_does_not_rewrite_w09_gate`），属出口断言而非替代 |
| AC-21 的 W09 集成是否保住权威地位 | C9 单独实跑 | 4 条既存脱敏 pipeline 用例 0 failed，未删、未 skip、未被 T04 用例覆盖替代 |

**结论**：AC-15 / AC-20 / AC-21 依赖的既存用例**真被引用且真实执行，未被新建替代用例绕过**。

---

## 四、独立错误路径验证（QA 自行构造，非重跑工程师用例）

脚本 `D:/qa_w10_qa_tmp/qa_w10_ep.py` 位于**仓库外**临时目录，对仓库只读；不修改 W10 生产代码、不修改既有断言。共 51 项检查，**51 PASS / 0 FAIL / exit 0**。

| 组 | 独立构造的错误路径 | 项数 | 结果 | 关键证据（真实输出） |
|---|---|---|---|---|
| EP-1 | `model_commit=None` 但 `metrics` 非空 | 3 | 3/3 PASS | `model_validate` → `ValidationError`「model_commit 为 None 时 metrics 必须为 None（禁止编造指标）」；`model_copy(update=...)` 与 `model_construct` 绕过路径 → `BENCHMARK_INVALID`「model_commit 为 None 时不得输出任何 metrics」 |
| EP-2 | 分母为 0 却被塞入数值 | 2 | 2/2 PASS | → `BENCHMARK_INVALID`「分母为 0 却有指标数值（编造指标）」；**反向对照**分母改 7 后不被拒（证明拒的是分母 0，非恒拒） |
| EP-3 | `SftGate` 三开关注入 | 11 | 11/11 PASS | 实例赋值 ×3 → 抛错且 property 仍 `False`；字典注入 ×6（含 3 个 `ClassVar` 名）→ `extra=forbid` 的 `ValidationError`；`model_copy(update=...)` → property 仍 `[False,False,False]`；**加强**：直接篡改类级 `SftGate.DPO_ALLOWED/RL_ALLOWED/FULL_14B_ALLOWED = True` 后 property 仍 `[False,False,False]`（返回字面常量，无注入路径） |
| EP-4 | `shortcoming_located=True` 下越界 / 版本不等 | 8 | 8/8 PASS | `sample_count=9/51` → `False`；`10/50` → `True`；`harness_version`、`baseline_harness_version`、`testset_version`、`baseline_testset_version` 四种不等 → 全部 `False`；8 项**均不抛错** |
| EP-5 | W10-CORR-01 第③分支（临时目录注入 `ROOT`） | 5 | 5/5 PASS | 用 `tempfile.TemporaryDirectory()` 建 5 个 T03 占位文件并改 `mod.ROOT`（不改仓库文件；`DEFAULT_METRICS_PATH` 为 import 时计算的绝对路径，仍读真实冻结件）：**空文件** → `REJECT`（`AssertionError: task_success_rate`）；**占位文件** → `REJECT`；**仅 14 个指标名、无逐字定义** → `REJECT`；**真实完整文档** → `PASS`（反证守卫非恒拒）；**文档不存在** → `PASS`（第②分支历史语义保留） |
| EP-6 | Oracle 真值泄漏 | 11 | 11/11 PASS | 6A `FailureCase` 塞 `expected_root_cause` → `extra=forbid` `ValidationError`；6B 构造含 `SELECT *`/`password=`/三真值字段/`postgresql://` 的恶意 `failure_reason` → 报告 `safe_reason=CASE_EXCEPTION`、文本零泄漏、`scan_texts_for_oracle_leak=()`、`assert_no_fabricated_metrics` 通过；6C 反例把真值塞进 `notes` → `BENCHMARK_INVALID`「报告出现 Oracle 真值 / SQL 原文标记」；6D 5 个 `benchmarks/w10/*.json` 三真值字段名 / SQL 原文 / 参数值**零命中**（`fault_seed` 命中登记见 §七 L-5）；6E 独立复刻离线替身真实跑一次 `BenchmarkRunner` 产出 `cases.jsonl`（3 行 = 3 cases，零真值、公开 `fault_seed` 保留），并把真值拼进产物文本触发写盘硬闸门 → `BENCHMARK_INVALID`「产物文本命中 Oracle 真值字段名」 |
| EP-7 | tokenizer / 快照 / 模型缺席诚实性 | 9 | 9/9 PASS | `baseline_report.json`：`model_commit`/`tokenizer_commit`/`metrics`/`confidence_intervals`/`cost` 全 `None`（非 `0`/`{}`）、`status=BLOCKED`、`blocked_reason` 含 `BLOCKED_MODEL_ENV_MISSING`、14 个分母全 `int 0` 如实保留、全文无「预计/估计/约」；冻结 `baseline_manifest.json` model/tokenizer 均 `null`；`RunManifest.from_baseline()` → `env_image_digest='UNKNOWN'` / `source='UNKNOWN'`；`AppTelemetryAdapter().parse(None)` → `collection_status=UNKNOWN` + `message='应用池状态未确认'` + 6 项指标全 `None` |
| EP-8 | `core/benchmark/` 顶层 import 边界 | 2 | 2/2 PASS | `ast` 严格只取 `tree.body` 直接 `Import`/`ImportFrom`：扫描 11 个 `.py`、102 条顶层 import，`apps`/`workers`/`core.gateway`/`core.approval`/`core.agent`/`benchmarks.*` **违规 = []**；`benchmarks.*` 顶层 = 0，函数体内惰性 import 共 3 处（`runner.py:383/391/400`，带 `# noqa: PLC0415` 惰性硬约束） |
| **合计** | | **51** | **51 PASS / 0 FAIL** | exit 0 |

**独立视角结论**：工程师只验证了 happy path 的部分，本次 51 项错误路径全部按 PRD / 设计预期被拒或返回正确布尔值；特别地 EP-2 反向对照、EP-5 「真实文档 PASS / 文档缺席 PASS」双反证、EP-3 类级 `ClassVar` 直接篡改、EP-6 运行期 `cases.jsonl` 真实产物复现，均排除了「恒拒 / 恒放行 / 静态文件存在即通过」的伪验证。

---

## 五、冻结资产与依赖边界

| 边界项 | 核对方法 | 结果 |
|---|---|---|
| `benchmarks/baseline_v0/**`、`benchmarks/faults/**` 零修改 | `git diff --name-only` + `git status --short` 过滤冻结路径；`git ls-files` 计数 | 两条命令**均零命中**；31 个文件 TRACKED@HEAD，零修改 ✓ |
| `core/evidence/models.py`（W09 冻结）、`core/redaction/**` 未被 W10 改写 | 19 个 tracked-modified 清单过滤 | **零命中** ✓ |
| `core/benchmark/` 无 `apps/**`、`workers/**`、`core/gateway/**`、`core/approval/**`、`core/agent/**` 顶层 import | EP-8 ast 严格顶层扫描 | 违规 `[]` ✓ |
| `core/benchmark/` 无 `benchmarks.*` **顶层** import | EP-8 | 顶层 0；函数体内惰性 3 处（设计允许的 R-13 惰性解析）✓ |
| CORR-02 事实（tracked-modified 仍为 19） | `git status --short` 计数 + `git diff --name-only` 计数 | `74 = 19 个 ' M' + 55 个 '??'`；`git diff --name-only` = **19**，与会话首帧一致；`tests/unit/test_w10_metrics.py` 为 `??` 未跟踪，故改写不进入 `git diff --name-only`；**未执行 `git add` 凑数** ✓ |
| 19 个 tracked-modified 清单中出现的 W09 文档 / `core/errors.py` / `core/audit/events.py` | 清单逐项核对 | `docs/w09/02-design.md`、`03-task-cards.md`、`06-qa-report.md` 为会话起始即存在的 **W09 存量 `M` 文件**（CORR-02 已确认 19 清单逐项相同，W10 本轮未新增 tracked-modified）；`core/errors.py` / `core/audit/events.py` 为 Q-W10-A 授权的**只追加**改动；W10 全部新文件均为 `??` 未跟踪 |
| 设计 §10.3 事实偏差 | `grep -rn _DEGRADED_STATUSES core/` + 读 `core/evidence/service.py:23-29` | 实测 `_DEGRADED_STATUSES = frozenset({DENIED, TIMEOUT, NOT_SUPPORTED})`，**不含 `UNKNOWN`**；登记为设计文档事实偏差，**未改生产代码**（见 §七 L-2） |

---

## 六、串行复跑真实数字

**全部串行，无任何两个 pytest 并发**；每条 pytest 均带 `-o addopts="" -q -p no:cacheprovider`；未写 `--timeout=`；需 DB 的用外层 `timeout 180`；解释器 `.venv/Scripts/python.exe`。

| # | 命令（路径参数） | 真实输出 | exit |
|---|---|---|---|
| C1（首次，批次 1 首条） | `pytest tests/unit` | `Windows fatal exception: access violation` / `Segmentation fault`（收集阶段，堆栈全在 `_pytest/assertion/rewrite.py:traverse_node` 递归） | **139** |
| **C1-RETRY（单独隔离重跑）** | `pytest tests/unit` | **1582 passed in 8.58s** | **0** |
| C2 | `pytest tests/unit/test_write_tool_gate.py tests/unit/test_tools_contracts.py` | `11 passed in 0.13s` | 0 |
| C3 | `pytest tests/unit/test_w10_baseline_report.py tests/unit/test_w10_sft_gate.py` | `63 passed in 0.26s` | 0 |
| C4 | `pytest tests/unit/test_baseline_manifest.py tests/unit/test_verification_perf.py` | `19 passed in 0.15s` | 0 |
| C5 | `pytest tests/unit/test_w10_metrics.py` | `21 passed in 0.19s` | 0 |
| C6 | `pytest tests/integration/test_w10_runner_replay.py` | `8 passed in 0.14s` | 0 |
| C7 | `timeout 180 pytest tests/integration/test_w10_oracle_timing.py` | `2 passed in 2.47s`（DB 非 skip） | 0 |
| C8 | `timeout 180 pytest tests/integration/test_w10_telemetry_evidence.py` | `2 passed in 1.11s`（DB 非 skip） | 0 |
| C9 | `timeout 180 pytest tests/integration/test_w09_redaction_pipeline.py` | `4 passed in 2.81s`（AC-21 既存，DB 非 skip） | 0 |
| C10 | `pytest tests/unit/test_w10_oracle_isolation.py tests/unit/test_w10_group_split.py` | `69 passed in 0.31s` | 0 |
| C13 | `pytest tests/unit/test_w10_run_manifest.py tests/unit/test_w10_bootstrap.py tests/unit/test_w10_telemetry_adapter.py tests/unit/test_w10_token_stats.py` | `93 passed in 0.76s` | 0 |
| C11（非 pytest） | `python -m core.benchmark plan --suite benchmarks/w10/suite_manifest.json --json` 两跑写仓库外临时文件 | 各 **5520 字节**；`cmp` = **BYTE_IDENTICAL**；sha256 `89a8ec7021497b0c74505b09abf7cd514b8a3a13a866b0e587e66ec810573e26`（两跑相同） | 0 / 0 |
| C12（非 pytest） | `baseline_report.json` 经 `BaselineReport.model_validate` + `assert_no_fabricated_metrics` | 两者均 OK；`status=BLOCKED`、`model_commit=None`、`metrics=None`、`tokenizer_commit=None`、`total_cases=0`、`attr_sum=0`、`denoms=14 all_zero=True`、`env_codes=['ENV-MODEL']`、`blocked_reason` 非空 | 0 |
| EP（非 pytest） | 独立错误路径脚本 `D:/qa_w10_qa_tmp/qa_w10_ep.py` | `total=51 passed=51 failed=0` | 0 |

**复现性结论**：`pytest tests/unit` 全量 1582、T06 两文件 63、AC-20 既存 11、AC-21 既存 4、3 个 W10 集成 2/8/2、不变量 19、CORR-01 文件 21、plan 双跑字节与 sha256，均与工程师登记数字一致。C1 首次 139 事件如实登记于 §七 L-3，不冒充一次通过、不判 FAIL。

---

## 七、遗留与已知问题清单

| 编号 | 类别 | 事实 | 处置口径 |
|---|---|---|---|
| **L-1** | W09 存量告警 | `git diff --check` **exit 2**，唯一告警 `docs/w09/06-qa-report.md:624: new blank line at EOF.`（W09 存量 `M` 文件）。另有 10 条 `LF will be replaced by CRLF` 提示（git 行尾提示，非 diff 错误） | 按纪律**未动**该文件；并入 W09 收口件；不判 FAIL |
| **L-2** | 设计文档事实偏差 | `docs/w10/02-design.md` §10.3 称 `UNKNOWN` 会被 `EvidenceService.create` 拒；实测 `core/evidence/service.py:23-29` `_DEGRADED_STATUSES = frozenset({DENIED, TIMEOUT, NOT_SUPPORTED})` **不含 `UNKNOWN`**（`-DEGRADED_STATUSES` 仅命中 `service.py:23/103`） | 登记为**设计文档事实偏差**，**未改生产代码**；W11 交接须引用实测闭集；不判 FAIL |
| **L-3** | 环境 / 解释器层事件 | C1 `pytest tests/unit` 在批次 1（与另 4 条 pytest 同 shell 串行、C1 为首条）收集阶段触发 `Windows fatal exception: access violation` / exit 139；堆栈自 `_pytest/assertion/rewrite.py` 的 `traverse_node` 递归 → `ast.py:506 visit`，**未触及任何 W10 生产代码**；单独隔离重跑同一命令 → 1582 passed / 0 failed / exit 0 | 属裁定台账 §5 已登记的 CPython 3.14.7 / pytest assertion-rewrite 不稳定族的一次观测；如实登记首次失败与隔离重跑结果，**不判 FAIL、不修环境、不冒充一次通过** |
| **L-4** | 口径事实登记 | 仓库内 `find -name '*.jsonl'` **零命中**；`cases.jsonl` 是 `BenchmarkRunner._write_artifacts`（`core/benchmark/runner.py:680`）的**运行期产物**，非静态文件 | 已由 C6 用例 + EP-6E 独立复现（3 行、零真值、公开 `fault_seed` 保留）；非缺陷，仅登记载体性质 |
| **L-5** | 口径事实登记 | `core/benchmark/oracle.py:48` `ORACLE_FORBIDDEN_FIELDS = ("expected_root_cause", "fault_seed")`；`scan_texts_for_oracle_leak` 对 `suite_manifest.json`（6 个 `fault_seed`）与 `metrics-v1.json`（notes 聚类口径）命中 **`fault_seed`** | `suite_manifest.json:65` 明写「逐字取自 `benchmarks/faults/manifests/*.json` 的**公开 data_seed**；Oracle 真值不在此清单内」，且 W10-Q5 Bootstrap 需按 `(engine, scenario, fault_seed)` 聚类 → 属**冻结评测清单 / 口径文档内的公开复现参数**。泄漏红线约束的是训练样本文本 / 报告文本 / `FailureCase` / 审计 payload / 运行产物，**不含冻结评测清单本身**。四个真值项（`expected_root_cause`/`injection_params`/`truth_assertions` + SQL 原文 / 参数值）在 5 个 JSON、`baseline_report.json`、`FailureCase` 输出、运行期 `cases.jsonl` 中**零命中**。非缺陷，精确登记边界 |
| **L-6** | 环境阻塞 | 五项 `ENV-*` 全部未解，详见 §八 | 相关用例 `NOT_EXECUTED` / `NOT_VERIFIED`；**不判 FAIL、不冒充通过、不修环境** |

**已知问题数**：实质遗留 **3 项**（L-1 / L-2 / L-3）+ 口径事实登记 **2 项**（L-4 / L-5）+ 环境阻塞 **5 项**（L-6 / §八）。**无 W10 生产代码缺陷，无测试代码缺陷**。

---

## 八、五项 `ENV-*` 环境阻塞登记（C-2 / §6 编号约定）

| 编号 | 事实 | 对 W10 的影响 | 本次 QA 处置 |
|---|---|---|---|
| `ENV-PG5432` | PostgreSQL LAB 5432 拒绝连接 | 所有 PG case 不可执行 | C6 `test_pg_cases_are_env_blocked_and_never_judged_as_failure` 通过；PG case 记 `NOT_EXECUTED` + 归 `ENVIRONMENT_FAULT`，不计入模型失败；**不判 FAIL、不修环境** |
| `ENV-1045` | `DG_ADMIN_DSN` root@localhost 认证失败（1045） | 测试清理不可用 | C7 / C8 / C9 用外层 `timeout 180`，只按本任务 id 断言，**未依赖 admin**；不碰凭据、不放宽 `dg_ctl` |
| `ENV-RESIDUE` | 控制库未观测残留 **140 行**且随时间增长 | 集成用例连库继续累加 | **未 DELETE / UPDATE / TRUNCATE**；C7 / C8 / C9 均 0 failed 且非 skip；按 `ENV-RESIDUE` 登记，处置挂 W11 |
| `ENV-MODEL` | 模型 `BLOCKED_MODEL_ENV_MISSING`（`model_commit=null`） | 无法推理 → 模型侧指标未测得 | C12 自检：`status=BLOCKED`、`metrics/ci/cost/tokenizer` 全 `None`、14 分母如实 0、`blocked_reason` 非空、`environment_blocked` 含 `ENV-MODEL`；EP-7 独立核对；**不下载模型、不联网、不填估计值** |
| `ENV-NV1` | 容器运行态 `NOT_VERIFIED` | `env_image_digest` 不可从镜像取 | EP-7 实测 `RunManifest.from_baseline()` → `env_image_digest='UNKNOWN'` / `env_image_digest_source='UNKNOWN'`；**配置层已实测 / 运行态 `NOT_VERIFIED`**；不臆造 digest、不声称容器已验证 |

五项全部如实登记，相关用例 `NOT_EXECUTED` / `NOT_VERIFIED`，**不判 FAIL、不冒充通过、不修环境**。

---

## 九、上游口径与表述禁令自查（C-1 / C-7）

| 上游 / 条目 | 固定口径（本报告采用） |
|---|---|
| W07 | **「有条件放行；门禁通过：否」**（容器：配置层已实测 / 运行态 `NOT_VERIFIED`） |
| W08 | **「有条件放行」** |
| W09 | **「T01–T06 + L1/L2 已实现并通过定向验收，W09 门禁未评」** |
| W10 门禁 | **未评**；本报告只写「定向验收通过」，不替主理人签门禁 |
| 开工 / 上游签署 | 不宣布「W10 开工」、不写「上游已通过」、不以本周证据替代上游门禁证据 |

| C-7 禁用表述 | 自查 |
|---|---|
| 「W07 已收口 / 门禁通过 / 放行」作为正面结论 | 未出现。文中「有条件放行」及「门禁通过：否」**仅作为 W07 / W08 上游固定口径的逐字引用**，且带否定后缀，非 W10 结论 |
| 「容器网络隔离已验证」 | 未出现；统一写「配置层已实测 / 运行态 `NOT_VERIFIED`」 |
| 「环境已清理 / 已可复现清零」 | 未出现；统一写「五项 `ENV-*` 未解、不修环境」 |
| 「QA 独立实跑通过」 | 未出现；结论只写「定向验收通过」（任务授权措辞） |
| 「回归全绿」 | 未出现；C1 首次 exit 139 与隔离重跑均如实登记 |
| 「Alpha 已就绪」 | 未出现；SFT / DPO / RL / 14B 维持「推迟到 Alpha 之后」的记录口径 |

---

## 十、纪律自查

- [x] 解释器 `.venv/Scripts/python.exe`（3.14.7）；每条 pytest 带 `-o addopts="" -q -p no:cacheprovider`；**未执行无路径全量 pytest**；**未写 `--timeout=`**；需 DB 的用外层 `timeout 180`；**所有 pytest 严格串行、零并发**
- [x] 未执行 `git commit` / `add` / `checkout` / `reset`
- [x] 未碰 `.env` / `DG_ADMIN_DSN` / `dg_ctl`；未 DELETE / UPDATE / TRUNCATE 历史残留
- [x] 未新增依赖 / 迁移 / DDL / API / 前端
- [x] 未改冻结资产（`benchmarks/baseline_v0/**`、`benchmarks/faults/**`）、未改 W09 / W07 既有文件、未改 `docs/w09/06-qa-report.md:624`
- [x] **未修改 W10 生产代码与既有断言**；对 W10-CORR-01 改写只做独立验证（EP-5 五场景含双反证），未作任何改动，无需回报裁定
- [x] QA 临时验证脚本与 `plan` 双跑产物均放**仓库外** `D:/qa_w10_qa_tmp/`
- [x] 本报告为 `docs/w10/` 下**新增未跟踪文件**；`git diff --name-only` 仍为 19，未执行 `git add`
- [x] 最多 2 轮：第 1 轮完成，无源码 / 测试改动，未进入第 2 轮；2 轮硬上限未触及

---

## 十一、收口签署

**智能路由判定：`NoOne`** —— 独立错误路径 51/51 PASS、AC 21/21 已覆盖、`pytest tests/unit` 全量 1582 passed / 0 failed、定向子集与集成 0 failed、冻结边界与 CORR-02 事实复核一致、plan 双跑字节一致、真实 Baseline 报告自检通过；未发现源码 bug，未发现测试代码 bug。L-1 / L-2 / L-3 与五项 `ENV-*` 如实登记，不判 FAIL、不冒充通过。

**W10 定向验收：通过。W10 门禁：未评（由主理人裁定）。**

> 产出人：**严过关（Yan）｜QA 工程师**
> 团队：`software-dbguardian-w10-resume` ｜ 时点锚 `HEAD = 2850eca`，全部改动未提交
