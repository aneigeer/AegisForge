# Baseline v0 资产目录

本目录冻结 DBGuardian V2.2 W04 的未微调 Baseline v0 可上机资产。
当前 `status=BLOCKED_MODEL_ENV_MISSING`：无模型权重、无云端 GPU，未训练、未推理、未写入任何指标。

| 文件 | 用途 |
| --- | --- |
| `baseline_manifest.json` | 模型候选、版本冻结与阻塞状态 |
| `prompt.txt` | 诊断系统提示词（结构化 Hypothesis JSON、evidence_id、无 Oracle 真值字段） |
| `tool_schema.json` | PostgreSQL/MySQL 各 7 个固定只读工具 Schema |
| `inference_config.json` | 上机推理参数（显存与框架版本待实测） |
| `testset_manifest.json` | 6 组合测试集预览规划（preview=true、executed=false） |
| `results.json` | 占位：status=NOT_RUN、scores=null |
| `run_baseline.py` | 资产校验与 dry-run 规划，不加载权重、不联网、不训练 |

完整说明与上机步骤见 `docs/baseline_v0.md`。

```powershell
.\.venv\Scripts\python.exe benchmarks\baseline_v0\run_baseline.py --dry-run
```
