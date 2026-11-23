# W11 RC 冻结件（`w11-rc-v1`）

- 生成方式：`core.assurance.rc.build_rc_freeze()`（**纯字符串拼装**）+ `collect_rc_manifest()`；
  正文里的限制清单 JSON 与 `docs/w11/limitations.json` **同源**（同一 `build_limitations()` 输出）。
- 时点锚：`commit = 1be436942bb7e74f328fc124fc905c4191572c0f`（由 `git rev-parse HEAD` 取得，**未写死**）
- 上游门禁口径：W07「有条件放行；门禁通过：否」、W08「有条件放行」、W09「门禁未评」、W10「门禁未评」。
  **本文件不构成门禁结论，也不预写 Alpha 结论**（W12-Q1/Q4）。

## 1. 冻结清单

| 字段 | 取值 |
|---|---|
| `manifest_version` | `w11-rc-v1` |
| `commit` | `1be436942bb7e74f328fc124fc905c4191572c0f` |
| `db_image_digest` | `UNKNOWN`（W11-Q7：未验证，登记为问题） |
| `adapter_version` | `mysql:1.0;postgres_mcp:1.0` |
| `tool_schema_version` | `tool-schema-v0` |
| `policy_version` | `w05-v1` |
| `prompt_version` | `prompt-v0` |
| `model_version` | `null`（ENV-MODEL） |
| `testset_version` | `testset-v0-preview` |
| `docs_migrations_lag` | `False`（D-06：`docs/migrations.md` 已覆盖全部迁移） |
| `container_runtime_status` | `NOT_VERIFIED` |
| `metric_definition_version` | `metrics-v1`（仅字面引用） |
| `frozen_evidence_available` | `True`（AC-21 按实测） |

### 1.1 迁移版本（枚举 `database/migrations/*.sql`，共 17 项）

| # | 迁移 |
|---|---|
| 1 | `001_harness_core` |
| 2 | `002_v22_evidence` |
| 3 | `003_evidence_redaction_hash` |
| 4 | `004_tenant_composite_fk` |
| 5 | `005_w05_identity_policy` |
| 6 | `006_w06_approval_audit` |
| 7 | `007_w07_disclosure` |
| 8 | `008_w08_verification` |
| 9 | `009_w09_redaction` |
| 10 | `010_w09_report` |
| 11 | `011_w09_memory` |
| 12 | `012_p1_byok_model_providers` |
| 13 | `013_p1_byok_provider_versions` |
| 14 | `014_p3_knowledge_confirmations` |
| 15 | `015_p5_eval_platform` |
| 16 | `016_p4_oidc_subject_mapping` |
| 17 | `017_p6_readonly_tool_dispatch` |

### 1.2 登记问题（缺项一律 `UNKNOWN` + 登记，不得静默）

| 问题编号 | 字段 | 取值 | 说明 |
|---|---|---|---|
| `ISSUE-DB-IMAGE-DIGEST` | `db_image_digest` | `UNKNOWN` | 容器运行态 NOT_VERIFIED（ENV-NV1）：未实跑 build/compose up，镜像 digest 取不到；deploy/environment-manifest.example.json 的 "NOT_VERIFIED" 不是 digest，不借用 |
| `ISSUE-CONTAINER-RUNTIME` | `container_runtime_status` | `NOT_VERIFIED` | 容器网络隔离只在配置层核对过，运行态未验证（不声称已验证） |
| `ISSUE-MODEL-VERSION` | `model_version` | `null` | ENV-MODEL：BLOCKED_MODEL_ENV_MISSING，model_commit 为 null；模型版本与相关指标保持 null，不填估计值 |

## 2. 报告索引

| 类别 | 文件 | 状态 |
|---|---|---|
| 安全｜负面集 29 条（AC-01…AC-04） | `benchmarks/w11/negative_cases.json` | PASSED |
| 安全｜跨租户 + 十类并发（AC-05…AC-07） | `benchmarks/w11/concurrency_cases.json` | PASSED |
| 可靠性｜六类故障注入与恢复（AC-08…AC-11） | `docs/w11/fault-injection-report.md` | PASSED |
| 恢复｜逻辑影子回放 + 长稳计划（AC-12…AC-14） | `docs/w11/backup-restore-report.md` | PARTIAL |
| 性能｜指标口径仅字面引用（AC-21） | `benchmarks/w10/metrics-v1.json` | REFERENCE_ONLY |
| 成本｜未做成本实测（无独立 Object Store / 未跑容器） | `docs/w11/limitations.json` | UNKNOWN |

## 3. 未通过项与延期方案

- `TECH_PREVIEW_ONLY` 相关的未清项见第 4 节限制清单（自动项**不可删**）；延期方案：
  `ENV-PG5432` / `ENV-1045` / `ENV-NV1` / `ENV-MODEL` 四项落 W12 实跑，`ENV-RESIDUE` 只做归因不清理。
- AC-21（上游指标口径未回退）：按实测执行（`tests/unit/test_w10_metrics.py` 真跑），
  但 **W10 门禁仍未评**，本文件不写「同一冻结版本已复核」，也不另造 `metrics-v2`。

## 4. 发布结论（由 `publish_decision` 纯函数派生，不可人工覆盖）

| 字段 | 取值 |
|---|---|
| `decision` | `TECH_PREVIEW_ONLY` |
| `reason` | `ENV_BLOCK_UNRESOLVED` |
| `failed_test_ids` | `[]` |
| `environment_blocks` | `['ENV-1045', 'ENV-MODEL', 'ENV-NV1', 'ENV-PG5432', 'ENV-RESIDUE']` |
| `inputs_digest` | `74c55984c69f5afb9dd8b9d7af1ab4aa811f5cb723bc9653bff5a66ae3f5961f` |
| `metric_definition_version` | `metrics-v1` |

> `failed_test_ids` 为空表示**没有红线失败**；环境阻塞属于完备性守卫（Q-W11-A），
> 不计入失败用例（PG 段仍是 `NOT_EXECUTED`，不判 FAIL）。
> 三份结果集（29 / 10 / 6）在收口时由 QA 用真实运行数据重算；本文件只固化口径与索引。

## 5. 限制清单（同源 JSON，单一数据源）

- 对账摘要：`limitations_digest = 89f75988f6b7b2e6d5950b25217ec74942b187523099d8bc5aaba8484a18f4f3`（`canonical_digest(限制清单 JSON)`）
- 与 `docs/w11/limitations.json`：**同一函数**（`build_limitations()` + `limitations_to_json()`）序列化，
  不存在两个真值；自动项 6 项不可删（删 → `LIMITATION_REMOVED`）。

```json
{
  "version": "w11-limitations-v1",
  "decision": "TECH_PREVIEW_ONLY",
  "items": [
    {
      "item_id": "ENV-PG5432",
      "marker": "NOT_SUPPORTED",
      "origin": "AUTO",
      "statement": "PostgreSQL LAB（5432）拒绝连接：跨引擎只读诊断的 PG 段未执行，按 NOT_EXECUTED 处置，不判通过、不冒充已执行。",
      "added_by": null,
      "reason": null
    },
    {
      "item_id": "ENV-1045",
      "marker": "NOT_SUPPORTED",
      "origin": "AUTO",
      "statement": "DG_ADMIN_DSN（root@localhost）认证失败 1045：库级物理备份恢复不可用，W11 只做逻辑影子回放，不声称物理恢复已支持。",
      "added_by": null,
      "reason": null
    },
    {
      "item_id": "ENV-RESIDUE",
      "marker": "PARTIAL",
      "origin": "AUTO",
      "statement": "控制库只追加表存在未观测历史残留（W09 实测 140 行且随时间增长）：W11 各卡继续只追加不清理，所有计数按 task_id/run_id 过滤。",
      "added_by": null,
      "reason": null
    },
    {
      "item_id": "ENV-NV1",
      "marker": "UNKNOWN",
      "origin": "AUTO",
      "statement": "容器运行态 NOT_VERIFIED、镜像 digest 为 UNKNOWN：容器网络隔离未在运行态验证，不声称已验证。",
      "added_by": null,
      "reason": null
    },
    {
      "item_id": "ENV-MODEL",
      "marker": "UNKNOWN",
      "origin": "AUTO",
      "statement": "模型环境 BLOCKED_MODEL_ENV_MISSING：模型版本与生成时延等指标未测得，相关字段一律保持 null。",
      "added_by": null,
      "reason": null
    },
    {
      "item_id": "DECISION-TECH-PREVIEW",
      "marker": "PARTIAL",
      "origin": "AUTO",
      "statement": "发布结论为 TECH_PREVIEW_ONLY：存在未清环境阻塞或红线失败，不得表述为完整 Alpha。",
      "added_by": null,
      "reason": null
    }
  ]
}
```
