# W11 T05 备份恢复与长稳报告（逻辑影子回放，不是物理备份恢复）

- 依据：`docs/w11/02-design.md` §8、`docs/w11/03-task-cards.md` T05（`:332-415`）、`docs/w10-w12-lead-rulings.md` §3 W11-Q2/Q3/Q4/Q6、§8 Q-W11-B/C
- 实现：`core/assurance/backup.py`、`core/assurance/soak.py`；验收用例：`tests/integration/test_w11_backup_restore.py`、`tests/unit/test_w11_soak_report.py`
- 时点锚：`git HEAD = 2850eca`，全部改动**未提交**
- 上游门禁口径（不变）：W07「有条件放行；门禁通过：否」、W08「有条件放行」、W09「门禁未评」、W10「门禁未评」。**本报告不构成门禁结论**

> **这就是逻辑回放，不是物理备份恢复。** 本文不写「备份恢复已验证」、不写「RPO/RTO 已测得」、不写「环境已清零」。

---

## 1. 四项硬标注（Q-W11-B，四处一律不得省）

| 字段 | 值 | 含义 |
|---|---|---|
| `restore_mode` | `LOGICAL_SHADOW` | 逻辑影子回放（不是物理库级恢复） |
| `physical_restore` | `NOT_SUPPORTED` | 物理恢复不支持（`ENV-1045`：`DG_ADMIN_DSN` root@localhost 1045 不可用） |
| `object_store` | `NOT_SUPPORTED` | 无独立 Object Store（W11-Q6） |
| `measured_rpo_minutes` / `measured_rto_hours` | `null` / `null` | 未做物理恢复 ⇒ 实测缺席；`sla_claim is False` |

`sla_note` 固定为「建议目标，未验证 SLA」。

---

## 2. 四步可控构造（W11-Q2：不以「环境可清零」为前提）

1. **backup**：对本卡 `task_id` 集合**只读 SELECT** 四类对象 → `BackupSnapshot` +
   `backup_digest = canonical_digest(...)`；
2. **append**：经既有仓储**只 INSERT** `BACKUP_APPEND_N = 12` 条（四类各 3 条，带本卡 `run_id` 后缀）；
3. **restore**：`ShadowStore.replay(snapshot)` 把快照**真实回放**成独立可查询存储
   （② 追加的 12 条**不在**其中 ⇒ 点恢复，而不是"备份文件存在"）；
4. **verify**：四类计数逐值校验 + `seq == list(range(n))` +
   `AuditService.verify`（`core/audit/service.py:153`）+ Evidence 引用可解析。

**四类对象的物理落点**（`BACKUP_OBJECT_CLASSES` 是冻结的逻辑类名）：

| 逻辑类 | 表 | 说明 |
|---|---|---|
| `task_events` | `dg_system.task_events` | 追加式事件（哈希链本体） |
| `approvals` | `dg_system.approvals` | 审批行 |
| `step_evidence` | `dg_system.evidence` | 证据行（引用可解析性由此校验） |
| `audit_chain` | `dg_system.step_observed` | 审计链的「执行后回执」段 |

数据纪律：全流程只 `SELECT` + `INSERT`；**无** `DELETE` / `UPDATE` / `TRUNCATE` / `DROP` /
`CREATE DATABASE` / 影子表；不依赖 `DG_ADMIN_DSN`；不用 `scripts/repair_event_chain.py`。

---

## 3. RPO / RTO：目标值与实测值**分列**

| 指标 | 建议目标（仅目标，不作门禁硬阈值） | 实测值 | 说明 |
|---|---|---|---|
| RPO | `target_rpo_minutes = 15.0` | `measured_rpo_minutes = null` | 未做物理恢复，实测缺席（W11-Q3） |
| RTO | `target_rto_hours = 4.0` | `measured_rto_hours = null` | 同上 |

- `sla_claim` 恒为 `False`；模型校验器强制「`measured_*` 任一为 `null` ⇒ 不得 `sla_claim=True`」；
- `logical_replay_duration_s` 只是**回放耗时**，**不是 RTO**，不得填进 `measured_*`；
- 目标字段与实测字段互不覆盖（计划值 15 分钟 / 4 小时只出现在目标列）。

---

## 4. 对象存储边界（W11-Q6，如实登记）

- `object_store = "NOT_SUPPORTED"`，`storage_topology` 固定为
  「控制库 `dg_system.*` + `evidence` 表；无独立 Object Store」；
- 证据：`core/audit/worm.py` 的 `ObjectStorageArchiveSink` 是**占位类**（构造即抛
  `NotImplementedError`，注释明写「未实测、不装配、不宣称具备 WORM 能力」），
  `LocalArchiveSink` 全仓无装配点；
- 本卡**不实例化**该占位类、**不编造**任何对象存储路径/bucket/endpoint。

---

## 5. 长稳：计划值与实测值分列（W11-Q4 / Q-W11-C）

| 常量 | 冻结值 | 含义 |
|---|---|---|
| `SOAK_SAMPLE_SIZE` | `200` | **计划**采样量 |
| `SOAK_DURATION_S` | `3600` | **计划**时长（秒） |
| `SOAK_POLL_INTERVAL_S` | `5.0` | 采样间隔（秒） |
| `SOAK_SMOKE_TEST` | `False` | 不得用短时 smoke 冒充长稳 |
| `BACKUP_APPEND_N` | `12` | 恢复演练的追加行数（四类各 3） |

| 长稳字段 | 计划/实测 | 本卡取值 |
|---|---|---|
| `sample_size` | 计划 | `200` |
| `duration_s` | 计划 | `3600` |
| `smoke_test` | — | `False` |
| `measured_sample_size` | 实测 | `null`（未实跑 1 小时长稳） |
| `measured_duration_s` | 实测 | `null` |
| `p50_ms` / `p95_ms` | 实测 | `null`（样本为空，不编造分位数；有样本时复用 `core/verification/perf.py::percentile`） |
| `status` | — | `PLANNED` |

告警清单 6 项（集合相等，不多不少）：`lease_lost` / `audit_write_failed` /
`evidence_unavailable` / `database_error` / `model_degraded` / `backup_failed`。

**长稳未实跑**：本卡只交付"计划值冻结 + 实测字段缺席 + 口径可断言"，不交付 1 小时长稳的实测结论；未实测值一律 `null`。

---

## 6. 环境项（未解，不冒充通过）

| 编号 | 对 T05 的影响 | 处置 |
|---|---|---|
| `ENV-1045` | 不能做物理库级恢复、不能用 `scripts/repair_event_chain.py` | 逻辑影子回放；`physical_restore="NOT_SUPPORTED"`；**不碰账号/`.env`** |
| `ENV-PG5432` | 跨引擎段不可执行 | 本卡不涉及 PG；不判 FAIL |
| `ENV-RESIDUE` | 控制库历史残留随时间增长 | 四类计数**按本卡 `task_id`/`run_id` 过滤**，不做全表 `COUNT(*)`；**不清理** |
| `ENV-NV1` | 容器运行态 `NOT_VERIFIED` | 本卡不评容器 |
| `ENV-MODEL` | 模型环境 `BLOCKED_MODEL_ENV_MISSING` | 长稳的模型相关指标 `null` |

---

## 7. 验收命令（串行可复现）

```bash
.venv/Scripts/python.exe -m pytest tests/unit/test_w11_soak_report.py -o addopts="" -q -p no:cacheprovider
timeout 180 .venv/Scripts/python.exe -m pytest tests/integration/test_w11_backup_restore.py -o addopts="" -q -p no:cacheprovider
.venv/Scripts/python.exe -m pytest tests/unit/test_verification_perf.py -o addopts="" -q -p no:cacheprovider   # 10 passed 不变量（percentile 未被重写）
```

实测结果见批 3 回传与 QA 收口件（本文件只登记口径，不预写结论）。

---

## 8. 已知偏离（KD-W11-01…03，如实登记，移交 W12；本轮**不改逻辑**）

> 本报告**不声称备份恢复已覆盖全部对象类**。`physical_restore` / `object_store` 仍为
> `NOT_SUPPORTED`，`measured_rpo_minutes` / `measured_rto_hours` 仍为 `null`。

| 编号 | 偏离 | 影响 | 关闭条件 |
|---|---|---|---|
| **KD-W11-01** | `BACKUP_OBJECT_CLASSES` 的 `audit_chain` 逻辑桶实际落 `dg_system.step_observed`（执行回执段），与设计 §8.2 的桶定义/命名不一致；**`task_events` 才是哈希链本体**（本模块文档自述） | AC-12 记**部分覆盖**（四类计数逐值校验成立，但"审计链"桶的语义与设计不一致） | W12 设计须修正桶命名，并把 `task_events` 明确纳入审计链快照 |
| **KD-W11-02** | 设计 §8.2 明列的 `approval_consumptions` / `step_prepared` **未**纳入 `BACKUP_OBJECT_CLASSES`（当前四类不含它们） | AC-12 记**部分覆盖**（这两类对象未参与快照/回放计数） | W12 纳入两桶，并把"四类计数断言"扩为"六类计数断言" |
| **KD-W11-03** | G1–G3 的「拒绝不改数据」用**哨兵归属**验证（`no_change_probe` 以本用例 `task_id` 过滤 + `delta==3` / `after>before` 缓解断言），不是逐条对真实目标行做前后探测 | AC-03 记**部分覆盖**（口径可核对，但未逐条探测真实目标行） | 后续轮次补"逐条真实目标行前后探测"，或由 QA 在收口件中按现状接受 |

其它口径（经 QA 独立证伪为成立、本轮不动）：`GATE_IDS` 为结构判定而非弱 grep；S2 的单引擎写按 W07 冻结协议复原；`limitations.json` 与 `rc-freeze.md` 内嵌块为同一函数产物（单一真值）；W08 改写仅 1 个 hunk；所有计数按本卡 `task_id`/`run_id` 过滤。
