# 高阶：批量、队列、修订与集成

批量与复用、运行时队列、计划外修订，以及 SDK / 显式图 / 旧版本边界。出错或确实需要时再读。

## 目录

- [批量任务与子工作流](#批量任务与子工作流)
- [高阶：运行中追加条目的引擎队列](#高阶运行中追加条目的引擎队列)
- [高阶：修订、依赖级联与实例重做](#高阶修订依赖级联与实例重做)
- [高阶：SDK、显式图与兼容](#高阶sdk显式图与兼容)

## 批量任务与子工作流

固定几个阶段用顺序 task；对每个条目执行同样步骤用 foreach；
相同步骤需多处复用时抽成独立工作流，通过 subflow 调一次或 foreach 调多次。
foreach 顺序执行，不能用它承诺并行加速。

### 有清单的批量任务：foreach

下面是节点片段，假设 plan 已输出 chapters 数组与 outline 文件引用。
完整可运行版本在范例目录的 `novel-chapters.json`。

```json
{
  "id": "chapters",
  "type": "foreach",
  "items": {"ref": "plan.chapters"},
  "id_field": "number",
  "max_instances": 100,
  "inputs": {
    "outline": {"ref": "plan.outline"},
    "previous": {"ref": "previous"}
  },
  "nodes": [
    {
      "id": "draft",
      "type": "task",
      "inputs": {
        "chapter": {"ref": "item"},
        "outline": {"ref": "outline"},
        "previous": {"ref": "previous"}
      },
      "prompt": "按本章规划写作；previous 非空时先读上一实例导出的正文，保持连续性。",
      "output": {"file": "${run.dir}/chapter-${item.number}.md"},
      "check": {
        "criteria": ["关键事件完整", "与大纲及前章连续", "冲突推进且章尾有钩子"],
        "max_attempts": 3
      },
      "review": {"requirements": ["本章是否可以定稿"]}
    }
  ],
  "exports": ["draft"]
}
```

- items 可以是数组、`{"ref":"path"}` 指向数组或文件引用、`{"$file":"清单路径"}`（文件里是 JSON 数组，静态）。
  数量事先未知时改用引擎队列 `{"queue": true}`，见下文与高阶“运行时队列”。
- 子级 context 只含显式 inputs，加当前 `item` 和从零开始的 `index`；
  不会自动继承父 context。
- 父级 inputs 解析时可用 `previous`：前一个实例的 exports 对象，第一项为 null。
  需显式映射后才传给子级。previous 不来自目录、不会携带任意兄弟 context。
- 静态清单用 `id_field` 作稳定实例标识；队列模式 (`{"queue": true}`) 恒用固定的 `id`。
  两种情况重做都保持实例身份，不自行数目录。
- `as`/`index_as` 可重命名 item/index，避免复杂复用时冲突。
- exports 声明子级要返回的 context 键；内联时可用数组，也可用 key→Schema 映射。
- 父级输出是引擎写的结果清单 `$file`，默认 context 键为 foreach id；
  清单每项含 key/status/exports。汇总读取清单，失败项不得当作成稿。
- `on_fail: "abort"` 默认中止；`continue` 留存失败再处理下一项。
- foreach 是顺序执行；级联重做命中 foreach 时整体重做。
- 先前实例更新后，仅 redo 本实例不重做其他实例；有衔接依赖时应整体重做。
- `max_steps` 是每个 run 的推进预算，默认 200；内联体从父定义继承数值，但预算按各自
  的 node_runs 独立计算，不是父子共享一份。
  它按该 run 的 `node_runs` 记录条数计（含 start/end、failed、rejected 与修订复用的
  skipped），因此就是记录条数上限；不等于业务条目数（如章节数），
  需为循环和验收拒绝留余量。

### 汇总批量产物

父级 foreach 完成后，下一个任务写：

```json
{
  "id": "assemble",
  "type": "task",
  "inputs": {"manifest": {"ref": "chapters"}},
  "prompt": "读取 manifest 清单，按顺序读取 success 项的 exports.draft 文件并汇总；列明失败项，不把失败当成成稿。",
  "output": {"file": "${run.dir}/novel.md"}
}
```

manifest 是文件引用，需要 Agent 读文件；不是内存中的结果数组，
不能直接 `{"ref":"chapters.0.exports.draft"}`。
结果 JSON 清单的每项包含 key/status/exports。
`on_fail: "continue"` 时即使父流程完成也可能有失败项，交付前须明确数量及影响。

### 单次复用：subflow

子工作流 `analyze-piece` 的顶层声明：

```json
{
  "input_schema": {
    "type": "object",
    "required": ["text"],
    "properties": {"text": {"type": "string"}}
  },
  "exports": {
    "analysis": {
      "type": "object",
      "required": ["summary"],
      "properties": {"summary": {"type": "string"}}
    }
  }
}
```

以上是顶层字段片段；子定义仍需 schema_version/id/name/nodes，
且实际节点必须输出 analysis。父节点调用：

```json
{
  "id": "analyze",
  "type": "subflow",
  "workflow": "analyze-piece",
  "inputs": {"text": {"ref": "text"}}
}
```

子流程结束后，父 context.analyze 包含 exports 对象，
下游取 `{"ref":"analyze.analysis.summary"}`。不是直接 `analyze.summary`。
顶层 exports 只能是“键 → Schema”的对象，不能写数组；不约束类型可用 `{}`。
两个完整定义见范例目录的 `subflow-parent.json` 与 `analyze-piece.json`。

### 批量调用复用体

将 foreach 的内联 nodes 换成 `"workflow":"analyze-piece"`，
在 foreach.inputs 中把条目映射为子流程要求的 text：

```json
{
  "id": "analyses",
  "type": "foreach",
  "items": ["第一段", "第二段"],
  "workflow": "analyze-piece",
  "inputs": {"text": {"ref": "item"}}
}
```

此时 exports 在被调用定义的顶层配置，不在 foreach 上配置。
nodes 与 workflow 必须二选一；内联体自动包 start/end，不可手写。
内联 exports 可用数组 `["draft"]`，也可用 Schema 映射。
被调用工作流的 input_schema 会校验子级实际入参；嵌套最多八层。
完成后执行 `aiwf validate --project`，检查子定义、必需入参和调用环。

每项 run.dir 独立，避免所有实例覆盖同一文件；需要共享目录时显式传入，
同时保证文件名有稳定实例标识。未知数量的批量任务另读高阶“运行时队列”。

## 高阶：运行中追加条目的引擎队列

只有数量事先未知、且需要边执行边补充条目时才使用。
已知清单用静态 foreach；固定次数返工用 task.check。

### 队列不是文件

节点字段片段：

```json
{
  "id": "chapters",
  "type": "foreach",
  "items": {"queue": true},
  "max_instances": 100
}
```

队列是 **run 状态**（记录顶层的 `queues.<节点 id>`），由**引擎**持有。
任何调用方（包括 Agent）都不写队列文件；改动一律走 `aiwf items` 命令。
运行中这份状态可能还停在 `record.log.jsonl` 增量里、尚未并进 `record.json` 快照，
所以**读权威值要走引擎 / CLI**（`aiwf items list`、`runs show --json`），别直接解析快照文件。
`foreach-<节点 id>-items.json` 只是引擎写出的**只读快照**，供 `view` 与读者查看，
不是可编辑的源——改它没有用，下次成稿会被覆盖。

旧形态 `{"done": bool, "items": [...]}`（Agent 维护的可变清单文件）**已移除**，
引擎读到会直接报错；`{"$file": "…"}` 退回纯静态语义（文件里必须是 JSON 数组）。
完整定义见范例目录 `queue-items.json`。

### item 的形状

固定字段 `id` 与 `name`，其余键是调用方拓展，原样透传给子级 `item`：

```json
{"id": "CH-001", "name": "醒来", "outline": "…", "words": 3000}
```

- `id`：实例键（取代静态模式的 `id_field`），非空、匹配 `^[A-Za-z0-9_-]+$`、队列内唯一；
- `name`：人读标签，非空；
- 拓展键：随业务定义，引擎不解释，只在 `update` 时按补丁合并；
- **`status` 由引擎派生**（`pending` / `running` / `success` / `failed`，就是对应子运行的实例状态），
  不存在队列里，所以不会与实跑漂移，也与 `view` 用同一套词；
- `queue.done` 是**封口**标记，与条目状态无关，别把两者混在一起读；
- `id` 不可修改：它认的就是审计身份。

### 命令

正常投喂不需要这些命令：`awaiting_items` 时改 `submit.json` 的 `output`（条目数组）/
`seal`，再 `aiwf submit` 即可。下面是队列的排查/精细编辑入口。

| 命令 | 作用 |
|---|---|
| `aiwf submit <run_id> [--item-file items.json] [--seal]` | 正常投喂/封口（推荐） |
| `aiwf items list <run_id> [--node <node>]` | 打印队列与每项的派生状态 |
| `aiwf items add <run_id> [--node <node>] --item-file items.json` | 追加一项或一批（JSON 数组/对象；`--item '<JSON>'` 仍可用） |
| `aiwf items update <run_id> [--node <node>] --id <id> --set '<JSON>'` | 改一项**未启动**条目的字段 |
| `aiwf items remove <run_id> [--node <node>] --id <id>` | 删一项**未启动**条目 |
| `aiwf items move <run_id> [--node <node>] --id <id> --to <index>` | 重排一项**未启动**条目（0 基目标位） |
| `aiwf items seal <run_id> [--node <node>]` | 封口：不再接受追加，停止等待 |

`run_id` 可省略（唯一 running / 最近）；`--node` 在一个 run 只有一个队列时可省。

`<run_id>` 是**声明该节点的那一层**的 run。队列可以挂在根 run，也可以挂在子流程里，
引擎会沿 parent 链找到持有该节点的记录。

`--json` 给结构化结果，`items list` 每项带 `index / id / name / status / editable / fields`。

### 推进顺序

1. 节点启动时队列为空且未封口 → run 停在 `awaiting_items`。
2. 调用方逐条 `items add`。**每次 add 会自动推进**：只要 run 正停在 `awaiting_items`
   且队列里有待办项，引擎就地恢复调度、为最先的待办项建子运行——不必另发 `resume`。
3. 队列暂时没有可跑的项而业务尚未结束时，`add` 后仍停在 `awaiting_items`
   （action.kind=`resume`）。可用 `resume` 触发一次重调度，但**不能忙循环**。
4. 业务结束时 `items seal`。封口后 `add` 报错；已有项全部结束后节点收尾。
   **空队列 + 已封口直接收尾**，不再等待。

`current` 只读；看状态不会消费任何条目。

### 可变范围

| 条目的状态 | 追加新项 | update / remove / move 它 |
|---|---|---|
| 未启动（`pending`，尚无子运行） | — | 可以 |
| 已启动（已有子运行） | 只能追加**新**项 | **不可以**，改走修订 / 重做 |

`move --to <index>` 的可用区间是 `[已启动数, 队列长度)`：未启动项能移到
「下一个将要执行」那个位置，但**不能越过任何已经跑过的项**。

审计身份是 `(key, run_id, started_at)`；已启动项有一条子运行与之对应，
再改它会让记录与实跑对不上。因为 foreach **严格顺序执行**，
已启动项恒是队列**前缀**，所以编辑 / 删除 / 重排后缀里的待办项
**不会给已跑过的项重新编号**，对齐不被破坏。

需要重写已启动的条目，用明确的修订 / 重做流程，不要偷改队列使审计身份失效。

每次改动前先 `items list` 看清哪些还可编辑；同一根运行由一个执行者驱动，
不要让多个 Agent 同时改队列与状态。

队列不是质量检查器：先确认前一实例结果，再决定下一项，
不要单凭目录里出现文件推断上一章成功。
连续性依赖用 `foreach.inputs` 显式传 `previous`，其值是前一实例 `exports`，不是全文。

### 边界和排查

| 现象 | 检查 |
|---|---|
| 一直 awaiting_items | 队列是否已 seal；是否还有待办项；`add` 是否真的成功 |
| `add` 报 sealed | 已经 `items seal` 过；封口是终局 |
| `add` 报 id 已存在 | id 在队列内必须唯一 |
| `add` 报超 max_instances | 队列容量上限；调节点字段或清理待办项 |
| `update` / `remove` / `move` 报已启动 | 该项已有子运行；改用修订 / 重做 |
| `move` 报越界 | 目标位落到了已启动前缀里；只能移到 `[已启动数, 长度)` |
| 清单被拒绝 | 缺 `id`/`name`、id 不合法或重复 |
| 子级读不到新材料 | 材料是否显式通过 inputs 传入；文件是否实际存在 |

动态次数虽未知，也要给 `max_instances` 与 `max_steps` 合理上限。
内联子运行从父定义继承 `max_steps` 数值，但每个 run 按自己的 `node_runs` 计数，
预算互不相干；不要把 `max_steps` 当成「最大章节数」。

## 高阶：修订、依赖级联与实例重做

仅在用户改变已完成内容、已有结果必须返修时使用。
当前任务的常规质量返工用 check / review；临时工具失败修好后 resume。

### 先选操作

| 需求 | 操作 |
|---|---|
| 改已完成 task，自动重做依赖它的结果 | revise --mode cascade（默认） |
| 仅改目标 task，完成后返回发起位置 | revise --mode return |
| 更改已完成 task 的用户答案并启动修订 | provide --node |
| 只重做 foreach / subflow 的一个实例 | revise --target --instance |
| 代码或工具临时失败，定义本身正确 | 修好外部条件后 resume |
| 定义写错、需要改变节点图 | 修改源定义、校验并启动新 run |

普通叶子动作使用根 run_id 和 action.id；下列是**显式管理操作**，
必须使用目标所在的实际 run_id。通过 action.run_id 或审计记录定位，
不要把根 id 当成子任务 id。

### 修订已完成任务

```bash
aiwf revise <target_run_id> --target outline --reason "用户调整主线" --mode cascade --json
aiwf revise <target_run_id> --target outline --reason "仅修正错字" --mode return --json
```

目标必须是已成功执行的 task。cascade 按依赖关系标记 stale，
未受影响的节点可复用，审计中记录为 skipped/node_reused。
return 只重做目标，不保证下游结果与它保持业务一致；确有内容影响时使用 cascade。

显式控制范围：

```bash
aiwf revise <target_run_id> --target outline --reason "全篇受影响" --scope all --json
aiwf revise <target_run_id> --target outline --reason "指定修订范围" --scope manual --nodes chapter_a,final --json
```

自动依赖来源包括 inputs 的 ref/插值、prompt 插值、when、output 路径、
check 检查项与 consumes。它是数据依赖分析，不理解隐含剧情或代码业务影响。
Agent 私自从目录读取但未声明的材料，可能不会被正确判为依赖。

必要时补充 task 字段：

```json
"consumes": ["outline", {"glob": "chapters/*.md"}]
```

glob 匹配被引用产物的文件路径，不是让引擎执行文件搜索或生成材料。
通常直接声明 inputs 就够了；使用共享可变文件时尤其要核对依赖。

### 修改历史用户输入

```bash
aiwf provide <target_run_id> --node outline -p tone="冷峻" --reason "用户调整风格" --json
```

values 仍按该 task 的 user_input_schema 校验，成功后开启修订。
这与当前 awaiting_input 的 `provide --action` 不同；不要混用。

### 单实例重做

```bash
aiwf revise <parent_run_id> --target chapters --instance 1 --reason "第一章需要重写" --json
```

instance 是稳定实例 key：静态清单取 `id_field` 字段，队列模式 (`{"queue": true}`)
取 item 的 `id`；两者都没有时是从零开始的索引键。
单实例 redo 不自动重做后面的兄弟实例，也不自动重写所有下游成品。
章节依赖 previous 时，改第一章通常影响后续章节；应明确扩大修订范围，
或从上游任务发起能覆盖整个 foreach 的级联修订。
整体 foreach 重做会保留子运行审计，汇总应重新读取结果清单，不能数目录。

### 检查预算耗尽与恢复

check 拒绝耗尽后 run 为 failed。resume 不清零 attempt，也不能强行补报 PASS。
用户决定继续返修时，可先 resume 恢复运行，再对已有成功上报的该 task 显式
revise，开启新的检查预算；若没有可修订的成功记录，则修正定义 / 输入后新建运行。
记录具体原因，不能无限自动重置预算逃过终止保护。

### 修订完成与审计

revision 不是“当前正在执行旧节点”的同义词。
级联修订要等受影响范围被重做或按规则复用后才结束，
所以已经进入新节点时，修订状态仍可能存在。
用 runs show/events 核对 stale/reused 与 revision_completed，
不要仅凭当前 node id 判断是否已关闭。

1.1 run 使用定义快照。修改源 JSON 不会修复正在运行的图；
不要直接编辑 record.json、workflow.json 快照来绕过契约。

## 高阶：SDK、显式图与兼容

日常创建和执行优先 CLI；仅自建驱动器、维护旧运行时读取本页。

### SDK 的 1.1 驱动循环

```python
from aiwf import Storage, WorkflowEngine

storage = Storage(".aiwf").init_dirs()
engine = WorkflowEngine(storage)
workflow = storage.load_workflow("opening")
record = engine.start(workflow, {"idea": "一次有代价的重生"})
root_id = record["run_id"]
engine.settle(root_id)
packet = engine.action_spec(root_id)
# 外部 Agent 执行业务、生成 result；不要让引擎调用 LLM。
# packet = engine.act(root_id, packet["action"]["id"], "complete", output=result)
```

start 后 settle 负责推进自动节点；action_spec 只读。
act 按动作代号提交并返回下一任务包，支持 complete/provide/review/fail；
用户回答用 `values`，人工验收用 `verdict` 和 `feedback`。
结果仍需经过文件契约、Schema、质检和人工验收。
不要直接循环低级 complete 来跳过 action 检查或子运行调度。
低级 API 为兼容保留，其职责和 CLI 的自动 settle 边界不同。

action.id 用于拒绝过期结果，不是多进程锁。同一根运行只允许一个驱动者。
外部工具执行中断时，接管者读取 current 并核实文件是否完成，避免重复修改。

### 显式图与预算

省略 edges 时数组顺序连线；一旦给 edges，就明确列出需要的普通连线。
branch / 独立 review 使用 routes，不再写出边。显式 start 最多一个；
无 start 则从第一个节点开始。顶层可写 end 表示多个路径的共同出口。
foreach 内联体自动生成 start/end，不手写它们。

循环和人工拒绝会消耗推进预算 max_steps，默认 200。
它按 node_runs 记录条数计（start/end、failed、rejected 与修订复用的 skipped 都算一条）。
它是最后的终止保护，不应代替具体业务的结束条件。
嵌套深度上限为八层，不支持递归工作流调用；用项目校验检查调用环。

### 模型与用量只是提示 / 审计

顶层 default_model、节点 model 提示外部 Agent 选择模型。
引擎不创建模型客户端、不调用 LLM，也不保证当前 Agent 能切换模型。

```bash
aiwf complete <root_run_id> --action <最新id> --output-file result.json \
  --model <实际模型> --tokens-file usage.json --json
```

usage.json 可包含 prompt/completion/total；不知道的值不编造。
节点 duration_ms 包含思考、工具、等待时间，不等于纯模型耗时。

### 1.0 与 1.1 的边界

| 项目 | 1.0 旧协议 | 1.1 推荐协议 |
|---|---|---|
| 数据引用 | 历史 var / 字符串插值 | ref 保留类型；插值只做文字 |
| 叶子调度 | Agent 跟踪父子 run | 根 run_id 直接得到当前叶子 action |
| 自动节点 | 逐节点 complete | settle 自动推进 start/end/when |
| 上报 | 无 action.id | 必须使用最新 action.id |
| 产物文件声明 / 有限质检 | 不支持 output/check | task.output / check |

旧 task.input_schema 是 user_input_schema 的兼容别名，旧定义和快照仍可读。
新建或编辑 task 时用 user_input_schema，不同时写两者；
**工作流顶层的 input_schema 不改名**。

旧记录的执行方式：

- start/end 与确定性分支按旧协议 `complete --empty`。
- foreach 从 node.child.run_id 取得子运行，分别 current/complete/provide/review。
- 子级完成会回传父级；current 只读，需要重新调度时显式 resume。

不要直接改一个未完成 1.0 run 的 schema_version。
保留旧定义供旧运行恢复，另存新 id 的 1.1 定义并创建新运行。
新运行有 workflow.json 快照，源修改只影响未来运行；
旧记录没有快照且定义版本已改变时，引擎拒绝静默升级。
运行记录自身的 schema_version 与工作流版本分开，勿批量替换 record.json 中版本号。
