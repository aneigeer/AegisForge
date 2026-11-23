# W11 T03 故障注入与恢复核对报告（六类）

- 依据：`docs/w11/02-design.md` §7（§7.1 严格六类、§7.2 注入方式、§7.3 fail-closed/告警/恢复）、`docs/w11/03-task-cards.md` T03（`:192-250`）、`docs/w10-w12-lead-rulings.md` §3 W11-Q5、§8 D-03
- 冻结清单：`benchmarks/w11/fault_cases.json`（`fault-set-v1`，**恰 6 条**，六类各一条）
- 实现：`core/assurance/faults.py`；验收用例：`tests/integration/test_w11_fault_injection.py`
- 时点锚：`git HEAD = 2850eca`，全部改动**未提交**
- 上游门禁口径（不变）：W07「有条件放行；门禁通过：否」、W08「有条件放行」、W09「门禁未评」、W10「门禁未评」。**本报告不构成门禁结论**

> 本报告只写**本卡实际执行到的事实**。未实测项一律写 `null` 或「未执行」，不用估计值、不写「全部可自动恢复」。

---

## 1. 严格六类（W11-Q5）

| # | `FaultClass` | 是否在枚举内 | 说明 |
|---|---|---|---|
| 1 | `EVIDENCE_STORE` | 是 | 证据存储写入不可用 |
| 2 | `AUDIT_WRITER` | 是 | 审计账本不可写（fail closed 落点） |
| 3 | `DB_DISCONNECT` | 是 | 目标库断连 |
| 4 | `MODEL_SERVICE` | 是 | 模型服务不可用 |
| 5 | `WORKER_CRASH` | 是 | Worker 崩溃（进程终止可观测） |
| 6 | `CONTROL_DB_TRANSIENT` | 是 | 控制库短暂故障（可重试瞬时错误） |
| — | 网络分区 / 磁盘满 | **否** | W11-Q5 明令不纳入；`FAULT_CLASSES` 无此二值，传入即 `FaultTypeError`（用例 `test_seventh_fault_class_is_rejected` 已覆盖） |

`FAULT_CLASSES` 与 `fault_cases.json` 顺序逐项相等，条数 `== FAULT_CASE_COUNT == 6`；少一条或出现第 7 类都会使 `load_fault_cases` 抛错（用例 `test_fault_counts_are_frozen`）。

---

## 2. 六类逐条：注入方式 / 实际结果 / 断言证据 / 恢复边界

| # | 类 | 注入方式（既有接缝，未加生产分支） | 实际结果（本次实测） | 断言证据 | 边界 |
|---|---|---|---|---|---|
| 1 | `EVIDENCE_STORE` | `monkeypatch` 仓库 `EvidenceRepository.insert` 抛 `DatabaseUnavailableError`（等价存储不可用） | `EvidenceService.create` fail closed：抛 `DB_UNAVAILABLE`、证据表 **0 新增行**（不伪造证据）；解除注入后同一次重试成功、引用可解析 | `raised_code == DB_UNAVAILABLE`；`rows_after_failure == evidence_before`；恢复后 `evidence_count + 1` 且 `validate_task_references` 无缺失 | **AUTO** |
| 2 | `AUDIT_WRITER` | 审计仓库替身 `insert_event` 抛 `DatabaseQueryError`（真实 `AuditService.emit_required` / `try_emit` 原样使用） | **AC-08**：R2 请求在 `emit_required`（`core/audit/service.py:209`）处被阻断 → 结果 `DENIED` + `AUDIT_UNAVAILABLE`，工具 **0 次执行**、`tool_calls` **0 新增**；**AC-09**：R0 路径 `try_emit` 返回 `False`（降级不抛错），告警 `AUDIT_WRITE_FAILED` 落在结构化日志 | `r2_result.error.code == AUDIT_UNAVAILABLE`；`storage.executed == 0`；`tool_calls_after == tool_calls_before`；`r0_degraded is True`；`"AUDIT_WRITE_FAILED" in caplog alerts` | **MANUAL** |
| 3 | `DB_DISCONNECT` | 工具上下文的 `client.fetch_all` 抛 `DatabaseUnavailableError`（真实观察器 `get_db_version` 的错误映射） | 工具返回 `status=ERROR` + `code=DB_UNAVAILABLE` + `data == {}`（**不伪造采集结果**）；连接恢复后同一工具返回 `SUCCESS` | `failed_status == error`；`failed_code == DB_UNAVAILABLE`；`failed_data_empty is True`；`recovered_status == success` | **MANUAL** |
| 4 | `MODEL_SERVICE` | `monkeypatch` 模型适配器 `complete`：① 抛领域错误 `ModelUnavailableError`；② 抛非领域 `RuntimeError` | ① 领域错误原样上抛 `MODEL_UNAVAILABLE`（`ModelGateway._invoke` 对 `DBGuardianError` 不包装）；② 非领域异常归一化为 `MODEL_CALL_FAILED`；两条路径都不产生工具选择；`DisabledModelGateway` 显式抛 `MODEL_UNAVAILABLE`（降级不静默）；模型恢复后同网关返回文本 | `raised_code == MODEL_UNAVAILABLE`；`normalized_code == MODEL_CALL_FAILED`；`degraded_code == MODEL_UNAVAILABLE`；`recovered_text` 非空 | **AUTO** |
| 5 | `WORKER_CRASH` | ① 真实子进程 `Popen.kill()`（固定 argv、不经 shell、不写文件）；② 崩溃**等价构造**：`StepPrepared` 落盘但无 `StepObserved`，随后交给 `Reconciler.sweep_task` | ① 子进程被 kill，`returncode != 0`（进程终止可观测）；② 核对结论 `NEEDS_RECONCILIATION` 且 `transitioned is True`，事件仅 `RECONCILIATION_REQUIRED`，**无 `TOOL_CALL_RETRIED`**、**不重放** | `child_returncode != 0`；`conclusions == [NEEDS_RECONCILIATION]`；`"TOOL_CALL_RETRIED" not in reconciler_event_types`；事件 payload `replay_attempted: False` | **MANUAL** |
| 6 | `CONTROL_DB_TRANSIENT` | `monkeypatch` 控制库实例 `client.execute` 抛 `DatabaseQueryError`（瞬时错误） | `emit_required` fail closed 抛 `AuditUnavailableError`，**故障期事件序号无空洞**（`seq` 与故障前完全相等）；故障消失后重试成功，`seq` 从故障前继续连续（`0..n-1`） | `raised_code == AUDIT_UNAVAILABLE`；`seq_during_failure == seq_before`；`seq_after_recovery == list(range(n))`；`audit_chain_ok is True` | **AUTO** |

**边界统计（不写「全部可自动恢复」）**：`AUTO` = 3（`EVIDENCE_STORE`、`MODEL_SERVICE`、`CONTROL_DB_TRANSIENT`）；`MANUAL` = 3（`AUDIT_WRITER`、`DB_DISCONNECT`、`WORKER_CRASH`）。用例 `test_fault_boundaries_are_not_uniformly_auto` 逐值断言该分布。

---

## 3. AC-08 / AC-09 的方向性（D-03，最容易做反的一条）

`core/audit/service.py` 实测两条路径**必须分清**：

| 行 | 方法 | 语义 | 本卡用途 |
|---|---|---|---|
| `:209` | `emit_required()` | **fail closed**：写入异常 → `_alert_write_failed` → `:240` 抛 `AuditUnavailableError` | **AC-08**：R2 请求必须走这条，被阻断 |
| `:248` | `try_emit()` | **best-effort**：捕获 `AuditUnavailableError` 返回 `False` | **AC-09**：R0 读取降级走这条 |

- AC-08 的断言是「R2 被阻断 + `tool_calls_delta == 0` + 目标行不变」，**没有**把 `emit_required` 降级为 best-effort，也没有「先执行后补审计」；
- AC-09 的断言是「按策略返回 + `alert_emitted is True`」；账本不可写时告警只剩结构化日志（ADR-0006:129），已在用例里用 `alert == "AUDIT_WRITE_FAILED"` 的日志记录核对。

---

## 4. AC-10 / AC-11：恢复完整性（六项谓词）

`assert_recovery_integrity()` 是唯一谓词，六项**全部**由真实事实取得：

| 项 | 取数来源 |
|---|---|
| `seq_continuous` | 真实 `dg_system.task_events`：`seq == list(range(n))` |
| `audit_chain_ok` | `core/audit/service.py:153 AuditService.verify`（复用 `verify_event_chain`，**未重写**） |
| `evidence_refs_resolvable` | `EvidenceRepository.validate_task_references` |
| `state_version_monotonic` | 故障前后 `tasks.state_version` 比较 |
| `tool_calls_delta` | 本卡 `task_id` 过滤的 `tool_calls` 计数差（**不是全表 COUNT**） |
| `replay_occurred` | 本卡任务事件里是否出现 `TOOL_CALL_RETRIED` |

六类全部满足 `tool_calls_delta == 0`、`replay_occurred is False`；**恢复期绝不重放**（AC-11）。核对优先于行动：`Reconciler` 只收敛为待核对状态，工具调用次数不变。

`scanned_total` 口径沿用 W09（`core/execution/reconciler.py` `SweepReport`），本卡**不重复实现**、不改其返回值契约。

---

## 5. 覆盖边界与未执行项（如实登记）

1. **`WORKER_CRASH` 的进程级重启矩阵未在本卡重跑**：`Popen.kill()` 只证明「崩溃接缝可观测、进程确实终止」；恢复核对走「已 Prepared 未 Observed」等价构造。真实的 Worker 进程崩溃/接管矩阵证据仍是 W08 演练的既存 7 例（本卡按纪律**只读不改**，并作为不变量复跑）。
2. **`DB_DISCONNECT` 恢复期的行集是 LAB 库替身**：只断言「断连 → `ERROR/DB_UNAVAILABLE` + `data` 为空」「连接可用 → `SUCCESS`」这两条**状态映射**，不对目标库数据正确性下结论。
3. **`AUDIT_WRITER` 故障窗口内的事件无法自动补写**：账本恢复后只能保证后续事件继续追加；窗口内丢失的「必须成功」事件留下的是结构化日志（ADR-0006:129）与人工核对，故边界判 `MANUAL`。
4. **`MODEL_SERVICE` 未测真实模型时延/TTFT**：`ENV-MODEL`（`BLOCKED_MODEL_ENV_MISSING`）未解，`null`/未执行，不用估计值。
5. **未涉及的环境项**：`ENV-PG5432`（不连 PG）、`ENV-1045`（不用 `DG_ADMIN_DSN`）、`ENV-NV1`（容器运行态 `NOT_VERIFIED`）、`ENV-RESIDUE`（本卡只按 `task_id` 过滤计数，不清理）。
6. **数据纪律**：本卡只 INSERT 追加（每类自建 tenant/environment/task/事件），不 DELETE / UPDATE / TRUNCATE / DROP，不清理残留。
7. **未新增任何生产分支、错误码、事件类型**：`core/errors.py`、`core/audit/events.py`、`benchmarks/faults/injector.py` 一字未改；`benchmarks.*` 在 `core/assurance/faults.py` 内**只做函数内惰性 import**（用例 `test_adapter_imports_benchmarks_lazily` 用子进程核对 `sys.modules` 无 `benchmarks` 条目）。

---

## 6. 验收实测（可复现）

| 命令 | 结果 |
|---|---|
| `timeout 180 .venv/Scripts/python.exe -m pytest tests/integration/test_w11_fault_injection.py -o addopts="" -q -p no:cacheprovider` | **16 passed / 0 failed / exit 0**（5.14 s） |
| `timeout 180 .venv/Scripts/python.exe -m pytest tests/integration/test_w08_crash_matrix.py -o addopts="" -q -p no:cacheprovider`（既存 7 例不变量，只读不改） | 首跑 `..F...` 后外层 `timeout` 到点（exit 124）；隔离重跑（`timeout 420`）**1 failed / 6 passed**（238.15 s）。唯一失败见 §7，**与本批实现无关** |

六类实测合计：`injected` 6/6、`fail_closed_ok` 6/6、`alert_emitted` 1/6（仅 `AUDIT_WRITER`，其余类无告警是**预期事实**而非缺失）、`replay_occurred` 0/6、`tool_calls_delta` 合计 0。

---

## 7. 跨卡不变量：`tests/integration/test_w08_crash_matrix.py` 的 1 条失败（如实登记，非本批引入）

| 项 | 事实 |
|---|---|
| 失败用例 | `test_03_connection_interrupted_during_reconciliation`（`tests/integration/test_w08_crash_matrix.py:365` → `assert report.clean is False`） |
| 首跑 | `timeout 180` → `..F...` + 外层超时 `exit 124`；**隔离重跑一次**（`timeout 420`）→ `1 failed / 6 passed in 238.15s`，失败可复现、非 flake |
| 直接原因 | 该用例 `monkeypatch.setattr(worker_stack.reconciler, "sweep", broken_sweep)` 只替换了 `sweep`；而 `workers/runner.py::_invoke_sweep` **优先调用 `sweep_detailed()`**（W09 T06 KD-2 的"真实扫描行数"口径），真正的核对因此照常成功 → `report.failed == 0` → `clean is True`，断言落空 |
| 定性 | **断言前提失效（测试侧打桩目标过期）**，不是生产缺陷、也不是本批引入：本批**零既有文件修改**（`git diff --name-only` 仍 19，`tests/integration/test_w08_crash_matrix.py` 不在改动清单内，`workers/runner.py` / `core/execution/reconciler.py` 一行未动） |
| 处置 | 按纪律 **不改** 该冻结用例（`docs/w08/**` 与本文件均为只读）；已回报 team-lead 请求裁定（可对标 W10-CORR-01 的"断言前提消失"先例，把打桩目标改为 `sweep_detailed`）。AC-10/AC-11 的恢复完整性由本卡自己的 6 条用例独立覆盖 |
| 时间事实 | 该文件 7 例在本机实测 **238 s**，已超过任务卡给的外层 `timeout 180`，故"到点"本身不是失败信号 |

