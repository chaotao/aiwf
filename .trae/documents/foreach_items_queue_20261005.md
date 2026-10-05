# foreach items 队列重构设计（2026-10-05）

> 状态：**已实施**（2026-10-05，同日拍板并落地）。
> 拍板结果：**D1 = C 骨架 + B 批量入口**（先落 C）、**D2 = 固定字段 `id` + `name`**（`status` 派生）、
> **D3 = 顶层 `aiwf items` 子命令 + `add` 自动推进**、**D4 = queue 模式直接替掉动态模式**
> （`{$file}` 退回纯静态源）。
> 实现与实测证据见 `sim/plan-task/README.md` 与 `.workbuddy/memory/2026-10-05.md`。
> 下文保留提案原文以便对照；其中「未实施」「待拍板」等时态一律以本行为准；
> 与实施的一处偏离：`items` 的动词落在 **顶层子命令**（`aiwf items add <run_id> --node X`），
> 而不是本文 §3.3 草案里的 `aiwf items <run> --node X add`（argparse 要求动词在前）。
> 需求原话：*「foreach 的 item 应该是个引擎定义的固定 schema 的数据（包含固定字段，也支持拓展字段），
> 而且由 workflow 维护，而不是 agent 自己写文件。引擎提供能力修改这个 items 队列的数据。」*

---

## 1. 现状（事实，逐条带证据）

### 1.1 items 源与两种模式

`items` 支持四种写法（`engine.py:662-687`，schema 同构于 `workflow.schema.json#/$defs/items_source`）：

| 写法 | 含义 |
|---|---|
| 字面数组 | 静态清单 |
| `{"ref": "a.b"}` | 从父 context 取 |
| `{"var": "a.b"}` | 同上（1.0 写法，1.1 已建议迁移） |
| `{"$file": "…"}` | 从文件读（路径先 `render`，再读 JSON） |

解析结果两分（`_resolve_items`，`engine.py:681-687`）：

- 值是**数组** → `static` 模式；
- 值是 **`{"done": bool, "items": [...]}`** → `dynamic` 模式。

### 1.2 item 现在是「任意 JSON 值」

- item 可以是字符串、数字、任意对象；
- 只有 `id_field` 指定的字段**且 item 是 dict 时**才用作实例键，否则退回下标字符串
  （`_instance_key`，`engine.py:614-623`）；
- 键必须匹配 `[A-Za-z0-9_-]+`，否则直接抛错。

### 1.3 队列的生命周期

- **静态**：节点启动时读一次，`max_instances` 卡上限，全量实例一次性建好，
  并把数组写进快照 `run.dir/foreach-<node>-items.json`（`engine.py:1154-1158`）。
- **动态**：每个实例边界重读源文件（`_advance_foreach` → `_resolve_items` → `_merge_items`，
  `engine.py:1042-1049`）；`_merge_items` 的规则是
  **只追加、前缀不可变、不可删改重排**，且 `previous` 前缀不一致就整条拒绝
  （`engine.py:700-707`）。
- 队列用完（或 `done`）后 `_finish_foreach_node` 把每实例的 `{key,status,exports}` 写成
  结果清单 `foreach-<node>-results.json`（`engine.py:1061-1077`）。

### 1.4 队列现在的唯一写者是 Agent（写文件）

`skills/aiwf/references/advanced-dynamic.md:30-38` 明文写着：

> 1. 启动前由 Agent 创建队列文件，把当前已知项写入 items，done=false。
> 4. Agent 根据业务决定下一项，更新队列，再 `resume`。
> 5. 业务结束时把 done 改为 true。
> **是否完结、下一章序号和内容由 Agent 决定，引擎不维护业务计数。**

即：**队列文件的格式与业务含义完全靠约定，引擎只做「是不是 append-only」这一层形式校验。**

### 1.5 引擎侧已经有的状态

`record.subflow_state` 已经是一份结构化的队列状态（`engine.py:639-660`）：

```json
{"node_id":"work","status":"active","kind":"foreach","items_mode":"dynamic",
 "items_source":{"$file":"…"},"as":"item","index_as":"index","id_field":"id",
 "cursor":0,"total_seen":2,"done":false,"on_fail":"abort","output_key":"task_results",
 "instances":[{"key":"TASK-001","index":0,"status":"pending","run_id":null,
               "started_at":null,"ended_at":null,"error":null,"exports":{}}]}
```

`instances[]` 就是**每项的引擎侧状态**（`engine.py:607-612`）。
换句话说，「引擎维护的、带状态的 item 列表」**已经存在**，只是它与 Agent 写的那个数组
是两份互不相干的数据，靠 `index` 对齐。

### 1.6 另有一坨同名不同物的东西

`engine/aiwf/plan.py`（874 行）是另一套「长周期任务清单」：存在 `<AIWF_HOME>/plans/<id>/index.md`，
**Markdown 格式、人写、有状态词表（`TASK_STATUSES`）与依赖图校验（`_check_acyclic`）**，
和 foreach 无任何代码耦合（`plan.py:1-39` 自己声明了「Deliberately not part of the run state machine」）。

> ⚠️ 命名冲突风险：`plan` / `task` 这一对词在这个仓库里已经有两套含义。
> 本次重构起名前必须先定清楚，否则文档与命令会互相污染。

---

## 2. 目标与约束

### 目标（由需求原话拆解）

| 编号 | 目标 |
|---|---|
| G1 | item 是引擎定义的**固定 schema**：固定字段 + 允许拓展字段，引擎校验 |
| G2 | 队列由**引擎**维护，Agent 不再写队列文件 |
| G3 | 引擎提供**修改队列数据的能力**（增 / 改 / 删 / 封口） |

### 约束（现状带来，不可无视）

| 编号 | 约束 | 证据 |
|---|---|---|
| C1 | `OUTPUT_LIMIT = 2000`，节点上报对象超限直接驳回 | `engine.py:53,132-142`；实测 16 条任务（125 字符/条）即 2051 字符 |
| C2 | 已启动实例的审计身份 `(key, run_id, 起止时间)` 不能改 | `instances[]` 是 record 的一部分 |
| C3 | 现有四种 items 写法与动态模式必须继续可用 | 兼容性 |
| C4 | 引擎是纯状态机，不调模型、不替 Agent 做业务判断 | 全仓设计前提 |

---

## 3. 设计

### 3.1 item 的固定 schema（草案）

```json
{
  "id":   "TASK-001",       // 必填。实例键（取代 id_field）；^[A-Za-z0-9_-]+$；队列内唯一
  "name": "核心循环 spec",   // 必填。人读标签；view.items.label 直接用
  "deps": ["TASK-000"],     // 选填。引擎校验「存在 + 无环」，不参与调度
  "…":    "任意拓展字段"      // additionalProperties: true
}
```

**引擎持有、Agent 不可写**的两个字段：

- `status`：`pending / running / done / failed / skipped` —— 由 `instances[].status` **派生**，不另存一份副本
  （一份真相，避免两份状态漂移）；
- `index`：入队顺序。

于是「队列」= `items`（Agent 贡献的业务字段） + `instances`（引擎贡献的执行状态）的**合并视图**。
`status` 直接复用 `instances[].status` 的词表，不新造状态机。

### 3.2 队列的真相放在哪

| 数据 | 现在 | 改后 |
|---|---|---|
| 队列内容 | Agent 写的文件（`{$file}`） | `record.subflow_state.items`（run 状态里） |
| 每项状态 | `subflow_state.instances[]` | 不变 |
| 审计快照 | `foreach-<node>-items.json` | 降级为**只读投影**，由引擎从 `subflow_state` 重写 |

唯一写者是引擎。Agent 只能经过引擎命令改队列。

### 3.3 引擎能力（CLI 草案）

```
aiwf items <root_run> --node work list   [--json]
aiwf items <root_run> --node work add    --item '{"id":"TASK-003","name":"…"}' [--item …]
aiwf items <root_run> --node work update --id TASK-003 --set '{"name":"…"}'
aiwf items <root_run> --node work remove --id TASK-004
aiwf items <root_run> --node work seal            # 封口：此后只出不进
```

每条命令的统一动作：**校验 item schema → 查重复 id → 查不可变约束 → 写 journal 事件并落盘 → 回显新队列**。
`add` 时若 run 正处于 `awaiting_items`，命令自动把 run 推进（等价于现有 `resume`），
避免「加完还要再 resume 一次」的两步操作。

### 3.4 不可变约束（把「只追加」放宽到哪一步）

| 项状态 | add | update | remove | 重排 |
|---|---|---|---|---|
| 未入队（新项） | ✅ | — | — | — |
| `pending`（未启动） | — | ✅ | ✅ | ✅ |
| `running` / `done` / `failed` | — | ❌ | ❌ | ❌ |

判据：**审计身份 = `(key, run_id, 起止)`**。未启动项还没有身份，改动无损；
已启动项一旦改动，`record` 就与事实不符，必须先走 `redo` 这类显式返工通道。

这条直接取代 `_merge_items` 现在那句「动态 items are append-only; existing items changed or removed」
（`engine.py:703`），并且**只放宽到 `pending`**——比上一轮设想的「保护前缀」更窄、更好证。

### 3.5 与现有模式的关系

- 三种**静态**源（数组 / `{ref}` / `{$file}`）：**保持不变**，仍是一次性给全量；
- 动态 `{done, items}`：**保留**，兼容期继续可用；
- 新增第三种模式：`items: {"queue": true}` —— 启动时队列为空，`done` 由 `seal` 决定，
  而不是文件里那个布尔。

---

## 4. 承重墙：队列的**初始内容**怎么进引擎（D1）

这一条决定整个重构的形状。**三种通道**：

| | 通道 | 好在哪 | 代价 | 适合 |
|---|---|---|---|---|
| **A** | Agent 写文件，引擎一次性 ingest 入队 | 无新概念；长清单天然不受 C1 限制；实现量最小 | 「Agent 不写文件」这条没真兑现——文件从「队列」降级为「运输桶」，但仍是 Agent 落盘 | 想小步改、保留现有 `{$file}` 心智 |
| **B** | 节点上报里带 `items`，引擎抽取入队（**不进 context、不受 C1 限制**） | 真的零文件；`output_schema` 能直接声明并逐项校验；一次调用完成 | 要给 `complete` 开一个「这个键不进 context、改入队列」的特例；一个节点只能喂一个队列 | 计划一次成型、后续只做少量增删 |
| **C** | Agent 逐条调 `aiwf items … add` | 零文件、零特例；天然支持「边想边排」（流式规划） | N 条 = N 次引擎调用；批量要自己循环 | 规划本身就是增量的（章节、待办边做边加） |

**推荐：C 作骨架 + B 作批量入口（分两步落地，先 C 后 B）。**

理由：
1. C 是唯一**同时**满足 G2（零文件）、G3（引擎能力）、C4（引擎不做业务判断）的通道；
2. B 是 C 的性能补丁而非替代品——它省掉 N 次调用，但要求引擎为单个键破一次 `OUTPUT_LIMIT` 规矩，
   这个特例值得单独拍一次；
3. A 最省事，但它把「Agent 不写文件」这条需求缩水成「Agent 换个文件名写文件」，没有正面回答需求。

选定 C 之后，`plan` 节点（本仿真里产生计划的那个节点）的行为变成：
**产出计划 → 逐条 `aiwf items … add` → `seal`**，全程不落队列文件；
`plan.md` 仍按既有约定落业务目录，供人读。

---

## 5. 若拍板，落地范围

| 层 | 改动 |
|---|---|
| schema | `workflow.schema.json` 新增 `$defs/item`（固定字段 + `additionalProperties: true`）；`items_source` 增 `{"queue": true}` |
| engine | `_resolve_items` 支持 queue 模式；新增 `_queue_add/_queue_update/_queue_remove/_queue_seal`；`_merge_items` 的不可变规则按 §3.4 重写；实例键一律取 `item.id` |
| cli | 新增 `aiwf items` 子命令组 |
| storage | `foreach-<node>-items.json` 改为由 `subflow_state` 重写的只读投影 |
| docs | `advanced-dynamic.md` 重写；`patterns.md` 补一例；`view.md` 说明 `items.label` 可用字段 |
| tests | 不可变性、id 唯一、seal 语义、旧模式兼容四组用例 |
| sim | `sim/plan-task`：`work.items` 从 `{$file}` 换成 `{"queue": true}`；`plan` 节点改走 `items add` |

---

## 6. 拍板记录（原「待拍板」，已定）

| 编号 | 问题 | 结论 |
|---|---|---|
| **D1** | 队列初始内容的通道（A / B / C） | **C 作骨架 + B 作批量入口**；先落 C。B 形态由 `items add --item` 可重复承担（一次调用投多条），未给 `complete` 开 `OUTPUT_LIMIT` 特例 |
| D2 | 固定字段集 | **`id` + `name` 必填**；`deps` **不**升为引擎级字段（由调用方拓展、引擎不校验依赖图）；`status` **派生不存储** |
| D3 | 修改入口形态 | **独立顶层子命令 `aiwf items`**；`add`/`seal` 在 `awaiting_items` 上**自动推进**，不要求额外 `resume` |
| D4 | 兼容策略 | **queue 模式直接替掉动态模式**：`{"done": bool, "items": [...]}` 成为显式报错；`{$file}` 退回纯静态语义（文件须为 JSON 数组）；`plan.py` 命名冲突未动（两者仍同名不同物、零耦合） |

实施时相对本文草案的偏离（两处，均已落地）：

1. **动词位置**：`aiwf items add <run_id> --node X`（顶层子命令，动词在前），
   不是 §3.3 的 `aiwf items <run> --node X add`（argparse 子命令要求动词在最前）。
2. **`status` 词表**：草案 §3.1 写 `pending / running / done / failed / skipped`，
   实施时**统一为引擎既有的实例词汇** `pending / running / success / failed`——
   `view` 的条目状态本来就直读 `inst["status"]`，另造一套 `done` 会让同一事实有两个名字，
   而且 `done` 在队列表里已是**封口**标记，会撞车。`skipped` 未使用，故不列入。

---

## 附：与上一轮 `runtime_context_mutation_20261005.md` 的关系

那份文档解决的是「**运行期改 context 变量**」，其中「队列只追加太紧」的结论是
「保护范围收窄到『已启动项不可变』，尾部可增删改」。本文 §3.4 就是那条结论的落地形态，
两者不冲突：那份讲**通用 context**，本文讲**foreach 队列这一等公民**。
