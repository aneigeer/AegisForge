# W12 Alpha 验收报告（T05 / AC-17 / AC-18）

> 生成方式：`core/release/publish.py::build_alpha_acceptance_report()` + `write_release_documents()`；
> 报告版本 `w12-alpha-report-v1`；发布结论由 `derive_release_type()` 按 W11 发布决定派生（**不由人工覆盖**）。

## 0. 发布结论摘要

| 项 | 值 |
|---|---|
| `release_type` | `TECH_PREVIEW` |
| `release_name` | DBGuardian 技术预览（受限发布） |
| `effective_decision` | `TECH_PREVIEW_ONLY` |
| `source` | `W11_PUBLISH_DECISION` |
| `decision_digest` | `d22aff9f4c21f5401dcff62e091ecfb764a770a635b936c8a21877510fb29bd1` |
| 登记项 | 无 |

> 口径：本次发布为**受限发布**，功能面仍存在红线与未覆盖路径；报告正文逐章只写实测事实，不写结论性溢美表述。

## 功能验收

AC-01 五态生命周期：create=PASSED、query=PASSED、pause=PASSED、resume=PASSED、cancel=PASSED（每态 task_events 可查询、seq 连续、事件链校验通过）。AC-02 三类诊断报告五字段非空 3/3（PASSED；KD-W12-02 已于 2026-11-19 关闭：无 table_stats 证据的输入形状改为显式降级 UNKNOWN + 证据不足，并补真实形状回归 2 例）。AC-03 审批修复闭环：独立验证结论 PASS、闭环状态 PASSED（原因码 OK；KD-W12-01 已关闭：验证器改为优先读 StepObserved.result["data"]、回落顶层，真实形状可判定）。AC-04 第二引擎只读：写定义集合为空 （True）；PG 段 NOT_EXECUTED（ENV-PG5432：环境不可达，不判 FAIL）。

## 安全验收

AC-05 W11 负面集复核 29/29 条被拒（w11_source_status=PASSED）。AC-06 R4 未注册=True、R3 被拒=True。AC-07 四类绕过：mcp_param=True、model=True、sql_comment=True、ui=True。AC-08 UNKNOWN 不重试=True、审计失败 fail closed=True。AC-09 脱敏不越界=True、知识索引门禁=False。

## 可靠性

可靠性口径：不可逆/不确定路径一律转核对态，不自动重试。UNKNOWN 不重试实测=True（结论 NEEDS_RECONCILIATION、tool_calls 增量 0、无 TOOL_CALL_RETRIED）。审计不可写即阻断实测=True（emit_required 抛 AuditUnavailableError；R2 链不接 try_emit）。独立验证逐条断言（KD-W12-01 修复后实测）：affected_rows=PASS、snapshot_comparable=PASS、target_note_final=PASS；原登记（修复前，2026-11-16）：`affected_rows` 曾为 `UNKNOWN`（`OBSERVED_MISSING`）、`snapshot_comparable=FAIL`，该断言态已于 2026-11-17 关闭（`core/verification/plan.py` 读取端形状归一，未改写入端与断言常量；留档见 `docs/w12/known-limitations.md` §3.1）。

## 性能

性能：本轮未做压测与长稳实跑，故不提供任何时延/吞吐数值。部署五项检查状态分布 {'NOT_EXECUTED': 5}（container_runtime_status=NOT_VERIFIED）。容器运行态未验证（ENV-NV1），不据此推断性能上限。

## 模型收益

模型状态：未微调；指标状态：指标未测得；阻塞原因：BLOCKED_MODEL_ENV_MISSING。baseline_score=None（不填任何数值，含 0）。SFT 无可重复收益（sft_reproducible_gain=False）；DPO / 全量 14B / RL 均延期。

## 成本

成本：本轮未产生训练与推理账单，故不提供金额类数值。镜像 digest=UNKNOWN、制品 hash=bd4bc40fa08609a100232f21dcd437ae191808f2570806eccfc8ec15a2af8408、commit=1be436942bb7e74f328fc124fc905c4191572c0f（缺失项均记 UNKNOWN 并登记，不用估计值顶替）。

## 已知问题

KD-W12-01（已关闭，2026-11-17）：AC-03 独立验证结论已由 FAILED 转 PASS（闭环 PASSED；原因码 OK）。修复点：`core/verification/plan.py` 读取端形状归一（优先 `result["data"]`、回落顶层），未改写入端与断言常量；已补真实形状回归 5 例。本条已从 `docs/w12/known-limitations.json` 移除，改记入 `known-limitations.md` §3.1 已关闭偏离留档。 KD-W12-02（已关闭，2026-11-19）：慢查询分析器在「全表扫描 + 无 table_stats」输入下的 UnboundLocalError 已修（入口初始化 + 无证据时显式降级 UNKNOWN 并标注缺失证据），并补真实形状回归 2 例；本条已从 `docs/w12/known-limitations.json` 移除，改记入 `known-limitations.md` §3.1 已关闭偏离留档。 KD-W11-01/02（备份覆盖漏项 / 负集哨兵归属）：P2-T2 已关闭——备份对象扩到六类（含 approval_consumptions / step_prepared）并实测 restore 前后非空；负集 29 条改用真实目标行并断言基线非空。KD-W11-03（audit_chain 桶命名对齐）仍登记未改。 ENV-PG5432：PG 段 NOT_EXECUTED（不判 FAIL、不冒充通过）。 ENV-1045：DG_ADMIN_DSN 不可用，升级演练未执行。 ENV-NV1：容器运行态 NOT_VERIFIED，镜像 digest UNKNOWN。 ENV-MODEL：模型环境缺失，指标未测得。 DOC-PARTIAL：迁移说明与回滚清单已补至 011、.env.example 三态已补齐；W07 冻结四件套的两件补件形成过程未经独立复核，仍不声称齐全。 部署登记项：W12-Q2/ENV-NV1：容器运行态未验证，镜像 digest=UNKNOWN；ENV-NV1：container_runtime_status=NOT_VERIFIED（不作硬门槛）

## 延期项

延期项：DPO、全量 14B、RL 三项均未实施（恒为延期）；性能压测与长稳未实跑；容器网络级隔离未在运行态验证；W11 publish_decision 的具体值不由 W12 预写。

## 证据引用

| 路径 |
|---|
| `core/release/__init__.py` |
| `core/release/functional.py` |
| `tests/integration/test_w12_functional_acceptance.py` |
| `core/audit/service.py` |
| `core/evidence/models.py` |
| `adapters/mysql/lab_repair.py` |
| `docs/w09/12-w09-qa-closeout.md` |
| `docs/w10/04-w10-qa-closeout.md` |
| `docs/w11/05-w11-qa-closeout.md` |
| `core/release/security.py` |
| `tests/integration/test_w12_security_acceptance.py` |
| `tests/integration/test_w09_redaction_pipeline.py` |
| `tests/integration/test_w11_security_negative.py` |
| `core/security/policy.py` |
| `adapters/postgres_mcp/tools.py` |
| `docs/w09/13-l1-l2-design.md` |
| `docs/w10/02-design.md` |
| `docs/w11/02-design.md` |
| `benchmarks/w11/negative_cases.json` |

## 口径与边界

- `baseline_score = None`、`model = baseline`、`sft_reproducible_gain = False`；
- `dpo_deferred` / `full_14b_deferred` / `rl_deferred` 恒为 `True`（`ClassVar`，不可覆盖）；
- 演示件（`docs/w12/demo-script.md`）**不进入**本报告的判定输入（AC-20）；
- 限制清单随包发布：`docs/w12/known-limitations.json`（唯一真值）。
