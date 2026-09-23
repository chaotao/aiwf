# foreach / 子工作流实施计划

> 状态：待评审。评审通过后才动代码。本计划只覆盖 **v1：顺序执行**；并行/并发预留接缝，不实现。

## 一、Repository Research（现状与要解决的问题）

引擎已完成 P0（consumes/output\_schema/when），当前模型仍有三个表达力缺口：

1. **迭代没有身份**：循环靠图成环，所有迭代共享同一组 context key
   （小说循环里 `chapter_draft` 被反复覆盖）。引擎区分不了"第 3 章"与"第 7 章"。
2. **无法只重做单个迭代**：revise 只能到节点粒度，"只重写第 3 章"引擎听不懂。
3. **流程无法组件化复用**："写一章""跑一轮测试"只能在每个工作流里复制粘贴。

四类场景的共同诉求 = **一个可复用的子流程 + 按清单批量执行 + 实例级追踪与重做**：

| 场景   | items 来源       | 子流程          |
| ---- | -------------- | ------------ |
| 写小说  | 章节清单（动态未知章数）   | 细纲→正文→质检→验收  |
| 短剧   | 分镜清单 / 语言清单    | 画面+台词 / 字幕翻译 |
| 游戏开发 | 资产清单（模型/贴图/音效） | 导入→压缩→LOD→挂载 |
| 软件开发 | 变更模块 / 待测文件清单  | 改动→测试→验证     |

## 二、总体设计

### 1. 新增两种节点（推荐方案）

| type      | 语义                           | 关键字段                                                                                                                         |
| --------- | ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `subflow` | 调用另一个工作流**一次**（组件化）          | `workflow`、`inputs`、`output_key`                                                                                             |
| `foreach` | 按清单**批量重复**执行同一子流程；子流程可外引或内联 | `workflow` 或 `nodes`（二选一）、`items`、`as`、`index_as`、`inputs`、`outputs_key`、`exports`（内联时）、`on_fail`、`max_instances`、`id_field` |

理由：单调用与批量在输出形态（单对象 vs 集合）、审计（有无实例表）、用户心智上
差异明显，拆成两个类型校验清晰；引擎内部共用同一套 child 生命周期辅助函数。

foreach 的循环体两种写法（**`workflow`** **与** **`nodes`** **互斥，必须给其一**）：

* 外引：`"workflow": "chapter-writing"`——复用已定义组件；

* 内联：直接在节点里写 nodes（edges 可省略，省略时按数组顺序串联；不要求手写
  start/end，引擎自动包裹）：

```json
{
  "id": "write_chapters", "type": "foreach", "name": "逐章创作",
  "items": { "$file": "novel/manifest.json" },
  "as": "item", "index_as": "index",
  "inputs": { "chapter_no": "${index}" },
  "exports": ["chapter_file"],
  "nodes": [
    { "id": "chapter_outline", "type": "task", "prompt": "为第 ${chapter_no} 章写细纲" },
    { "id": "chapter_draft", "type": "task", "prompt": "写第 ${chapter_no} 章正文" },
    { "id": "chapter_qc", "type": "branch", "prompt": "质检",
      "routes": {"PASS":"end","REVISE":"chapter_draft"}, "default":"end" }
  ]
}
```

内联是**语法糖**：进入 foreach 时引擎把 nodes 合成为一份子流程定义
（生成 id `<父workflow>.<node>`，input\_schema 由 inputs 键推导，exports 取节点
`exports` 字段），物化到父运行目录后走与外引完全相同的机制——执行路径不分叉。
选型指引：一次性循环体用内联（免建文件）；需要跨工作流复用、单独测试循环体时
用外引。`subflow` 单调用不支持内联（无收益——直接把节点平铺进父流程即可）。

调用示例（单调用）：

```json
{
  "id": "run_tests", "type": "subflow", "name": "跑测试套件",
  "workflow": "test-suite",
  "inputs": { "changeset": "${change}", "module": "core" },
  "output_key": "test_result"
}
```

调用示例（批量）：

```json
{
  "id": "write_chapters", "type": "foreach", "name": "逐章创作",
  "workflow": "chapter-writing",
  "items": { "$file": "novel/manifest.json" },
  "as": "item", "index_as": "index",
  "inputs": { "chapter_no": "${index}", "spec": "${item}" },
  "outputs_key": "chapter_results",
  "on_fail": "abort", "max_instances": 1000
}
```

### 2. 子流程契约：普通工作流 + `exports`

子流程就是一份普通工作流 JSON，复用现有字段并新增顶层 `exports`：

```json
{
  "id": "chapter-writing",
  "input_schema": {
    "type": "object",
    "required": ["chapter_no", "spec"],
    "properties": { "chapter_no": {"type":"integer"}, "spec": {"type":"object"} }
  },
  "exports": {
    "chapter_file": { "type": "object", "description": "章节文件的 $file 引用" },
    "word_count": { "type": "integer" }
  },
  "nodes": [ ... ]
}
```

* `input_schema`：父级调用必须满足的入参契约（已有字段，直接复用）。

* `exports`（新增，可选）：key → 可选 JSON Schema 片段。子流程走到 end 时，引擎
  校验这些 key 存在于子 context 且满足片段；无 `exports` 则父级只拿到状态。

* 所有导出值仍受 **2000 字符**上限约束，大产物一律 `$file` 引用，教义不变。

### 3. 上下文作用域：子级全新 context，显式传参

* 父级在进入子级前，用**父 context** 渲染 `inputs` 映射（foreach 额外注入
  `item`/`index`），并按子流程 `input_schema` 校验，不通过不创建子运行。

* 子级 context = 渲染后的 inputs，**不继承**父 context、看不到兄弟实例 → 强制
  显式契约、组件可复用。

* 子级内部就是普通运行：自己的 input\_schema 提问、review、branch、revise、
  甚至嵌套 subflow/foreach，全部现有机制照常工作。

### 4. items 来源与动态终止（写小说场景的关键）

`items` 三种来源，两种模式：

| 来源写法                                       | 模式 | 语义                                                        |
| ------------------------------------------ | -- | --------------------------------------------------------- |
| 字面量数组 `[ ... ]`                            | 静态 | 进入节点时快照，按序执行完即结束                                          |
| `{"var":"key"}` / `{"$file":"p"}` 指向**数组** | 静态 | 同上                                                        |
| 指向**对象** `{"done": bool, "items": [...]}`  | 动态 | 每个实例边界重新读取；追加的项继续执行；**仅当** **`done=true`** **且全部实例成功才结束** |

* 动态模式由 Agent 控制终止（大纲节点一次写满，或自由创作时完结后置 `done:true`），
  引擎不维护业务计数——符合现有约定，也无需放大 max\_steps 硬扛。

* 实例 key 默认=数组下标（字符串）；条目为对象时可用 `id_field` 指定稳定 id。

### 5. 结果如何回到父级 context

* **subflow（单调用）**：exports 对象写入 `context[output_key]`。
  例：`context["test_result"] = {"passed": true, "report": {"$file":"..."}}`。

* **foreach**：集合不能进 context（无限膨胀）。引擎在父运行目录写引擎自管清单
  `foreach-<node_id>-results.json`（结构 `{"items":[{"key","status","exports"}]}`），
  `context[outputs_key] = {"$file": "<该清单路径>"}`。
  下游需要逐项结果时读清单，或继续用 `consumes` glob 匹配原始产物。

### 6. 驱动协议（对 Agent 尽量零新增）

* Agent 用现有命令驱动子级：`current <child_run_id>` 拿 spec → 执行 →
  `complete/fail`。子级到达 end 时引擎自动：①校验并快照 exports 到父级实例表；
  ②foreach 重读 items → 生成下一个子运行，或收尾（写结果清单、父级记 node\_run、
  推进）；③**该次 complete 直接返回父级的下一个 spec envelope**，Agent 循环无缝
  继续。

* 子级运行期间，父级 status=running、next\_node 仍为该 subflow/foreach 节点，
  `current <parent>` 输出"子级进行中"视图并显著标注 child\_run\_id。

* 新增命令仅一条：

  ```text
  aiwf redo <parent_run_id> --node <foreach_node> --instance <key> --reason "..."
  ```

  重开该实例的子级 record（同 run\_id、追加审计，不新建目录），cursor 跳到该实例，
  成功后续跑；清单中该条结果被替换。单调用 subflow 节点上 `redo --node` 等价于
  整体重新调用。

* 实例查看不新增命令：`runs show <parent>` 扩展输出实例表。

### 7. 失败语义

* 子级 fail → 实例 failed。foreach 按 `on_fail`：

  * `abort`（默认）：父级 failed（错误信息指明实例），`resume` 重试该失败实例；

  * `continue`：记录失败实例继续跑，收尾时节点完成、失败项留在清单，context.md
    高亮失败。

* 子级进行中父级不接受 complete/revise，报错并提示先驱动子级。

### 8. 防护与校验

* `max_instances`（默认 1000）：清单扇出上限，超限父级失败。

* 嵌套深度上限 8（沿 parent 链计数）。

* `workflow` 字段必须是字符串字面量；validate 时跨文件构建调用图，**拒绝环**、
  拒绝缺失定义；静态检查 inputs 映射覆盖子流程 input\_schema 的 required 键。

* 依赖图增强：dependency\_graph 扫描 subflow/foreach 的 `inputs` 映射中的 `${var}`，
  使上游 revise 能正确把节点标脏；produced\_keys 增加其 output key。

* 上游 revise 到 foreach 节点：v1 语义 = 整个 foreach 重做（实例表归档、全部
  实例重跑）；实例级精修走 `redo --instance`，不做级联。

### 9. 子级记录的存储布局（嵌套在父运行目录内）

一个父运行文件夹自包含全部内容，便于归档/拷贝/清理：

```text
.aiwf/runs/<父workflow>/<父run_id>/
  record.json                              # 父级记录
  context.md                               # 父级共享上下文（索引子级）
  foreach-<node_id>-results.json           # foreach 集合结果清单
  children/
    <node_id>/
      subflow.json                         # 仅内联：合成的子流程定义
      <instance_key>/
        record.json                        # 该实例的子级运行记录
        context.md
```

* 单调用 subflow：instance\_key 固定 `0`；foreach：instance\_key = id\_field 值或下标。

* redo/重试**不换目录、不换子 run\_id**：在原实例 record 内追加 node\_runs/events，
  审计连续。

* 子 record 仍带自己的 `workflow_id`/`run_id` + `parent` 回溯链；Storage 提供
  child 目录辅助；runs 的索引改为递归扫描 record.json，因此子级仍可凭 run\_id
  全局定位（find\_run）。

* `runs list` 默认只列顶层运行（子级数量多、避免刷屏），新增 `--all` 时附带
  子级行并标注 parent run\_id。

* 子流程被**独立执行**（不经过父节点，如单独调试）时仍写普通路径
  `runs/<workflow>/<run_id>/`，与作为子级时互不影响。

## 三、Files and Modules

* `schemas/workflow.schema.json`：节点 enum 增加 `subflow`/`foreach`；新增
  `$defs`（inputs 映射、items 来源、exports、内联 nodes）；顶层增加 `exports`。

* `schemas/run_record.schema.json`：新增可选 `parent`（子级回溯链）、
  `subflow_state`（节点 id、cursor、instances\[]：key/status/run\_id/时间/error/
  exports）；events 增加 `subflow_started`/`instance_completed`/`instance_failed`/
  `instance_redone`。

* `engine/aiwf/engine.py`：子级生命周期（启动子级、边界重读 items、实例表维护、
  子级完成向父级传播、redo、abort/continue、深度与实例数防护）；依赖图扫描
  inputs 映射；node\_spec 支持父级"子级进行中"视图。

* `engine/aiwf/schema.py`：exports 结构检查、跨文件调用图环检测、契约检查
  （required 入参覆盖）。

* `engine/aiwf/storage.py`：父运行目录下 `children/<node>/<instance>/` 子级
  目录辅助、foreach 结果清单与内联 subflow\.json 的读写；runs 索引改递归扫描。

* `engine/aiwf/cli.py`：`redo` 子命令；`complete` 在子级 end 时输出父级 spec；
  `runs show` 渲染实例表；父级 child-in-progress 文本视图。

* `tests/test_engine.py`：新增夹具与用例（见验证）。

* `skills/aiwf/**`（root 与 novel-demo 内副本各一份）：create-workflow\.md 增
  subflow/foreach/exports 章节；run-workflow\.md 增子级驱动与 redo；
  inspect-runs.md 增实例表字段说明。

## 四、Implementation Steps（依赖顺序）

1. workflow schema：两种节点 + exports + 相关 \$defs；`aiwf validate` 自测。
2. run\_record schema：parent/subflow\_state/新事件。
3. storage：子级嵌套目录、结果清单、内联 subflow\.json 的读写辅助；递归索引。
4. engine 核心：

   * 内联 nodes 合成 + subflow\.json 物化；

   * 子级启动（inputs 渲染→契约校验→建子运行→父级 subflow\_state）；

   * 子级 end 完成传播（exports 校验→快照→静态/动态 items 边界→下一个或收尾）；

   * node\_spec 父级子级进行中视图；
5. schema.py：调用图环检测（外引跨文件 + 合成定义内部）、契约/exports 校验。
6. 失败路径：on\_fail abort/continue、resume 重试实例。
7. redo 单实例 + CLI 接线 + runs show 实例表。
8. 依赖图扫 inputs 映射、foreach 整体重做语义、深度/max\_instances 防护。
9. 补测试、跑全量。
10. 更新两份 skill 文档并同步副本。

## 五、Dependencies and Considerations

* 纯加法演进：schema\_version 保持 "1.0"，旧定义与旧记录仍合法；novel-demo 的
  成环循环不动，作为向后兼容实证。

* **崩溃一致性**：子级保存与父级保存是两次写盘，中间崩溃会出现"子已完成、父仍
  等待"。处理：进入节点/current 时做**对账**——发现子级已完成则自动传播，无需
  人工干预。

* 引擎仍不读业务文件内容；仅对 manifest/结果清单做 JSON 解析（引擎自管协议
  文件），不违反纯状态机定位。

* **定义解析路由**：子 record 的 workflow\_id 是合成 id 时，普通
  `load_workflow` 在 workflows 目录找不到；Storage 增加按 record 解析定义的
  路由——有 `parent` 且为合成 id 时，读父运行目录下的
  `children/<node>/subflow.json`，外引与独立运行仍走 workflows 目录。

* 并行接缝：实例表、子运行记录结构已可并行化；未来加 `concurrency` 字段即可
  激活多实例 + join，v1 不做、不占字段。

## 六、Validation

* 新增单测（预计 +8\~10 个）：

  * subflow 单调用：入参映射/契约校验、exports 回到父级 context；

  * foreach 静态数组（外引 + 内联各一）：顺序执行、结果清单为 \$file、下游可消费；

  * foreach 动态 manifest：追加实例、`done=true` 才收尾；

  * 子级失败：abort→父 failed→resume 重试；continue→带失败收尾；

  * redo --instance：单实例重跑、结果替换、其余实例不动；

  * 调用环/缺失定义/缺 required 入参 → validate 拒绝；

  * 嵌套子流程（含深度上限）；上游 revise → foreach 整体重做。

* 全量单测通过（当前 26 个 + 新增）。

* CLI 冒烟：静态 foreach、动态 manifest、redo 三条链路端到端。

* 现有 novel-demo `validate` 通过、记录 schema 校验无回归。

## 七、Risks

* **复杂度集中在跨记录状态机**：用集中的 child 生命周期辅助函数隔离，引擎其余
  部分不感知父子关系。

* **Agent 驱动错对象**：所有 envelope 都带 run\_id；父级等待视图强提示 child
  run\_id；对子级误发父级命令时有明确报错。

* **动态清单永不 done**：max\_instances 兜底失败；文档要求 Agent 对动态源负责
  置位。

* 范围蔓延：并行、实例级级联重做明确排除在 v1 外，出现真实需求再单独立项。

