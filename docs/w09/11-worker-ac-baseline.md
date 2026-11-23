# W09｜Worker 载体四条 AC 基线复验（M03，T03 前）

- 执行人：严过关（Yan）· QA 工程师｜任务：M03 / 任务 #5｜语言：简体中文
- 现场：`D:/DBGuaredian`，`git HEAD = 2850eca`，`.venv/Scripts/python.exe` = Python 3.14.7
- 依据：`docs/w09/10-lead-rulings.md` §2 补充裁定 / §4 / §5 / §6；`docs/w09/08-worker-design-and-tasks.md` A.1、A.2、A.8、B.4.2
- 性质：**本轮是新的独立基线实跑**，在 T03 改动之前锁定四条 AC 的 Worker 执行链对照基线；**不等于** W07 GR-4 的「只读数据复核（依赖工程师实跑产物）」
- 唯一写入文件：**本文件 `docs/w09/11-worker-ac-baseline.md`**。未改任何 `.py` / `.sql` / 配置 / 既有 docs（含 `docs/w09/` 下全部未提交文档）；未改数据库账号、未执行 DELETE、未执行 git commit / checkout / reset；未跑无路径全量 pytest；未写 `--timeout=`（仓库未装 pytest-timeout，DB 定向用 Git Bash 外层 `timeout 180`）
- 命令口径：`.venv/Scripts/python.exe -m pytest <定向路径> -o addopts="" -q -p no:cacheprovider`

---

## ① 现象：四条定向命令原文汇总行

> 三条 unit 命令另加外层 `timeout 90` 作保险（不改变任何 pytest 参数）；DB 定向命令按要求加外层 `timeout 180`。补充一条非 pytest 的 AC① 只读冒烟（B.4.2 列出）。

### 命令 1（AC④ 静态守护）

```
$ cd /d/DBGuaredian && timeout 90 .venv/Scripts/python.exe -m pytest tests/unit/test_worker_layering.py -o addopts="" -q -p no:cacheprovider
..........                                                               [100%]
10 passed in 0.06s
EXIT=0
```

### 命令 2（AC④ 分层反向守护）

```
$ cd /d/DBGuaredian && timeout 90 .venv/Scripts/python.exe -m pytest tests/unit/test_worker_isolation.py -o addopts="" -q -p no:cacheprovider
..                                                                       [100%]
2 passed in 0.03s
EXIT=0
```

### 命令 3（AC① 生命周期 / 租约）

```
$ cd /d/DBGuaredian && timeout 90 .venv/Scripts/python.exe -m pytest tests/unit/test_worker_runner.py -o addopts="" -q -p no:cacheprovider
......................................                                   [100%]
38 passed in 0.25s
EXIT=0
```

### 命令 4（AC②③ 端到端，DB 定向）

```
$ cd /d/DBGuaredian && timeout 180 .venv/Scripts/python.exe -m pytest tests/integration/test_worker_process.py::test_worker_process_executes_approved_repair_once -o addopts="" -q -p no:cacheprovider
.                                                                        [100%]
============================== warnings summary ===============================
tests/integration/test_worker_process.py::test_worker_process_executes_approved_repair_once
  D:\DBGuaredian\tests\integration\test_worker_process.py:237: UserWarning: 控制库清理不完整（需可用的 DG_ADMIN_DSN）：task_id=task_e3c52a92c438417e9beba9c302103dc1 未清理表=approval_consumptions,step_observed,step_prepared,tool_calls,execution_envelopes,approvals,task_events,task_steps,worker_leases,checkpoints,evidence,tasks
    warnings.warn(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1 passed, 1 warning in 49.18s
EXIT=0
```

**该 1 warning 的定性**：`tests/integration/test_worker_process.py:228-241` 的 teardown 清理用 `admin_client` 逐表 DELETE，被 `except DBGuardianError`（`:234-235`）降级为 `warnings.warn`，显式列出 12 张未清理表与 `task_id`，**不静默吞**。这是 `DG_ADMIN_DSN` root 1045 的已知环境项，**不是 failed / 不是 error / 不是断言失败**；本轮未执行任何 DELETE 清理，残留按 `docs/w07/worker-residue.md` 如实登记。用例本体 1 passed。

### 补充：AC① 进程入口只读冒烟（非 pytest 用例，B.4.2 列出）

```
$ cd /d/DBGuaredian && timeout 30 .venv/Scripts/python.exe -m workers --help
usage: python -m workers [-h] [--once] [--self-check] [--dry-run]
                         [--worker-id ID] [--tenant TENANT]

DBGuardian 独立 Worker：轮询已批准的执行候选，经唯一 Tool Gateway 执行并收敛结果。

options:
  -h, --help       show this help message and exit
  --once           只处理一轮候选后退出（用于验收与定时任务）
  --self-check     装配执行栈并自检连接/环境/候选数量，打印 JSON 后退出
  --dry-run        只发现并列出候选（不领租约、不提交工具、不写审计）
  --worker-id ID   覆盖 DG_WORKER_ID（租约归属与审计标注）
  --tenant TENANT  覆盖 DG_DEFAULT_TENANT_ID（只处理该租户的候选）
EXIT=0
```

`--help` 在 `workers/cli.py:197-199` 的 `parse_args` 阶段即退出，不触发 `get_settings()` / 不连库，属纯只读。五个 flag 全部可见。

### 汇总

| 命令 | 原文汇总行 | pass/fail | error | skip |
|---|---|---|---|---|
| 1 `test_worker_layering.py` | `10 passed in 0.06s` | 10 passed / 0 failed | 0 | 0 |
| 2 `test_worker_isolation.py` | `2 passed in 0.03s` | 2 passed / 0 failed | 0 | 0 |
| 3 `test_worker_runner.py` | `38 passed in 0.25s` | 38 passed / 0 failed | 0 | 0 |
| 4 `test_worker_process.py::test_worker_process_executes_approved_repair_once` | `1 passed, 1 warning in 49.18s` | 1 passed / 0 failed | 0 | 0 |
| 合计 | — | **51 passed / 0 failed** | **0** | **0** |

> **有无转红项：无。** 四条定向命令全部 pass，无 failed / error / skip；唯一非绿项是 1 条环境项 `UserWarning`（清理降级）。

---

## ② 只读核对证据（源码 / 配置 / 文档，零改动）

| # | 对象 | 文件:行号 | 只读核对结果 |
|---|---|---|---|
| 1 | 生产装配引用 | `workers/stack.py:26` | `from adapters.lab_assembly import build_lab_stack, is_lab_mysql_target` |
| 2 | 生产装配引用 | `workers/stack.py:35` | `from core.gateway.tool_gateway import ToolGateway` |
| 3 | 真实调用（不是导入即忘） | `workers/stack.py:230-243` | `lab_stack = build_lab_stack(...)` 并取出 `registry / repository / audit / approvals / step_records / executor` |
| 4 | 真实构造 | `workers/stack.py:260-271` | `gateway = ToolGateway(registry, repository, lease_service, PolicyEngine(...), executor, principal_resolver, context_factory, settings=settings, approval_service=approvals, audit=audit)` |
| 5 | Gateway 注入 Worker | `workers/stack.py:272-279` | `Worker(..., lease_service, gateway, signing_key=settings.gateway_key())` → 同进程 `GatewayPort`，无跨进程协议 |
| 6 | 进程入口 | `workers/__main__.py:1-10` | `from workers.cli import main`；`if __name__ == "__main__": sys.exit(main())` |
| 7 | CLI 五个 flag | `workers/cli.py:38-73` | `--once` / `--self-check` / `--dry-run` / `--worker-id` / `--tenant` |
| 8 | main 装配与 fail closed | `workers/cli.py:197-252` | `get_settings()` → `build_worker_stack(...)`；装配异常返回 `EXIT_CONFIG_ERROR`（`:213-216`）；`--once` → `runner.run_once()`（`:234-242`） |
| 9 | compose `worker` service | `docker-compose.yml:129-153` | `build: deploy/Dockerfile.worker`；`control_net` + `lab_net`（`:151-153`）、**不含 `model_net`**；`DG_MODEL_PROVIDER: disabled`（`:145`）；`DG_GATEWAY_SIGNING_KEY` 显式注入（`:143`） |
| 10 | compose 遗留占位 | `docker-compose.yml:155-162` | `tool_gateway` 仍 `profiles: ["future"]`，默认不启动（W07 KD-2 有意保留，本轮不动） |
| 11 | 镜像入口 | `deploy/Dockerfile.worker:31` | `CMD ["python", "-m", "workers"]`，无 EXPOSE，`USER dguser` |
| 12 | Gateway 8b / 8c / 9 | `core/gateway/tool_gateway.py:282`、`:311-314`、`:406` | `self._approval_service.consume(...)` → `emit_required(STEP_PREPARED)` → `executor.execute(..., idempotency_key=f"apr:{grant.approval_id}")` |
| 13 | 一次性消费出口 | `core/approval/service.py:563`（`consume`）、`:859`（`APPROVAL_CONSUMED`）、`:874`（`issue_grant`） | 九项校验 → 事务 → 唯一 grant 出口 |
| 14 | AC④ 静态断言 | `tests/unit/test_worker_layering.py:72-89` | `build_lab_stack(` / `ToolGateway(` 必须是调用；`assert "ToolRegistry()" not in source`；`builders == ["stack.py"]`（`:83-89`） |
| 15 | 部署面静态断言 | `tests/unit/test_worker_layering.py:145-152` | `worker` 块含 `control_net`/`lab_net`、不含 `model_net`、`DG_MODEL_PROVIDER: disabled`、含 `DG_GATEWAY_SIGNING_KEY` |
| 16 | AC③ 断言集 | `tests/integration/test_worker_process.py:286-331` | 四表各 1、outcome/status=success、LAB 目标行真被改、`approvals.status=CONSUMED`、`tasks.status=EXECUTING` 且 `state_version == waiting+1` |

工作区状态（**四条命令实跑时点**的 `git status --short` / `git diff --stat HEAD -- '*.py' '*.sql' docker-compose.yml`）：3 份既有 `M` 文档 + 5 份未提交 `?? docs/w09/*` + 未跟踪目录，**受跟踪的源码/配置 diff 为空** → 基线时点源码/配置与 `2850eca` 一致。

**并行事件（本基线实跑之后出现，如实登记）**：本报告定稿前复查 `git status --short`，发现 `tests/integration/test_evidence_service.py` 变为 `M`（`+30 / -19`）。`git diff HEAD` 显示这属并行任务 M01（任务 #4，工程师）按 `10-lead-rulings.md` §4 做的**测试侧夹具改动**：autouse 清理夹具由 `admin_client` 改为 `control_client`、teardown 不再执行 DELETE、改为按 `count_for_task` 只读核对本用例范围内证据行数。该文件**不在本轮四条定向命令内**；复查四个基线测试文件、`workers/stack.py`、`workers/runner.py`、`tests/conftest.py` 的 sha256 与 §6.3 锚值逐一比对**全部未变**，故本基线结论与可比性不受影响。按本任务边界，QA 对该文件**未复跑、未验证其效果、未作判定**；其「改前/改后 `--collect-only -q` 比对与断言集合复核」属 M01/M09 范围。

---

## ③ AC①–④ 逐条判定（T03 前基线）

| AC | 内容（A.1 原样） | 基线判定 | 证据（实跑 + 只读） | 边界 / 未覆盖 |
|---|---|---|---|---|
| **①** | 可从独立 Worker 进程调用 `consume()` 的入口 | **基线成立** | `test_worker_runner.py` `38 passed`（候选只取 `WAITING_APPROVAL`、租约/心跳/停止语义、`run_once`/`run_forever`、`build_parser` 五 flag）；`python -m workers --help` rc=0 且五 flag 齐全；只读链路 `workers/__main__.py:9-10` → `cli.py:197-252` → `stack.py:231/260/272` → `tool_gateway.py:282` → `approval/service.py:563`；AC③ 用例的 `approval_consumptions==1` 间接证实 `consume()` 在真实进程链上被调用 | A.8 列出的 `test_help_lists_worker_flags`、`test_self_check_returns_zero_when_stack_is_available` 两条 pytest 用例**不在本轮四条命令内，未实跑**；`--self-check` 真实 LAB 自检未跑。AC① 的「入口形态 + 生命周期」两层已由上述证据锁定，但不声称「全量覆盖」 |
| **②** | 执行经 `ToolGateway` 且由一次性 `ApprovalGrant` 放行 | **基线成立（端到端单次成功链）** | 命令 4 `1 passed`：`approval_consumptions==1`（一次性消费）、`tool_calls==1`（grant 后仅一次写）、`approvals.status=CONSUMED`、`outcome=success`；只读：`tool_gateway.py:282` 8b → `:311-314` 8c `STEP_PREPARED` → `:406` `idempotency_key=apr:<id>`；`approval/service.py:874` `issue_grant` 唯一出口；`test_worker_layering.py` `10 passed` 证生产装配点唯一（`builders == ["stack.py"]`）、无第二写入口 `ToolRegistry()` | A.8 中 AC② 的**六类拒绝矩阵 / N=8 并发消费恰好 1 成功 / 复用原审批被拒**（`test_w07_rejection_matrix.py`、`test_approval_consume.py`）**不在四条命令内，未实跑**；本基线只锁定「单次成功执行链」 |
| **③** | 端到端可断言 `step_prepared` / `step_observed` / `approval_consumptions` 各 1 行且 `tool_calls = 1` | **基线成立** | 命令 4 `1 passed, 1 warning in 49.18s`。逐条断言随用例成立：`test_worker_process.py:298-301` 四表各 1；`:304-308` `step_observed.outcome=success`；`:309-313` `tool_calls.status=success`；`:315` LAB `seed_orders.note` 真被改；`:317-321` `approvals.status=CONSUMED`；`:325-331` `tasks.status=EXECUTING` 且 `state_version == waiting+1` | 1 条 `UserWarning`（12 张表清理降级，root 1045）为**环境项**，不是失败；残留未 DELETE。A.8 的 `test_dry_run_lists_candidate_without_executing`（dry-run 三表 0 行）**未实跑** |
| **④** | 生产装配点出现 `build_lab_stack` / `ToolGateway` 的真实引用 | **基线成立** | `test_worker_layering.py` `10 passed`（含 `:83-89` `builders == ["stack.py"]`、`:145-152` compose 部署面断言）；`test_worker_isolation.py` `2 passed`（领域层零 DB 依赖、`Worker` 构造器无 DSN 参数）；只读核对 `workers/stack.py:26/35/231/260/272`、`docker-compose.yml:129-153`、`deploy/Dockerfile.worker:31` | 沿用 GR-2 口径：**「生产运行时装配点唯一，测试夹具另计」**，不写成「全仓唯一」（`docs/w07/07-gate-review.md:56`）。NV-1 容器运行态 `NOT_VERIFIED`（0 容器，本轮不列入），`docker compose config` 未执行 |

> 四条 AC 现状满足（与 `10-lead-rulings.md` §2 补充裁定一致），**不是待实现项**。本基线只作 T03 前对照锚，不构成任何「门禁通过」判定。

---

## ④ 与 A.8 测试落点的对应关系

| A.8 行（AC） | A.8 指定的测试文件 / 用例 | 本轮命令 | 对应状态 |
|---|---|---|---|
| ① 进程入口 | `test_worker_process.py::test_help_lists_worker_flags` | 未列入四条命令 | **未实跑**；由补充只读冒烟 `python -m workers --help`（rc=0、五 flag）部分覆盖，但不等价于该 pytest 用例 |
| ① 装配自检 | `test_worker_process.py::test_self_check_returns_zero_when_stack_is_available` | 未列入 | **未实跑**（需真实 LAB） |
| ① 生命周期/租约 | `tests/unit/test_worker_runner.py`（`test_install_signal_handlers_forwards_to_request_stop`、`test_runner_request_stop_prevents_new_work`、`test_run_forever_stops_after_request_stop`、`test_candidate_task_statuses_is_waiting_approval_only`） | **命令 3** | ✅ 全文件 `38 passed`（含上述 4 条）；NV-2 口径不变：Windows 无 POSIX 信号，只声明「接线 + 语义」两层 |
| ② Gateway + grant | `tests/unit/test_worker_layering.py`（`test_stack_module_imports_lab_assembly_and_tool_gateway`、`test_stack_is_the_only_module_building_the_gateway`） | **命令 1** | ✅ `10 passed`，含 `builders == ["stack.py"]`、无 `ToolRegistry()` |
| ② 拒绝优先 | `test_w07_rejection_matrix.py`、`test_approval_consume.py` | 未列入 | **未实跑**（六类拒绝矩阵 / N=8 并发消费） |
| ③ dry-run 零副作用 | `test_worker_process.py::test_dry_run_lists_candidate_without_executing` | 未列入 | **未实跑** |
| ③ 端到端各 1 行 | `test_worker_process.py::test_worker_process_executes_approved_repair_once` | **命令 4** | ✅ `1 passed, 1 warning`（环境项 warning） |
| ④ 部署面 | `test_worker_layering.py`（`test_worker_dockerfile_uses_module_entrypoint`、`test_pyproject_ships_workers_package`、`test_docker_compose_declares_worker_without_model_network`） | **命令 1** | ✅ `10 passed`（三条均在文件内） |
| 分层反向守护 | `tests/unit/test_worker_isolation.py`、`test_worker_layering.py`（`test_core_worker_remains_free_of_database_dependencies`、`test_workers_contain_no_raw_sql`、`test_worker_domain_layer_does_not_import_workers_package`） | **命令 2 + 命令 1** | ✅ `2 passed` + `10 passed` |

> A.8 的「AC③ 环境诚实边界」在本轮复现一致：`approved_repair` fixture 需要真实 MySQL LAB 与 `admin_client` 清理，1045 未解 → 清理降级为 `warnings.warn`（`test_worker_process.py:228-241`），残留如实登记、**绝不 DELETE**、绝不断言「表已清空」。W07 GR-4 的口径是「工程师实跑 4 passed + QA **只读数据复核**」，本轮是**新的独立基线实跑**，两者不得混称。

---

## ⑤ 未验证项（本轮只读边界内未做，如实登记）

1. A.8 中未列入四条命令的 pytest 用例未实跑：`test_help_lists_worker_flags`、`test_self_check_returns_zero_when_stack_is_available`、`test_dry_run_lists_candidate_without_executing`、`test_w07_rejection_matrix.py`（六类拒绝矩阵）、`test_approval_consume.py`（N=8 并发消费 / 复用原审批被拒）。因此 AC② 的「拒绝优先 / 并发一次性」分支、AC① 的 `--self-check` 分支、AC③ 的 dry-run 零副作用分支**未由本基线覆盖**。
2. `docs/w09/08-worker-design-and-tasks.md` B.4.2 中的 `docker compose config` **未执行**：不在本任务四条定向命令内；且该命令会解析 `.env` 并可能回显其中的变量值，本轮只读边界不引入该输出。NV-1 容器运行态 `NOT_VERIFIED`（0 容器），按 `10-lead-rulings.md` §2-5 不列入本轮。
3. `tests/integration/test_evidence_service.py` **本轮未复跑、QA 未改动**。该环境项按上一份报告 `docs/w09/09-env-blocker-report.md` 的固定口径登记（历史基线，见 §⑦-3）。定稿前该文件被并行任务 M01 改动（测试侧夹具去噪），QA 未验证其改后行为、未据此推断凭据或残留状态变化；如需复核由 M09 另行安排。
4. 未验证 T03–T06 改动后的任何行为（本任务就是改动前基线）；未验证 `DG_ADMIN_DSN` 凭据可用性；未做登录/容器探测；未执行任何 DELETE、账号变更、环境变量变更、服务重启。
5. 未跑 `tests/unit` 全量（按 `10-lead-rulings.md` §2-4，逐卡只跑定向文件，单元全量回归由 M09 统一安排一次）。

---

## ⑥ 供 T06 后对照的基线锚（M08 使用）

### 6.1 复跑命令集（M08 请逐字重跑，含外层 timeout）

```bash
cd /d/DBGuaredian
timeout 90  .venv/Scripts/python.exe -m pytest tests/unit/test_worker_layering.py  -o addopts="" -q -p no:cacheprovider
timeout 90  .venv/Scripts/python.exe -m pytest tests/unit/test_worker_isolation.py -o addopts="" -q -p no:cacheprovider
timeout 90  .venv/Scripts/python.exe -m pytest tests/unit/test_worker_runner.py    -o addopts="" -q -p no:cacheprovider
timeout 180 .venv/Scripts/python.exe -m pytest tests/integration/test_worker_process.py::test_worker_process_executes_approved_repair_once -o addopts="" -q -p no:cacheprovider
# 补充只读冒烟（B.4.2）：timeout 30 .venv/Scripts/python.exe -m workers --help
```

### 6.2 基线锚值

| 锚 | 基线值 |
|---|---|
| git HEAD | `2850eca` |
| 解释器 | `.venv/Scripts/python.exe`（Python 3.14.7） |
| 源码/配置相对 HEAD（**四条命令实跑时点**） | `git diff --stat HEAD -- '*.py' '*.sql' docker-compose.yml` 为空；定稿前出现的 `M` 文件仅 `tests/integration/test_evidence_service.py`（M01 并行改动，不在四条命令内，见 §② 与 §6.4-6） |
| 命令 1 | `10 passed in 0.06s` |
| 命令 2 | `2 passed in 0.03s` |
| 命令 3 | `38 passed in 0.25s` |
| 命令 4 | `1 passed, 1 warning in 49.18s`（warning = 12 张表清理降级，环境项） |
| 合计 | `51 passed / 0 failed / 0 error / 0 skip / 1 warning`；耗时允许浮动 |

### 6.3 文件 sha256 锚（只读 `sha256sum`，用于确认哪些文件被 T03–T06 改动）

```
dd41be5c872646284d039ffee675e14b7319502b17b6b00f1d3d411c91e97262 *tests/unit/test_worker_layering.py
ac47d3e4d1c69059d4ae11ebbfad708cb4b90f9352cc1b5c2f5e0fca26bad02d *tests/unit/test_worker_isolation.py
7a222efe872e6e71329e5798aa91c5882d3bdb208f83b7d693dc713613184fdd *tests/unit/test_worker_runner.py
5a2c69bfc816bbc75bbda22e040b36669ac14de7c78c971ef53cd2c31ccd494b *tests/integration/test_worker_process.py
a09dfbf51789be8e70727eaeaaea9995867b1bfdb2c29e3f726b2096ecbe6227 *workers/stack.py
fb31a6c0a0bf691c565859d8d2f032fd653e4f09a71947db136b3a73d5d372f8 *workers/runner.py
fdc182af6e838a74ce852f9197bf055bfff18d6d1973b692e26a1d247cff0ff7 *workers/__main__.py
c117d9cbf4800e53cb341c14313ca831445e88f596adfbd05d73aa125a0ec407 *workers/cli.py
b56bb8b33f6e67f375d29ac4a319592f77fc3a53380c9d9d5ecc66d1eaf4dd91 *core/approval/service.py
75fa398597cdfccffe88526089e85a230f416e4bd002b1d88747cc3e81a3455f *core/gateway/tool_gateway.py
72b400fb60f3fc606956c97630c078601f6056fedf230b6e0055ddd9ef12e5bf *docker-compose.yml
80e60caad414d774acc84f4a035ab91e302961f32ecc1f9ce515fea8bf3ad9bf *deploy/Dockerfile.worker
```

**预期会被后续卡改动的文件**（正是本基线要隔离的变量，非异常）：`core/approval/service.py`（M04 / T03 的 KD-1）、`workers/runner.py` + `core/execution/reconciler.py` + `core/audit/service.py` + `tests/unit/test_worker_scheduler.py`（M07 / T06）、`docker-compose.yml`（M09 若授权落地 compose 方案）。`workers/stack.py` 归 T02 独占，按 `10-lead-rulings.md` §2 补充裁定本轮**零写入**，M08 若发现其 hash 变化须立即回报主理人。

### 6.4 归因规则（供 M08 判定「T03–T06 未破坏 W07 载体」）

1. M08 重跑同一命令集，比较**汇总行的 pass/fail/error/skip 计数**（允许耗时浮动），不以耗时判绿/红。
2. 基线在 `2850eca` 源码状态下四条全绿；若 M08 出现 failed / error / 新增 skip，且 `workers/stack.py` / 四个测试文件 hash 未变，则可**明确归因于 T03–T06 的改动面**（`core/approval/service.py`、`workers/runner.py` 等），**而非载体本身**，按 `08-worker-design-and-tasks.md` §B.9-R4 处理。
3. 若 `workers/stack.py` 或四个测试文件的 hash 变了，先排除「测试被放宽 / 断言被改 / 用例被删」再归因；不得为让结果变绿放宽断言、删用例、加 skip/xfail。
4. 命令 4 的 1 条 `UserWarning` 属环境项，M08 若仍是 1 条（或若 M01 完成后环境噪声变化）须单独说明，**不得**把 warning 记作失败，也**不得**因为 warning 消失就推断凭据已可用或残留已清。
5. 命令 4 会真实写库（只 INSERT + 用例 finally 恢复 LAB 目标行），每次 `task_id` 唯一、断言均为「按本任务 id 计数」，因此两次基线之间残留行累加**不影响**可比性；M08 不得据此断言「表已清空」或执行 DELETE。
6. M01（任务 #4）已在本基线实跑之后**并发改动** `tests/integration/test_evidence_service.py`（测试侧清理夹具不再依赖 `admin_client`、不再 DELETE），该文件**不在**本四条命令内，四个基线测试文件与 `workers/stack.py` / `workers/runner.py` / `tests/conftest.py` 的 hash 复查均未变 → **不改变本基线的可比性**。M08 须注明这是**测试侧去噪**，不是环境凭据修复；该文件改后是否仍为 6 passed / 0 errors 须由 M01/M09 另行复核，本基线不代判。

---

## ⑦ 结论口径（表述禁令遵守）

1. **W07**：写「**GR-0 有条件放行；门禁通过：否**」（`docs/w07/07-gate-review.md:11,20`）。本轮是**新的独立基线实跑**，不等于 W07 GR-4 的「只读数据复核（依赖工程师实跑产物）」；W07 两份历史冻结件仍缺，按 KD-5 登记为已知偏离。
2. **W08**：写「**有条件放行**」（`docs/w08/07-gate-review.md:10`）。
3. **环境项**：`test_evidence_service.py` 沿用上一份报告固定表述 —— **用例本体 6 passed、6 个 teardown errors 为环境缺陷**（历史基线口径，本轮未复跑该文件，不因定稿前 M01 的并行测试侧改动而改写）；`DG_ADMIN_DSN` root 1045 未解，本轮命令 4 的 1 条 `UserWarning` 同源。残留行按只追加纪律如实登记，未执行任何 DELETE。
4. 四条 AC 现状已满足、非待实现项；本文件只作 T03 前对照锚，**不作任何门禁通过、整体回归通过或环境恢复一类的声明**。
5. 沿用 `docs/w09/10-lead-rulings.md` §6 表述禁令；本文件未使用其中禁止的措辞（写入前已自检）。

---

## ⑧ 本轮读写范围自检

| 项 | 状态 |
|---|---|
| 新建文件 | **仅** `docs/w09/11-worker-ac-baseline.md` |
| **本轮 QA** 修改 `.py` / `.sql` / 配置 / `docker-compose.yml` / 既有 docs | **0**（四条命令实跑时点 `git diff --stat HEAD -- '*.py' '*.sql' docker-compose.yml` 为空；3 份既有 `M` 文档与 5 份 `??` 文档均未触碰）。定稿前工作区出现的 `M tests/integration/test_evidence_service.py` 系并行任务 M01（工程师）所写，**非本轮 QA 所为**，已在 §② / §6.4-6 如实登记 |
| 改数据库账号 / 环境变量 / 重启服务 / 执行 DELETE | **0**（命令 4 的用例自身只 INSERT + 恢复 LAB 目标行，清理降级为 warning） |
| git commit / checkout / reset / add | **0** |
| 无路径全量 pytest / `--timeout=` | **0**（四条均为定向路径；DB 定向用外层 `timeout 180`） |
| 放宽断言 / 删用例 / 加 skip / xfail | **0**（四条命令原样执行，无失败项） |
| 与其他成员直连通信 | **0**（仅回传 team-lead） |
