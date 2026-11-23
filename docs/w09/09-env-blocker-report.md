# W09｜环境阻塞只读诊断报告（任务 #3）

> 性质：**只读诊断**，不是回归测试，不是修复。本轮**未改动任何 `.py` / `.sql` / 配置 / 既有文档**，仅新建本文件。
> 复验人：严过关（Yan）· QA 工程师 ｜ 工作区：`D:/DBGuaredian` ｜ git HEAD：`2850eca` ｜ 解释器：`.venv/Scripts/python.exe`（Python 3.14.7）
> 命令口径：一律 `-o addopts="" -q -p no:cacheprovider`；定向文件加 `timeout 90`；**禁止跑全量 pytest**（历史记录挂起 14 分钟无输出）。
> 凭据处理：本报告只记录环境变量**名称**与**脱敏后 DSN**（`MysqlDsn.redacted`，`database/mysql/dsn.py:29-30`），**不记录任何口令/凭据明文**。
> 只读边界：本轮只读源码/配置/文档，只执行只读 SELECT；**未执行任何 INSERT / UPDATE / DELETE 清理**（测试自身按全局纪律只 INSERT）；未改账号、未重启服务、未改环境变量、未执行 git commit / checkout / reset。

---

## ① 现象

`tests/integration/test_evidence_service.py` 定向复跑，汇总行为 `6 passed, 6 errors`；**6 个 error 全部发生在 teardown**，`用例本体 6 passed，6 个 teardown errors 为环境缺陷`。

本轮实跑（带 `timeout 90`）：

```
$ cd /d/DBGuaredian && timeout 90 .venv/Scripts/python.exe -m pytest tests/integration/test_evidence_service.py -o addopts="" -q -p no:cacheprovider
.E.E.E.E.E.E                                                             [100%]
=================================== ERRORS ====================================
_____________ ERROR at teardown of test_create_list_get_roundtrip _____________
... （6 条同构错误，逐条均为 ERROR at teardown）
E   core.errors.DatabaseAuthError: 数据库认证失败，请检查账号配置
6 passed, 6 errors in 6.60s
EXIT=1
```

6 条 ERROR 对应的用例（`tests/integration/test_evidence_service.py`）：

| # | 用例 | 用例本体 | teardown |
|---|---|---|---|
| 1 | `test_create_list_get_roundtrip` | PASSED | ERROR |
| 2 | `test_create_rejects_cross_tenant_and_environment_mismatch` | PASSED | ERROR |
| 3 | `test_reference_validation_cross_task_tenant_environment` | PASSED | ERROR |
| 4 | `test_report_facts_rejects_foreign_evidence_and_ignores_missing` | PASSED | ERROR |
| 5 | `test_quality_constraint_for_degraded_status` | PASSED | ERROR |
| 6 | `test_cursor_pagination_has_no_duplicates` | PASSED | ERROR |

对照复跑（同样只读/只 INSERT 纪律）：

```
$ timeout 90 .venv/Scripts/python.exe -m pytest tests/unit -o addopts="" -q -p no:cacheprovider
1138 passed in 5.35s

$ timeout 90 .venv/Scripts/python.exe -m pytest tests/integration/test_w09_redaction_pipeline.py -o addopts="" -q -p no:cacheprovider -rs
4 passed in 3.75s          # -rs 无 skip 行 → 真实连库，未走 skip 分支
```

→ 数据库**可连**（控制账号 `dg_ctl` 正常），只有**管理员账号**这条路径不通。

---

## ② 证据

### 2.1 `admin_client` / `DG_ADMIN_DSN` 的定义与装配路径（只读源码定位）

| 环节 | 文件:行号 | 内容 |
|---|---|---|
| 环境变量名 | `core/config.py:26` | `env_prefix="DG_"` → 字段 `admin_dsn` 对应环境变量 `DG_ADMIN_DSN` |
| 字段定义 | `core/config.py:52` | `admin_dsn: SecretStr \| None = None`，注释「仅初始化/测试清理使用；应用运行时不读取」 |
| 字段校验 | `core/config.py:164-173` | 非空时按 `parse_mysql_dsn` 校验语法；不校验口令是否正确 |
| 模板值 | `.env.example:88` | `DG_ADMIN_DSN=mysql://root:CHANGE_ME@127.0.0.1:3306/mysql?charset=utf8mb4`（占位口令） |
| `settings` 夹具 | `tests/conftest.py:24-29` | session 级；`Settings()` 抛 `ValidationError` 才 `pytest.skip`。本轮未 skip → `.env` 必填字段齐备 |
| `admin_client` 夹具 | `tests/conftest.py:68-79` | session 级；仅当 `settings.admin_dsn is None` 才 skip（`:70-71`）；否则 `return MysqlClient(settings.admin_dsn, database_id="dg_admin", read_only=False, ...)`（`:72-79`） |
| 客户端连接时机 | `database/mysql/connection.py:96-111` | `MysqlClient.__init__` **只保存 DSN，不连库**（懒连接）→ 夹具 setup 阶段不会触发 1045 |
| 初始化脚本 | `scripts/bootstrap_mysql.py:88`、`:93-101` | `_require_env("DG_ADMIN_DSN")` → `pymysql.connect(host/admin.user, ...)`，账号建库/建用户/授权 |
| 启动脚本 | `scripts/start_lab.ps1:78-83` | `if ($env:DG_ADMIN_DSN)` 才跑 `bootstrap_mysql.py`；否则跳过 MySQL 初始化 |
| 迁移/回滚 | `docs/migrations.md:36,69,82-85`、`database/migrations/rollback/007_rollback.sql:3`、`008_rollback.sql:3`、`009_rollback.sql:3`、`:7` | 需 `DROP`/`RENAME`/重算链的操作一律「必须由 `DG_ADMIN_DSN` 人工执行」，属人工运维路径 |
| 维修脚本 | `scripts/repair_event_chain.py:10,25` | 「需要 DG_ADMIN_DSN（管理员连接串）；应用控制账号不允许 UPDATE 事件表」 |

### 2.2 清理为什么必须用管理员账号（授权实测）

`scripts/bootstrap_mysql.py:42-68`：`dg_ctl` 在 `dg_system` 上只被授予 `SELECT, INSERT, CREATE, ALTER, INDEX, REFERENCES`（`:45-49`），`UPDATE/DELETE` 只逐表授予少数状态表（`:51-68`）。本轮以 `dg_ctl` 执行只读 `SELECT` 查 `information_schema`，实测：

```
schema_privileges (dg_system, grantee='dg_ctl'@'%') -> SELECT, INSERT, CREATE, REFERENCES, INDEX, ALTER   # 库级无 DELETE
table_privileges  (dg_system) 有 DELETE 的表 -> tool_calls, checkpoints, environments, tenants,
                                                memberships, task_steps, tasks, users
```

`evidence`、`task_events` 等只追加表**不在** DELETE 名单内。历史实测同口径见 `docs/w07/worker-residue.md:39-55`（`SHOW GRANTS`，并注明「这是 W06 刻意的设计，不是遗漏」）。因此 `tests/integration/test_evidence_service.py:45-67` 的 autouse 清理夹具**必须**走 `admin_client`（`database_id="dg_admin"`），才能对其中的 `dg_system.evidence` / `dg_system.task_events` 发 DELETE。

> 该设计被 `docs/w07/07-gate-review.md:111` 明确保护：「不得为『好清理』而放宽 `dg_ctl`」。

### 2.3 1045 具体发生在哪一步（本轮 traceback 原文链路）

```
tests/integration/test_evidence_service.py:57-59   # teardown：yield 之后，遍历 created
    admin_client.execute("DELETE FROM dg_system.evidence WHERE task_id = %s", (task_id,))
database/mysql/connection.py:184                   # with self.connection() as conn:  ← 在 try 之外
database/mysql/connection.py:165                   # conn = self.connect()
database/mysql/connection.py:152                   # pymysql.connect(**self._connect_params())
    -> pymysql OperationalError(1045, "Access denied for user 'root'@'localhost' (using password: YES)")
database/mysql/connection.py:155-156               # except pymysql.MySQLError -> _raise_connect_error(exc, "dg_admin")
database/mysql/connection.py:59-60                 # 1045 ∈ _AUTH_ERROR_CODES(:30) -> raise DatabaseAuthError
tests/integration/test_evidence_service.py:62      # except DatabaseQueryError: pass  ← 抓不到
core/errors.py:106-110 / :134-138                  # DatabaseAuthError 与 DatabaseQueryError 是兄弟类（同继承 DBGuardianError）
    -> 异常继续上抛，pytest 记为 ERROR at teardown
```

要点：

1. **1045 发生在建连阶段**（`pymysql.connect`），**在任何 SQL 发出之前**。因此这 6 次 teardown 里**一条 DELETE 都没有真正执行**，不存在「部分清理」，残留行完整保留。
2. `admin_client` 夹具本身**没有报错**：它只是懒构造 `MysqlClient`；`settings.admin_dsn` 非 `None`（否则 `tests/conftest.py:71` 会 skip 而不是 error）。
3. 夹具 `:62` 的 `except DatabaseQueryError` 只包住第一条 DELETE，且类型不匹配 `:64-67` 的 5 条 DELETE 更无任何异常保护；但循环根本没被走到（第一条就在建连时炸了）。
4. DSN 写的是 `127.0.0.1`，MySQL 侧按 `'root'@'localhost'` 匹配账号 ⇒ 需要的是 `root`@`localhost` 的**正确口令/认证方式**，不是改主机名。

### 2.4 环境变量只读探测（名称 + 脱敏 DSN，无明文）

```
== DG_* credential-related env key presence (names only, NO values) ==
   DG_ADMIN_DSN -> set          DG_CONTROL_DSN -> set
   DG_CONTROL_PASSWORD -> set   DG_MYSQL_ROOT_PASSWORD -> set
   DG_RO_DSN -> set             DG_RO_PASSWORD -> set
   DG_RW_DSN -> set             DG_RW_PASSWORD -> set
   （另有 DG_PG_* 若干，本轮不涉及）
== redacted DSNs ==
   DG_ADMIN_DSN   -> mysql://root:***@127.0.0.1:3306/mysql
   DG_CONTROL_DSN -> mysql://dg_ctl:***@127.0.0.1:3306/dg_system
   DG_RO_DSN      -> mysql://dg_ro:***@127.0.0.1:3306/dg_lab
   DG_RW_DSN      -> mysql://dg_rw:***@127.0.0.1:3306/dg_lab
```

→ `DG_ADMIN_DSN` **已配置且语法合法**（所以 `Settings()` 通过、`admin_client` 未 skip），问题出在**凭据值在本机 MySQL 8 上认证不通过**。历史记录 `docs/w07/worker-residue.md:57-60`：DSN 自带口令、`DG_MYSQL_ROOT_PASSWORD`、空口令、`root` 四种都试过，仍 1045。本轮**未做额外登录探测**（除 pytest 隐式触发的这一次），未验证 root 口令的真实值。

### 2.5 残留行实测（只读 SELECT，观测窗口 = 本次 `test_evidence_service.py` 复跑前后）

| 表 | 复跑前 | 复跑后 | 增量 |
|---|---|---|---|
| `dg_system.tasks` | 1492 | 1500 | +8 |
| `dg_system.evidence` | 219 | 230 | +11 |
| `dg_system.task_events` | 72403 | 72419 | +16 |
| `dg_system.checkpoints` | 2935 | 2943 | +8 |

`TaskService.create_task`（`core/tasks/service.py:86-100`）每次写 `tasks` 1 行 + `TASK_CREATED` 事件 + 检查点；6 个用例共创建 8 个 task、11 条 evidence，与增量吻合。报告收尾时（另含后续 `tests/unit` 与 pipeline 两次复跑，同样只 INSERT）总量为 `tasks=1504 / evidence=232 / task_events=72432 / checkpoints=2947`。**本轮及既有纪律下未执行任何 DELETE**（`docs/w09/03-task-cards.md:10,292,733`）。

> 探针局限（如实登记）：含 `%` 字面量的 `goal LIKE 'Evidence%'` 类 SELECT 在本次探针中因 pymysql 参数占位解析失败（`DatabaseQueryError`，`mysql_error_code=None`），**未取得按 goal 的分项计数**；上表为全表 COUNT。此为探针写法限制，未据此推断项目缺陷。

---

## ③ 根因判定

**主判定：本机环境凭据缺陷。**
`DG_ADMIN_DSN` 指向的 `root`@`localhost` 口令/认证方式在本机 MySQL 8 上不通过（1045 `Access denied ... (using password: YES)`），使所有依赖 `admin_client` 的清理动作在建连阶段失败。

判定依据：

- **不是「用例设计缺陷」**：6 个用例本体全部 PASSED，断言与 `docs/w09/06-qa-report.md:519-523` 的 `payload_hash` 冻结口径一致，无一条 FAILED、无一条断言失败；`docs/w09/04-baseline.md:99` 已把「把 `DG_ADMIN_DSN` 1045 导致的 fixture teardown ERROR 直接判为新代码缺陷」列为**禁止**项；`docs/w08/07-gate-review.md:76` 亦将 E-2 登记为环境阻塞。
- **不是「测试 fixture 缺陷」为主因，但它是放大器（次要因素）**：`_cleanup_created_tasks` 为 autouse（`:45`）、强依赖 `admin_client`（`:46`）、只捕获 `DatabaseQueryError`（`:62`）而 1045 归一化为兄弟类 `DatabaseAuthError`，且 `:64-67` 无异常保护 ⇒ 凭据失败被升级为 pytest teardown ERROR，而不是像 `tests/integration/test_w08_worker_process.py:168`、`test_worker_process.py:238` 那样降级为显式 `UserWarning`。这属于**清理夹具健壮性改进项**，不是错误断言、不是实现错误。
- **环境凭据是直接触发条件**：控制账号 `dg_ctl` 可连、控制库可读可写（6 个用例本体与 pipeline 4 条均真实连库通过），MySQL 3306 正常；唯独管理员账号认证失败。

**6 个 error 的准确定性**：pytest **teardown 阶段的 fixture 清理 ERROR**（`ERROR at teardown of ...`），由本机管理员凭据不可用引起；**不是 FAILED、不是断言失败、不是实现缺陷**。汇总口径固定为：**用例本体 6 passed，6 个 teardown errors 为环境缺陷**。

补充历史一致性：`docs/w09/06-qa-report.md:23` 的「全程未连库」是 **T01 离线复验**的范围说明（§1.1 复验对象为 `core/redaction/**`），与 §11.1 的 T02 真实库复跑不矛盾，不应据此推断「本机控制库不可连」。

---

## ④ 影响面

| 维度 | 结论 |
|---|---|
| 直接受影响的测试 | 仅 `tests/integration/test_evidence_service.py` 全部 6 个用例（清单见 ①）；**用例本体 6 passed，6 个 teardown errors 为环境缺陷** |
| 潜在同构影响（**源码装配路径推断，本轮未逐一实跑，标注未验证**） | 凡带 `autouse` 且签名含 `admin_client` 的清理夹具都会在同一建连步骤失败：`test_worker_gateway.py`、`test_api_v1.py`、`test_agent_loop.py`、`test_auth_service.py`、`test_auth_api.py`、`test_rbac_api.py`、`test_audit_service.py`、`test_task_evidence_chain.py`、`test_approval_consume.py`、`test_approval_service.py`、`test_step_prepared.py`、`test_diagnosis_flows.py`、`test_w03_integrity.py`、`test_w06_acceptance.py`、`test_worker_process.py` 等（由 `Grep admin_client` 命中确认）。`docs/w08/07-gate-review.md:76` 记录过 135 条 fixture teardown ERROR（历史值，本轮未复现） |
| 明确不受影响 | `tests/unit`（本轮 `1138 passed in 5.35s`）；`tests/integration/test_w09_redaction_pipeline.py`（本轮 `4 passed in 3.75s`，`-rs` 无 skip；该文件 `:5-10` 明确不依赖 `admin_client`、不执行 DELETE、只断言本用例范围内事实） |
| 受影响的表 | 夹具原计划 DELETE 的 6 张：`dg_system.evidence` / `task_events` / `tool_calls` / `task_steps` / `checkpoints` / `tasks`（`test_evidence_service.py:59,64-67`）。本轮实际新增写入落在 `tasks`(+8) / `evidence`(+11) / `task_events`(+16) / `checkpoints`(+8)；`tool_calls`、`task_steps` 本轮未写入 |
| 残留行 | 因 1045 在建连阶段发生，**DELETE 一条都没发出**；残留继续累积。按全局纪律「需要 DB 的用例只 INSERT、不清理；残留行不得 DELETE」（`docs/w09/03-task-cards.md:10,292,733`），本轮**不做任何清理**；历史残留处置须用户另行授权 |
| 对 W09 T03–T06 验收结论 | **不构成变更、也不构成否定**。`docs/w09/06-qa-report.md:606` 明确「T03 / T04 / T05 / T06 **未开工，未验证**」；`:608` 仅把 `test_evidence_service.py` 6 条（**含 teardown error**）列为 T02 真实库证据之一。既然 T03–T06 本轮无验收结论，就不存在「被这 6 个 error 推翻」的问题。需提醒的是：若后续卡沿用 `admin_client` 清理夹具，会把同一环境阻塞带进新卡——`03-task-cards.md:10,733` 已在设计层禁止 |
| `tests/unit` 1138 passed 是否仍可信 | **仍可信，但仅限 unit 层**。证据：① 本轮独立复跑 `1138 passed in 5.35s`；② `tests/conftest.py` 中所有 DB 夹具（`:24-107`）均 session 级、按需实例化、**无 autouse**；③ 全量检索 `tests/unit` 未发现任何用例请求 `admin_client` / `control_client` / `ro_client` / `rw_client` / `settings` 夹具（命中的是同名局部变量、`_FakeReadOnlyClient`、`**settings` 形参等），unit 用例离线；④ 1045 只在 `admin_client` 建连路径出现，unit 不触碰该路径。**不得把 1138 passed 外推为 integration 全量或 W09 整体结论**（`docs/w08/07-gate-review.md:17` 同口径） |

---

## ⑤ 待用户执行动作（本机系统级 / 账号相关操作，按项目规矩由用户本人执行，QA 不代执行）

> 共 **5 条**（4 条配置 + 1 条复验）。所有口令由用户本人掌握，**不要写进 `.env.example`、不要提交 Git、不要在聊天/报告中回传明文**。
> 前置原则：**不得放宽 `dg_ctl` 权限**（W06 刻意设计），**不得对本轮及历史残留行执行 DELETE 清理**（如需处置另立授权清单）。

1. **确认本机 MySQL 8 的 root 登录事实**（用户本人执行）。用能登录的客户端核对 `root`@`localhost` 的认证插件与可用口令，例如：
   ```
   mysql -h 127.0.0.1 -P 3306 -u root -p -e "SELECT CURRENT_USER(), VERSION();"
   ```
   注意 DSN 写 `127.0.0.1`，但 MySQL 按 `'root'@'localhost'` 匹配账号；`docs/w07/worker-residue.md:59-60` 记录 DSN 口令、`DG_MYSQL_ROOT_PASSWORD`、空口令、`root` 四种均不通，故需确认**真实口令来源**（本机安装/容器环境变量/密码管理器），不要猜测。
2. **二选一建立可用管理员凭据**（用户本人执行）：
   - **A（推荐，不动 root）**：新建专用管理员账号，仅授予本场景所需权限，例如
     ```sql
     CREATE USER 'dg_admin'@'localhost' IDENTIFIED BY '<用户自设强口令>';
     GRANT SELECT, DELETE ON dg_system.* TO 'dg_admin'@'localhost';
     ```
     说明：`test_evidence_service.py:59,64-67` 的 6 条 DELETE 只覆盖 `dg_system` 的 6 张表；`GRANT ... ON dg_system.*` 是最小充分集。若该账号还要承担 `scripts/bootstrap_mysql.py:88-168` 的初始化，则需另授予 `CREATE USER` / `GRANT OPTION` / `CREATE DATABASE` 等（**本轮不需要**，仅为复验 teardown）。
   - **B**：修正 `root`@`localhost` 的口令并使用正确口令（不要使用空口令）。
3. **把凭据写入根目录 `.env` 的 `DG_ADMIN_DSN`**（用户本人执行），格式：
   ```
   DG_ADMIN_DSN=mysql://<管理员账号>:CHANGE_ME@127.0.0.1:3306/dg_system?charset=utf8mb4
   ```
   - 上行的 `CHANGE_ME` 由用户替换为**自设口令**（不得留空、不得写入模板或提交）：占位符与
     `.env.example` 同一约定，`scripts/ci_check.ps1` 的密钥扫描按此约定放行占位符。
   - 建议把库名从 `mysql` 改为 `dg_system`：DSN 的 database 会进入 `pymysql.connect(database=...)`（`database/mysql/connection.py:129-142`），改成管理员已被授权的库可避免「对 `mysql` 库无权限」的二次 1044/1045；DELETE 语句本身是全限定名 `dg_system.xxx`，不受影响。
   - 保持 `DG_CONTROL_DSN` / `DG_*_PASSWORD` 原值不变；确认 `.env` 与进程环境变量中 `DG_ADMIN_DSN` 的值一致（进程环境变量优先于 `.env`）。
   - `.env.example:88` 保持 `CHANGE_ME` 占位；`scripts/ci_check.ps1:58` 有占位符校验，勿把真实口令写入模板或提交。
4. **确认权限与纪律未被改动**（用户本人确认）：`dg_ctl` 的授权保持不变（仍是 `SELECT/INSERT/CREATE/ALTER/INDEX/REFERENCES` + 少数状态表的 UPDATE/DELETE）；不为「好清理」加 DELETE；本轮及历史残留行**不做 DELETE**，如需清理由用户另立清单并单独授权。
   > 重要口径冲突提示：一旦 `DG_ADMIN_DSN` 可用，该夹具会**真实执行 6 条 DELETE**（仅针对本次运行 `created` 的 task_id）。这与 W09「只 INSERT、不清理」纪律存在冲突；第 5 条复验只用于**验证凭据可用性**，**不等于授权清理历史残留**。若要长期消除该冲突，优先走 ⑥ 的方案 A（改测试侧，不改系统）。
5. **执行复验并核对输出**（命令与预期见 ⑦）。仍不可用则如实保留 `6 passed, 6 errors`，并把结论按 ⑧ 口径登记，**不要为了让汇总变绿去改测试、放宽断言、加 skip / xfail**（`docs/w09/03-task-cards.md:7`）。

---

## ⑥ 测试侧改进建议（**只写建议，本轮不动手改**；目标：不改系统配置也能让结论更干净）

> 前提：不改变任何断言强度、不删用例、不加 `skip`/`xfail`、不缩小参数化范围（`03-task-cards.md:7`）；不扩大 `dg_ctl` 权限。

- **方案 A（首选，仓库内已有范例）**：让 `test_evidence_service.py` 的清理夹具不再依赖 `admin_client`。照 `tests/integration/test_w09_redaction_pipeline.py:5-10` 的成熟写法——**不执行 DELETE、不断言「全表为空」、只断言本用例自己新建任务范围内的事实**（`count_for_task`）；需要 DB 的用例只 INSERT。这样 teardown 没有 DELETE，1045 无从触发，6 个 error 自然消失，且不改变任何断言强度。该文件属 W03 遗留集成测试，改动需另立任务卡并避开「任一时刻不得有两张卡改同一个文件」（`03-task-cards.md:5,12`）。
- **方案 B（若必须保留清理夹具）**：把 `:62` 的 `except DatabaseQueryError` 扩为捕获 `DBGuardianError`（或至少 `DatabaseAuthError` / `DatabasePermissionError`），并**降级为显式 `UserWarning`，列清未清理的表与 `task_id`，不静默吞异常**——与 `tests/integration/test_worker_process.py:238`、`test_w08_worker_process.py:168` 的既有做法一致。同时给 `:64-67` 的 5 条 DELETE 加同类保护。这样把 ERROR 变为可归因的环境 warning，但**残留行仍在**，只是结论更干净。
- **方案 C（报告/汇总口径）**：在测试报告与 CI 汇总中把 teardown error 单独归类为 `ENV_TEARDOWN_ERROR`，与用例结果**双列并存**（`用例本体 6 passed` / `6 teardown errors 为环境缺陷`），既不合并成「6 failed」、也不抹掉；复跑记录沿用 `docs/w09/06-qa-report.md:519-523` 与 `03-task-cards.md:292` 的登记格式。
- **明确不推荐**：① 用 `control_client` 替换 `admin_client` 做 DELETE——本轮 `information_schema` 实测 `dg_ctl` 在 `evidence` / `task_events` 上**无 DELETE**，替换后只会把 1045 换成权限错误，且违反「残留行不得 DELETE」；② 给 `admin_client` 加 session 级 `ping()` 探针并 `pytest.skip`——这会把 6 个用例变成 skip，直接违反 `03-task-cards.md:7`；③ 任何放宽 `dg_ctl` 授权的做法（`docs/w07/worker-residue.md:54-55`）。

---

## ⑦ 复验命令与预期

> 全部在 `D:/DBGuaredian` 下、用 `.venv/Scripts/python.exe`；**只跑定向文件，禁止全量 pytest**。

主复验（凭据就绪后由用户执行）：

```bash
cd /d/DBGuaredian
timeout 90 .venv/Scripts/python.exe -m pytest tests/integration/test_evidence_service.py -o addopts="" -q -p no:cacheprovider
```

| 场景 | 预期汇总行 | 预期细节 |
|---|---|---|
| 管理员凭据**可用** | `6 passed in <x>s`（0 errors） | `ERRORS` 段消失；不再出现 `DatabaseAuthError` / `1045` / `Access denied for user 'root'@'localhost'`。**注意**：此时夹具会真实执行 6 条 DELETE（仅本次新建的 task_id），历史残留不受影响、也不得据此声称残留清零 |
| 管理员凭据**仍不可用** | `6 passed, 6 errors in <x>s` | 6 条 `ERROR at teardown of ...`，均为 `DatabaseAuthError`；**用例本体 6 passed，6 个 teardown errors 为环境缺陷** |

只读自检（可选，确认凭据是否真的通了，不需要跑测试）：

```bash
cd /d/DBGuaredian
.venv/Scripts/python.exe -c "from dotenv import load_dotenv; load_dotenv('.env'); from core.config import Settings; from database.mysql.connection import MysqlClient; s=Settings(); c=MysqlClient(s.admin_dsn, database_id='dg_admin', read_only=False); print('admin ping ok:', c.ping(), 'ms')"
# 预期：凭据可用时打印 "admin ping ok: <毫秒> ms"；不可用时抛 DatabaseAuthError（1045）
```

对照命令（确认分层证据没有被环境阻塞污染）：

```bash
timeout 90 .venv/Scripts/python.exe -m pytest tests/unit -o addopts="" -q -p no:cacheprovider                          # 预期 1138 passed
timeout 90 .venv/Scripts/python.exe -m pytest tests/integration/test_w09_redaction_pipeline.py -o addopts="" -q -p no:cacheprovider -rs   # 预期 4 passed 且 -rs 无 skip 行
```

留痕：把复跑汇总行、时间戳、`git rev-parse --short HEAD` 追加登记到 QA 报告；若凭据仍不可用，维持本报告的结论口径，**不追求让汇总行变绿**。

---

## ⑧ 结论口径（固定表述）

1. **用例本体 6 passed，6 个 teardown errors 为环境缺陷**；`tests/integration/test_evidence_service.py` 的 6 个 error 全部发生在 teardown，属本机 `DG_ADMIN_DSN`（`root`@`localhost`）认证失败 1045 导致的 fixture 清理 ERROR，**不是 FAILED、不是断言失败、不是实现缺陷**。
2. 根因一句话：`DG_ADMIN_DSN` 配置的管理员凭据在本机 MySQL 8 上认证不通过（1045），而遗留集成测试的 autouse 清理夹具强制用管理员账号执行 DELETE，故 6 次 teardown 在建连阶段即失败——**本机环境凭据缺陷**，清理夹具健壮性不足是放大器。
3. 本轮**未执行任何 DELETE**（DELETE 在建连失败前未发出），残留行按全局纪律「只 INSERT、不清理」如实登记；`tests/unit` 1138 passed 仍可信，但仅限 unit 层，不得外推。
4. W09 T03–T06 在 `docs/w09/06-qa-report.md:606` 已明确「未开工，未验证」，本轮 6 个 teardown error **不改变、也不构成**其验收结论。
5. 沿用 `docs/w09/03-task-cards.md:735` 的表述禁令；本报告**不**作任何修复完成、整体通过、环境可清零一类的声明。本报告是诊断记录，不是修复记录。

---

## ⑨ 未验证项（本轮只读边界内未做，如实登记）

- 未逐一实跑 ④ 中列出的其他 `admin_client` 集成文件（仅做源码装配路径推断）；`docs/w08/07-gate-review.md:76` 的 135 teardown ERROR 为历史值，未复现。
- 未验证 `root`@`localhost` 的真实口令、认证插件与口令来源；未做独立登录探测（除 pytest 隐式触发的一次建连）。
- 未探测 PG 5432、未探测容器运行态（沿用 `docs/w09/04-baseline.md:76` 的登记状态）。
- 未取得按 `goal` 前缀的分项残留计数（探针 `%` 字面量解析失败，见 ② 末注）；未做跨表 JOIN 明细清点。
- 未执行任何账号变更、服务重启、环境变量修改、DELETE 清理或 git 写操作。
