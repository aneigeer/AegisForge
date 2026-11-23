# W12 可复现演示脚本（T03 / AC-12）

> 本文件是 **W12 对外材料**：四段演示脚本 + 隔离检查记录。
> 口径：`docs/w12/02-design.md` §9.2、`docs/w12/03-task-cards.md` T03、
> `docs/w10-w12-lead-rulings.md` §4（W12-Q7：**无 Web 控制台**）。
>
> **演示 ≠ 验收（AC-20）**：演示只证明"可复现地跑一遍"，**不进入**任何 `ac_result`
> 判定输入；验收结论只来自 `core/release/functional.py` 与 `core/release/security.py`
> 的实测结果件。演示四段本轮**未实跑**（`NOT_EXECUTED` + 原因），**不**冒充通过。

## 0. 隔离检查记录

`core/release/deploy.py::describe_isolation()` 的固定口径（可复核）：

| 项 | 值 | 依据 |
|---|---|---|
| `lab_only` | `True` | 只连 `dg_lab`（MySQL LAB）/ `dg_lab_pg`（PG LAB） |
| `production_dsn_referenced` | `False` | 脚本不读生产 DSN，只读 `DG_RO_DSN` / `DG_RW_DSN` / `DG_CONTROL_DSN` 的 LAB 指向 |
| 允许的写动作 | `mysql_lab_update_order_note` / `1.0` | `adapters/mysql/lab_repair.py`（LAB、R2、单行、需一次性审批） |
| `web_console_used` | `False` | W12-Q7：无 Web 控制台，全部经 CLI |
| 复用接缝 | `scripts/demo_agent_run.py`、`adapters/lab_assembly.py`、`workers/stack.py` | 不新增脚本装配 |
| PG 段 | `NOT_EXECUTED` | `ENV-PG5432`：PG LAB 不可达，不判 FAIL、不冒充通过 |

隔离说明：以上为**配置层只读复核**；容器运行态未验证（`ENV-NV1`），账号级隔离依据见
`docs/w07/07-gate-review.md`（KD-1）。本文件不据此下"网络隔离已验证"的结论。

## 1. 前置条件

```bash
# 1) LAB 与控制库探针（只读，确认种子行与环境事实存在）
.venv/Scripts/python.exe -c "from core.config import Settings; print(Settings().database_id)"
.venv/Scripts/python.exe -c "
import pymysql" 2>/dev/null || true

# 2) 只读确认 LAB 种子行（不写入）
#    dg_lab.seed_orders WHERE order_id = 2001 必须存在
```

- 需要 `DG_RO_DSN` / `DG_RW_DSN` / `DG_CONTROL_DSN` 指向**本机 LAB**；
- **不**使用 `DG_ADMIN_DSN`（本机认证失败 `ENV-1045`），**不**连生产库。

## 2. 四段脚本

### S1｜双引擎只读诊断（MySQL 段可跑；PG 段 `NOT_EXECUTED`）

```bash
.venv/Scripts/python.exe scripts/demo_agent_run.py
# 或只跑只读观察器（不写库、不新增端点）
.venv/Scripts/python.exe -c "
from core.config import Settings
from core.tools.contracts import ToolContext
from database.mysql.connection import MysqlClient
from database.observers import build_observer_registry
s = Settings()
c = MysqlClient(s.ro_dsn, database_id=s.database_id, read_only=True,
                connect_timeout_s=s.connect_timeout_s,
                statement_timeout_ms=s.statement_timeout_ms,
                max_result_rows=s.max_result_rows)
r = build_observer_registry().get('get_db_version').run(ToolContext(client=c), {})
print(r.status, r.data.get('version'))
"
```

- 断言：`c.read_only is True`；返回 `version` 非空；
- PG 段：`DG_PG_RO_DSN` 不可达 → 记 `NOT_EXECUTED`（`ENV-PG5432`），**不判 FAIL**；
- 本轮状态：`NOT_EXECUTED`（未实跑演示脚本）。

### S2｜单引擎审批修复（唯一 R2 动作，需人工审批）

```bash
# 走 W07/W08 唯一组合根；审批→Gateway→真实单行 UPDATE→独立回读
.venv/Scripts/python.exe -c "
from core.config import Settings
from workers.stack import build_worker_stack
from core.release.functional import run_repair_closure
from database.mysql.connection import MysqlClient
s = Settings()
stack = build_worker_stack(s)
cc = MysqlClient(s.control_mysql_dsn(), database_id='dg_system', read_only=False,
                 connect_timeout_s=s.connect_timeout_s,
                 statement_timeout_ms=s.statement_timeout_ms,
                 max_result_rows=s.max_result_rows)
r = run_repair_closure(stack=stack, control_client=cc)
print(r.status.value, r.verdict, r.reason_code, r.tool_calls, r.row_changes)
"
```

- 只允许 `mysql_lab_update_order_note` / `1.0`；`tool_calls == 1`、目标行变化恰 1 次；
- **重要（KD-W12-01，已关闭 2026-11-17）**：该链路的独立验证结论曾实测为 `FAILED`
  （`affected_rows=UNKNOWN(OBSERVED_MISSING)`、`snapshot_comparable=FAIL`、
  `target_note_final=PASS`），原因是 `StepObserved.result` 形状与验证器读取口径不一致；
  修复后（`core/verification/plan.py` 读取端形状归一：优先 `result["data"]`、回落顶层）
  已由 `PASSED` 取代（`status=PASSED`、`verdict=PASS`、`reason_code=OK`，三条断言全 `PASS`），
  历史 `FAILED` 与关闭依据留档于 `known-limitations.md` §3.1「已关闭偏离」；
  演示稿仍**不得**把建议项描述成"已修复"（表述禁令不变）；
- 脚本自带复原：W07 冻结协议把目标 `note` 复位，保证可重复执行（不是清理）。
- 本轮状态：`NOT_EXECUTED`（未实跑演示脚本；该链路由 T01 AC-03 用例覆盖，结论 `PASSED`）。

### S3｜独立 Verifier 回读（异路径只读）

```bash
.venv/Scripts/python.exe -c "
from core.config import Settings
from adapters.mysql.readback import MysqlReadbackAdapter
from core.verification.readback import TargetObject
from database.mysql.connection import MysqlClient
s = Settings()
c = MysqlClient(s.ro_dsn, database_id=s.database_id, read_only=True,
                connect_timeout_s=s.connect_timeout_s,
                statement_timeout_ms=s.statement_timeout_ms,
                max_result_rows=s.max_result_rows)
rb = MysqlReadbackAdapter(c)
print(rb.read_only, rb.client_identity())
print(rb.read_target(TargetObject(database='dg_lab', table='seed_orders',
                                  key_column='order_id', key_value=2001, column='note')))
"
```

- 断言：`read_back.read_only is True`；回读 client 与执行侧 `rw` client **不是同一实例**；
- 回读只接受白名单固定目标（不接受 SQL 文本 / host / port / DSN）；
- 本轮状态：`NOT_EXECUTED`（未实跑演示脚本；异路径回读端口由 T01/T02 用例覆盖）。

### S4｜UNKNOWN 恢复演练（不重试）

```bash
.venv/Scripts/python.exe -c "
from core.config import Settings
from core.release.security import probe_unknown_outcome
from database.mysql.connection import MysqlClient
s = Settings()
cc = MysqlClient(s.control_mysql_dsn(), database_id='dg_system', read_only=False,
                 connect_timeout_s=s.connect_timeout_s,
                 statement_timeout_ms=s.statement_timeout_ms,
                 max_result_rows=s.max_result_rows)
o = probe_unknown_outcome(control_client=cc)
print(o.status.value, o.conclusion, o.tool_calls_delta, o.retried_events)
"
```

- 断言：结论 ∈ {`NEEDS_RECONCILIATION`, `MANUAL_INTERVENTION`}、`tool_calls_delta == 0`、
  **无** `TOOL_CALL_RETRIED`（UNKNOWN 绝不自动重试）；
- 本轮状态：`NOT_EXECUTED`（未实跑演示脚本；该链路已由 T02 AC-08 实测覆盖）。

## 3. 四段状态汇总（`core/release/deploy.py::build_demo_result()`）

| 段 | 名称 | 状态 | 原因 |
|---|---|---|---|
| S1 | 双引擎只读诊断 | `NOT_EXECUTED` | 本轮未实跑；PG 段受 `ENV-PG5432` 阻塞 |
| S2 | 单引擎审批修复 | `NOT_EXECUTED` | 本轮未实跑；验收结论见 T01 AC-03（AC-03 结论 `PASSED`，`KD-W12-01` 已关闭） |
| S3 | 独立 Verifier 回读 | `NOT_EXECUTED` | 本轮未实跑；端口由 T01/T02 用例覆盖 |
| S4 | UNKNOWN 恢复演练 | `NOT_EXECUTED` | 本轮未实跑；不重试已由 T02 AC-08 实测覆盖 |

- `lab_only = True`、`production_dsn_referenced = False`、`web_console_used = False`；
- `report_has_completion_wording_for_advice = False`（本文件不存在把建议项写成完成态的句子；
  禁令语境行由扫描器的否定标记豁免）；
- 演示结论只写进本文件，**不写进** `alpha-acceptance-report.md` 的判定输入（AC-20）。
