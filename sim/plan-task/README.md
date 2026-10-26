# sim/plan-task —— 把「制定计划 + 执行计划」承载到工作流上的仿真

这个仿真验证一件事：**一个工作流能否从用户诉求出发，自己产出计划（任务清单），
再逐条执行、整体验收、交付完成态计划**，并且 `view` 能把节点流呈现成一张人看得懂的计划看板
（目标是什么、做了哪些、还剩哪些）。

关键前提两条：

1. **plan 不是人的输入，而是工作流的第一个产出**。人只给诉求（`requirement`）与
   指令（验收/批准），计划怎么拆、拆成什么，由工作流里的「制定计划」节点决定。
2. **任务清单不是文件，而是引擎维护的运行时队列**。`plan` 节点逐条 `aiwf items add` 投喂、
   `aiwf items seal` 封口；**没有任何人（包括 Agent）写队列文件**。

## 目录

```
sim/plan-task/
  inputs.json                   # run 入参：{"requirement": "<自然语言诉求 + 约束 + 成功标准>"}
  .aiwf/workflows/plan-task.json# 工作流定义（含 view 声明）
  drive.py                      # 仿真驱动：把诉求立项 → 制定计划（投喂队列）→ 造产物 → 过质检 → 批验收
  plan/<plan_name>/             # ★ 业务产物（Agent 产出，给人看）
  .aiwf/runs/plan-task/…        # ★ 引擎记录（队列就在这里；只有引擎运转所需的东西）
  .archive/                     # 历史 run 与旧产物（换形状前的证据，不参与运行）
```

**产物分家**：给人看的 md 落在**项目目录** `plan/<plan_name>/`；
`.aiwf` 只放引擎运转所需的记录（run 记录、定义快照、条目快照、子 run）。

> `plan.json` 这个名字在本版里**已经不存在**了。上一版它是任务清单的载体
> （Agent 写、foreach 读）；现在清单由引擎持有，见下文。

## 工作流形状

```
诉求 ─▶ 立项 intake ─▶ 制定计划 plan ─▶ 逐条执行 work(foreach) ─▶ 整体验收 accept ─▶ 交付完成 finalize
                      （投喂队列 + 计划书）      │
                                                └─ 每个任务一个节点：执行与自检 execute(check + review)
```

| 节点 | 做什么 | 产出 |
|---|---|---|
| `intake` | 读 `requirement`，提炼 `goal / scope / constraints / acceptance / plan_name` | **短对象，内联上报、不写文件** |
| `plan` | ① 逐条投喂引擎队列 + `seal`；② 写人可读的计划书 | `plan/<name>/plan.md`（`$file` 契约内） |
| `work` | `foreach`，`items: {"queue": true}`，`max_instances: 12` | 引擎结果清单（`foreach-work-results.json`） |
| `execute` | 每个任务一个实例：执行 + 机器 `check`（最多 3 稿）+ 人工 `review` | `plan/<name>/tasks/TASK-0NN.md` |
| `accept` | 整体验收门（`gate: true`） | `plan/<name>/acceptance.md` |
| `finalize` | 交付完成态计划 | `plan/<name>/plan-final.md` |

节点上报后引擎按 **执行 → check → review** 推进（事件流里 `check_requested` 先于
`review_requested`）。`plan` 的 `check` 有第三条判据：
**「队列条目与 plan.md 逐项一致、无凭空增删，且队列已 seal」**。

## 任务清单：引擎维护的队列

### 定义

```json
{
  "id": "work", "type": "foreach",
  "items": {"queue": true},
  "max_instances": 12,
  "inputs": {"plan": {"ref": "plan"}, "intake": {"ref": "intake"},
             "previous": {"ref": "previous"}},
  "nodes": [ … ],
  "exports": ["execute"],
  "outputs_key": "task_results"
}
```

`items: {"queue": true}` 就是全部声明。**队列不是文件**：它是 run 状态，保存在记录顶层的
`queues.work`（`{items, done}`），唯一写者是引擎。`foreach-work-items.json` 只是引擎写出的
**只读快照**（`view` 的 `items.label` 与读者用它），改它无效。

### item 的形状

固定字段 `id` + `name`；其余键（本仿真用 `goal` / `deps` / `acceptance`）是调用方拓展，
原样透传给子级的 `item`：

```json
{"id": "TASK-001", "name": "核心循环 spec",
 "goal": "把探索-解谜-合成-通关的核心循环写成可执行规格",
 "deps": "无", "acceptance": "循环每一环都有明确的输入输出与状态迁移"}
```

**`status` 由引擎派生**（`pending` / `running` / `success` / `failed`），取的就是该条目
对应子运行的实例状态——不另存一份，不会与实跑漂移，且与 `view` 用**同一套词**。
`queue.done` 是**封口**标记，与条目状态无关，别看混。

### 谁在什么时候改队列

`plan` 节点建 2 条、封口（`drive.py:do_plan`）：

```python
for task in tasks:
    cli("items", "add", rid, "--node", "work",
        "--item", json.dumps(task, ensure_ascii=False), "--json")
cli("items", "seal", rid, "--node", "work", "--json")
```

**`add` 会自动推进**：run 正停在 `awaiting_items` 时，一次 `add` 就地恢复调度，
不必再补一发 `resume`。封口之后 `add` 报错；**空队列 + 已封口 = 收尾**，不是等待。

为什么是在 `plan` 节点里投喂、而不是让它写文件：`plan.md` 是给人看的**产出**，
队列是喂给引擎的**输入**，二者归属不同（见下文「为什么不再有两份产物之争」）。

## 队列的实测形态

run `20261005-203343-0ef2fc`（停在 TASK-002 验收门，队列已封口）：

```console
$ aiwf items list 20261005-203343-0ef2fc --node work
queue work  (2 items, sealed)
  [ok] TASK-001  核心循环 spec  (success)
  [>>] TASK-002  潮汐谜题与关卡原型  (running)
```

`--json` 多给 `index` 与 `editable`；`editable` 就是「还能不能改」这个判据本身：

```json
{"index": 0, "id": "TASK-001", "name": "核心循环 spec",
 "status": "success", "editable": false, "fields": { … }}
```

同一刻记录里 `subflow_state.instances[].status` 是 `["success", "running"]`
——与上面**逐项逐字相等**。这就是「一套词表、一份真相」的直接证据。

> 运行中这份队列状态可能还停在 `record.log.jsonl` 增量里、尚未并进 `record.json` 快照。
> 读权威值走 `aiwf items list` / `aiwf runs show --json`，**别直接解析快照文件**。

### 可改动范围

| 条目状态 | 追加新项 | update / remove / move 它 |
|---|---|---|
| 未启动（`pending`，尚无子运行） | — | 可以 |
| 已启动（已有子运行） | 只能追加**新**项 | **不可以**，改走修订 / 重做 |

判据是审计身份 `(key, run_id, started_at)`：已启动项有一条子运行与之对应，
再改它记录就与实跑对不上。因为 foreach **严格顺序执行**，已启动项恒是队列**前缀**，
所以动后缀不会给已跑过的项重新编号。

`move --to <index>` 只能落在 `[已启动数, 长度)`：未启动项能移到「下一个将执行」的位置，
但**不能越过任何已跑过的项**。实测（临时项目，3 条；`add` 触发自动推进，首条立刻 running）：

```console
$ aiwf items list $RID --node chapters
queue chapters  (3 items, open)
  [>>] C1  第一章  (running)
  [  ] C2  第二章  (pending)
  [  ] C3  第三章  (pending)

$ aiwf items move $RID --node chapters --id C3 --to 0
error: cannot move 'C3' to index 0; pending items live in [1, 3)   # 越不过已启动的 C1

$ aiwf items move $RID --node chapters --id C3 --to 1
queue chapters  (3 items, open)
  [>>] C1  第一章  (running)
  [  ] C3  第三章  (pending)      # 合法重排生效
  [  ] C2  第二章  (pending)
```

### 命令一览

```bash
aiwf items list   <run_id> --node work [--json]
aiwf items add    <run_id> --node work --item '<JSON>' [--item '<JSON>' …]
aiwf items update <run_id> --node work --id TASK-002 --set '{"name":"新名字"}'
aiwf items remove <run_id> --node work --id TASK-002
aiwf items move   <run_id> --node work --id TASK-002 --to 0
aiwf items seal   <run_id> --node work
```

除 `list` 外，每条命令改完都回显「新队列 + run 现在停在哪」（`--json` 里是 `{queue, spec}`）。

## view

`view` 把 5 个顶层节点映射成 5 个业务阶段（一一对应）：

```json
"view": {
  "stages": [
    {"name": "立项",     "nodes": ["intake"],   "detail": "目标：${node.intake.goal}"},
    {"name": "制定计划", "nodes": ["plan"],     "detail": "拆解为 ${node.plan.task_count} 个任务"},
    {"name": "逐条执行", "nodes": ["work"]},
    {"name": "整体验收", "nodes": ["accept"], "gate": true},
    {"name": "交付完成", "nodes": ["finalize"]}
  ],
  "items": {"label": "${item.name}｜依赖：${item.deps}"}
}
```

「逐条执行」不写 `detail`，用引擎默认：`已完成 x/y · 当前 <名称>（<子阶段>）· 还剩 z`。
跑完自动变 `已完成 2/2 · 全部完成`，不会出现 `当前 —`。
`items.label` 里的 `item` 就是队列条目本身，所以标签能直接用业务字段（这里是 `name` / `deps`）。

## 为什么 plan 节点不再有「两份产物」之争

上一版任务清单外置成 `plan.json`，于是 `plan` 要产 {`plan.json`, `plan.md`} **两份文件**，
而 `output` 是**单文件契约**（`create.md` 产物设计明确规定不支持 `output.files` / 数组）——
只能一份进契约、另一份靠 `check` 兜底，并因此多出一整段取舍说明。

本版把清单交给**引擎队列**：清单不再是文件，不参与任何文件契约。于是取舍消失了：

| | 上一版 | 本版 |
|---|---|---|
| 清单 | `plan.json`（Agent 写，`{$file}` 源） | `queues.work`（引擎持有，无文件） |
| `plan` 的契约产出 | `plan.json`（`plan.md` 契约外，靠 check 兜底） | **`plan.md`** |
| 产物索引里的清单 | `plan.json` 在、`plan.md` 不在 | 反过来：**`plan.md` 在，没有清单文件** |

`plan` 节点现在只有一个产出（计划书），干净地进契约与产物索引；
清单与计划书的一致性由 `check` 第三条判据管。

## 产物落点：business 与 engine 分家

`output.file` 是**模板**，用 run 的 context 渲染；引擎核对上报的 `$file` 与声明路径是否
指向同一文件（`same_file_path` → `abspath` 归一化），**落盘由 Agent 完成**，
所以相对路径按执行命令时的 cwd 解析。这就是业务产物能写在项目目录、
而引擎自己的记录仍留在 `.aiwf` 的原因。

```
plan/wu-gang-ch1/                  ← 业务产物（人看 + 下游读）
  plan.md                          # 人可读计划书                                    ← plan（契约内）
  tasks/TASK-001.md … TASK-002.md  # 每任务的「改动说明 + 自检结论」                  ← execute（每实例）
  acceptance.md                    # 整体验收报告                                    ← accept
  plan-final.md                    # 完成态计划                                      ← finalize

.aiwf/
  workflows/plan-task.json         # 定义
  runs/plan-task/<run_id>/
    record.json                    # 权威状态（含 queues.work；journal 只在有未落盘增量时存在）
    record.log.jsonl               # 快照之后的增量日志（运行中常见）
    workflow.json                  # 定义快照（顶层 run 有）
    context.md                     # 给 Agent 的交接摘要（含产物索引）
    foreach-work-items.json        # 队列的只读快照（供 items.label 与读者）
    foreach-work-results.json      # 每实例结果清单（= context.task_results）
    children/work/subflow.json     # 内联合成定义（子 run 的**定义源**）
    children/work/TASK-00N/…       # 子 run 记录（record.json / context.md）
```

`plan_name` 由 `intake` 产出（slug，`^[a-z0-9][a-z0-9-]*$`），下游路径统一用
`plan/${intake.plan_name}/…`。foreach 的 `inputs` 里显式透传了 `intake`，因为**子 run 的 context
只由 foreach 的 `inputs` 映射构成**（`_begin_instance` → `_new_child_record`），不继承父级全部键。

关于 `.aiwf` 里的两个精简点（引擎侧已落地）：

- **静止的 run 不留空 journal**：`_snapshot_run` 写完 `record.json` 后**删除**它覆盖的增量日志，
  不再写成 0 字节文件（读取侧本就把缺失日志当空）。
- **内联子流程实例不再各存一份 `workflow.json`**：定义源就是父 run 的
  `children/<node>/subflow.json`，实例层副本是纯重复；解析器通过 parent 链回落到它。
  外部 subflow（`node.workflow` 指向外部定义）**仍写**自身快照——它的定义在 run 之外，会变。

## 上报体积：清单根本不进上报对象

`plan` 上报的是 `{"$file": "plan/wu-gang-ch1/plan.md", "summary": …, "task_count": 2}`，
实测 **86 字符**——和任务数**无关**，因为清单是引擎队列，从不序列化进 `complete`。
`OUTPUT_LIMIT = 2000`（`engine.py` 顶部常量，超限直接驳回）仍然管着**文档**这一路：
所以长正文一律进文件、上报只带 `$file`（本仿真 5 个业务产物全是这么交付的）。

## 复现

```bash
cd sim/plan-task

# 诉求 → 制定计划 → 执行 → 交付（脚本代替 Agent：过质检 PASS、批验收 APPROVE）
python drive.py

# 停在 TASK-002 的验收门 / 停在整体验收门
python drive.py --stop-at-task 2
python drive.py --stop-at-accept

# 从既有 run 续跑（可再叠加停靠点）
python drive.py --continue <run_id>
python drive.py --continue <run_id> --stop-at-task 2
python drive.py --continue <run_id> --stop-at-accept

# 看视图
aiwf runs show <run_id> --view
aiwf runs show <run_id> --view --json

# 看队列（只看不改）
aiwf items list <run_id> --node work
```

`drive.py` 自己设好 `PYTHONPATH=<repo>/engine`、`AIWF_HOME=.aiwf`，cwd 落在本目录即可运行；
它不是引擎的一部分，只是让「视图」可被无 LLM 地复现。诉求在 `inputs.json`；
`drive.py` 里 `PLAN_NAME/GOAL/SCOPE/.../PLANNED_TASKS` 是「立项 / 制定计划」节点会产出的内容
（为确定性而写死）。**测试跑只排 2 个任务**，把一次全链路回归的开销压到最小；
真实使用时任务数是计划节点自己决定的。短上报对象走 `--output` 内联，**不落临时文件**。

## 三种状态的实测输出

run `20261005-203108-d85a1c`（单 plan 节点 + 队列模式 + 单实例节点 + 产物分家），
同一个 run 依次截取三态。

### 1. 中途——TASK-001 已验收，TASK-002 停在验收门

```
view       :
  [ok  ] 立项  -- 目标：做出《雾港》第一章可玩的垂直切片
  [ok  ] 制定计划  -- 拆解为 2 个任务
  [wait] 逐条执行  -- 已完成 1/2 · 当前 潮汐谜题与关卡原型｜依赖：TASK-001（执行与自检） · 还剩 1
    [ok  ] TASK-001  核心循环 spec｜依赖：无
    [>>  ] TASK-002  潮汐谜题与关卡原型｜依赖：TASK-001  <- 当前（执行与自检）
    -> aiwf review 20261005-203111-5aca78 --action f488c0ce2df55c59fb0d9fed --verdict APPROVE|REJECT
  [    ] 整体验收
  [    ] 交付完成
```

门命令指向**子 run**（`20261005-203111-5aca78`），不是父 run——那个 action 属于当前实例。

### 2. 2 个任务全部完成，停在整体验收门

```
view       :
  [ok  ] 立项  -- 目标：做出《雾港》第一章可玩的垂直切片
  [ok  ] 制定计划  -- 拆解为 2 个任务
  [ok  ] 逐条执行  -- 已完成 2/2 · 全部完成
    [ok  ] TASK-001  核心循环 spec｜依赖：无
    [ok  ] TASK-002  潮汐谜题与关卡原型｜依赖：TASK-001
  [wait] 整体验收  -- 整体回归通过，闭环成立
    -> aiwf review 20261005-203108-d85a1c --action 43681819b5dac56dbe3bdd21 --verdict APPROVE|REJECT
  [    ] 交付完成
```

这次门命令指向**父 run**（没有队列在等，action 属于根运行）。

### 3. 完成

```
view       :
  [ok  ] 立项  -- 目标：做出《雾港》第一章可玩的垂直切片
  [ok  ] 制定计划  -- 拆解为 2 个任务
  [ok  ] 逐条执行  -- 已完成 2/2 · 全部完成
    [ok  ] TASK-001 … [ok  ] TASK-002
  [ok  ] 整体验收  -- 整体回归通过，闭环成立
  [ok  ] 交付完成  -- 计划完成，已交付完成态计划
```

### 该 run 的产物与体积

```
plan/wu-gang-ch1/  2787 B / 5 文件   plan.md 814 B｜2 个任务说明 + 验收报告 + 完成态计划
.aiwf/runs/<rid>/ 51982 B / 10 文件  1 份 record + 1 份定义快照 + 2 份 foreach 快照 + 子 run 记录
```

`context.md` 的产物索引：

```
## 产物索引
- `plan/wu-gang-ch1/plan.md` ← plan（制定计划）
- `.aiwf\runs\plan-task\20261005-203108-d85a1c\foreach-work-results.json` ← work（逐条执行）
- `plan/wu-gang-ch1/acceptance.md` ← accept（整体验收）
- `plan/wu-gang-ch1/plan-final.md` ← finalize（交付完成）
- `plan/wu-gang-ch1/tasks/TASK-001.md` ← 子运行 20261005-203110-a9944f（work/TASK-001）
- `plan/wu-gang-ch1/tasks/TASK-002.md` ← 子运行 20261005-203111-5aca78（work/TASK-002）
```

**清单不在表里——因为它不是文件**；`plan.md` 在表里，因为它是 `plan` 的 `$file` 产出。
（表里那个 `foreach-work-results.json` 在 `.aiwf` 下却混在业务产物里，属已知边界。）

## 这次仿真说明了什么

| 结论 | 证据 |
|---|---|
| **计划由工作流产出，不是人的输入** | 无 `plan/` 输入目录；清单与计划书同由 `plan` 节点产出 |
| **清单由引擎持有，Agent 不写文件** | 队列在 `queues.work`；`foreach-work-items.json` 是只读快照；`plan.json` 已不存在 |
| **item 有固定 schema，也容得下拓展** | `id`+`name` 必填受校验；`goal`/`deps`/`acceptance` 原样透传给子级 `item` |
| **引擎提供改队列的能力，且知道该拦什么** | `items add/update/remove/move/seal`；已启动项一律拒绝并给出去处 |
| **状态一套词表、一份真相** | `items list` 的 status 与 `subflow_state.instances[].status` 逐字相等 |
| **`add` 能自动推进，不必补 resume** | `awaiting_items` 上 `add` 直接恢复调度（`queue_add` → `_queue_commit`） |
| **一个业务动作就是一个节点** | 「制定计划」= `plan` 一个节点，视图里也是一行 |
| **单文件契约不再逼出取舍** | `plan` 只产 `plan.md`；清单是状态，不占契约口子 |
| **长文档不撞 2000 上限** | 上报恒为 86 字符；正文全走 `$file` |
| **短对象不落文件** | `intake` 无 `output.file`，`context.intake` 就是那 214 字符的对象 |
| **一个节点就能承载 执行+check+review** | `execute` 上同时挂 `check`/`review` |
| **业务产物与引擎记录分家** | `plan/<name>/` 2.8 KB（业务）｜`.aiwf/runs/<rid>/` 52 KB 全是记录（引擎） |
| **未运行的阶段照样列出来** | 中途态里 `整体验收`、`交付完成` 已是空标记 |
| **门能定位到正确层级** | 队列里的门给子 run 命令，队列外的门给父 run 命令 |
| **执行态由引擎派生、人只发指令** | 视图每读一次从记录重算；人会话只回 APPROVE |

## 已知边界

- **已启动项不可改**：要改就 `redo` / `revise` 那一个实例；队列只放行未启动项。
- **`move` 不能越过已跑过的项**：目标位限制在 `[已启动数, 长度)`。
- **`${node.plan.tasks}` / `ref: plan.tasks` 不可用**：`context.plan` 只有
  `{$file, summary, task_count}`；要全量清单用 `aiwf items list`（这正是 Agent 的真实行为，
  `drive.py` 的 `queue_rows` 与 `do_finalize` 都这么读）。
- **产物索引会混入引擎自己的文件**：`foreach-work-results.json` 在 `.aiwf` 里，
  却和业务产物列在同一张表。要分层，需要让 `_collect_artifacts` 区分「业务产物」与「引擎清单」。
- **业务产物路径是项目相对路径**：所以驱动 / CLI 要在项目目录下执行。这与本机
  `AIWF_HOME=.aiwf` 本身是相对路径的约定一致（`context.run.dir` 也是相对路径）。
- **条目行不显示「待验收」这个词**：视图行标记只有 `ok/>>/空`；实例「卡在门上」只在阶段行的
  `（执行与自检）` 与 `<- 当前` 里体现（`--json` 里能读到派生 status）。要让每行直接写出
  计划状态词，需把**实例的门状态**加进 `items.label` 的变量域——目前 `item.*` 只指向条目本身，
  属机制改动。
- **`当前` 短语不带任务 id**（用的是 `items.label`，而 label 已去掉 id 以免与左侧 key 重复）。
- **依赖只有展示意义**：foreach 顺序执行，不按依赖拓扑排序；依赖是否满足需另做校验。
- **计划的「再规划」覆盖到「未启动项可增删改重排」**：执行中追加 / 修改 / 删除 / 重排
  **未启动**条目都由 `aiwf items` 支持；已启动项的返工走 `redo` / `revise`。
  整体重开一轮计划（放弃已跑的实例）需要新建 run。
