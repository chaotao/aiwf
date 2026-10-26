# 业务进展视图（view）

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

## 一、什么时候写

- 流程有**多个阶段**，且阶段名不是节点名（`plan` → 「企划与大纲」）；
- 流程有**批量队列**（foreach），你想看到「做到第几个、还剩几个」；
- 流程有**人工门**，你想让视图直接告诉你敲哪条命令；
- 流程**产出的东西有名字**（小说、短剧、方案），想让这个名字出现在 `runs list` 里——
  用 `dynamic_title`（见下节），它的名字往往要等某个节点跑完才定得下来；
- 想让非作者（产品、美术、用户）也能一眼看懂跑成什么样。

一次性或三两步的直筒流程不必写——没有 `view` 的定义行为完全不变。

## 二、怎么写

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

### 这次运行叫什么（`dynamic_title`）

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

## 三、阶段状态怎么来

引擎从 run 记录派生，按这个顺序判断：

| 状态 | 标记 | 判据（按此顺序判定） |
|---|---|---|
| `failed` | `ERR` | 该阶段任一节点有失败记录 |
| `waiting` | `wait` | run 正停在该阶段的**门上**（phase 为 `awaiting_*`）。注意：门前的节点可能**已经成功**，但还没人裁决——所以它不算完成；含队列里当前项卡住的情形 |
| `done` | `ok` | 该阶段所有节点都已成功 |
| `active` | `>>` | 正停在该阶段，或已越过它开始执行 |
| `pending` | （空） | 还没走到 |

## 四、`detail` / `dynamic_title` 的变量域（**封闭**）

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

## 五、默认渲染

不写 `detail` 时引擎兜底：

- **普通阶段** → 该节点输出里的 `summary`；没有就回落 `node_run.summary`；
- **foreach 阶段** → 阶段行给 `已完成 2/7 · 当前 第 3 章 定计 · 还剩 5`，
  全部完成时是 `已完成 7/7 · 全部完成`；逐条清单缩进在阶段行下面，当前项带
  `<- 当前`（子流程阶段名附在其后）。

## 六、怎么读

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

## 七、校验会拦住什么

`aiwf validate` 对声明了 `view` 的定义额外检查：

- 阶段里的节点 id 必须存在；
- **每个 task / review / subflow / foreach 节点都必须被某阶段覆盖**——
  漏覆盖的节点会让视图"闷声少说"，比没有视图更坏。**只声明 `dynamic_title`、没写
  `stages` 的 view 不做这项检查**：那是在给 run 命名，不是在描述流程；
- 占位符的根、节点引用、`loop.*` 字段名、`item` 的使用位置——`dynamic_title` 同样在此列；
- **已经废弃的 `view.title` 一写就报错**（改成 `dynamic_title`），免得名字静默丢失。

写错在定义期就报错，不会拖到渲染时变成一个 `—`。

## 八、已知边界

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
