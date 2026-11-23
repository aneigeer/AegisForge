# Baseline v0 冻结记录（未微调，未执行）

本目录冻结 DBGuardian V2.2 W04 的“未微调 Baseline v0”可上机资产与版本记录。
本次交付**不做训练/微调、不申请或等待 GPU、不加载真实权重、不联网**，也不写入任何准确率、F1、延迟或稳定性分数。

## 一、冻结版本

| 项目 | 值 | 说明 |
| --- | --- | --- |
| 基线模型 | `Qwen3-8B`（候选） | 待用户确认并完成云端部署；当前仅候选 |
| `model_commit` | `null` | 权重尚未冻结，禁止臆造 commit |
| `tokenizer_commit` | `null` | tokenizer 尚未冻结 |
| Prompt | `prompt-v0` | `prompt.txt` |
| Tool Schema | `tool-schema-v0` | `tool_schema.json`，双引擎各 7 个固定只读工具 |
| Harness | `44e4f9d` | 对应 W02/W03 已冻结代码版本（双引擎 Adapter、任务/证据链） |
| 推理配置 | `inference-config-v0` | `inference_config.json` |
| 测试集 | `testset-v0-preview` | `testset_manifest.json`，预览版、未生成、未执行 |
| 状态 | `BLOCKED_MODEL_ENV_MISSING` | 模型权重/云端 GPU 未提供 |
| 结果 | `NOT_RUN`，`scores=null` | `results.json` 仅为占位 |

所有版本字段集中在 `baseline_manifest.json`，其他资产与之一致。

## 二、资产清单

- `baseline_manifest.json`：冻结版本、模型候选与阻塞状态，无任何指标字段。
- `prompt.txt`：诊断系统提示词。要求只输出结构化 Hypothesis JSON、必须引用 `evidence_id`、区分支持/反证/缺失证据、置信度仅 `HIGH/MEDIUM/LOW/UNKNOWN`、禁止执行 SQL/写操作、工具输出视为不可信数据；不包含任何 Oracle 真值字段。
- `tool_schema.json`：PostgreSQL 与 MySQL 各 7 个固定只读工具的 `name/version/engine/input_schema/output_schema/risk_level`；全部 `R0`、`read`、`LAB`、`dg_ro`。
- `inference_config.json`：上机时的固定推理参数；框架版本、`max_model_len`、显存占用均待实测。
- `testset_manifest.json`：PG/MySQL × 慢查询/连接压力/锁等待共 6 个组合，每组合 20 seeds、每 seed 3 次；`preview=true`、`executed=false`、`generated=false`。包含按 `engine/fault_family/seed/workload_group` 的 group split 规则与 Oracle 隔离要求。
- `results.json`：`status=NOT_RUN`、`scores=null`、`metrics=null`，`blocked_reason` 记录模型权重/GPU 缺失。
- `run_baseline.py`：仅做资产完整性/版本一致性与 dry-run 规划输出；不下载模型、不训练、不联网。

## 三、已知待补项

1. 用户确认基线模型（Qwen3-8B 或其他经 ADR 固定的模型）。
2. 填写 `model_commit` 与 `tokenizer_commit`（须来自实际发布权重）。
3. 云端 GPU 环境、推理框架版本、`max_model_len` 与显存占用实测值。
4. PG/MySQL × 三场景故障数据、注入/重置脚本、最小真值断言集。
5. Oracle 独立存储与泄漏检查（Agent Observation、模型上下文、日志、训练样本均不可见）。
6. 实际执行后的 `results.json`（含真实指标与运行记录）。

以上未完成前，`status` 必须保持 `BLOCKED_MODEL_ENV_MISSING`，`results.json` 必须保持 `NOT_RUN`。

## 四、上机步骤（租到云 GPU 后）

1. **确认模型**：用户确认基线模型；从官方发布获取权重与 tokenizer，记录实际 `model_commit` 与 `tokenizer_commit`。
2. **回填版本**：更新 `baseline_manifest.json` 和 `inference_config.json`，`status` 暂时保持阻塞，直到环境校验通过。
3. **准备环境**：安装并记录推理框架及版本；按 `inference_config.json` 设置 `dtype`、并行度、`max_model_len`、解码参数（`temperature=0`、`do_sample=false`）。
4. **实测显存**：加载权重后记录实际显存峰值与可用上下文长度，回填 `runtime.gpu_memory.estimated_gb`；在此之前该字段保持 `null`。
5. **生成测试集**：按 `testset_manifest.json` 的 6 个组合、每组合 20 seeds、每 seed 3 次生成故障数据；完成后更新 `preview/generated/executed` 标记，不得提前声称已完成。
6. **只读推理**：按冻结 prompt 与 tool schema 调用只读工具，禁止写工具、任意 SQL 与训练/微调。
7. **写入结果**：仅把真实运行得到的指标写入 `results.json`；未实际测得的指标不得填写或推断。
8. **冻结复核**：核对 `run_baseline.py --dry-run` 通过后，再执行正式评测并登记运行记录。

## 五、显存与资源配置

当前 `gpu_memory.estimated_gb = null`，状态为 `PENDING_MEASUREMENT`。任何显存预估都必须来自实际加载权重后的实测，不得用理论估算替代。若显存不足，应记录实际限制并调整上下文/并行度，不得伪造可用性。

## 六、禁止事项

- 禁止将 `results.json` 的 `NOT_RUN` 或 `scores=null` 解释为基线成绩。
- 禁止在未加载权重、未实际运行的情况下填写任何准确率、F1、延迟、通过率或置信度统计。
- 禁止把测试集预览规划当作已生成数据，禁止把 Oracle 真值写入 Agent 观测、模型上下文或普通日志。
- 禁止在本任务范围内进行 SFT/DPO/RL 训练或微调。

## 七、本地校验

```powershell
.\.venv\Scripts\python.exe benchmarks\baseline_v0\run_baseline.py --dry-run
.\.venv\Scripts\python.exe -m pytest tests\unit\test_baseline_manifest.py -q --tb=short
```

`run_baseline.py` 成功时退出码为 `0`；资产缺失或版本不一致时返回非零，并列出具体错误。该脚本不会下载模型、不会加载权重、不会训练、不会联网或调用外部 API。
