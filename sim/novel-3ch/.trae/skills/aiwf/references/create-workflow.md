# 创建 / 编辑 aiwf 工作流

把用户的任务流程落成一份**符合 Schema 的 JSON 工作流定义**并保存到项目。
不要凭记忆写 Schema——以本文件和 `aiwf validate` 的结果为准。

引擎是纯状态机：定义描述的是交给**执行 agent** 的节点规格，以及人介入的位置。

## 存储布局（每个项目内）

```
.aiwf/
  workflows/<workflow_id>.json                  # 工作流定义，一个流程一个文件
  runs/<workflow_id>/<run_id>/record.json       # 审计记录，每次运行一个文件夹
  runs/<workflow_id>/<run_id>/context.md        # 自动生成的共享上下文
```

同一工作流可执行多次：每次运行由 `run_id` 独立成文件夹，互不覆盖。
先确保已初始化：`aiwf init`（已存在则无副作用）。

## 工作流定义结构（schema_version 1.0）

```json
{
  "schema_version": "1.0",
  "id": "short-drama",
  "name": "短剧生成",
  "description": "一句话说明用途",
  "default_model": "可选，给执行 agent 的模型建议",
  "max_steps": 50,
  "input_schema": { "标准 JSON Schema(draft-07)，校验运行启动输入" },
  "exports": { "可选；作为子流程被调用时回传的 key → 可选 schema 片段" },
  "nodes": [ ... ],
  "edges": [ {"from": "node_a", "to": "node_b"} ]
}
```

- `edges` 可省略：省略时节点按数组顺序串联。
- `id`：小写字母/数字/`-`/`_`，同时作为文件名。
- `input_schema` 的 required 字段是启动前置条件，缺字段运行无法创建。

## 节点类型

| type | 作用 | 关键字段 |
|------|------|---------|
| `start` | 入口（最多一个；省略则从第一个节点开始） | — |
| `task` | 交给执行 agent 的一个工作步骤；是否调用工具（写文件、查 API）由 agent 自行组织，输出写入 context（大产物写文件、只回引用） | `prompt`、`model`（建议）、`input_schema`（执行前向用户收集的信息）、`output_key`、`output_schema`（产物契约）、`consumes`（显式依赖）、`review`（执行后人工验收） |
| `branch` | 执行 agent 回答标签，引擎按 routes 自动决定下一节点，不停顿；配置 `when` 时由引擎确定性求值 | `prompt`、`routes`、`default` 或 `when`、`routes`（TRUE/FALSE） |
| `review` | 纯人工检查点：流程到此暂停等人验收，本身不产出 | `requirements`、`routes`（APPROVE 去向必须显式给出） |
| `subflow` | 调用另一个工作流一次，其 exports 回写到 context | `workflow`、`inputs`、`output_key` |
| `foreach` | 对 items 逐个调用子工作流（v1 仅顺序执行），结果写引擎清单 | `items`、`workflow` 或 `nodes`（内联体）、`inputs`、`outputs_key`、`as`/`index_as`、`on_fail`、`max_instances` |
| `end` | 显式结束 | — |

### 模板变量

字符串中用 `${var}` 引用 context（运行输入 + 之前 task 的输出），支持**点路径**
深入对象/数组：

- `${item.number}`：foreach 注入的当前条目字段；
- `${plan.chapter_count}`：某 task 产物（整体存于其 `output_key`）的内部字段；
- `${items.0}`：数组按下标取值。

引擎下发前完成渲染；文件引用渲染为路径，其它非字符串值自动 JSON 序列化；变量
（含点路径中间段）缺失即报错。`aiwf validate` 会静态检查每个变量根键有来源
（运行输入、某节点产出键、或节点 input_schema 字段）。

### 大产物外置：文件引用（重要约定）

记录里**不要塞完整产出**（章节正文、剧本、长报告等）：这些内容由执行 agent
写入文件，context / 审计中只保存一个**文件引用**：

```json
{ "$file": "novel/chapter-1.md" }
```

- 引擎对单个节点输出有 **2000 字符**硬上限（序列化后），超限 `complete` 直接
  报错，提示改用引用——这是防止记录膨胀的最后一道约束。
- 引用在模板中渲染为**路径**：定义里写 `第一章：${chapter1}`，下发后变成
  `第一章：novel/chapter-1.md`；需要正文的节点（质检、续写、汇总）在 prompt 里
  明确要求「先用读文件工具读取该路径」。
- 交付物写到项目目录（如 `novel/`），中间产物可放入该次运行文件夹；引用统一用
  项目相对路径。
- **一个输出含多个文件时，每个路径都用 `{"$file": ...}` 标记**（可挂在不同键
  上），例如 `{"$file":"a.md","plan_file":{"$file":"b.md"}}`——context.md 的产物
  索引只识别 `$file` 标记，普通字符串路径不会被收集。
- 引擎在每次落盘时把全部引用（含嵌套 `$file`、foreach items 源、各子运行产物）
  汇总到运行文件夹的 `context.md`，作为人和 agent 的共享/交接入口（无需额外加
  节点）。

### task 节点的两个人机触点（均为可选）

1. **执行前收集信息**：节点级 `input_schema`（标准 JSON Schema）。运行到该节点时
   若 required 字段缺失，流程暂停（awaiting_input），agent 按字段的
   title/description 向用户提问，回答经校验后写入 context，prompt 再引用。
   这些信息也允许在 `aiwf run` 时随输入预填，预填了就不再问。
2. **执行后人工验收**：内嵌 `review` 字段：
   ```json
   "review": {
     "requirements": ["检查项一（支持 ${var}）", "检查项二"],
     "routes": { "APPROVE": null, "REJECT": null }
   }
   ```
   缺省语义：APPROVE 沿 edges 前进（null），REJECT 重做本节点；需要打回到其他
   节点时在 routes 写明目标 id。

### branch 与 review 节点的选择

- `branch`：**LLM 自动**判定，不等人（如自动质检返工循环）。prompt 明确要求只
  回答标签，如 `只回答 PASS 或 REVISE`；可指向之前节点形成循环。
- 独立 `review` 节点：**人**做决策，用于多节点联合验收、或验收本身是一个独立阶段。
  若验收只针对紧邻的单个 task，优先用 task 内嵌 `review`，避免节点数膨胀。

branch 和独立 review 都不能再连出 edges，路由完全由 routes/default 决定。

路由字段的值有严格语义（validate 会检查）：

- **branch 的 `routes`：值必须是目标节点 id（string），不允许 null**——null 的
  「沿 edges 前进」语义只属于 task 内嵌 review，不要类推。
- branch 的 `default`：值也是**兜底目标节点 id**（回答不匹配任何标签时去这里），
  不是默认标签，如 `"default": "finish-chapter"`。
- 独立 review 的 `routes`：APPROVE 去向必须显式给出；REJECT 可给节点 id。
- 内联 foreach 的循环体不能定义 start/end；branch 想结束循环体时，把 PASS 指向
  一个产出 exports 的真实 task（通常是"定稿"节点），不要指向 end。

### 产物契约：output_schema（可选）

task 可挂标准 JSON Schema，`complete` 时引擎自动校验上报产物；不合格则**驳回**：
运行不失败、不占人工验收，node_runs 记一条 `rejected`、事件流记 `output_rejected`，
`last_feedback` 写入原因并随下次 spec 下发；你按反馈修正后重新 complete 同一节点
即可，run_id/目录不变。适合结构化产物（如 `{"status":"PASS","$file":"..."}`）。

```json
"output_schema": {
  "type": "object",
  "required": ["$file"],
  "properties": { "$file": { "type": "string" } }
}
```

### 显式依赖：consumes（可选）

级联重做的依据是数据依赖。prompt 里的 `${var}` 自动入图；当依赖是通过**读文件**
发生、prompt 没有引用时，用 `consumes` 显式声明：

```json
"consumes": ["outline", { "glob": "novel/chapters/*.md" }]
```

- 字符串：依赖某个 context key，效果等同于在 prompt 引用它。
- glob：按 `$file` 产物路径匹配（fnmatch），一条声明覆盖任意数量文件，专给
  「读目录、批量汇总」型节点（如汇总全书）。
- 追踪边界只是**节点产物**：节点内部改了多少文件都不进图，业务粒度（如哪些测试
  受影响）由执行 agent 判断并写入产物，引擎只传播「上游产物变了」。

### 确定性条件：when（可选）

确定的判断不要交给 LLM。branch 配 `when` 后由引擎直接对 context 求值，结果走
TRUE/FALSE 路由；agent 只需对该分支执行 `complete --empty`。

```json
{
  "id": "gate", "type": "branch",
  "when": { "gte": [ { "var": "chapter_count" }, { "var": "target_chapters" } ] },
  "routes": { "TRUE": "assemble", "FALSE": "write_next" }
}
```

操作符：`eq/ne/gt/gte/lt/lte`（两个操作数）、`and/or`（数组）、`not`；操作数为
字面量或 `{"var":"key"}`（可带 `default`）。典型用途：测试/构建结果（exit code）、
数量与清单完备性判断。条件引用的计数/状态由执行 agent 写入 context。

## 子工作流与 foreach（v1：仅顺序执行）

用于复用一段流程、或对一批条目重复同一段流程。

### 子流程契约

子流程就是**普通工作流**，外加顶层 `exports` 声明回传什么：

```json
{
  "id": "chapter-wf",
  "input_schema": {
    "type": "object", "required": ["outline"],
    "properties": { "outline": { "type": "string" } }
  },
  "exports": { "summary": { "type": "string" } },
  "nodes": [ ... ]
}
```

- `input_schema` 复用作父级入参契约；`exports` 为 key→可选 JSON Schema 片段。
- 子级有**全新 context**：只有父级渲染后的 `inputs`（foreach 额外注入当前 item），
  不继承父 context、看不到其他实例。
- 子级走到 end 时引擎按 `exports` 快照校验（缺 key / 不符 schema 即子级失败）。

### 单次调用：subflow

```json
{
  "id": "call", "type": "subflow",
  "workflow": "chapter-wf",
  "inputs": { "outline": "主线：${topic}" },
  "output_key": "chapter"
}
```

子级 exports 对象写入父 context 的 `output_key`（默认节点 id）。

### 批量循环：foreach

循环体两种写法**二选一**：外引 `"workflow"`，或内联 `"nodes"`（一次性循环体，
免建子流程文件；可带 `edges`，省略时按序串联，start/end 自动包裹）：

```json
{
  "id": "chapters", "type": "foreach",
  "workflow": "chapter-wf",
  "items": [ { "outline": "第一章" }, { "outline": "第二章" } ],
  "inputs": { "outline": "${item.outline}" },
  "outputs_key": "results"
}
```

```json
{
  "id": "batch", "type": "foreach",
  "items": [ "甲", "乙" ],
  "inputs": { "topic": "${item}" },
  "nodes": [
    { "id": "write", "type": "task", "output_key": "summary", "prompt": "写：${topic}" }
  ],
  "exports": [ "summary" ],
  "outputs_key": "results"
}
```

- 注入键：item 默认 `item`（可改 `as`），序号默认 `index`（可改 `index_as`）；
  `id_field` 指定用 item 某字段值作为实例 key（默认用序号）。
- 结果：引擎把每个实例的 `{key,status,exports}` 写入父运行目录下的
  `foreach-<node>-results.json`，父 context 只放 `{"$file": 清单路径}`。
- 内联体是**语法糖**：进入节点时合成子流程定义（id=`<父wf>.<node>`），物化到
  `children/<node>/subflow.json` 后走同一机制。

### items 三种来源、两种模式

| 来源 | 模式 | 语义 |
|------|------|------|
| 字面量数组 | 静态 | 进入节点时快照 |
| `{"var":"key"}` 指向数组 | 静态 | 同上，值来自 context |
| `{"$file":"path"}` 指向数组 | 静态 | 同上，值来自文件 |
| 上述来源指向 `{"done":bool,"items":[...]}` | 动态 | 每实例边界重读；新增条目追加实例，**仅 done=true 且全部结束才收尾** |

动态模式条目只追加；初始为空且未 done 时流程进入 `awaiting_items`，外部补充后
`aiwf resume` 继续。

### 失败处理与防护

- `on_fail`：`abort`（默认，父运行 failed，resume 重试该实例）；`continue`
  （失败留在结果清单，继续后续实例）。
- `max_instances`：默认 1000，超限拒绝（防止失控）。
- 嵌套深度上限 8 层；`aiwf validate --project` 建跨文件调用图：拒缺失定义、拒环、
  并检查 `inputs` 是否覆盖子 `input_schema` 的全部 required。
- 重做/重试**不换目录不换 run_id**：子记录固定在父运行目录
  `children/<node>/<instance>/`，同 record 内追加审计。
- 上游修订波及 foreach 节点时，v1 语义=**整体重做**（重新生成全部实例）。
- 单实例重做：`aiwf redo <run_id> --node <node> --instance <key> --reason ...`
  （父节点已完成后使用；仅重跑该实例，清单其余保留）。

## 工作流程（按顺序执行）

1. 与用户澄清：有哪些阶段、每阶段输入来自哪里、哪里需要向用户收集信息、哪里
   需要人工验收、产物如何处理（执行 agent 自己写文件；正文等大产物回 `$file`
   引用，不进记录）。
2. 编写 JSON 到 `.aiwf/workflows/<id>.json`。
3. 运行 `aiwf validate <path>`；定义了 subflow/foreach 时再跑
   `aiwf validate --project`（跨文件调用图、环、必填入参覆盖）。报错就修改后重试。
4. 向用户汇报：工作流 id、节点清单、启动所需输入（工作流 input_schema）。
5. 不要在此场景内直接运行工作流——运行见 [run-workflow.md](run-workflow.md)。

## 最小示例

```json
{
  "schema_version": "1.0",
  "id": "hello-novel",
  "name": "小说开头生成",
  "input_schema": {
    "type": "object",
    "required": ["topic"],
    "properties": { "topic": { "type": "string" } }
  },
  "nodes": [
    {
      "id": "draft", "type": "task", "name": "初稿",
      "input_schema": {
        "type": "object",
        "required": ["tone"],
        "properties": { "tone": { "type": "string", "title": "叙事风格" } }
      },
      "prompt": "以风格 ${tone} 为主题《${topic}》写 300 字开篇，并存入 novel/opening.md",
      "review": { "requirements": ["开头是否有悬念"] }
    }
  ]
}
```

## 完整模式示例：逐章创作（外引子流程 + 自动质检返工 + 人工验收）

父工作流 `short-novel.json`：企划产出章节清单 → foreach 外引子流程逐章写 → 汇总。

```json
{
  "schema_version": "1.0",
  "id": "short-novel",
  "name": "短篇小说逐章创作",
  "max_steps": 100,
  "input_schema": {
    "type": "object",
    "required": ["idea"],
    "properties": { "idea": { "type": "string" } }
  },
  "nodes": [
    { "id": "start", "type": "start" },
    {
      "id": "outline", "type": "task", "name": "大纲与分章",
      "input_schema": {
        "type": "object",
        "required": ["target_chapters"],
        "properties": {
          "target_chapters": { "type": "integer", "title": "章节数" }
        }
      },
      "prompt": "基于 ${idea} 写大纲并拆分 ${target_chapters} 章；章节清单（每项含 title/summary）写入 novel/chapters.json，大纲写入 novel/outline.md。只输出 {\"$file\":\"novel/outline.md\"}",
      "review": { "requirements": ["分章是否合理"] }
    },
    {
      "id": "chapters", "type": "foreach", "name": "逐章写作",
      "items": { "$file": "novel/chapters.json" },
      "workflow": "chapter-wf",
      "inputs": {
        "chapter": "${item}",
        "plan_file": "novel/outline.md"
      },
      "outputs_key": "chapter_results"
    },
    {
      "id": "assemble", "type": "task", "name": "汇总成书",
      "consumes": [{ "glob": "novel/chapter-*.md" }],
      "prompt": "读取清单 ${chapter_results} 与 novel/chapter-*.md，按序汇编为 novel/full-novel.md。只输出 {\"$file\":\"novel/full-novel.md\"}"
    },
    { "id": "end", "type": "end" }
  ],
  "edges": [
    { "from": "start", "to": "outline" },
    { "from": "outline", "to": "chapters" },
    { "from": "chapters", "to": "assemble" },
    { "from": "assemble", "to": "end" }
  ]
}
```

子工作流 `chapter-wf.json`：写正文 → QC 分支（REVISE 回到写）→ 人工验收 → 定稿，
定稿产物经 exports 回传父级。

```json
{
  "schema_version": "1.0",
  "id": "chapter-wf",
  "name": "单章写作子流程",
  "input_schema": {
    "type": "object",
    "required": ["chapter", "plan_file"],
    "properties": {
      "chapter": { "type": "object" },
      "plan_file": { "type": "string" }
    }
  },
  "exports": { "chapter": {} },
  "nodes": [
    { "id": "start", "type": "start" },
    {
      "id": "write", "type": "task", "name": "章节正文",
      "prompt": "读取大纲 ${plan_file}，按《${chapter.title}》（${chapter.summary}）写 800-1200 字正文到 novel/chapter 文件。只输出 {\"$file\":\"路径\",\"word_count\":实际字数}",
      "output_schema": {
        "type": "object",
        "required": ["$file", "word_count"],
        "properties": {
          "$file": { "type": "string" },
          "word_count": { "type": "integer", "minimum": 800, "maximum": 1200 }
        }
      }
    },
    {
      "id": "qc", "type": "branch", "name": "自动质检",
      "prompt": "读取本章正文与大纲，合格只回答 PASS，不合格只回答 REVISE。",
      "routes": { "PASS": "review", "REVISE": "write" }
    },
    {
      "id": "review", "type": "review", "name": "人工验收",
      "requirements": ["剧情是否落实、章尾是否有钩子"],
      "routes": { "APPROVE": "finish", "REJECT": "write" }
    },
    {
      "id": "finish", "type": "task", "name": "章节定稿",
      "output_key": "chapter",
      "prompt": "确认本章文件完整，只输出 {\"$file\":\"该章路径\"}"
    },
    { "id": "end", "type": "end" }
  ],
  "edges": [
    { "from": "start", "to": "write" },
    { "from": "write", "to": "qc" },
    { "from": "finish", "to": "end" }
  ]
}
```
