# W12 发布说明（Release Notes）

## 版本：DBGuardian 技术预览（受限发布）

| 项 | 值 |
|---|---|
| `release_type` | `TECH_PREVIEW` |
| `release_name` | DBGuardian 技术预览（受限发布） |
| `effective_decision` | `TECH_PREVIEW_ONLY` |
| `source` | `W11_PUBLISH_DECISION` |
| `decision_digest` | `d22aff9f4c21f5401dcff62e091ecfb764a770a635b936c8a21877510fb29bd1` |
| 报告版本 | `w12-alpha-report-v1` |

## 发布范围

- 只读诊断、证据留存、单引擎审批修复（唯一 R2 动作）、独立验证与核对链；
- 第二引擎（PostgreSQL/MCP）**只读**；生产写入不在范围内；
- 演示只走隔离 LAB，无 Web 控制台。

## 已知限制（随包发布）

- 完整限制清单：`docs/w12/known-limitations.json`（13 个自动基础项不可删 + 3 条在册已知偏离；已关闭偏离见 `known-limitations.md` §3.1）；
- 本次登记项：无；
- 关键已知问题：KD-W12-01（已关闭，2026-11-17）：AC-03 独立验证结论已由 FAILED 转 PASS（闭环 PASSED；原因码 OK）。修复点：`core/verification/plan.py` 读取端形状归一（优先 `result["data"]`、回落顶层），未改写入端与断言常量；已补真实形状回归 5 例。本条已从 `docs/w12/known-limitations.json` 移除，改记入 `known-limitations.md` §3.1 已关闭偏离留档。 KD-W12-02（已关闭，2026-11-19）：慢查询分析器在「全表扫描 + 无 table_stats」输入下的 UnboundLocalError 已修（入口初始化 + 无证据时显式降级 UNKNOWN 并标注缺失证据），并补真实形状回归 2 例；本条已从 `docs/w12/known-limitations.json` 移除，改记入 `known-limitations.md` §3.1 已关闭偏离留档。 KD-W11-01/02（备份覆盖漏项 / 负集哨兵归属）：P2-T2 已关闭——备份对象扩到六类（含 approval_consumptions / step_prepared）并实测 restore 前后非空；负集 29 条改用真实目标行并断言基线非空。KD-W11-03（audit_chain 桶命名对齐）仍登记未改。 ENV-PG5432：PG 段 NOT_EXECUTED（不判 FAIL、不冒充通过）。 ENV-1045：DG_ADMIN_DSN 不可用，升级演练未执行。 ENV-NV1：容器运行态 NOT_VERIFIED，镜像 digest UNKNOWN。 ENV-MODEL：模型环境缺失，指标未测得。 DOC-PARTIAL：迁移说明与回滚清单已补至 011、.env.example 三态已补齐；W07 冻结四件套的两件补件形成过程未经独立复核，仍不声称齐全。 部署登记项：W12-Q2/ENV-NV1：容器运行态未验证，镜像 digest=UNKNOWN；ENV-NV1：container_runtime_status=NOT_VERIFIED（不作硬门槛）

## 发布门禁口径

- `release_type` 只由 W11 发布决定派生：`decision is None` 或 `TECH_PREVIEW_ONLY` 均产出 `TECH_PREVIEW`；签名内不存在任何覆盖形参；
- 训练收益口径：未微调、指标未测得（`BLOCKED_MODEL_ENV_MISSING`），不宣称收益；
- 本文件不改变 W07「有条件放行；门禁通过：否」、W08「有条件放行」、W09/W10/W11「门禁未评」的既有口径。
