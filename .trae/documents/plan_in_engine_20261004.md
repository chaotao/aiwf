# plan 该不该进引擎，以及如何设计

日期：2026-10-04
状态：**提案**。当前只抛出一项待拍板（决策 11）。本轮零代码改动。
**→ 决策 11 已定 = B；写入层亦已落地。见下方「后续进展」。**
前置：`godot_agent_to_aiwf_migration_20261004.md`（决策 10：替代边界）、
`template_generality_and_professionalism_20261004.md`（决策 6：`run_defaults`）

---

> **后续进展（2026-10-04 晚 · 事后回填）**
>
> **决策 11 已定 = B**，且不止只读——**写入命令也已落地**。本文的分析（§1 实测、§2 状态映射、
> §3 三层判断、§4 设计）是**当时的判断依据，原样保留**；下表只记已被实测改变的部分。
>
> | 本文的判断 | 实况 |
> |---|---|
> | §3「plan 该进的是**视图**，不是数据」 | **方向反**。plan 是 run 的**源**——run 要引用 plan 才存在，所以 plan 不可能是 run 的派生物。此点由 `plan_as_task_list_20261004.md` §1 自证 |
> | §4.5 写回策略推荐 **i 原地替换** | 采纳，且实现比"改一个字段"更保守：**行级外科手术**，只替换必须改的行。实测发现"无条件重写进度行"是**破坏性降级**（会把 `TASK-002→demo01 TASK-009`、`TASK-004（并入 machine-core）` 抹成裸 id 列表），已改为 **id 集合确实不对才动** |
> | §2 末尾「待定：`status=failed` 映射」 | **已定 → `进行中`**。判据：一次失败的 run 只说明任务没做完，**只有 `completed` 才收口一个 task**（`engine/aiwf/plan.py:301-318`） |
>
> **落地**：`engine/aiwf/plan.py`（874 行：解析 / 校验 / 派生 / 写入）、`storage.plans_dir`、
> CLI `plan` 命令组、`skills/aiwf/references/plan.md`（新）、`tests/test_plan.py`（45 用例）。
> 目录 `<root>/plans` 即 **`.aiwf/plans/`**，与 `runs/`、`inbox/` 并列——本文 §1.6 引的 inbox 范式，
> 正是它照抄的。
>
> **未落地**：`plan run`（本文 §4.3 列为"可选"的封装命令）。

---

## 0. 先拆掉提问里的歧义

"plan 进引擎"能指三件完全不同的事，混着谈必然吵不清：

| 说法 | 实际含义 | 改动量级 |
|---|---|---|
| ① 定义的进 | task 列表变成引擎的数据结构（`.aiwf/plans/*.json`） | 中：新增实体 + 与文档双写 |
| ② 状态的进 | run 记录里加 `plan` / `task` 字段 | 大：改 `run_record.schema.json`（`additionalProperties:false`） |
| ③ 视图的进 | 一个命令，读 plan 文档 + 读 run 记录，输出合并结果 | 小：只读，不动引擎核心 |

**本方案的结论是 ③，且只做 ③。** 下面每一条判断都有实测支撑。

---

## 1. 现状实测

### 1.1 plan 的实际构成（`mc-plus/plan/machine-core/index.md`）

| 块 | 内容 | 性质 |
|---|---|---|
| 计划信息 | 游戏 / 计划 ID / 状态 / 目标 / 范围 / 创建·更新日期 / 上下文链接 | 人工 |
| **当前焦点** | 当前任务 / 当前状态 / 下一步 / 阻塞 / 必读文件 / 按需读取 | **纯视图** |
| 任务 | `- [TASK-NNN](path)｜标题｜状态：X｜依赖：Y` | 索引：定义人工、状态派生 |
| **进度** | 已验收 N/M / 进行中 / 待验收 / 阻塞 / 已取消或拆分 | **纯视图** |
| `tasks/TASK-NNN.md` | 契约（任务契约·范围·运行方式·验收标准·自动验证·风险·记录） | 知识 |
| `context.md` | 稳定上下文（输入文档 / 完成定义 / 代码基线 / 实现策略） | 知识 |

**"当前焦点"与"进度"两块没有独立信息**——它们全部可从"任务行 + run 事实"算出来。

### 1.2 任务行格式：106/106 严格统一

实测 15 个 plan、106 个任务行：

```
$ cat */plan/*/index.md | grep -cP '^- \[TASK-\d+\]\([^)]+\)｜.+｜状态：.+｜依赖：.+$'
106          # 严格四段格式，零例外
```

好消息：**格式已经是机器可解析的**，不需要新造格式。

### 1.3 但状态值已经漂移（这是"该进"的硬证据）

> **更正（2026-10-04 晚）**：本节原先归因为"人手维护的自由文本状态会烂"，**归因错误**。
> 主人澄清：`index.md` **不是人维护的，是 AI 通过 `g-plan` 维护的**（执行态由 `g-do` 维护）。
> 真实的漂移机制与两条新证据（plan 级状态连枚举都没有、且已滞后）见
> `plan_long_term_role_20261004.md` §1。**"漂移"这个事实本身不变，变的只是原因**——
> 写入方是 AI，照样漂，因为缺的是枚举约束而不是人的认真程度。

g-do 的权威状态机是 **8 态**（`skills/g-do/SKILL.md:95-103`）：

```
待开始 → 进行中 → 待验收 → 已完成
                    ↓
              验收未通过 → 进行中
待开始/进行中 → 阻塞 → 进行中
待开始 → 已拆分      # 仅 g-plan / g-clarify
待开始/阻塞 → 已取消  # 仅 g-plan / g-clarify
```

实际落盘的状态值分布：

| 值 | 计数 | 在声明内 |
|---|---|---|
| 已完成 | 83 | ✅ |
| 待开始 | 10 | ✅ |
| 已取消 | 5 | ✅ |
| 待验收 | 4 | ✅ |
| 已拆分 | 3 | ✅ |
| **已验收** | **1** | ❌ **声明外** |

漂移点：`project/mc-plus/plan/visual-polish/index.md:45`

```
- [TASK-001](tasks/TASK-001.md)｜新方块注册与目录扩充落地｜状态：已验收｜依赖：无
```

`已验收` 不在 8 态枚举里，且与 `已完成` 语义重叠。**这是"自由文本状态会漂"的一手证据**（写入方是 AI，不是人），不是推测。根因是"两个散文 skill 协同写同一字段、无枚举校验"。

### 1.4 g-do 里还有若干条机器可校验的不变量

同文件 `:105` 明写：

- 「默认最多一个 `进行中`」
- 「进度只计用户已验收的 `已完成`」→ 进度 = 计数，不该人手算
- 「两组互不越界」：`g-plan` 不得推进执行态，本技能不得改 `已拆分`/`已取消`

这些都是**命令能校验、而散文只能靠自觉**的东西。

### 1.5 run 侧现有能力（够用，不需要加字段）

run 目录：`runs/<workflow_id>/<run_id>/{record.json, record.log.jsonl, workflow.json, context.md}`

`record.json` 已有并落盘（实测 `%TEMP%/aiwf-pro-1`）：

```json
{"workflow_id": "std.review-loop", "status": "running", "phase": "executing",
 "inputs": {"project_dir": "...", "test_cmd": "...", "request": "..."}}
```

- `status` ∈ `running | completed | failed`，`phase` ∈ `executing | awaiting_input | awaiting_review | …`
- **`inputs` 是自由 object 且落盘** → 关联键可以放这里，**零 schema 改动**
- `events` 里有 `review_decided`（带 `verdict`）→ "验收未通过"可判

### 1.6 先例：`inbox` 已经做过一次同样的取舍

`engine/aiwf/inbox.py` 的模块 docstring 第一句：

> *"Deliberately **not** part of the run state machine. An inbox lives at `<AIWF_HOME>/inbox/`, next to `runs/`, and outlives every run."*

它的做法：**Markdown 是唯一事实来源**，模块是薄层，只做"散文做不到的四件事"——分配 id、拒绝无理由条目、一步归档、校验（`aiwf inbox check`）。run 关联用自由文本 `source`，**不校验存在性、不碰 run record**。

**这就是 plan 的现成范式**：同样的"跨 run 存活、不是状态机的一部分、需要机器做几件小事"。

---

## 2. 状态映射：8 态里 5 个可派生、3 个必须人工

| g-do 状态 | 客观来源 | 派生? |
|---|---|---|
| 待开始 | 没有对应 run | **人工**（规划决定） |
| 进行中 | `status=running` + `phase=executing` | **派生** |
| 待验收 | `status=running` + `phase=awaiting_review` | **派生** |
| 已完成 | `status=completed`（且经 review APPROVE） | **派生** |
| 验收未通过 | 末次 `review_decided` 为 REJECT | **派生** |
| 阻塞 | 无客观来源 | **人工** |
| 已拆分 | 规划决定 | **人工** |
| 已取消 | 规划决定 | **人工** |

**派生覆盖的恰好全是"每次实施都会发生"的态，人工的恰好全是"规划期才有"的态**——与决策 10 划出的边界完全重合。

一个待定的映射：aiwf 有 `status=failed`，g-do 8 态里**没有**对应项。需在上线时决定（映射到"阻塞"，或作为第 9 个派生值）。

---

## 3. 判断：三层分别回答

| 层 | 进不进 | 判据 |
|---|---|---|
| **知识**（TASK 契约、context、design/spec） | **不进** | 文档是给人读的，引擎读了也没处用；进去就是把 aiwf 变万能容器（决策 10 已否） |
| **索引**（task 定义 + 依赖） | **不进** | 已在 `index.md` 且格式 106/106 统一，**解析即可，无需存储**；搬家会切断「人可读可编辑」与 git diff |
| **视图**（状态 / 当前焦点 / 进度） | **进** | 唯一有派生价值的部分，且现状**全靠手工维护**（§1.3 已实测漂移 1 处） |

一句话：**plan 该进的是"视图"，不是"数据"。**

---

## 4. 如果进：设计

### 4.1 两个数据源，join 出视图

```
源 A（人工，项目内）  <game>/plan/<plan-id>/index.md
                       └─ 任务行 = 定义（id / 标题 / 依赖）+ 状态（待派生覆盖）
源 B（引擎，自动）    .aiwf/runs/**/record.json
                       └─ status / phase / events / inputs

关联键               run 的 inputs.plan 与 inputs.task
```

**不新增存储**：plan 文档留在游戏仓库里（跟游戏走、人可 git），引擎只读它。

### 4.2 关联怎么建（本设计的关键）

选项：

| 方式 | 做法 | 代价 |
|---|---|---|
| **a 约定键** | run 启动时 `-p plan=<id> -p task=<TASK-NNN>` | 零改动；靠纪律，漏传则 run 成"孤儿" |
| **b 封装命令** | `aiwf plan run <plan-id> <task-id>` 自动注入关联键并选对 workflow | 把纪律变成命令，最不易漏 |
| c 改 run schema | 加 `plan`/`task` 字段 | 大，且 `additionalProperties:false` 要动 schema |

推荐 **a + b**：键走 `inputs`（零 schema 改动），再用一个封装命令消灭"忘传"。

### 4.3 命令面（建议最小集）

```
aiwf plan list                  # 所有 plan + 派生进度（跨 plan 视图）
aiwf plan status <plan-id>      # 单 plan 合并视图：定义 + 派生状态 + 焦点 + 进度
aiwf plan check                 # 校验不变量（§4.4）
aiwf plan run <plan-id> <task>  # 可选：带关联键启动一次 TASK
```

### 4.4 `plan check` 能校验的不变量（都来自 g-do，现在只能靠自觉）

1. 状态值 ∈ 8 态枚举 → **当前会报 1 处**（`visual-polish` 的 `已验收`）
2. 每 plan 最多一个 `进行中`
3. 依赖引用的 task 存在，且依赖图无环
4. 文档状态 vs run 派生状态一致（不一致时报出，**不自动改**）
5. 进度 = 计数，不人手写

### 4.5 写回策略（第二项待定，先列分歧）

| 选项 | 做法 | 好在哪 | 代价 |
|---|---|---|---|
| **i 原地替换** | 命令只改 `｜状态：X｜` 一个字段，严格正则，其余字节不动 | 不破坏现有格式，g-do 的读法（`SKILL.md:110,119`）不变 | 命令会改"人手写的文件" |
| ii 生成块 | index.md 里加 `<!-- aiwf:status:begin/end -->`，人工块写定义、生成块写状态 | 边界最清晰（先例：`wf index` → `library/INDEX.md`） | 要改 106 行现状 + g-do 要改读法 |
| iii 不写回 | `plan status` 只读输出 | 零风险 | **消除不了手工维护**，漂移还会发生 |

推荐 **i**：它是唯一"既消灭手工维护、又不改现有格式与 g-do"的选项。

---

## 5. 待拍板（当前只这一项）

**决策 11：plan 进引擎的范围。**

- **A 只读派生** —— `plan list/status/check`，不写任何文件。零风险，但状态仍要人手工改。
- **B（推荐）只读 + 原地回填状态** —— A 的全部，加 `plan sync` 只改 `｜状态：X｜` 字段。
- **C 定义也进引擎** —— task 列表搬进 `.aiwf/plans/*.json`，与文档双写或取代文档。

推荐 **B**：

- 它对准唯一痛点——**状态手工维护已实测漂移**（§1.3），而定义/知识搬家无收益还有代价；
- 不新增存储、不改 schema、不改现有文档格式；
- 与 `inbox`（Markdown 为源）和 `wf index`（生成产物）两个先例都一致；
- C 会被"不提前造机制"直接否掉：定义已经在统一格式的文档里，没有搬的理由。

决策 11 定了，第二项是**写回方式**（§4.5 的 i / ii / iii）。

---

## 6. 明确不做

- 不改 `run_record.schema.json`（关联走 `inputs`，够用）。
- 不做跨项目的 plan 注册表——"一个项目就是一个独立的游戏"下，plan 天然属于游戏本地（`<game>/.aiwf/` 与 `<game>/plan/` 同根）。
- 不让引擎"调度" plan：不自动推进状态、不自动跨 TASK 连续执行（`g-do:123` 的"默认一次一个任务"是人工节奏）。
- 不把 TASK 契约、context、design/spec 变成引擎数据。

---

## 附：最终形态（三层各归其位）

```
<game>/
  plan/<plan-id>/index.md          # 定义（人写）+ 状态（命令回填）   ← 源 A
  plan/<plan-id>/tasks/TASK-NNN.md # 契约（知识，人不看引擎）
  plan/<plan-id>/context.md        # 稳定上下文（知识）
  .aiwf/workflows/<wf>.json        # 执行骨架（模板，如 dev-task）
  .aiwf/runs/<wf>/<run-id>/        # 一次实施（事实）                  ← 源 B
  .aiwf/config.json                # run_defaults（项目固有值）
```

一次实施 = **契约（文档）+ 模板（骨架）→ 一个 run**；task 状态 = **该 run 的派生**。
