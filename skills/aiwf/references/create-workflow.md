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
| `task` | 交给执行 agent 的一个工作步骤；是否调用工具（写文件、查 API）由 agent 自行组织，输出写入 context（大产物写文件、只回引用） | `prompt`、`model`（建议）、`input_schema`（执行前向用户收集的信息）、`output_key`、`review`（执行后人工验收） |
| `branch` | 执行 agent 回答标签，引擎按 routes 自动决定下一节点，不停顿 | `prompt`、`routes`、`default` |
| `review` | 纯人工检查点：流程到此暂停等人验收，本身不产出 | `requirements`、`routes`（APPROVE 去向必须显式给出） |
| `end` | 显式结束 | — |

### 模板变量

字符串中用 `${var}` 引用 context（运行输入 + 之前 task 的输出）。引擎下发前完成
渲染；文件引用渲染为路径，其它非字符串值自动 JSON 序列化；变量缺失运行时报错
——定义时保证每个引用有来源。

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
- 引擎在每次落盘时把全部引用汇总到运行文件夹的 `context.md`，作为人和 agent 的
  共享/交接入口（无需在工作流里额外加节点）。

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

## 工作流程（按顺序执行）

1. 与用户澄清：有哪些阶段、每阶段输入来自哪里、哪里需要向用户收集信息、哪里
   需要人工验收、产物如何处理（执行 agent 自己写文件；正文等大产物回 `$file`
   引用，不进记录）。
2. 编写 JSON 到 `.aiwf/workflows/<id>.json`。
3. 运行 `aiwf validate <path>`；报错就修改后重试，直到通过。
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
