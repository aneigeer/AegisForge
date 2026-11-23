# W07 冻结事实索引（追溯补记）

> ## 本件是追溯补记件，不是 2026-11-11 冻结时刻的原始证据
>
> - **补记日期**：2026-11-17（W07 收口于 2026-11-12，本件晚于冻结时刻 `T0 = 2026-11-11T23:52:44+08:00` **6 个日历日**）
> - **性质**：本件是**冻结事实索引**——把已经客观存在、可随时复算的事实（tag、sha256、W06 冻结向量、迁移文件、提交口径）逐条登记到一处；**它不是冻结动作本身**。
> - **不可追溯生成**：2026-11-17 补写的文件**不可能**等于 2026-11-11 冻结时刻的证据。因此**本件不声称 W07 在冻结时刻已存在该件**，也不得被引用为「W07 冻件在 T0 已齐」的证明。
> - **效力边界**：本件不改变任何既有判定。W07 的口径固定为 **「有条件放行；门禁通过：否」**（`docs/w07/07-gate-review.md:11` GR-0 唯一结论行）。本件的存在**不使** W07 变成「冻结四件套齐全」。

---

## §0 本件性质与 KD-5 关闭说明

### 0.1 原裁决原文（逐字引用，不得改写）

`docs/w07/07-gate-review.md:105` 登记 KD-5：

> | KD-5 | W07 冻结四件套缺件 | `docs/w07/05-freeze.md` 与 `06-conformance-audit.md` **不存在**（W06 有；W07 现有 `00/01/02` + `worker-residue.md` + `07-gate-review.md` + `08-pm-ruling.md`）。**裁决：本轮不补，如实登记 + W08 首日承接。** W07 冻结向量以 `docs/w06/05-freeze.md` 的 `action_digest` 基准（W06 冻结值逐字节未变，C4 已验）+ `007` 迁移为锚，**不声称 W07 已有独立冻结四件套** |

该行的三条要点，本件逐条承接、不做任何放宽：

1. 两件确实**不存在**——这不是记录疏漏，是 W07 收口时的既成事实；
2. 裁决是「**本轮不补**」+「W08 首日承接」；
3. W07 冻结向量**以 W06 基准 + `007` 迁移为锚**，**不声称**已有独立冻结四件套。

### 0.2 W08 未承接的事实

- W08 未执行 KD-5 承接：全仓检索表明，`05-freeze` / `06-conformance-audit` 两类文件名**只有 W06 存在**（`git ls-files | grep -E "05-freeze|06-conformance"` → 仅 `docs/w06/05-freeze.md`、`docs/w06/06-conformance-audit.md`）。
- `docs/w08/` 现有文件为 `01-prd.md`、`02-design.md`、`03-task-cards.md`、`06-qa-report.md`、`07-gate-review.md`、`08-drill-report.md`，**无 05/06 两件**；W08 之后 W09–W12 直接推进，KD-5 自 2026-11-12 起**持续挂账**。
- 因此本次补记**不是「按期补齐」**，而是「**超期追溯补记**」——该定性必须与文件同时留存，否则等于用一份晚到的文档掩盖一次未执行的承接动作。

### 0.3 本件的效力边界（四条硬约束）

| # | 边界 | 说明 |
|---|---|---|
| E-1 | **不代表冻结时刻** | 本件全部内容均为「事后可复算的客观事实」，不包含任何只有冻结时刻才能产生的证据（如当时的 mtime 快照、当时的工作区状态）。 |
| E-2 | **不声称「W07 已有独立冻结向量」** | W07 未新增独立 `action_digest` 冻结向量；向量基准引自 `docs/w06/05-freeze.md`（见 §2）。 |
| E-3 | **不改变 GR-0** | W07 门禁结论仍为「**有条件放行；门禁通过：否**」，本件不构成放行依据、不解除 C-1..C-4 之外的任何条件。 |
| E-4 | **不覆盖 KD-1..KD-4** | 已知偏离 KD-1..KD-4 的效力完全由 `docs/w07/07-gate-review.md:99-105` 与 `docs/w07/02-design.md:859-878` 决定，本件不重述、不改判。 |

### 0.4 KD-5 的关闭口径

KD-5 登记的是「**缺件**」，不是「门禁不通过」。本次以两件补记件关闭它：

| 件 | 作用 | 关闭的是 |
|---|---|---|
| `docs/w07/05-freeze.md`（本件） | 冻结**事实索引**：锚点 / 哈希 / 向量基准 / 提交口径 | 「冻结事实无单点登记」 |
| `docs/w07/06-conformance-audit.md` | 对 tag `w07-close` 快照的**实施期符合性审计** | 「实现与设计无逐项对齐记录」 |

**关闭后仍必须保留的表述**：W07 是**追溯补记**，不是「冻结时刻四件套齐全」；KD-5 的正确状态是「**已补记（追溯），非原始冻结件**」。

---

## §1 冻结锚点

| 项 | 值 | 取法 |
|---|---|---|
| 锚点 tag | `w07-close` | `git rev-parse w07-close` |
| tag 指向提交 | `c8a739ad9ff1dabdcf4908e353bb11baacd30075` | `git rev-parse w07-close`（**补记时实测值，仅作记录；取锚点一律用命令，不写死 hash**） |
| 提交时间 | `2026-11-12 14:14:01 +0800` | `git show -s --format='%ci' w07-close` |
| 提交信息 | `W07-close: 门禁件 §7 锚点校准与 P6 后表述修正` | `git show -s --format='%s' w07-close` |
| 冻结时刻 | `T0 = 2026-11-11T23:52:44+08:00` | `docs/w07/07-gate-review.md:143` |
| tag 内 `docs/w07/` 文件数 | **6 个**：`00-action-spec.md`、`01-prd.md`、`02-design.md`、`07-gate-review.md`、`08-pm-ruling.md`、`worker-residue.md` | `git ls-tree -r --name-only w07-close docs/w07/` |

**「以 tag 为锚，不以工作区快照为锚」**（沿用 `docs/w07/07-gate-review.md:144`）：

- tag 内 `docs/w07/` **没有 `05-freeze.md`、没有 `06-conformance-audit.md`** —— 这就是 KD-5 的客观事实，也是本件必须写成「补记」而非「原件」的根因。
- 补记时（2026-11-17）的工作区已包含 W08–W12 的全部改动（`workers/runner.py` `+408/−6`、`workers/stack.py` `+86/−1`、`core/execution/reconciler.py` `+158/−3` 等，见 §3.3）。**任何以工作区内容代替 tag 内容的核对，都会把后来者的改动当成 W07 的交付**，故本件与 `06-conformance-audit.md` 的证据一律取自 `git show w07-close:<path>`。
- 取锚点的唯一合法写法：`git rev-parse w07-close`；**不写死 commit hash**（复用 `07-gate-review.md:144/:160` 的口径）。

---

## §2 冻结向量（引自 W06，W07 未新增）

### 2.1 `action_digest` 基准

| 项 | 值 | 来源 |
|---|---|---|
| `action_digest` | `f31f153767a028a758bb3ef02fb9322422e8fc0ff77c287ad459893f7a8d5bc2` | `docs/w06/05-freeze.md:135` |
| `parameters_digest` | `195322569497093bcfc59f0025575f18a24d24c163878f02f0ae83d71e45c904` | `docs/w06/05-freeze.md:137` |
| `target_objects_digest` | `6f14bf084071058b294d76e9ea2f372822d8f637c6bd64ef4287471d7eb3eb59` | `docs/w06/05-freeze.md:138` |
| `prerequisites_digest`（W06 冻结三项输入） | `c5b4b8175724de5a55a16772a1bffc6da2276950f69c37d695aa0e78bd64d0dc` | `docs/w06/05-freeze.md:161` |
| `ACTION_DIGEST_VERSION` | `w06-v1`（**未升版**） | `git show w07-close:core/approval/digest.py:24` |

**口径（不得放宽）**：

- W06 冻结值**逐字节未变**（`docs/w07/07-gate-review.md:105` 已记「C4 已验」），因此 W07 复用 W06 基准即可，**无需重算、也不得宣称重算**。
- `DESCRIPTOR_KEYS`（`core/approval/digest.py:28`）未改；三节披露（风险等级 / 补偿方案 / 验证计划）**不进 `action_descriptor`**，只进 `disclosure_json` 并单独取 `disclosure_digest`（`docs/w07/02-design.md:19`、`:788`）。
- `_CHECK_SOURCES` 由 `("server_probe","control_db")` 扩为含 `target_probe`（`core/approval/digest.py:492`），属**增量允许值**：既有 5 条检查的取值不变，W06 冻结输入形态与摘要均不变（设计已预登记，`docs/w07/02-design.md:192`）。

### 2.2 W07 的向量锚：`007` 迁移 + 回滚

| 件 | 内容 | 证据 |
|---|---|---|
| 正向迁移 | `dg_system.approvals` 增 `disclosure_json` / `disclosure_digest` / `disclosure_version` / `disclosed_at` 四列 + 索引 `idx_approvals_disclosure_digest` | `git show w07-close:database/migrations/007_w07_disclosure.sql:9-14` |
| 回滚 | 删索引与四列，并 `DELETE FROM dg_system.schema_migrations WHERE version='007_w07_disclosure'`；**非幂等、必须由 `DG_ADMIN_DSN` 人工执行**（`dg_ctl` 对 `schema_migrations` 只有 SELECT/INSERT） | `git show w07-close:database/migrations/rollback/007_rollback.sql:1-19` |
| 版本登记 | `docs/migrations.md` 版本表含 `007_w07_disclosure` 一行 | `git show w07-close:docs/migrations.md:13`（回滚说明 `:82`） |
| 完整性约束 | 披露完整性由「`disclosure_digest = canonical_digest(disclosure_json)` 三处重算比对」+「repository UPDATE 列白名单不含这四列」共同保证，不依赖触发器 | `007_w07_disclosure.sql:1-7` |

**W07 未新增独立冻结向量**这一事实，在本表中体现为：W07 的冻结对象是**列 + 摘要 + 迁移**，而不是新的 `action_digest` 向量集。任何「W07 已有新冻结向量」的表述均不成立。

---

## §3 文件哈希基准

### 3.1 复算方式（可逐条重放）

```bash
git rev-parse w07-close                                   # 取锚点，不写死 hash
git show w07-close:<path> | sha256sum                     # 逐文件复算
```

### 3.2 七个基准值（tag 内复算 vs `07-gate-review.md` 记载）

| # | 文件 | tag 内复算 sha256 | 大小 / 行数 | `07-gate-review.md:150-158` 记载 | 对照 |
|---|---|---|---|---|---|
| 1 | `workers/runner.py` | `fe2d3bd4ef9fd20f708dfb1bc272db220afdcb312a7188e9d85038e489890907` | 17,465 B / 465 行 | 同值 | **一致** |
| 2 | `workers/stack.py` | `e258ebcdf9300fdadf344e984907109c753e941500b504e82598c8c7efe14d49` | 9,897 B / 264 行 | 同值 | **一致** |
| 3 | `workers/discovery.py` | `dc6c016207a422ec725c1f849f475ddcfb3e0967166c3fd5d1f6ca93012f0723` | 10,933 B / 315 行 | 同值 | **一致** |
| 4 | `tests/unit/test_worker_runner.py` | `7a222efe872e6e71329e5798aa91c5882d3bdb208f83b7d693dc713613184fdd` | 36,039 B / 1,083 行 | 同值 | **一致** |
| 5 | `tests/integration/test_worker_process.py` | `5a2c69bfc816bbc75bbda22e040b36669ac14de7c78c971ef53cd2c31ccd494b` | 12,773 B / 331 行 | 同值 | **一致** |
| 6 | `workers/README.md` | `6cdeac106c4712eeb3ad1f9cca97333d1f8e8b63ada5f1c6f44b9d43726bb20a` | 8,011 B / 102 行 | **tag 内实际值**同值；该行另记「QA 复核时基准 `c74cc3fc…`（7,504 B，P6 更正前版本）」 | **一致**（详见 §3.4） |
| 7 | `docs/w07/worker-residue.md` | `6fd7a1b14ca69090aea3c40b1b3ab523786946e2f8f51bb41b55bc8ea3057469` | 6,707 B / 110 行 | 同值 | **一致** |

**结论**：7 个文件在 tag `w07-close` 内的 sha256 **逐条与 `07-gate-review.md:150-158` 记载完全相同**，大小/行数与记载亦一致（`workers/README.md` 的 8,011 B / 102 行与 `:157` 记载一致）。**无一处差异需要标注。**

### 3.3 为什么必须用 tag 复算（工作区已污染）

补记时工作区相对 tag 的代码改动（`git diff --stat w07-close -- workers core adapters database docker-compose.yml tests apps`）包含 W08–W12 的 73 个文件、+13,832/−176 行，其中与本件相关的就有：

| 文件 | 相对 tag 的变化（`git diff --numstat w07-close -- <path>`） |
|---|---|
| `workers/runner.py` | `+408 / −6` |
| `workers/stack.py` | `+86 / −1` |
| `core/execution/reconciler.py` | `+158 / −3` |
| `core/approval/repository.py` | `+114 / −54` |
| `core/approval/service.py` | `+76 / −3` |
| `tests/unit/test_worker_scheduler.py` | `+901 / −0`（W08 新增文件） |

→ 若对工作区文件取 sha256，**必然全部不匹配**，且不匹配的原因与 W07 无关。因此本表**只承认 `git show w07-close:<path> | sha256sum`**。

### 3.4 `workers/README.md` 两个值的说明（沿用 `07-gate-review.md:157/:160`）

- **tag 内实际值**：`6cdeac10…`（8,011 B / 102 行，已含 P6 的 NV-1 三段式更正）。
- **QA 复核时基准**：`c74cc3fc…`（7,504 B，P6 更正前版本）。
- 二者不是矛盾，是**同一文件的更正前后两态**；本次复算确认 tag 内为**更正后**的 `6cdeac10…`。该文件不在 T0 冻结的代码/测试哈希核对范围内，其变更不影响其余 6 个文件的冻结效力（`07-gate-review.md:83/:157`）。

---

## §4 提交与交付口径（如实登记，含不被采纳的做法）

| # | 口径 | 来源 |
|---|---|---|
| P-1 | **逐路径 `git add -- <路径>`**；禁用 `-A` / `-u` / 目录形式（`git add -- docs/w07/` 属首次入库的历史写法，后续追加提交不再使用） | `07-gate-review.md:146` |
| P-2 | **tag 前移口径**：定稿入库后 `w07-close` 前移；取锚点用 `git rev-parse w07-close`，**不写死 hash** | `07-gate-review.md:144/:145/:160` |
| P-3 | **`d937b77` 为首次入库提交，不 amend、不删除**（初版 157 行，缺 KD-4/KD-5 与 §7 提交口径） | `07-gate-review.md:145` |
| P-4 | 提交范围**排除 `.workbuddy/`**（WorkBuddy 项目数据，不删也不提交）与 **`项目规划/`**（28 份 docx 磁盘唯一副本，单独处理） | `07-gate-review.md:29`（C-4） |
| P-5 | **T0 冻结只适用于代码与已冻结文档**，不覆盖治理文档的后续追加（本复核件、PM 签署件、附 C 更正均属此类） | `07-gate-review.md:146` |
| P-6 | `deploy/environment-manifest.*.json` 的 `migration_version` 变更**不入库**（`.gitignore` 既有忽略策略），版本权威来源是 `007` 迁移 + 回滚 + `docs/migrations.md` | `07-gate-review.md:104`（KD-4） |
| P-7 | **本件与 `06-conformance-audit.md` 是 2026-11-17 的补记件，不在 `tag w07-close` 内**；tag 不因本件前移，也不得声称「tag 已含冻结四件套」 | 本件 §0、§1 |

P-7 是本件最关键的口径：补记件**不进入**已被 QA 复核过的 tag 快照。若需要把它们纳入版本锚点，应由主理人另起提交并**显式声明 tag 内容已变**（沿用 `07-gate-review.md:160` 的「任何 P6 类事后更正必须同步本表、追加提交并前移 tag」规则），而不是在此默认。

---

## §5 与 W06 四件套的关系

| 维度 | W06 | W07（本件） |
|---|---|---|
| 冻结件性质 | **真实冻结件**：T0 前后产生的原始证据（审计 Schema、审批协议、digest 冻结向量、故障演练） | **追溯补记的事实索引**：只登记事后可复算的事实 |
| 文件构成 | `05-freeze.md` + `06-conformance-audit.md` 等，属收口当日产出 | 2026-11-17 补写，**晚于 T0 六个日历日** |
| 向量 | 自持完整冻结向量（V1–V6、`action_digest` 基准、失效子向量、`prerequisites_digest` 输入） | **无独立向量**，全部引 W06（§2） |
| 可引用性 | 可作为 W06 冻结时刻证据 | **只能**作为「W07 冻结事实的索引入口」，**不能**作为冻结时刻证据 |
| 结论口径 | 有条件放行（其自有 GR 结论） | **有条件放行；门禁通过：否**（`07-gate-review.md:11`） |

**禁止表述（违反即返工）**：

| 禁止 | 应改为 |
|---|---|
| 「W07 冻结四件套齐全」 | 「W07 冻结件为 **2026-11-17 追溯补记**；W07 无独立冻结向量，向量基准引自 W06」 |
| 「W07 已收口 / 门禁通过 / 放行」 | 「**有条件放行；门禁通过：否**」 |
| 「KD-5 已按期补齐」 | 「KD-5 **超期追溯补记**；原裁决『本轮不补 + W08 首日承接』**未被执行**」 |
| 「`tag w07-close` 含 05/06 两件」 | 「`tag w07-close` 内 `docs/w07/` **只有 6 个文件**，无 05/06」 |

---

## §6 复算清单（供任何复核者逐条重放）

```bash
# 1) 锚点
git rev-parse w07-close
git show -s --format='%H%n%ci%n%s' w07-close

# 2) tag 内 docs/w07 文件清单（应为 6 个，无 05/06）
git ls-tree -r --name-only w07-close docs/w07/

# 3) 七个 sha256（与 §3.2 逐条对照）
for f in workers/runner.py workers/stack.py workers/discovery.py \
         tests/unit/test_worker_runner.py tests/integration/test_worker_process.py \
         workers/README.md docs/w07/worker-residue.md; do
  printf '%-45s ' "$f"; git show w07-close:"$f" | sha256sum | cut -d' ' -f1
done

# 4) 向量版本未升
git show w07-close:core/approval/digest.py | sed -n '24p'      # ACTION_DIGEST_VERSION = "w06-v1"

# 5) 007 迁移与回滚
git show w07-close:database/migrations/007_w07_disclosure.sql
git show w07-close:database/migrations/rollback/007_rollback.sql
```

---

## §7 关联件

| 件 | 作用 |
|---|---|
| `docs/w07/07-gate-review.md` | W07 门禁复核件（GR-0 唯一结论行；KD-5 登记处） |
| `docs/w07/06-conformance-audit.md` | 本件的配对件：对 tag `w07-close` 快照的实施期符合性审计 |
| `docs/w06/05-freeze.md` | W07 冻结向量基准的**唯一**来源 |
| `docs/w07/02-design.md` | 设计与未验证项/已知偏离（附 C，`:840-887`） |
| `docs/w07/worker-residue.md` | 残留事实（7 条，不得表述为「已清理」） |
