# 高阶：运行中追加条目的引擎队列

只有数量事先未知、且需要边执行边补充条目时才使用。
已知清单用静态 foreach；固定次数返工用 task.check。

## 队列不是文件

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

## item 的形状

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

## 命令

| 命令 | 作用 |
|---|---|
| `aiwf items list <run_id> --node <node>` | 打印队列与每项的派生状态 |
| `aiwf items add <run_id> --node <node> --item '<JSON>'` | 追加一项（`--item` 可重复） |
| `aiwf items update <run_id> --node <node> --id <id> --set '<JSON>'` | 改一项**未启动**条目的字段 |
| `aiwf items remove <run_id> --node <node> --id <id>` | 删一项**未启动**条目 |
| `aiwf items move <run_id> --node <node> --id <id> --to <index>` | 重排一项**未启动**条目（0 基目标位） |
| `aiwf items seal <run_id> --node <node>` | 封口：不再接受追加，停止等待 |

`<run_id>` 是**声明该节点的那一层**的 run。队列可以挂在根 run，也可以挂在子流程里，
引擎会沿 parent 链找到持有该节点的记录。

`--json` 给结构化结果，`items list` 每项带 `index / id / name / status / editable / fields`。

## 推进顺序

1. 节点启动时队列为空且未封口 → run 停在 `awaiting_items`。
2. 调用方逐条 `items add`。**每次 add 会自动推进**：只要 run 正停在 `awaiting_items`
   且队列里有待办项，引擎就地恢复调度、为最先的待办项建子运行——不必另发 `resume`。
3. 队列暂时没有可跑的项而业务尚未结束时，`add` 后仍停在 `awaiting_items`
   （action.kind=`resume`）。可用 `resume` 触发一次重调度，但**不能忙循环**。
4. 业务结束时 `items seal`。封口后 `add` 报错；已有项全部结束后节点收尾。
   **空队列 + 已封口直接收尾**，不再等待。

`current` 只读；看状态不会消费任何条目。

## 可变范围

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

## 边界和排查

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
