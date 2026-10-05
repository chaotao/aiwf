# 运行期修改上下文变量：机制现状、缺口与候选方案

状态：设计呈交，**未实施**。日期 2026-10-05。

## 0. 问题

如何在**运行期**修改工作流的上下文变量（`record.context`），从而影响运行的走向？

## 1. 结论先行

1. `context` 是引擎的权威状态。既定原则是**人只发指令、引擎持有并推进状态**；所以设计目标不是"给人开一个写 context 的口子"，而是**补齐"用指令驱动 context 重算"的通道**。
2. 现有通道已覆盖大部分诉求，但**粒度是「节点」**——`revise` 重做节点、由引擎重算它的输出；没有"只改一个字段"的入口。
3. **明确不做**：直接编辑 `record.json` / `workflow.json` 快照。`advanced-revisions.md:88-89` 已明文禁止。
4. 当前真实缺口只有一个：**动态队列只能追加**（`_merge_items` 保护的是全部历史条目，`engine.py:703`），所以"**删除 / 修改一个尚未执行的任务**"没有低成本路径。

## 2. 现状：五条正规通道

| 通道 | 命令 | 改的是什么 | 引擎如何响应 |
|---|---|---|---|
| 途中提问 | `provide <run> --action <id> -p k=v` | 当前节点缺失的**用户输入键** | 写入 context → 渲染任务材料 |
| 改历史输入 | `provide <run> --node <task> -p k=v --reason ...` | 已完成 task 的**用户输入键** | 写 context **并开启修订** |
| 修订节点 | `revise <run> --target <node> --mode cascade\|return` | 已完成 task 的**产出**，连带下游 | 标记 stale → 重做 → 重写 context |
| 单实例重做 | `redo <run> --node <foreach> --instance <key>` | 一个 foreach 实例 | 重跑该实例 |
| 动态队列 | 改 items 文件 + `resume <run>` | 尚未启动的**队列项** | 实例边界重读队列，追加实例 |

出处：`interaction.md:40-48`、`advanced-revisions.md:21-41 / 52-59 / 61-71`、`advanced-dynamic.md:28-38`；
命令定义见 `engine/aiwf/cli.py` 的 `provide` / `revise` / `redo` / `resume`。

**要点**：这五条没有一条直接写 context 值——全部是"发指令 → 引擎重算 / 重写"。

## 3. 为什么不应直接写 context

1. **审计身份**：context 是"某节点在某输入下产出了什么"的证据链；手改会让记录与实际执行不符。
2. **依赖一致性**：下游 stale 判定依赖"上游被重做"这一**事件**；手改值不触发级联，下游会拿旧结果去配新上游。
3. **快照契约**：1.1 run 用定义快照（`advanced-revisions.md:88`），手改同类文件属于绕过契约。

## 4. 缺口（逐条附源码）

### 缺口 1：动态队列只追加

`_merge_items`（`engine.py:689`）在动态边界读 `items_snapshot`，第 703 行：

```python
if items[:len(previous)] != previous:
    raise ValueError("dynamic items are append-only; existing items changed or removed")
```

`previous` 是**上一次声明的全量**，不是"已启动部分"。结论：**任何历史条目**（含尚未启动的）都不可改、不可删。

影响：执行到第 3 个任务时想"把第 5 个任务改掉 / 删掉"，只能 `revise` 重做**整个** foreach。

### 缺口 2：没有字段级入口

只有节点级：`revise --target <node>` 会**重跑**该节点（重新驱动 Agent），不能只改它输出里的一个字段。

### 缺口 3（澄清，非缺口）：子 run 丢失的重置不破坏下标对齐

`engine.py:1200`：

```python
except FileNotFoundError:
    inst.update(status="pending", run_id=None)
    self._begin_instance(workflow, record, node, state, inst)
```

它**复用同一个 inst（同一 `index`）**重建，下标语义不变。我此前"它会破坏前缀性质"的推断**不成立**，在此更正——放宽判据时不需要处理这一处。

## 5. 候选方案

### L1：把队列表的保护范围收窄到「已启动项」（推荐）

- 改 `_merge_items`：`previous[:n_started]` 不可变（`n_started` = 已有 `run_id` 的实例数）；**尾部**（未启动部分）允许改写、新增、删除。
- 配套：把 `instances` 截断到 `n_started`，再按新 items 重建尾部——`_make_instance`（`engine.py:610`）目前只 append，需要补一段尾部重建。
- 语义：**"计划可以改，已经动手的不能改"**，正合 plan-task 的业务直觉。
- 代价：动 `_merge_items` 核心约束 + 尾部重建 + 测试；要重新保证 `items_snapshot` 与 `inst["index"]` 的对齐（`_begin_instance` 用 `read_json_file(snapshot)[inst["index"]]` 取条目，`engine.py:873-874`）。
- 风险：中（核心约束）。收益：高——精确增删未启动任务，不必重跑已完成任务。

### L2：通用的「可变引用」（大）

- 引入 `{"$file": "...", "live": true}`：引擎在**每次渲染下游材料**时重读文件，而不是只读一次。
- 语义：节点输出可"活引用"一个外部文件，改文件即改下游所见。
- 代价：一致性 / 缓存 / 审计复杂度显著上升；"何时算变更"缺少自然事件，stale 判定会退化。
- 评价：能力强但代价大，**不建议现在做**。

### L3：新增 `aiwf set` 直接写 context（不推荐）

- 代价：与"人不写状态"原则、审计身份、级联一致性三条约束直接冲突。
- 评价：**不做**。

## 6. 推荐

推荐 **L1**：用一个局部约束改动补上唯一的真实缺口，语义与业务直觉一致；L2 代价过大、L3 破坏原则。

## 7. 对本仿真（`sim/plan-task`）的意义

- **现在**：改计划 = `revise --target plan` → `work` 整体重做（已被 `test_upstream_revision_redoes_foreach_wholly` 覆盖）。
- **L1 之后**：`work.items` 保持 `{$file: plan.json}`，把队列文件改成 `{done, items}` 形态，即可在"第 3 个任务待验收"时删掉第 6 个任务、追加第 7 个，而**不重跑前 3 个**。

## 8. 待拍板

1. 是否按 **L1** 放宽 `_merge_items` 到"已启动项不可变"？

（同意后再逐项确认：尾部重建的下标分配规则；`done` 与"删到空"的交互；测试面。）
