# W12 数据与 Benchmark 基线口径（T04 / AC-14）

> 本文件是 **W12 对外材料**：只记录**实测到的状态**，**不**包装未执行的基线。
> 口径：`docs/w12/02-design.md` §9.5、`docs/w12/03-task-cards.md` T04、
> `docs/w10-w12-lead-rulings.md` §4（W12-Q5：`baseline_score` 恒 `None`）。
>
> **红线**：`benchmarks/baseline_v0/results.json` 实测 `status=NOT_RUN`、`scores=null`、
> `metrics=null`。因此本文件**不填任何数值（含 0）**，也不写"估计/预计/约"之类字样。

## 1. 基线状态（实测）

| 项 | 实测值 |
|---|---|
| `results.json.results_version` | `baseline-results-v0` |
| `results.json.status` | **`NOT_RUN`** |
| `results.json.scores` | **`null`** |
| `results.json.metrics` | **`null`** |
| `results.json.run_id` / `started_at` / `finished_at` | `null` / `null` / `null` |
| `results.json.blocked_reason` | 模型权重与云端 GPU 未提供；按范围调整不做训练 |
| `baseline_score`（W12 清单字段） | **`None`**（`core/release/docs.py::DocInventory` 校验器强制） |

**结论口径（三点必须同时出现）**

1. 模型状态：**未微调**；
2. 指标状态：**指标未测得**；
3. 阻塞原因：**`BLOCKED_MODEL_ENV_MISSING`**（模型运行环境缺失）。

`DocInventory` 的三个冻结字段 `baseline_model_status="未微调"`、
`baseline_metrics_status="指标未测得"`、`baseline_blocked_reason="BLOCKED_MODEL_ENV_MISSING"`
在构造期强制；`baseline_score` 一旦被填成任何数值（含 `0`）即构造失败。

## 2. 冻结件与版本权威

| 主题 | 冻结件（只读） |
|---|---|
| 基线清单与版本字段 | `benchmarks/baseline_v0/baseline_manifest.json` |
| 基线结果占位 | `benchmarks/baseline_v0/results.json`（`NOT_RUN`） |
| 指标定义 | `benchmarks/w10/metrics-v1.json`、`docs/w10/metrics-definition.md` |
| 评测集版本 | `baseline_manifest.json` 的 `testset_version`（实测 `testset-v0-preview`） |
| 负面集（W11） | `benchmarks/w11/negative_cases.json`（29 条）、`benchmarks/w11/rc_manifest.json` |
| 故障场景 | `benchmarks/faults/manifests/**`（六场景） |

- 版本权威：`git rev-parse HEAD`（唯一动态值）+ 迁移文件枚举 + 上述冻结清单；
- **本轮未修改**任何 `benchmarks/**` 冻结资产。

## 3. SFT / DPO / RL 口径（不包装）

| 项 | 状态 |
|---|---|
| SFT 复现增益 | `sft_reproducible_gain = False`（无微调、无对比实验） |
| DPO | **延后**（未实施） |
| 全量 14B | **延后**（未实施） |
| RL | **延后**（未实施） |

- 上述状态写入 T05 的 `AlphaAcceptanceReport`（`model="baseline"`），W12 **不**宣称任何训练收益；
- 与 W10 口径一致：`Oracle` 隔离与数据切分是**设计约束**，其可执行证据见
  `docs/w10/02-design.md` 与 `benchmarks/w10/metrics-v1.json`，不代表基线已产出成绩。

## 4. 演示与诊断的口径分离

- 诊断演示（`docs/w12/demo-script.md`）只证明"可复现地跑一遍"，**不产出**任何指标；
- 诊断结论必须绑定证据与适用范围（`core/diagnosis/models.py::Hypothesis` 五项），
  `UNKNOWN` / `PARTIAL` 一律如实保留，**不**升级为"已定位根因"。

## 5. 与限制清单的关系

`docs/w12/known-limitations.json` 中与本文件直接相关的自动项：

| item_id | marker | 含义 |
|---|---|---|
| `ENV-MODEL` | `UNKNOWN` | 模型环境缺失（`BLOCKED_MODEL_ENV_MISSING`），指标未测得 |
| `DECISION-TECH-PREVIEW` | `PARTIAL` | 发布结论为 `TECH_PREVIEW_ONLY`，存在未清环境阻塞或红线失败 |
| `NP-EXACTLY-ONCE` | `NOT_SUPPORTED` | 跨系统 Exactly Once 不在交付范围 |

> 说明：以上三项均为**自动基础项**，不可人工删除或改写；人工追加的已知偏离见
> `docs/w12/known-limitations.md` 第 3 节。
