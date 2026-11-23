# 项目文件清理清单

- 扫描时间：2026-11-08 23:20（GMT+8）
- 扫描范围：`D:\DBGuaredian` 全目录，排除 `.venv` 与 `.git` 内部
- 运行时：Python 3.14.7；测试基线 660 项（单测 452 项全通过）
- 状态：**只扫描，未删除任何文件**。本清单供人工确认后执行。

## 一、结论

项目本身干净，不存在需要"抢救式"清理的问题：

| 检查项 | 结果 |
|---|---|
| 内容重复文件（>1 KB，按 MD5） | 0 组 |
| 临时/备份文件（`*.tmp`/`*.bak`/`*.orig`/`~$*`/`.DS_Store`） | 0 个 |
| 死代码（116 个模块全量比对引用） | 0 个 |
| 孤儿 `.pyc`（无对应 `.py`） | 0 个 |
| 可清理的缓存与构建产物 | 32 个目录 / 254 个文件 / 约 3.3 MB |

真正需要处理的不是垃圾文件，而是**28 个规划文档从未纳入版本控制**（见第五节）。

## 二、A 类：确定无用，可安全删除

全部已被 `.gitignore` 覆盖，删除后由工具链自动重建。

| 对象 | 数量 | 体积 | 重建方式 |
|---|---|---|---|
| `__pycache__` 目录 | 32 个 / 254 个 `.pyc` | 3,259 KB（磁盘约 3.8 MB） | 下次导入模块时自动生成 |
| `.pytest_cache/` | 1 个 | 75 KB | 下次 `pytest` 自动生成 |
| `dbguardian.egg-info/` | 1 个 | 23 KB | 重装 `pip install -e .` 时生成 |
| `.screenshots/.tmp/` | 1 个空目录 | 0 | 无（11-06 截图工具残留） |
| `deploy/environment-manifest.mysql.json`<br>`deploy/environment-manifest.postgresql.json` | 2 个 | 2 KB | `scripts/environment_manifest.py` 重新生成 |

合计约 **3.3 MB**（磁盘占用约 3.8 MB）。

### 2.1 `__pycache__` 明细

| 目录 | pyc 数 | 其中旧版 | 体积 |
|---|---|---|---|
| `tests/integration/__pycache__` | 43 | 4 | 1,107 KB |
| `tests/unit/__pycache__` | 68 | 5 | 1,085 KB |
| `database/observers/__pycache__` | 18 | 9 | 119 KB |
| `adapters/postgres_mcp/__pycache__` | 6 | 0 | 90 KB |
| `core/diagnosis/__pycache__` | 5 | 0 | 84 KB |
| `core/agent/__pycache__` | 6 | 0 | 74 KB |
| `core/tasks/__pycache__` | 5 | 0 | 65 KB |
| `core/__pycache__` | 8 | 4 | 55 KB |
| `benchmarks/faults/__pycache__` | 3 | 0 | 49 KB |
| `database/mysql/__pycache__` | 8 | 4 | 46 KB |
| `apps/api/routes/__pycache__` | 6 | 0 | 44 KB |
| `core/tools/__pycache__` | 7 | 3 | 43 KB |
| `scripts/__pycache__` | 8 | 0 | 42 KB |
| `core/gateway/__pycache__` | 4 | 0 | 42 KB |
| `core/security/__pycache__` | 7 | 0 | 41 KB |
| 其余 17 个目录 | 52 | 5 | 273 KB |

**34 个 `.cpython-312.pyc` 是切换运行时前的化石**（当前为 3.14.7），分布在 `database/observers`（9）、`tests/unit`（5）、`tests/integration`（4）、`core`（4）、`database/mysql`（4）、`core/tools`（3）、`apps/api`（2）、`tests`（1）、`database`（1）、`apps`（1）。这批确定无任何用途。

## 三、B 类：不是垃圾，需要业务决策

| 对象 | 规模 | 情况 | 建议 |
|---|---|---|---|
| `runs/` | 50 个目录 / 50 个 JSON / 612 KB | Agent 轨迹记录。最新一条生成于本次测试运行（22:50），说明**跑一次测试就会新增一条**，长期会无限增长 | 保留；后续在 W10 明确轨迹保留策略（按任务数或天数封顶） |
| `deploy/pg_lab/data` | 65 MB | PostgreSQL LAB 数据目录，含角色、schema、种子数据。删除后需 `initdb` + `bootstrap_pg_lab.py` + 重建角色 | 保留（重建成本高于收益） |
| `项目规划/项目规划1.0` | 1 个 docx / 68 KB | V1.0 完整研发总纲，已被 V2.2 取代 | 归档到 `archive/`，不删除 |
| `项目规划/项目规划1.1` | 14 个 docx / 596 KB | 上一版 12 周计划。README 明确"1.1 基线代码作为可复用资产保留"，文档同理 | 归档，与 1.0 同处 |
| `apps/web/` | 仅 `.gitkeep` | 开发规范中定义为"展示层（后续周次）"，尚未开工 | 保留，维持目录契约 |
| `workers/` | 仅 `.gitkeep` | 规范写"后台 Worker（W05 起）"，但实际代码在 `core/worker/`，两处职责重叠 | 建议 W06 定夺：要么迁移代码，要么改为 `README` 说明指针 |
| `datasets/` | 仅 `.gitkeep` | W10 数据集目录，尚未使用 | 保留 |

## 四、看着像无用、实际在用的（不要删）

| 对象 | 实际引用方 |
|---|---|
| `benchmarks/faults/assertions.py` | `tests/unit/test_fault_assertions.py`（全量 Manifest 断言可执行性校验） |
| `database/observers/server_info.py` | `database/observers/__init__.py` 导入并注册 `GetDbVersionTool`、`GetDbSettingsTool` |
| `scripts/bootstrap_pg_lab.py` | `scripts/start_lab.ps1` 第 86 行调用 |
| `scripts/demo_agent_run.py` | 手动演示入口，无自动引用属正常 |
| `requirements.lock.txt` | 运行时依赖锁，`ci_check.ps1` 用于比对 |

## 五、版本控制缺口（优先级高于清理缓存）

`git ls-files 项目规划` 返回 0：**28 个规划文档全部未纳入版本控制，磁盘上是唯一副本。**

当前工作区状态：

- 已修改文件 18 个（W05 对 `config.py`、`policy` 相关、`executor.py` 等的改动）
- 未跟踪条目 65 个，构成：规划 docx 28 个、W05 源码 32 个（`core/auth`、`core/security`、`core/gateway`、`core/worker`、`apps/api/security.py`、`apps/api/routes/auth.py`）、SQL/MD 等 5 个
- 最近一次提交停在 W04（`0b30043`）

也就是说，W05 整周成果与全部规划文档都不在 Git 里。建议在任何清理动作之前先完成提交。

## 六、依赖与文档漂移

| 问题 | 证据 | 影响 |
|---|---|---|
| `cryptography>=42.0` 声明但零引用 | 全仓库仅出现在 `pyproject.toml` 与 `requirements.lock.txt`，无任何 `import` | 无功能影响；密码哈希最终用标准库 `hashlib.scrypt`，该 C 扩展依赖为遗留 |
| `jsonschema` 属生产依赖但只被测试使用 | 仅 `tests/unit/test_dependency_contract.py`、`test_environment_manifest_schema.py` | 生产安装多带 4 个包（含 `rpds-py` 二进制） |
| README 测试数漂移 | README 记 `654 passed`，实际收集 660 项 | W05 新增 6 项未回写；不影响运行 |

## 七、建议执行顺序

1. **先提交**：把 28 个 docx 与 W05 源码纳入 Git（消除唯一副本风险）
2. **再清缓存**：删除 A 类 6 项，约回收 3.3 MB，零风险
3. **后归档**：`项目规划1.0`、`1.1` 移入 `archive/`，根目录只留 2.2 版
4. **最后修依赖**：移除 `cryptography`、`jsonschema` 移入 `[dev]`、README 数字改 660 —— 建议与 W06 改造合并提交，避免零散改动

## 八、复现扫描的命令

以下命令只读，不修改文件。

```powershell
# 缓存与 pyc 总量（应为 32 个目录、254 个 pyc）
$pyc = Get-ChildItem -Recurse -Filter *.pyc |
  Where-Object { $_.FullName -notlike "*\.venv\*" }
"pyc 数: $($pyc.Count)"
"合计字节: $(($pyc | Measure-Object Length -Sum).Sum)"

# 旧版本 pyc（应为 34 个；注意 -Filter 不支持字符类，须用 -match）
$old = Get-ChildItem -Recurse -Filter *.pyc |
  Where-Object { $_.FullName -notlike "*\.venv\*" -and $_.Name -match "cpython-31[23]" }
"旧版 pyc: $($old.Count)"

# 内容重复文件（应为 0 组）
Get-ChildItem -Recurse -File |
  Where-Object { $_.Length -gt 1KB -and $_.FullName -notlike "*\.venv\*" } |
  ForEach-Object { Get-FileHash $_.FullName -Algorithm MD5 } |
  Group-Object Hash | Where-Object Count -gt 1
```

确认清理时，建议先用 `-WhatIf` 预览：

```powershell
Get-ChildItem -Recurse -Directory -Filter __pycache__ |
  Where-Object { $_.FullName -notlike "*\.venv\*" } |
  Remove-Item -Recurse -Force -WhatIf
```

## 附录：仓库体积构成

| 部分 | 体积 | 占比 |
|---|---|---|
| `.venv/` | 66 MB | 48% |
| `deploy/pg_lab/data/` | 65 MB | 47% |
| 源码 + 测试 + 文档 + 规划 | 3.6 MB | 3% |
| 缓存（A 类） | 3.3 MB | 2% |

清理后不影响任何已通过测试：缓存全部可再生，`.venv` 与 LAB 数据目录不在清理范围内。
