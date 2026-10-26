# 审计 / 检索工作流运行记录

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

## 1. 列出运行

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

## 2. 查看单次运行

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
字段、占位符与默认渲染见 [业务进展视图](view.md)。

父运行含 subflow/foreach 节点时，概览额外列出**实例表**：实例 key、状态、
exports 摘要、子 run_id 与记录目录；单实例细节用 `runs show <child_run_id>`。

### node_run 字段含义

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

### 运行级字段

`inputs`（启动输入）、`context`（最新上下文）、`phase`、`last_feedback`、
`error`（失败原因）、`pending_review`（验收门状态）、`revision`（当前/最近修订）、
`parent`（子记录专用：父 workflow/run/node/instance）、`subflow_state`（父记录
专用：每个 subflow/foreach 节点的 kind、items_mode、done、instances 列表）。
1.1 额外有 `workflow_version` 和 `pending_check`；foreach 的 `items_snapshot`
指向本次运行使用的条目快照（队列模式随每次投喂重写）。输出文件存在不代表内容已通过业务检查。
顶层 `queues` 只在定义里出现 `items: {"queue": true}` 时才有，
存 `{items, done}`（`done` 即封口），改动一律经 `aiwf items`。

## 3. events 事件流

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

## 4. 关键词检索

```bash
aiwf runs search "赛博"
aiwf runs search "revision" --json
```

检索范围含 workflow、状态、error、inputs、events、各节点 id/名称/摘要。
命中后打印 run_id 与状态；细节用 `runs show`。

## 5. 共享上下文 context.md

与 `record.json` 同目录的 `context.md` 由引擎在每次落盘时自动重写（确定性、
无 LLM），含：运行状态与当前节点、输入、共享上下文内联短值、**产物索引**
（每个 `$file` 路径 + 来源节点）。产物索引递归收集：节点输出与嵌套对象/数组
中的全部 `$file`、foreach items 源里的文件（`{$file}` 形态；队列模式没有清单文件，
队列是 run 状态、不进产物索引）、以及所有子运行（含更深层嵌套）的
产物；只有 `{"$file": "..."}` 标记的路径会被收录，普通字符串路径不算。用途：

- 给人看进度：不用翻 JSON，读 context.md 即知当前在哪、产物在哪；
- 交接给另一个 agent：先读它再用 `current` 接管；
- 审计产物时，用产物索引核对是否有漏产出或路径失效。

## 审计要点（回答用户时按需覆盖）

- **时间线**：events 还原交互顺序，node_runs 还原耗时分布，指出最慢节点。
- **数据流**：task 输出经 output_key 进 context，后续节点用 inputs 中的 `{"ref":"key"}` 消费；
  大产物经 `$file` 引用、正文留在文件；结合 context.md 产物索引核对数据是否符合预期。
- **失败回溯**：定位第一个 failed 节点，结合 error 与该节点记录给原因。
- **修订审计**：检查 revision 与 revise_requested 的 stale/reused 清单是否合理，
  skipped 节点复用的输出是否确实不受影响，有无异常重做或漏改。
- **验收审计**：检查 review_requested/decided，确认验收轮次、结论与反馈。
- **子流程审计**：核对实例表与结果清单（成功/失败/待办数、exports），失败实例
  结合子记录回溯；确认 redo/整体重做后清单已刷新、无悬挂 pending。
