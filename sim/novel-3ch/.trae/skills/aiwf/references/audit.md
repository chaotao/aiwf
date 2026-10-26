# 审计：运行记录与业务视图

跑完之后怎么看：运行目录与记录结构、列表/详情/事件/检索，以及直接给出人话进度的业务进展视图。

## 目录

- [审计 / 检索工作流运行记录](#审计--检索工作流运行记录)
- [业务进展视图（view）](#业务进展视图view)

## 审计 / 检索工作流运行记录

1.1 的 `aiwf status [RUN] --json` 是只读叶子任务包（含标题/当前节点/next）；
`runs show` 读取指定 run 的完整记录（快照 + 回放增量日志）。`workflow.json` 是该运行的定义快照，
修改源定义不会改写快照。
`check_requested/check_decided` 记录 Agent 质检次数、原始输入/输出、理由、起始时间、
耗时和摘要；`review_decided` 记录人工结论，两者不得混淆。

所有执行记录都在 `.aiwf/runs/<workflow_id>/<run_id>/` 下：**每次运行一个独立
文件夹**，含 `record.json`（审计记录）、`context.md`（自动生成的共享上下文）
与 `record.log.jsonl`（增量日志）。同一工作流多次执行各自成目录，每次状态变更
立即落盘。记录分两层：**events 事件流**（用户输入、验收、修订的全过程）和
**node_runs 节点执行记录**。

`record.json` 是快照，`record.log.jsonl` 是它之后每次状态变更的一行增量；
引擎每 64 次落盘（或运行结束时）重写一次快照并清空日志。因此**运行中**的
`record.json` 最多落后日志 64 次变更，**运行结束后必然与全量状态一致**。
自己读盘时不要只读 `record.json`：用 `runs show/events/search` 或
`storage.load_record()`，它们会自动回放日志；直接读文件只在运行已结束可靠。

**子工作流记录嵌套在父运行目录内**，不另开顶层目录：

```
runs/<父wf>/<父rid>/
  record.json, context.md, workflow.json  # 1.1 定义快照
  record.log.jsonl                         # 快照之后的增量日志
  foreach-<node>-items.json                # foreach 条目快照（只读）
  foreach-<node>-results.json              # foreach 实例结果清单
  children/<node>/subflow.json             # 内联 nodes 合成的子流程定义
  children/<node>/<instance>/record.json   # 每个实例一份子记录（single 固定 "0"）
  children/<node>/<instance>/record.log.jsonl
```

引擎队列（`items: {"queue": true}`）**没有清单文件**：队列本体是记录顶层的
`queues` 字段（`queues.<节点 id>` = `{items, done}`）——运行中它可能还在
`record.log.jsonl` 增量里、尚未并进快照，读权威值请用 `runs show --json`。
`foreach-<node>-items.json` 只是它的只读快照，改快照无效。

子记录的 `parent` 字段指向父 run 与节点；查找时引擎沿该链定位。

### 1. 列出运行

```bash
aiwf runs list                      # 最近 20 条运行（--limit 0 看全部）
aiwf runs list short-drama          # 某个工作流的运行
aiwf runs list --status failed      # 只看失败
aiwf runs list --all                # 含子工作流实例（标注 "(of <父run_id>)"）
```

`runs list` 默认只列顶层运行、按时间倒序取前 20 条；子级用 `--all` 显示，
`--limit N` 调整条数（`0` = 全部）。

每行显示：**标题**、开始时间、run_id、workflow、状态、总耗时、节点数。标题在最前，
它就是「这次是哪个实例」的答案（来自 `run --title`，或被定义的 `view.dynamic_title`
取代）；标题过长的会截断并带 `…`，`--json` 里对应 `title` 字段，取的是完整值。
仍在运行的运行耗时列显示 `-`（`--json` 里 `duration_ms` 为 null）。

### 2. 查看单次运行

```bash
aiwf runs show                               # 最近一次运行
aiwf runs show <run_id>                       # 概览：业务阶段视图 + 节点耗时与摘要
aiwf runs show <run_id> --view                # 只看业务阶段视图（定义声明了 view 时）
aiwf runs show <run_id> --view --json         # 同上，机器可读
aiwf runs show <run_id> --node <node_id>      # 某节点的完整记录
aiwf runs show <run_id> --json                # 整条记录原始 JSON
aiwf runs events <run_id> [--json]            # 事件流时间线
```

定义声明了 `view` 时，概览**最前面**先渲染业务阶段（按节点顺序排列；阶段名、状态、
结论，卡住时连该敲的命令一起给出），循环阶段的队列条目**缩进在所属阶段下面**，
再是节点流水，最后是实例表；`--view` 只看这一层。
字段、占位符与默认渲染见 [业务进展视图](#业务进展视图view)。

父运行含 subflow/foreach 节点时，概览额外列出**实例表**：实例 key、状态、
exports 摘要、子 run_id 与记录目录；单实例细节用 `runs show <child_run_id>`。

#### node_run 字段含义

| 字段 | 说明 |
|------|------|
| `status` | `success` / `failed` / `skipped`（修订中复用旧产物）/ `rejected`（产物不符 Schema 或主文件契约，节点未关闭） |
| `started_at` / `ended_at` / `duration_ms` | 节点从进入到 complete/fail 的**开放墙钟时间**，毫秒。其中包含 agent 思考、工具执行与等待/交接时间，并不严格等于纯任务计算耗时，不宜直接当作性能指标对比 |
| `model` | 执行 agent 实际上报的模型；未上报为 null |
| `tokens` | agent 上报的 prompt/completion/total token；未上报 null |
| `input` | 渲染后 prompt 和显式 inputs 材料；start/end/skipped 为 null |
| `output` | 短结果或文件引用 `{"$file": "路径"}`（单输出 ≤2000 字符）；skipped 节点为复用的旧输出；rejected 节点保留被驳回的产物 |
| `summary` | 输出自动生成的 <=160 字摘要 |
| `decision` | branch 专用：answer + 实际选中的 route |
| `subflow` | subflow/foreach 专用：子工作流 id、实例进度（success/failed/pending） |
| `feedback` | 驱动本次（重）执行的验收反馈 |
| `revision_seq` | 所属修订的序号；非修订为 null |

> 独立 `review` 检查点本身**不产生 node_run**，只在事件流留
> `review_requested / review_decided`；内嵌验收则体现在被审 task 的记录里。
> 节点状态为 `rejected`（不符输出契约）时节点并未关闭，记录中保留被驳回
> 的产物，修正后重新 complete，run_id 与运行目录均不变。

#### 运行级字段

`inputs`（启动输入）、`context`（最新上下文）、`phase`、`last_feedback`、
`error`（失败原因）、`pending_review`（验收门状态）、`revision`（当前/最近修订）、
`parent`（子记录专用：父 workflow/run/node/instance）、`subflow_state`（父记录
专用：每个 subflow/foreach 节点的 kind、items_mode、done、instances 列表）。
1.1 额外有 `workflow_version` 和 `pending_check`；foreach 的 `items_snapshot`
指向本次运行使用的条目快照（队列模式随每次投喂重写）。输出文件存在不代表内容已通过业务检查。
顶层 `queues` 只在定义里出现 `items: {"queue": true}` 时才有，
存 `{items, done}`（`done` 即封口），改动一律经 `aiwf items`。

### 3. events 事件流

事件不可变、只追加，按发生顺序记录。类型：

| event | 含义 |
|-------|------|
| `run_started` / `run_resumed` / `run_failed` | 运行生命周期 |
| `input_provided` | 节点执行前用户提供的信息（values 为本次回答快照） |
| `input_changed` | 修改已完成节点的输入，自动开启修订 |
| `review_requested` | 进入验收门（含渲染后 requirements） |
| `review_decided` | 人的验收结论（verdict + feedback） |
| `output_rejected` | complete 上报的产物不符输出契约（含 reason；run 不失败，修正后重报） |
| `check_requested` / `check_decided` | Agent 质检与返工，含 attempt、verdict、reason 及 raw I/O |
| `revise_requested` | 发起计划外回跳（含 stale/reused 清单、mode、scope） |
| `node_reused` | 级联修订中某节点跳过、复用旧产物 |
| `revision_completed` | 修订关闭 |
| `subflow_started` | 实例启动（含 instance、child_run_id） |
| `instance_completed` / `instance_failed` | 实例结束（含 exports 快照 / error） |
| `instance_redone` | redo 重开单个实例（含 reason） |
| `queue_items_added` / `queue_item_updated` / `queue_item_removed` / `queue_item_moved` / `queue_sealed` | 引擎队列的投喂与改动记账（含 node 与 items/item；`moved` 另带目标位 `to`） |

队列事件只记「调用方喂了什么数据」，不改动 run 的调度位移，
因此**不进 action 指纹**——节点给队列投喂后仍持有自己那个有效的 action。

context 始终只存最新值；输入与决策的演变历史只在 events 中。

### 4. 关键词检索

```bash
aiwf runs search "赛博"
aiwf runs search "revision" --json
```

检索范围含 workflow、状态、error、inputs、events、各节点 id/名称/摘要。
命中后打印 run_id 与状态；细节用 `runs show`。

### 5. 共享上下文 context.md

与 `record.json` 同目录的 `context.md` 由引擎在每次落盘时自动重写（确定性、
无 LLM），含：运行状态与当前节点、输入、共享上下文内联短值、**产物索引**
（每个 `$file` 路径 + 来源节点）。产物索引递归收集：节点输出与嵌套对象/数组
中的全部 `$file`、foreach items 源里的文件（`{$file}` 形态；队列模式没有清单文件，
队列是 run 状态、不进产物索引）、以及所有子运行（含更深层嵌套）的
产物；只有 `{"$file": "..."}` 标记的路径会被收录，普通字符串路径不算。用途：

- 给人看进度：不用翻 JSON，读 context.md 即知当前在哪、产物在哪；
- 交接给另一个 agent：先读它再用 `status` 接管；
- 审计产物时，用产物索引核对是否有漏产出或路径失效。

### 审计要点（回答用户时按需覆盖）

- **时间线**：events 还原交互顺序，node_runs 还原耗时分布，指出最慢节点。
- **数据流**：task 输出经 output_key 进 context，后续节点用 inputs 中的 `{"ref":"key"}` 消费；
  大产物经 `$file` 引用、正文留在文件；结合 context.md 产物索引核对数据是否符合预期。
- **失败回溯**：定位第一个 failed 节点，结合 error 与该节点记录给原因。
- **修订审计**：检查 revision 与 revise_requested 的 stale/reused 清单是否合理，
  skipped 节点复用的输出是否确实不受影响，有无异常重做或漏改。
- **验收审计**：检查 review_requested/decided，确认验收轮次、结论与反馈。
- **子流程审计**：核对实例表与结果清单（成功/失败/待办数、exports），失败实例
  结合子记录回溯；确认 redo/整体重做后清单已刷新、无悬挂 pending。

## 业务进展视图（view）

`view` 是定义里一段**可选**声明，干两件事：把节点映射成「人关心的业务阶段」，
以及给这次运行起名（`dynamic_title`）。声明之后，`aiwf runs show` 会在原始节点流水
**之前**先渲染这层：标题 + 业务阶段。两件事各声明各的——只写 `dynamic_title`、
不写 `stages` 也合法（见[第二节](#二怎么写)）。

它解决的是节点流答不出的那个问题：

| 问题 | 只有节点流时 | 声明 `view` 之后 |
|---|---|---|
| 这次跑到哪一步了 | 要自己看节点名推断 | 直接给阶段名与状态 |
| **还剩哪些没做** | **答不出**——run 只记录发生过的事 | 未执行的阶段照样列出 |
| 卡在谁身上、下一步敲什么命令 | 要看 phase 再查文档 | 直接给出可执行命令 |
| 批量任务做到第几个 | 只有 `[success] 1 run <id>` | `已完成 2/7 · 当前 第 3 章 定计 · 还剩 5` |

**视图是派生的，从不存储。** 每次读取都从 run 记录重算，所以它不可能像手写状态
字段那样漂移。

### 一、什么时候写

- 流程有**多个阶段**，且阶段名不是节点名（`plan` → 「企划与大纲」）；
- 流程有**批量队列**（foreach），你想看到「做到第几个、还剩几个」；
- 流程有**人工门**，你想让视图直接告诉你敲哪条命令；
- 流程**产出的东西有名字**（小说、短剧、方案），想让这个名字出现在 `runs list` 里——
  用 `dynamic_title`（见下节），它的名字往往要等某个节点跑完才定得下来；
- 想让非作者（产品、美术、用户）也能一眼看懂跑成什么样。

一次性或三两步的直筒流程不必写——没有 `view` 的定义行为完全不变。

### 二、怎么写

```json
"view": {
  "dynamic_title": "${node.plan.title}",
  "stages": [
    {
      "name": "企划与大纲",
      "nodes": ["plan"],
      "gate": true,
      "detail": "大纲已出，共 ${node.plan.chapter_count} 章"
    },
    {
      "name": "逐章创作",
      "nodes": ["chapters"],
      "detail": "已完成 ${loop.done}/${loop.total} 章，还剩 ${loop.left} 章"
    },
    {
      "name": "汇编与交付",
      "nodes": ["assemble"],
      "gate": true
    }
  ],
  "items": {"label": "第 ${item.number} 章 ${item.title}"}
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `dynamic_title` | 否 | 本次运行的标题模板；**引用的占位符全部有值**才生效（见下） |
| `stages[].name` | 是 | 阶段名，给人看的 |
| `stages[].nodes` | 是 | 该阶段覆盖的**顶层**节点 id |
| `stages[].detail` | 否 | 结论模板；不写则用引擎默认（见下） |
| `stages[].gate` | 否 | 标记人工门；渲染时会把决定它的命令一并列出 |
| `items.label` | 否 | foreach 队列每行的标签模板 |

**多个阶段可以映射同一个节点。** `implement` 同时承载「实施改动」和「人工验收」
两段是常见写法——靠 run 的 phase 区分是谁在等待。

#### 这次运行叫什么（`dynamic_title`）

`aiwf run` **必须**给 `--title`。它是这次运行的**启动标题**：写小说时是「末世/重生」，
做重构时是「拆 storage 的写路径」。`runs list` 默认就显示它——没有标题的清单只能靠
run_id 认，没人认得出。

但启动时往往定不出正式名字（小说要等立项、方案要等评审）。`dynamic_title` 就是给这种
「名字后到」的情形用的：它是一个模板，**只要引用的占位符全部有值，就取代启动标题**。

| 顺序 | 取什么 | 例 |
|---|---|---|
| 1 | `dynamic_title` 模板（占位符**全部**有值） | `${node.plan.title}` → 《第七日重生》 |
| 2 | 启动时 `--title` 给的值 | 末世/重生 |
| 3 | `run_id`（老 run、子 run 没有启动标题） | 20260923-174619-840522 |

两条硬规则：

- **必须全部有值才生效**，缺一个就整条不用——半个名字（`《第七日重生》 · —`）比
  没有名字更糟。所以引用 `${node.plan.title}` 时，`plan` 没跑完之前一直是启动标题。
- **只在读取时算**，不落盘。改模板立刻影响所有历史 run 的显示；代价是**子 run 不继承
  父 run 的标题**，各按自己的 run 记录取（多数就是自己的 run_id）。

**把「定名」做成一个独立节点**，而不是塞进某个已有节点里。这样名字能在运行中单独改：

```bash
aiwf revise <run_id> --target <定名节点> --mode return --reason "书名改成《第七日重生：终章》"
```

列表随之更新，且下游产物不受影响——**前提是下游不引用这个节点的输出**（名字只交给
`dynamic_title` 用）。参考 `library/workflows/std.novel-writing.json` 的 `naming` 节点。

**`stages` 可以不写。** `view` 只声明 `dynamic_title` 也合法——流程短到没有阶段可讲时
（两三个节点的交付流程），照样能给 run 起名：`runs show` 不打印阶段块，只显示 `title`。
范例见[短剧多文件](../assets/examples/multi-file.json)。

`dynamic_title` 的变量域与 `detail` 完全相同（见第四节），但**更容易写错**：`detail` 里取不到
的占位符渲染成 `—` 就完事，`dynamic_title` 里取不到则整条回落——所以校验期就会拦住
未知节点、`${item.*}` 这类写法。

### 三、阶段状态怎么来

引擎从 run 记录派生，按这个顺序判断：

| 状态 | 标记 | 判据（按此顺序判定） |
|---|---|---|
| `failed` | `ERR` | 该阶段任一节点有失败记录 |
| `waiting` | `wait` | run 正停在该阶段的**门上**（phase 为 `awaiting_*`）。注意：门前的节点可能**已经成功**，但还没人裁决——所以它不算完成；含队列里当前项卡住的情形 |
| `done` | `ok` | 该阶段所有节点都已成功 |
| `active` | `>>` | 正停在该阶段，或已越过它开始执行 |
| `pending` | （空） | 还没走到 |

### 四、`detail` / `dynamic_title` 的变量域（**封闭**）

两者都是**只含占位符的字符串**，可以拼接，但**不能做算术**：

| 占位符 | 值 |
|---|---|
| `${node.<id>.<path>}` | 该节点最近一次成功输出的字段（支持点号路径） |
| `${node.<id>.summary}` | 人话摘要：节点输出里的 `summary`，没有则回落引擎的摘要 dump |
| `${loop.done}` | 队列已完成数 |
| `${loop.total}` | 队列总数 |
| `${loop.left}` | 队列剩余数 |
| `${loop.current}` | 当前项的实例 key |
| `${loop.current_index}` | 当前项下标 |
| `${loop.current_label}` | 当前项经 `items.label` 渲染后的名字 |
| `${loop.current_stage}` | 当前项走到子流程的哪一阶段 |
| `${loop.current_action}` | 当前项卡在的门（对象，一般不必写） |
| `${phase}` / `${status}` | run 的相位 / 状态 |
| `${run.<field>}` | `run_id` / `workflow_id` / `status` / `phase` / `started_at` / `ended_at` / `duration_ms` |

**为什么不给表达式**：`loop.left` 由引擎算好再交给你，模板只做拼接。这样
`validate` 能在定义期就查出写错的占位符，而不是运行时渲染出一个空值；也不需要
在引擎里塞一个求值器。**算术归引擎，模板只做拼接。**

取不到的占位符渲染成 `—`，不会中断命令。**`dynamic_title` 是个例外**：它取不到就整条
回落，绝不显示 `—`（理由见上一节）。

`${item.<path>}` **只在 `items.label` 里合法**——阶段详情与 `dynamic_title` 都没有"某一个项"可指。

### 五、默认渲染

不写 `detail` 时引擎兜底：

- **普通阶段** → 该节点输出里的 `summary`；没有就回落 `node_run.summary`；
- **foreach 阶段** → 阶段行给 `已完成 2/7 · 当前 第 3 章 定计 · 还剩 5`，
  全部完成时是 `已完成 7/7 · 全部完成`；逐条清单缩进在阶段行下面，当前项带
  `<- 当前`（子流程阶段名附在其后）。

### 六、怎么读

```bash
aiwf runs show <run_id>              # 概览：先业务阶段，再节点流水，最后实例表
aiwf runs show <run_id> --view       # 只看业务阶段
aiwf runs show <run_id> --view --json
```

输出形如——**标题在最前，阶段按节点顺序排列，队列条目缩进在所属阶段下面**：

```
title      : 《第七日重生》
view       :
  [ok  ] 方向选型  -- 题材与受众已定，视角：第三人称限知
  [ok  ] 大纲     -- 3 章梗概已出
  [ok  ] 人物设定
  [>>  ] 逐章创作  -- 已完成 2/3 章，还剩 1 章
    [ok  ] 1  第 1 章 第七日重生
    [ok  ] 2  第 2 章 定计
    [>>  ] 3  第 3 章 决战  <- 当前（草拟正文）
  [    ] 全书合稿
```

卡在门上时，阶段状态变成 `wait`，命令缩进在其下：

```
view       :
  [ok  ] 逐章创作  -- 已完成 3/3 章，还剩 0 章
    [ok  ] 1  第 1 章 第七日重生
    [ok  ] 2  第 2 章 定计
    [ok  ] 3  第 3 章 决战
  [wait] 全书合稿
    -> aiwf submit 20261005-101500-a1b2 --verdict APPROVE|REJECT --feedback "..."
```

门命令走 `submit`：模板里的 action 指纹负责"看到的是哪一步"，过期会退 4；
执行前后状态一变，旧模板立刻失效。视图每次读取都按当前状态重算，
所以**看到命令就立刻跑**，不要缓存。

缩进层级：阶段两格，阶段自己产出的行（队列条目、门命令）四格。
`--json` 里同一份数据在 `stages[].queue`（只有循环阶段才有该键）。

### 七、校验会拦住什么

`aiwf validate` 对声明了 `view` 的定义额外检查：

- 阶段里的节点 id 必须存在；
- **每个 task / review / subflow / foreach 节点都必须被某阶段覆盖**——
  漏覆盖的节点会让视图"闷声少说"，比没有视图更坏。**只声明 `dynamic_title`、没写
  `stages` 的 view 不做这项检查**：那是在给 run 命名，不是在描述流程；
- 占位符的根、节点引用、`loop.*` 字段名、`item` 的使用位置——`dynamic_title` 同样在此列；
- **已经废弃的 `view.title` 一写就报错**（改成 `dynamic_title`），免得名字静默丢失。

写错在定义期就报错，不会拖到渲染时变成一个 `—`。

### 八、已知边界

- **`loop.*` 只反映当前挂在记录上的那一个 foreach。** 记录里只有一份
  `subflow_state`，所以一个定义里**串行多个 foreach** 时，只有最后一个还能被读出。
- **`items.label` 读条目快照**（`foreach-<node>-items.json`）。1.1 运行都会写；
  队列模式下该文件随每次投喂重写，标签因此跟着更新；更早的历史记录没有该文件，标签会回落成实例 key（`1  1`）。
- **`current_stage` 需要子 run 记录可读**；读不到就是 `null`，不影响其余字段。
- **队列里卡住的门，命令指向子 run**，不是父 run——那个 action 属于当前实例。
  若 run 只是"在等队列"而没有具体门，阶段显示 `wait` 但**不给命令**：挂在父 run
  上的指令会被引擎判为 stale。
- **`kind` 取值**：`review` / `check` / `provide` / `resume`，与引擎一致。
- **门命令的引号是 POSIX 形态**（`--output '{"verdict":"PASS"}'`）。Git Bash / WSL 下可
  直接复制执行；**Windows PowerShell 会剥掉 JSON 的双引号**（实测 `'{"verdict":"PASS"}'`
  传成 `{verdict:PASS}`）。此时改用 `--verdict PASS --reason "…"` 或
  `--json-file <json 文件>`（已容忍 BOM），不要手拼内联 JSON。
- **视图跟随定义。** run 目录若有 `workflow.json` 快照就用快照；没有则用当前定义
  ——改定义会让老 run 的视图跟着变。这是刻意的：视图是"当下怎么看这次运行"。
- **记录早于 `workflow_version` 字段时定义无法解析**，视图位置显示
  `(unavailable: ...)` 并说明原因，`--view` 返回码 2。此前的数据可手工补该字段。
- **队列的可编辑范围由引擎定，视图只呈现**：已启动项不可动，**未启动**项可增删改
  （`aiwf items add|update|remove|seal`）。视图给你的 `--json` 里每项带 `editable`，
  要不要改由业务与引擎判据决定，视图不改变这一点。
