# W12 已知限制清单（T04 / AC-15 / AC-21）

> 本文件由 `core/release/docs.py::write_known_limitations()` 生成：
> **`docs/w12/known-limitations.json` 是唯一真值**，下列 JSON 块与本文件正文由**同一函数**序列化，
> 因此 `items_digest` / `w11_source_digest` 必然可对账（W11 Q-W11-D 同款）。
> 自动基础项（13 项）**不可人工删除或改写**；人工项只能追加并须写明 `added_by` / `reason`。

## 1. 版本与对账

- `version`：`w12-limitations-v1`
- `w11_source_status`：`PASSED`（`docs/w11/limitations.json` 只读同源对账）
- `w11_source_digest`：`fbc39d0b847e1e6513806cdf4e5861988315ebc9e4049c35f227f3ce238e4ba7`
- `items_digest`：`682a7cde9b8ecf4e2e6422fe4b0a3e077f9181773487b2216a3c96fd0de4cbf0`
- 自动项 13 / 人工项 3 / 合计 16

## 2. 自动基础项（13 项，不可删）

| item_id | marker | 陈述 |
|---|---|---|
| `ENV-PG5432` | `NOT_SUPPORTED` | PostgreSQL LAB（5432）拒绝连接：跨引擎只读诊断的 PG 段未执行，按 NOT_EXECUTED 处置，不判通过、不冒充已执行。 |
| `ENV-1045` | `NOT_SUPPORTED` | DG_ADMIN_DSN（root@localhost）认证失败 1045：库级物理备份恢复不可用，W11 只做逻辑影子回放，不声称物理恢复已支持。 |
| `ENV-RESIDUE` | `PARTIAL` | 控制库只追加表存在未观测历史残留（W09 实测 140 行且随时间增长）：W11 各卡继续只追加不清理，所有计数按 task_id/run_id 过滤。 |
| `ENV-NV1` | `UNKNOWN` | 容器运行态 NOT_VERIFIED、镜像 digest 为 UNKNOWN：容器网络隔离未在运行态验证，不声称已验证。 |
| `ENV-MODEL` | `UNKNOWN` | 模型环境 BLOCKED_MODEL_ENV_MISSING：模型版本与生成时延等指标未测得，相关字段一律保持 null。 |
| `DECISION-TECH-PREVIEW` | `PARTIAL` | 发布结论为 TECH_PREVIEW_ONLY：存在未清环境阻塞或红线失败，不得表述为完整 Alpha。 |
| `NP-PROD-AUTO-WRITE` | `NOT_SUPPORTED` | W12 不承诺「生产自动写入」：该项能力不在本轮交付范围内，不得据此推断已具备。 |
| `NP-GENERIC-AUTO-REPAIR` | `NOT_SUPPORTED` | W12 不承诺「通用故障自动修复」：该项能力不在本轮交付范围内，不得据此推断已具备。 |
| `NP-ALWAYS-DIAGNOSE` | `NOT_SUPPORTED` | W12 不承诺「所有场景必然诊断成功」：该项能力不在本轮交付范围内，不得据此推断已具备。 |
| `NP-FULL-DEADLOCK-INDEX` | `PARTIAL` | W12 不承诺「完整死锁/索引评估」：该项能力不在本轮交付范围内，不得据此推断已具备。 |
| `NP-EXACTLY-ONCE` | `NOT_SUPPORTED` | W12 不承诺「跨系统 Exactly Once」：该项能力不在本轮交付范围内，不得据此推断已具备。 |
| `NP-NETWORK-ISOLATION-VERIFIED` | `NOT_SUPPORTED` | W12 不承诺「网络级隔离已验证（KD-1 账号级）」：该项能力不在本轮交付范围内，不得据此推断已具备。 |
| `DOC-PARTIAL` | `PARTIAL` | 文档完整性滞后（D-05/D-07）：迁移版本表与回滚清单已于 2026-11-19 补至 011（11 正向 + 8 回滚）、`.env.example` 已写明 fake/openai_compat/disabled 三态；仍存疑者为 W07 冻结四件套：05-freeze.md / 06-conformance-audit.md 虽于 2026-11-17 落盘，其形成过程未经本卡独立复核，故不声称四件套齐全。 |

## 3. 已知偏离（人工项，`origin=MANUAL`）

> 逐条来自主理人裁定（`docs/w10-w12-lead-rulings.md` §13.3 / §14），只登记，
> 不驱动路由、不降级自动项。
> 已关闭的偏离（如 `KD-W12-01`）**已从 `items` 移除**，只在 §3.1 留档，
> 不再计入 `items_digest`。

| item_id | marker | 陈述 | 登记人 | 依据 |
|---|---|---|---|---|
| `KD-W11-01` | `PARTIAL` | W11 备份恢复口径偏离：audit_chain 桶命名与 s3 键不一致（docs/w11/backup-restore-report.md:124-133）。 | 主理人裁定 | docs/w10-w12-lead-rulings.md §13.3 已知限制 L-1：只登记，不驱动路由、不降级 AC；W12 只读引用，不修 W11 逻辑。 |
| `KD-W11-02` | `PARTIAL` | W11 备份恢复覆盖漏项：未覆盖 approval_consumptions 与 step_prepared 两张只追加表。 | 主理人裁定 | docs/w10-w12-lead-rulings.md §13.3 已知限制 L-2：只登记，不降级 AC；W12 不改 W11 实现。 |
| `KD-W11-03` | `PARTIAL` | W11 恢复报告哨兵归属错位（G1–G3），且 delta/after>before 缓解断言实属 T05 备份用例。 | 主理人裁定 | docs/w10-w12-lead-rulings.md §13.3 已知限制 L-3（含 KD-W11-03 文本小瑕说明）：只登记。 |

### 3.1 已关闭偏离（已从 `items` 移除，仅留档 2 项）

> 关闭项**不参与** `items_digest`，也不构成当前限制；此表保留关闭依据与验证数字，
> 供追溯与对账（`CLOSED_DEVIATIONS`，`core/release/docs.py`）。

| item_id | marker | 关闭日期 | 关闭依据（修复点） | 验证数字 | 原陈述 |
|---|---|---|---|---|---|
| `KD-W12-01` | `PARTIAL` | 2026-11-17 | core/verification/plan.py 读取端形状归一：优先 result["data"]（执行器落盘的真实形状）、回落顶层（兼容历史扁平注入），不改写入端与断言常量；AC-03 由 FAILED 转 PASSED | 真实形状回归 5 例（tests/unit/test_verification_plan.py）；W12 T01 AC-03：status=PASSED / verdict=PASS / reason_code=OK，三条断言全 PASS；W08 崩溃矩阵 7 passed；tests/unit 1801 passed | 审批修复闭环的独立验证结论曾为 FAILED：StepObserved.result 落成全量快照，验证器在顶层取指标，affected_rows=UNKNOWN / snapshot_comparable=FAIL。 |
| `KD-W12-02` | `UNKNOWN` | 2026-11-19 | core/diagnosis/analyzers.py::analyze_slow_query 入口把 unindexed 初始化为 False，并在无 table_stats 证据时走显式降级分支（结论 UNKNOWN + 证据不足 + missing_evidence 标注 table_stats），不再读未赋值局部变量；有证据路径逐值不变 | 回归 2 例（tests/unit/test_diagnosis_analyzers.py：无 table_stats 不抛异常且降级 / 补齐 table_stats 后正常路径逐值不变）；tests/unit 1803 passed | 慢查询分析器在「全表扫描 + 无 table_stats 证据」输入下会抛 UnboundLocalError（core/diagnosis/analyzers.py:425）；W12 以证据包规避，AC-02 未覆盖该输入形状。 |

> `KD-W12-01` 关闭裁定依据：docs/w10-w12-lead-rulings.md §14.1 关闭条件（修 _collect_observed 兼容 result["data"] 并补真实形状回归）已满足；主理人 2026-11-17 裁定关闭并同步交付件。

> `KD-W12-02` 关闭裁定依据：docs/w10-w12-lead-rulings.md §14.2 关闭条件（把 unindexed 初始化提前或显式分支处理并补回归）已满足；2026-11-19 发布件同步卡经生成器关闭并从当前限制清单移除。

## 4. 机器可读真值（与 `.json` 逐字一致）

```json
{
  "items": [
    {
      "added_by": null,
      "item_id": "ENV-PG5432",
      "marker": "NOT_SUPPORTED",
      "origin": "AUTO",
      "reason": null,
      "statement": "PostgreSQL LAB（5432）拒绝连接：跨引擎只读诊断的 PG 段未执行，按 NOT_EXECUTED 处置，不判通过、不冒充已执行。"
    },
    {
      "added_by": null,
      "item_id": "ENV-1045",
      "marker": "NOT_SUPPORTED",
      "origin": "AUTO",
      "reason": null,
      "statement": "DG_ADMIN_DSN（root@localhost）认证失败 1045：库级物理备份恢复不可用，W11 只做逻辑影子回放，不声称物理恢复已支持。"
    },
    {
      "added_by": null,
      "item_id": "ENV-RESIDUE",
      "marker": "PARTIAL",
      "origin": "AUTO",
      "reason": null,
      "statement": "控制库只追加表存在未观测历史残留（W09 实测 140 行且随时间增长）：W11 各卡继续只追加不清理，所有计数按 task_id/run_id 过滤。"
    },
    {
      "added_by": null,
      "item_id": "ENV-NV1",
      "marker": "UNKNOWN",
      "origin": "AUTO",
      "reason": null,
      "statement": "容器运行态 NOT_VERIFIED、镜像 digest 为 UNKNOWN：容器网络隔离未在运行态验证，不声称已验证。"
    },
    {
      "added_by": null,
      "item_id": "ENV-MODEL",
      "marker": "UNKNOWN",
      "origin": "AUTO",
      "reason": null,
      "statement": "模型环境 BLOCKED_MODEL_ENV_MISSING：模型版本与生成时延等指标未测得，相关字段一律保持 null。"
    },
    {
      "added_by": null,
      "item_id": "DECISION-TECH-PREVIEW",
      "marker": "PARTIAL",
      "origin": "AUTO",
      "reason": null,
      "statement": "发布结论为 TECH_PREVIEW_ONLY：存在未清环境阻塞或红线失败，不得表述为完整 Alpha。"
    },
    {
      "added_by": null,
      "item_id": "NP-PROD-AUTO-WRITE",
      "marker": "NOT_SUPPORTED",
      "origin": "AUTO",
      "reason": null,
      "statement": "W12 不承诺「生产自动写入」：该项能力不在本轮交付范围内，不得据此推断已具备。"
    },
    {
      "added_by": null,
      "item_id": "NP-GENERIC-AUTO-REPAIR",
      "marker": "NOT_SUPPORTED",
      "origin": "AUTO",
      "reason": null,
      "statement": "W12 不承诺「通用故障自动修复」：该项能力不在本轮交付范围内，不得据此推断已具备。"
    },
    {
      "added_by": null,
      "item_id": "NP-ALWAYS-DIAGNOSE",
      "marker": "NOT_SUPPORTED",
      "origin": "AUTO",
      "reason": null,
      "statement": "W12 不承诺「所有场景必然诊断成功」：该项能力不在本轮交付范围内，不得据此推断已具备。"
    },
    {
      "added_by": null,
      "item_id": "NP-FULL-DEADLOCK-INDEX",
      "marker": "PARTIAL",
      "origin": "AUTO",
      "reason": null,
      "statement": "W12 不承诺「完整死锁/索引评估」：该项能力不在本轮交付范围内，不得据此推断已具备。"
    },
    {
      "added_by": null,
      "item_id": "NP-EXACTLY-ONCE",
      "marker": "NOT_SUPPORTED",
      "origin": "AUTO",
      "reason": null,
      "statement": "W12 不承诺「跨系统 Exactly Once」：该项能力不在本轮交付范围内，不得据此推断已具备。"
    },
    {
      "added_by": null,
      "item_id": "NP-NETWORK-ISOLATION-VERIFIED",
      "marker": "NOT_SUPPORTED",
      "origin": "AUTO",
      "reason": null,
      "statement": "W12 不承诺「网络级隔离已验证（KD-1 账号级）」：该项能力不在本轮交付范围内，不得据此推断已具备。"
    },
    {
      "added_by": null,
      "item_id": "DOC-PARTIAL",
      "marker": "PARTIAL",
      "origin": "AUTO",
      "reason": null,
      "statement": "文档完整性滞后（D-05/D-07）：迁移版本表与回滚清单已于 2026-11-19 补至 011（11 正向 + 8 回滚）、`.env.example` 已写明 fake/openai_compat/disabled 三态；仍存疑者为 W07 冻结四件套：05-freeze.md / 06-conformance-audit.md 虽于 2026-11-17 落盘，其形成过程未经本卡独立复核，故不声称四件套齐全。"
    },
    {
      "added_by": "主理人裁定",
      "item_id": "KD-W11-01",
      "marker": "PARTIAL",
      "origin": "MANUAL",
      "reason": "docs/w10-w12-lead-rulings.md §13.3 已知限制 L-1：只登记，不驱动路由、不降级 AC；W12 只读引用，不修 W11 逻辑。",
      "statement": "W11 备份恢复口径偏离：audit_chain 桶命名与 s3 键不一致（docs/w11/backup-restore-report.md:124-133）。"
    },
    {
      "added_by": "主理人裁定",
      "item_id": "KD-W11-02",
      "marker": "PARTIAL",
      "origin": "MANUAL",
      "reason": "docs/w10-w12-lead-rulings.md §13.3 已知限制 L-2：只登记，不降级 AC；W12 不改 W11 实现。",
      "statement": "W11 备份恢复覆盖漏项：未覆盖 approval_consumptions 与 step_prepared 两张只追加表。"
    },
    {
      "added_by": "主理人裁定",
      "item_id": "KD-W11-03",
      "marker": "PARTIAL",
      "origin": "MANUAL",
      "reason": "docs/w10-w12-lead-rulings.md §13.3 已知限制 L-3（含 KD-W11-03 文本小瑕说明）：只登记。",
      "statement": "W11 恢复报告哨兵归属错位（G1–G3），且 delta/after>before 缓解断言实属 T05 备份用例。"
    }
  ],
  "items_digest": "682a7cde9b8ecf4e2e6422fe4b0a3e077f9181773487b2216a3c96fd0de4cbf0",
  "source_path": "docs/w12/known-limitations.json",
  "version": "w12-limitations-v1",
  "w11_source_digest": "fbc39d0b847e1e6513806cdf4e5861988315ebc9e4049c35f227f3ce238e4ba7",
  "w11_source_status": "PASSED"
}
```

## 5. 口径说明

- 六类不承诺能力逐条列在自动项内（`NP-*`）：既不表示已具备，也不构成任何后续承诺；
- `DOC-PARTIAL` 为文档完整性滞后项（迁移说明与回滚清单已补至 011、`.env.example` 三态已补齐；W07 冻结四件套的 05-freeze.md / 06-conformance-audit.md 于 2026-11-17 落盘但形成过程未经独立复核，故仍标 `PARTIAL`、不声称齐全）；
- 环境阻塞五项（`ENV-PG5432` / `ENV-1045` / `ENV-RESIDUE` / `ENV-NV1` / `ENV-MODEL`）未解，
  相关用例按 `NOT_EXECUTED` / `NOT_VERIFIED` 处置，**不判 FAIL**，环境未清零；
- 本清单不改变 W07「有条件放行；门禁通过：否」、W08「有条件放行」、W09/W10/W11「门禁未评」的既有口径。
