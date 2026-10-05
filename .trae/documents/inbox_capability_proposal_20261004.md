# aiwf inbox 能力方案与实施记录

目标：项目开发过程中临时冒出的想法与问题，能**不阻塞当前流程**地记下来，之后再整理交接。
参考：`godot_agent/skills/g-inbox`（纯文档技能）。

**状态（2026-10-04）：六项决策全部拍板，实现已完成。**
第 1–5 节是决策前的原方案（保留作论证存档，含被否掉的选项）；
第 6 节是决策记录与锁定输入；**第 7 节是实施记录，含三处实现期偏离与实证数据**。

---

## 0. 先看 g-inbox 里真正起作用的东西

读 `g-inbox/SKILL.md` + `references/inbox-format.md` 后，剥离掉 godot 系特有概念（`project/<game-id>`、
`g-plan` / `g-do` / `g-design` 分流），可复用的机制只有 6 条：

| # | 机制 | 为什么它有效 |
|---|---|---|
| 1 | **活页 / 历史两文件**：`inbox.md` 只放未处理，`inbox-history.md` 只追加归档 | 日常操作永远只读活页索引，成本恒定，不随历史增长而变慢 |
| 2 | **「为何停放」是必填且枚举**（非本 TASK / 非本计划后续 TASK / 用户明确不挡 / 计划收缩） | **这是整个技能最有价值的一条**。强制回答"为什么它可以等"，缺这行就停下来问——把「记了就不用管」变成「记了有理由」 |
| 3 | **编号活页历史共用、稳定递增、不复用**；新号 = 两份文件最大号 + 1 | 归档后编号仍可被 `拆自` / `吸收` 指向，不悬空 |
| 4 | **内聚原则**：一条只装一类事项；新想法先尝试并入已有条目（`吸收`），不能才新开号 | 防活页碎片化成流水账 |
| 5 | **正文 <500 字，超出走 `notes/INB-NNN.md` 详情** | 活页保持可扫读 |
| 6 | 只分流存放，**不排期、不跟踪是否做完、不写入 design/plan** | 边界清晰，所以它不会被当成"半个任务系统" |

反面观察（aiwf 不照抄的部分）：g-inbox 靠**单 agent 顺序操作文件**保证编号不冲突；
g-inbox 的"问题默认不记"是因为 godot 系有 `g-do` 承接当前任务缺陷，aiwf 没有对应物。

---

## 1. 关键设计判断（先立论，再给结构）

### 判断 1：inbox 的绝大多数使用场景，发生在**没有 run 在跑**的时候

主人原话是"正常项目开发过程中"。而 aiwf 的 run 是**有寿命的**：一个 run 要么 resting，要么在等
`complete`。项目开发中的"临时记一下"，十有八九是 agent 直接改代码时冒出来的，跟某个 run 无关。

**推论**：inbox **不能挂在 run 生命周期上**，必须是项目级（`<AIWF_HOME>/inbox/`），跨 run、跨计划存活。
这是与 `runs/` 并列的第二种持久化，不是 run 的子结构。

### 判断 2：主交付物是文档，不是引擎状态

因为判断 1，inbox 条目**天然不是状态机的一部分**。g-inbox 的 6 条机制里，1 / 4 / 5 / 6 条纯属
写作约定，引擎一点都帮不上忙（引擎不会判断"这两条算不算同类"）。

**推论**：能力主体是 **skill + Markdown 文档**，与 aiwf 现有 `skills/` / `references/` 体系同构。
模仿模板库的教训——不为文档写作约定造机制。

### 判断 3：但有 4 件事文档做不好，必须引擎做

| 问题 | 纯文档的后果 | 引擎能做 |
|---|---|---|
| 编号分配 + 活页/历史同步 | agent 手算最大号、手写 `### INB-003` 段落，格式漂移 / 重号 | `add` 一个命令原子完成 |
| 「为何停放」必填 + 枚举 | agent 图省事漏写或自造值 | `add` 拒绝并给出四个合法值 |
| 归档（改状态 + 追加历史 + 从活页删 + 更新索引） | 4 步手工，漏一步就出现"活页里躺着已交接" | `resolve` 原子完成，漏不掉 |
| 格式自检 | 无人校验，写歪了要等下次读才发现 | `check` 在测试里跑，把约定钉死 |

**推论**：走 **文档为准 + 引擎做薄校验与原子操作**。与 `run_defaults` 的取舍完全同构
（`storage.py` 只管读 config、只管合并，不替用户决定值）。

### 判断 4：引擎不读 run 记录，来源只作自由文本

`add --run <id> --node <id>` 写进条目 `来源` 字段，但**不校验存在性、不碰 record.json**。
理由：一旦校验，inbox 命令就要依赖 run 记录格式与状态，而判断 1 说它俩本就无关；
`来源` 的作用只是让人日后能追溯，自由文本足够。
（`aiwf inbox list --run <id>` 仍可按字符串过滤，这是安全的。）

### 判断 5：aiwf 的「不停放」清单与 g-inbox 不同

g-inbox 说"类型为问题且无法排除是本任务缺陷时，不记 inbox"。aiwf 里"当前任务"的粒度是**节点**，
更精确的表述是：**是当前节点自身的缺陷 → 不准停放**（该走 `check` REVISE 或 `aiwf fail`），
其余四种情况才准停放。四个枚举值也相应改成本项目的说法：

| 为何停放 | aiwf 语义 |
|---|---|
| `非本节点` | 与当前任务包无关，属别处发现 |
| `非本流程后续节点` | 本 run 的后续节点也不处理，是流程外的事 |
| `用户明确不挡` | 用户说了先记下 |
| `范围收缩` | 本 run 的既定范围主动砍掉（对应 g-inbox 的「计划收缩」） |

沿用 g-inbox 的硬约束：**缺这一行就停下问，不记下**。且工具箱要明写：
不得把当前节点的验收问题"记进 inbox"了事——这是 g-inbox 唯一一处强禁令，值得继承。

### 判断 6 —— **已拍板：不做"取用"命令**（主人 2026-10-04 定）

> 主人原话：「1. 不做」

**定案**：不提供 `inbox collect` 之类把 inbox 条目送进 run 入参的命令。
只记录来源（判断 4），消化时机与是否采用由人决定；下一轮启动若要用，由 agent / 用户显式
`-p parked="INB-006,INB-009"` 或写进 `run_defaults`。

理由（保留原始论证，供日后回看）：会造成"inbox → run 启动"的隐式通道，容易长成半个排期系统；
且与判断 1（inbox 与 run 生命周期无关）冲突。

**连带约束**：`engine/aiwf/inbox.py` 不得 import engine / storage 的 run 相关接口；
CLI 也不提供任何"取用"入口。日后若真出现需求，再按新决策单独立项。

原方案的两个选项（存档，不再执行）：

- 方案 α（**推荐**）：**不做**。只记录 `--run`，下一轮启动时由 agent / 用户显式传
  `-p parked="INB-006,INB-009"` 或写进 `run_defaults`。零耦合，符合判断 1。
- 方案 β：加 `aiwf inbox collect --run <id>` 输出未处理条目为 JSON，可直接喂 `--input-file`。
  约 30 行，但引入"inbox → run 启动"的隐式通道，日后容易长成半个排期系统。

推荐 α，把 β 列为"出现真实需求再上"（同 `run_defaults` 方案 B `${project.*}` 的处置方式）。

---

## 2. 目录与文档格式

```
<AIWF_HOME>/                     # 默认项目下 .aiwf/
  inbox/
    inbox.md                     # 活页，仅未处理
    history.md                   # 归档，已交接 / 丢弃；首次归档时创建
    notes/INB-NNN.md             # 可选，单条超 500 字时的详情
```

与 g-inbox 的三处刻意差异：
- 文件名 `history.md` 而非 `inbox-history.md`——目录已经叫 `inbox/`，不重复。
- **不按组分文件夹**。g-inbox 按 `project/<game-id>/` 分是因为一个仓库放多款游戏；
  aiwf 的 `<AIWF_HOME>` 本身就是一个项目一个，不需要再加一层。
- `inbox/` **纳入版本管理**（与 `.aiwf/runs/` 的既有立场一致），已确认 `.gitignore` 无需改动。

### 活页格式（与 g-inbox 同构，字段名对齐 aiwf）

```markdown
# Inbox

未进入当前流程的问题与想法。不是任务列表，不跟踪是否做完。
一个条目只装一类内聚事项；新想法先看能否并入已有条目。

交接后移入 [history.md](history.md)。

## 未处理

- INB-006：四种砖是否需要材料玩法差异

## 条目

### INB-006
- 状态：未处理
- 类型：想法
- 日期：2026-10-04
- 来源：novel-3ch / chapters / 对话
- 摘要：四种砖是否需要材料玩法差异。
- 为何停放：用户明确不挡
- 拆自：INB-001
```

字段全表（`inbox-format.md` 承载）：

| 字段 | 约定 |
|---|---|
| 编号 | `INB-NNN`，活页与历史共用一套，稳定递增不复用；新号 = 两份文件 `### INB-` 最大号 + 1 |
| 状态 | 活页仅 `未处理`；历史 `已交接` / `丢弃` |
| 类型 | `想法` / `体验` / `问题` / `技术债`（纯标签，不驱动分流） |
| 日期 | `YYYY-MM-DD`，**由 `add` 自动填**，不接受手填 |
| 来源 | 自由文本：`<run_id> / <node_id>`、`<workflow_id> / 对话`、或纯 `对话` |
| 摘要 | 一段，写清目标/现状/边界；整条正文 <500 字，超出走 `notes/` |
| 为何停放 | 未处理必填，四值枚举（见判断 5） |
| 拆自 / 吸收 / 相关 | 可选，`INB-NNN` |
| 备注 | 可选，≤3 行 |
| 去向 | 仅历史：一句话 + 可选锚点。**不写「已完成」** |

---

## 3. CLI 表面（6 个动词）

沿用既有风格：状态变更命令带 `--json` 回印结果；子命令用 `parents=[inbox_parent]` 共享公共参数
（照 `wf` 组的写法，`cli.py:840` 建 `wf_parent`、`cli.py:847` 起接线）。

```
aiwf inbox add  "四种砖是否需要材料玩法差异" \
                --type 想法 \
                --why 用户明确不挡 \
                [--merge-into INB-003] [--split-from INB-001] [--related INB-004] \
                [--run <run_id> --node <node_id>] [--note-file <path>] [--json]

aiwf inbox list [--all] [--run <run_id>] [--type 问题] [--json]
aiwf inbox show INB-006 [--json]
aiwf inbox resolve INB-006 --status 已交接 --to "纳入 next-round" \
                [--split-into INB-007,INB-008] [--json]
aiwf inbox check [--json]
aiwf inbox path            # 打印 inbox 目录，供人直接打开
```

语义要点：

- `add` 原子完成：分配编号 → 写条目 → 更新文首索引。
  `--merge-into INB-003` 时**不新开编号**：追加到 INB-003 的 `吸收` 字段，
  并立即把被吸收条目标 `已交接` / 去向 `合并进 INB-003` 归档到 history。
  `--why` 缺失或值非法 → **rc=2，不写任何文件**。
- `list` 默认只读活页索引、只展开 `未处理`；`--all` 才读 history 的标题行（不读正文）。
- `resolve` 原子完成：改状态 → 追加 history → 从活页删除 → 更新索引。
  `--split-into` 用于「重组」：原条归档为 `去向：拆分为 INB-007、INB-008`，
  新条目另行 `add --split-from INB-006` 产生（拆出的内容需要人来判断，不宜由引擎代写）。
- `check` 只读校验：编号唯一且连续可不要求、必填字段齐、索引与条目集合一致、
  活页无已处理状态、history 无重复编号。**这是把约定钉死的唯一手段**，测试里直接调它。
- 错误统一走 `ValueError`（与 `cmd_wf_show` 一致，`cli.py:631`），`main()` 已有兜底。

---

## 4. 改动文件清单

新增：

| 文件 | 内容 | 估计 |
|---|---|---|
| `engine/aiwf/inbox.py` | 纯逻辑：路径、解析、渲染、编号分配、add/resolve/check。**不依赖 engine/storage** | ~230 行 |
| `tests/test_inbox.py` | `add` 必填与枚举拒绝、编号递增跨活页历史、merge 归档、resolve 归档、check 报错、JSON 往返 | ~150 行 |
| `skills/aiwf/references/inbox.md` | 何时记 / 何时不记（含"当前节点缺陷不准停放"）/ 怎么记 / 怎么消化重组 / 与流程的关系 | ~80 行 |
| `.trae/documents/inbox_capability_proposal_20261004.md` | 本文档 | — |

改动：

| 文件 | 改动 |
|---|---|
| `engine/aiwf/cli.py` | docstring 命令表加一行；`from . import inbox as inbox_store`；6 个 `cmd_inbox_*`；`inbox` 子命令组接线 |
| `engine/aiwf/storage.py` | 只加一个 `inbox_dir` property（`os.path.join(self.root, "inbox")`）。**不动** run 记录 / journal / 快照 / config |
| `skills/aiwf/SKILL.md` | 入门表加一行「临时想法/问题不想打断流程 → [收件箱](references/inbox.md)」；「始终遵守」加一条"当前节点缺陷走 check/fail，不记 inbox" |
| `engine/aiwf/__init__.py` | **不动**（已确认 `__all__` 只导出 `Storage` / `WorkflowEngine`，`inbox` 与 `library` 同级，不必进包级 API） |
| `.trae/documents/` 决策记录 | 落定后按惯例记入 `.workbuddy/memory/` |

**明确不做**：不改 `schemas/*.json`（inbox 不是工作流节点，无 schema 变更，无 1.0/1.1 快照兼容问题）；
不进 run 记录与 journal；不做远程 / 多人并发锁；不做提醒与排期。

### 实现时必守的两条硬约束

1. **编码**：`inbox.py` 所有读写显式 `encoding="utf-8"`，测试同样。
   本机已实测：漏写会导致 cp936 下以 `UnicodeDecodeError` 假失败（`test_usability.py` 现有 7 例即此问题）。
2. **原子性**：活页 + 历史 + 索引的多文件改动走临时文件 + `os.replace`
   （复用 `Storage._atomic_write_text` 的思路，但 inbox.py 不 import storage，自带一份小 helper）。
   半途崩溃宁可整体不变，也不要出现"活页里躺着已交接"。

---

## 5. 与 workflow 的关系（一句话讲清，也是 skill 里最需要写清的部分）

- 在 run 中记录：agent 可直接 `aiwf inbox add`，**不产生 node_run、不消耗 check 预算、不改 run 状态**，
  记录完照常 `aiwf complete` 当前节点。这正好满足"不阻塞当前流程"。
- **唯一例外**：当前节点的产物质量问题是 run 内部事务，走 `check` REVISE / `aiwf fail` / `revise`，
  不许用 inbox 把它绕过去。
- 消化时机由人定：`aiwf inbox list` → 用户选去向 → `resolve`。**引擎不主动把 inbox 条目塞进任何 run**
  （判断 6 已拍板：不做取用命令）。

---

## 6. 决策记录（六项全部已定，2026-10-04）

1. ~~**判断 6：要不要"取用"能力**~~ —— **已定：不做**。
2. ~~**命令组名**~~ —— **已定：`inbox`**。所有命令为
   `aiwf inbox add|list|show|resolve|check|path`。
3. ~~**编号风格**~~ —— **已定：`INB-NNN`**，与 g-inbox 逐字一致，
   活页与历史共用一套号，稳定递增不复用；新号 = 两份文件 `### INB-` 最大号 + 1。
4. ~~**`为何停放` 四个枚举值**~~ —— **已定：四值如上**，锁定为
   `非本节点` / `非本流程后续节点` / `用户明确不挡` / `范围收缩`，未处理条目必填、缺则拒写。
5. ~~**是否接受"`resolve` 只归档、拆分内容另行 add"**~~ —— **已定：接受**。
   `resolve` 只改状态 + 追加 `history.md` + 从活页删除 + 更新索引；`--split-into` 仅把原条目去向写成
   `拆分为 INB-007、INB-008`，**不代写新条目正文**，新条目由调用方另行 `add --split-from`。
6. ~~**是否写进 `library/` 模板**~~ —— **已定：不写**。inbox 不是流程节点，
   没有定义 JSON 可装，进模板库会让人以为它是一道工序。

### 已定项汇总（代码实现时的锁定输入）

| 决策 | 定案 |
|---|---|
| 1 | 不做"取用"命令，只记录来源；`inbox.py` 不 import run 相关接口 |
| 2 | 命令组名 `inbox` |
| 3 | 编号 `INB-NNN`，活页历史共用、递增不复用 |
| 4 | `为何停放` ∈ {`非本节点`, `非本流程后续节点`, `用户明确不挡`, `范围收缩`}，未处理必填 |
| 5 | `resolve` 只归档；拆分内容另行 `add --split-from` |
| 6 | 不进模板库 |

---

## 7. 实施记录（2026-10-04 完成）

### 新增
- `engine/aiwf/inbox.py`——纯逻辑，**不 import engine / storage**：Markdown 解析与渲染、
  编号分配（活页 + 历史取最大号 + 1）、`add`（含 `--merge-into` 归并）、`resolve`
  （状态 + 历史 + 活页一步完成）、`check`（只读格式校验）。
  读写全部显式 `encoding="utf-8"`，多文件改动走 temp + `os.replace`。
- `tests/test_inbox.py`——26 用例。
- `skills/aiwf/references/inbox.md`——何时记 / 何时不记（红线）/ 记下 / 列出 / 消化 / 重组 / 校验 / 格式。
- 本文档。

### 改动
- `engine/aiwf/storage.py`——`Storage.__init__` 加 `self.inbox_dir = <root>/inbox`；
  布局 docstring 补 inbox 三行。**未动** run 记录 / journal / 快照 / config。
- `engine/aiwf/cli.py`——docstring 命令表加一行；`from . import inbox as inbox_store`；
  `_inbox_source` / `_inbox_dump` + 6 个 `cmd_inbox_*`；`inbox` 子命令组接线
  （`inbox_parent` 共享 `--json`）。
- `skills/aiwf/SKILL.md`——入门表加「临时冒出、不挡这一轮的想法或问题 → 收件箱」；
  「始终遵守」加红线一条。

### 与方案的实现期差异（三处，均为实现时才发现）
1. **`吸收` 字段不再自指**——原方案写 `吸收：INB-006（日期 并入）`，但归并的新输入并没有自己的编号，
   写成目标自己的 ID 是自指、看着像错误。改为 `并入：YYYY-MM-DD`。
   ⇒ 与 g-inbox 的语义差异：g-inbox 的 `吸收：INB-NNN` 指向**被吸收条目**（那条有自己的号并被归档）；
   aiwf 的 `--merge-into` 是"新输入直接并进已有条目"，新输入从未成为条目，故无号可引。
2. **`--split-into` 不预留编号**——拆分去向里的号只是文本记录，`next_id` 不认它们；
   实测 `resolve --split-into INB-007,INB-008` 后下一个真实条目仍是 `INB-002`。
   这是刻意的：预留会凭空跳号，且这些条目未必真被创建。测试里已显式钉住该行为。
3. **列表表格用显示宽度对齐**——中文标签是双宽字符，`str.ljust` 会错位，故加
   `display_width` / `pad_display`（`unicodedata.east_asian_width`）。

### 实证
- `tests/test_inbox.py` **26/26 通过**；全量 `tests/` **115/115 通过**（原 89 + 26），无回归。
- 解释器 `C:/Python314/python.exe`，命令 `PYTHONPATH=engine PYTHONUTF8=1 python -m unittest discover -s tests`。
- 手工 CLI 全链路核过：`add` → `--merge-into` 归并（不新开号）→ `list`（激活/归档两段）→
  `resolve --split-into` → `check` rc=0；三种拒绝均 rc=2 且**不落盘**（缺 `--why`、
  `--why` 非法值、`--type` 非法值）；`resolve` 缺去向、去向写「已完成」、重复归档均被拒。
- 对两个演示项目跑 `inbox check` 均通过（说明未使用时不会误报）。
- 已确认 `aiwf validate --project` 只扫 `.aiwf/workflows/`，inbox 目录不会被当成定义加载；
  `.gitignore` 未覆盖 `.aiwf/inbox/`，条目随仓库纳入版本管理。

### 编码加固（顺手，与记忆里的同类隐患不同）
`tests/test_inbox.py` 的 CLI 子进程显式钉住两侧编码：
`BASE_ENV` 带 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`，`subprocess.run` 用
`encoding="utf-8", errors="replace"`（不用 `text=True`）。
原因实测：在 CP936 主机上，父进程用 locale 编解码会让**中文 argv 与 stdout 同时变乱码**，
报错信息极具误导性（断言显示成 mojibake）。
**结果**：本套件在不设 `PYTHONUTF8` 的 CP936 主机上也 26/26 通过——
这是全仓第一个做到这一点的测试文件（`test_usability.py` / `test_skill_examples.py`
仍有 7 例因未指定编码而假失败，属既有范围外问题）。

