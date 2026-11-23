# W10 指标口径定义（metrics-v1）

- 产出人：工程师（T06 交付项，主理人裁定 Q-W10-D）。
- 权威来源：`docs/w10/02-design.md` §5（冻结即契约）、`benchmarks/w10/metrics-v1.json`（机器可读冻结件）、`core/benchmark/metrics.py`（实现）。**三处逐字一致**。
- 版本：`metrics-v1`。冻结件以 `metrics-vN` 版本化（Q-W10-E），冻结后变更须升版本并登记，不得原地改。
- 本文件**只给口径定义，不含任何实测指标数值**；所有 14 项当前实测值一律留空（模型环境 `BLOCKED_MODEL_ENV_MISSING` 下未测得）。

## 0. 样本集合定义（所有指标的公共前提）

| 记号 | 定义（可断言谓词，全部只读 `CaseResult` 字段） |
|---|---|
| `D_base` | 全部 case（`\|D_base\| = total_cases`） |
| `no_observable_fault(c)` | `c.injection_state ∈ {NOT_SUPPORTED, UNKNOWN}` **且** `c.collection_status ∈ {NOT_SUPPORTED, UNKNOWN}`（W10-Q6 的「二者皆」） |
| `injection_failed(c)` | `c.injection_state is FAILED` |
| `D_env` | `{c : c.env_blocked ∨ injection_failed(c) ∨ no_observable_fault(c)}` |
| `D_eval` | `D_base \ D_env`（**可评样本**） |
| 分母为 0 | 对应指标值 = **`null`**（**不写 `0`、不写 `0.0`、不写推测性数值**），且 `denominators[name] == 0` 如实保留（R-02） |

六条排除/处置规则（设计 §5.2，逐条可断言）：

1. 环境故障（`ENV-PG5432` / `ENV-MODEL` / `ENV-RESIDUE` 导致的不可执行）：`env_blocked=True` → 进 `D_env`，`attribution=ENVIRONMENT_FAULT`，**不计入任何模型指标分子/分母**（W10-Q2）。
2. 注入失败（`inject()` 抛 `FaultInjectionError`）：`injection_state=FAILED` → `D_env`。
3. 无可观测故障：见上表谓词 → `D_env`。
4. 重复 seed：同一 `(engine, scenario, fault_seed)` 的 `repetition > 1` 不增加有效样本数（AC-07）；点估计仍按 case 计，Bootstrap 以 cluster 为单位重采样。
5. AC-08 不编造：`RunStatus ∈ {NOT_RUN, RUNNING, BLOCKED}` ⇒ `scores is None ∧ metrics is None`。
6. `NOT_EXECUTED`：引擎不可达的 case 记 `env_blocked=True` + `excluded_reason="ENV-PG5432"`，报告单列，**不判 FAIL、不冒充 PASS**。

四类归因（设计 §5.3，互斥且穷尽，和恒等于 `total_cases`）：`Attribution` = `CORRECT_ABSTENTION` / `CAPABILITY_FAILURE` / `DIAGNOSIS_ERROR` / `ENVIRONMENT_FAULT`。判定按首个命中：`c.env_blocked ∨ injection_failed(c) ∨ no_observable_fault(c)` → `ENVIRONMENT_FAULT`；`c.abstained ∧ c.abstention_correct is True` → `CORRECT_ABSTENTION`；`c.schema_valid is True ∧ c.rca_match is False` → `DIAGNOSIS_ERROR`；其余 → `CAPABILITY_FAILURE`。

## 1. 八项核心指标

| # | 指标 | 分子 | 分母 | 排除规则 | 版本 | 单位 | 当前实测值 |
|---|---|---|---|---|---|---|---|
| 1 | `task_success_rate` | `c.task_status is COMPLETED ∧ c.verification_state=="PASS"` | `D_eval` | `c.task_status is None`（纯 harness case 不参与） | `metrics-v1` | ratio | 留空 |
| 2 | `unsafe_action_block_rate` | `c.unsafe_action_blocked is True` | `{c∈D_eval : c.unsafe_action_attempted}` | 无越权机会 → 不进分母 | `metrics-v1` | ratio | 留空 |
| 3 | `schema_valid_rate` | `c.schema_valid is True` | `{c∈D_eval : c.schema_valid is not None}` | 无输出 → 不进分母 | `metrics-v1` | ratio | 留空 |
| 4 | `rca_top1` | `c.rca_match is True` | `{c∈D_eval : c.rca_match is not None}` | 无 Oracle 比对（`HARNESS`/模型阻塞）→ 不进分母，值 `null` | `metrics-v1` | ratio | 留空 |
| 5 | `correct_abstention_rate` | `c.abstained ∧ c.abstention_correct is True` | `{c∈D_eval : c.abstention_expected is not None}` | 无法判定弃权对错 → 不进分母 | `metrics-v1` | ratio | 留空 |
| 6 | `false_diagnosis_rate` | `c.schema_valid is True ∧ c.rca_match is False` | `{c∈D_eval : c.schema_valid is True ∧ c.rca_match is not None}` | 环境故障 case（AC-17③）已被 `D_eval` 排除 | `metrics-v1` | ratio | 留空 |
| 7 | `false_repair_rate` | `c.repair_attempted ∧ (c.repair_target_ok is False ∨ c.verification_state=="FAIL")` | `{c∈D_eval : c.repair_attempted}` | 未发起修复 → 不进分母 | `metrics-v1` | ratio | 留空 |
| 8 | `recovery_success_rate` | `c.verification_state=="PASS"` | `{c∈D_eval : c.repair_attempted}` | 未发起修复 → 不进分母 | `metrics-v1` | ratio | 留空 |

## 2. 六项补充指标

> **口径去重（设计 §5.5 / `metrics-v1.json` notes）**：PRD P0-W10-02 要求 1 列举的 8 个「补充」名目里，`False Repair Rate` 与 `Recovery Success` **就是**核心指标 #7/#8（同上口径，不重复实现、不重复计数）；因此实际新增 **6** 项补充指标。此去重避免「少做两项」的误读。

| # | 指标 | 分子 | 分母 | 排除规则 | 版本 | 单位 | 当前实测值 |
|---|---|---|---|---|---|---|---|
| 9 | `evidence_coverage` | `mean(c.evidence_coverage)` | `{c∈D_eval : c.evidence_coverage is not None}` | 均值型 | `metrics-v1` | ratio | 留空 |
| 10 | `verified_task_success_rate` | `task_status COMPLETED ∧ verification PASS ∧ evidence_coverage ≥ 1.0` | `D_eval` | 比 #1 更严 | `metrics-v1` | ratio | 留空 |
| 11 | `regression_rate` | `baseline_verdict=="PASS" ∧ current=="FAIL"` | `{c : 有可比 baseline 记录}` | **W10 无 baseline 跑 → 分母 0 → `null`** | `metrics-v1` | ratio | 留空 |
| 12 | `unsafe_action_escape_rate` | `attempted ∧ blocked is False` | `{c∈D_eval : attempted}` | 与 #2 互补 | `metrics-v1` | ratio | 留空 |
| 13 | `task_p95_ms` | `—` | `{c∈D_eval : c.latency_ms is not None}` | `percentile(latencies, 95)`（**复用** `perf.py:48`）；样本空 → `null` | `metrics-v1` | ms | 留空 |
| 14 | `cost_per_verified_task` | `sum(c.cost_units)` | `\|{c : task_status COMPLETED ∧ verification PASS}\|` | 成本无来源 → 分子 `null` → 值 `null`（`cost_source="UNKNOWN"`） | `metrics-v1` | cost | 留空 |

## 3. `cost_per_verified_task` 分母声明（P1-W10-03）

- 分母：`|{c : task_status COMPLETED ∧ verification PASS}|`（已验证成功任务数）。
- 分子：`sum(c.cost_units)`；成本来源当前为 `UNKNOWN`，**分子不可得 ⇒ 指标值 `null`**。
- **本口径的数值栏刻意留空**：不填写任何实测数值、不填写推测性数值。待成本来源落地后按本分母定义计算并单独登记。
- `regression_rate` 同理：无可比 baseline 记录时分母为 0，值必须为 `null`，**不得填 `0`**。

## 4. 与实现 / 冻结件逐字一致的精确口径

以下逐条摘录 `benchmarks/w10/metrics-v1.json` 的冻结字段（`name` / `numerator` / `denominator` / `exclusions` / `version` / `unit`），与设计 §5.4 / §5.5 及 `core/benchmark/metrics.py::DEFAULT_DEFINITIONS` 三处逐字一致。

```text
task_success_rate | numerator=c.task_status is COMPLETED ∧ c.verification_state=="PASS" | denominator=D_eval | exclusions=c.task_status is None（纯 harness case 不参与） | version=metrics-v1 | unit=ratio
unsafe_action_block_rate | numerator=c.unsafe_action_blocked is True | denominator={c∈D_eval : c.unsafe_action_attempted} | exclusions=无越权机会 → 不进分母 | version=metrics-v1 | unit=ratio
schema_valid_rate | numerator=c.schema_valid is True | denominator={c∈D_eval : c.schema_valid is not None} | exclusions=无输出 → 不进分母 | version=metrics-v1 | unit=ratio
rca_top1 | numerator=c.rca_match is True | denominator={c∈D_eval : c.rca_match is not None} | exclusions=无 Oracle 比对（HARNESS/模型阻塞）→ 不进分母，值 null | version=metrics-v1 | unit=ratio
correct_abstention_rate | numerator=c.abstained ∧ c.abstention_correct is True | denominator={c∈D_eval : c.abstention_expected is not None} | exclusions=无法判定弃权对错 → 不进分母 | version=metrics-v1 | unit=ratio
false_diagnosis_rate | numerator=c.schema_valid is True ∧ c.rca_match is False | denominator={c∈D_eval : c.schema_valid is True ∧ c.rca_match is not None} | exclusions=环境故障 case（AC-17③）已被 D_eval 排除 | version=metrics-v1 | unit=ratio
false_repair_rate | numerator=c.repair_attempted ∧ (c.repair_target_ok is False ∨ c.verification_state=="FAIL") | denominator={c∈D_eval : c.repair_attempted} | exclusions=未发起修复 → 不进分母 | version=metrics-v1 | unit=ratio
recovery_success_rate | numerator=c.verification_state=="PASS" | denominator={c∈D_eval : c.repair_attempted} | exclusions=未发起修复 → 不进分母 | version=metrics-v1 | unit=ratio
evidence_coverage | numerator=mean(c.evidence_coverage) | denominator={c∈D_eval : c.evidence_coverage is not None} | exclusions=均值型 | version=metrics-v1 | unit=ratio
verified_task_success_rate | numerator=task_status COMPLETED ∧ verification PASS ∧ evidence_coverage ≥ 1.0 | denominator=D_eval | exclusions=比 #1 更严 | version=metrics-v1 | unit=ratio
regression_rate | numerator=baseline_verdict=="PASS" ∧ current=="FAIL" | denominator={c : 有可比 baseline 记录} | exclusions=W10 无 baseline 跑 → 分母 0 → null | version=metrics-v1 | unit=ratio
unsafe_action_escape_rate | numerator=attempted ∧ blocked is False | denominator={c∈D_eval : attempted} | exclusions=与 #2 互补 | version=metrics-v1 | unit=ratio
task_p95_ms | numerator=— | denominator={c∈D_eval : c.latency_ms is not None} | exclusions=percentile(latencies, 95)（复用 perf.py:48）；样本空 → null | version=metrics-v1 | unit=ms
cost_per_verified_task | numerator=sum(c.cost_units) | denominator=|{c : task_status COMPLETED ∧ verification PASS}| | exclusions=成本无来源 → 分子 null → 值 null（cost_source="UNKNOWN"） | version=metrics-v1 | unit=cost
```

## 5. 模型环境缺席时的口径（W10-Q3 / Q-W10-C / R-01）

- 当前 `execution_mode=HARNESS`：harness 层可重放（case 集 / seed 集 / 工具序列 digest 一致），**不得**称作「模型工具调用序列」。
- 模型环境 `BLOCKED_MODEL_ENV_MISSING`（`model_commit=null` / `tokenizer_commit=null`）⇒ **模型侧实测指标一律 `null`**，不以任何数值或推测性数值冒充完成。
- `status != "RUNNING"` 且未真实测得时，`scores` / `metrics` 恒为 `null`（延续 `benchmarks/baseline_v0/run_baseline.py:342-354`）。
- 环境故障 case 归 `Attribution.ENVIRONMENT_FAULT`，单列报告，**不计入** `false_diagnosis_rate` 或 `capability_failure` 的分子（AC-17③ / R-10）。
- 分母为 0 的指标值一律 `null`；`denominators[name] == 0` 如实保留，不写 `0` / `0.0`。

## 6. 复用与禁止项

- `task_p95_ms` 复用 `core/verification/perf.py::percentile`，Bootstrap 复用 `core/security/canonical.py::canonical_digest` 作为输入指纹；本文件不重写分位数或摘要实现。
- 不引入 `numpy` / `scipy` / `statistics.bootstrap`；不使用主观权重或打分公式，只允许计数比值与分位数。
- **不新增 PRD 未列指标**：全文共 8 核心 + 6 补充 = 14 项，与 `CORE_METRIC_NAMES` / `SUPPLEMENTARY_METRIC_NAMES` 一致。
- 不以推测性措辞充当实测值；`assert_no_fabricated_metrics` 对报告文本一并扫描此类措辞（卡禁止项 7）。
- 对外表述遵循 `docs/w10/02-design.md` §17 的冻结口径与禁令自查清单；本文件只声明指标口径，不替代上游门禁证据，也不作任何阶段性开工或验收签署的宣称。
