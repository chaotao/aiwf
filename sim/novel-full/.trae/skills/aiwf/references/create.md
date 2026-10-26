# 创建：定义、数据与产物

从零写一份定义，以及写定义时要查的四件事：结构入门、字段与数据作用域、产物形状、分支路由。

## 目录

- [创建入门](#创建入门)
- [字段与数据作用域](#字段与数据作用域)
- [产物设计：对象、文件与代码修改](#产物设计对象文件与代码修改)
- [分支与路由](#分支与路由)

## 创建入门

用最少的可验收阶段表达业务。一个节点至少应有独立验收、重试、复用或交接价值。
先列出阶段、材料来源、交付物，再决定哪些阶段需要用户参与。
默认只写顺序 task，不为简单流程添加 start/end、分支或子流程。

### 1. 初始化与选择范例

在目标项目执行 `aiwf init`，定义放 `.aiwf/workflows/<id>.json`。
`aiwf new <id> --kind sequential|checked|batch` 会生成一份可运行的骨架（可再改
prompt/产物/验收），比从空 JSON 起步更稳。
`aiwf skills install` 将本 Skill 安装到该项目的 `.trae/skills/aiwf`。
若命令不在 PATH，使用引擎脚本的绝对路径；相对文件路径按命令工作目录解释。
可从主入口的“范例目录”选择完整定义；先复制再按需求修改，勿修改 Skill 的模板。

### 2. 写第一个工作流

```json
{
  "schema_version": "1.1",
  "id": "opening",
  "name": "小说开篇",
  "input_schema": {
    "type": "object",
    "required": ["idea"],
    "properties": {"idea": {"type": "string"}}
  },
  "nodes": [{
    "id": "draft",
    "type": "task",
    "inputs": {"idea": {"ref": "idea"}},
    "prompt": "基于 idea 材料写小说开篇，建立冲突与悬念。",
    "output": {"file": "${run.dir}/opening.md"}
  }]
}
```

这里的 `idea` 来自启动输入；`inputs` 将它显式交给任务。
Agent 执行 prompt 并写文件，再上报 `{"$file":"任务包给出的实际路径"}`。
引擎不会创作正文，启动成功也不代表任务已执行。

### 3. 扩展为顺序阶段

在 `nodes` 数组末尾追加任务，后续任务用节点 id 引用前一任务结果：

```json
{
  "id": "polish",
  "type": "task",
  "inputs": {"draft": {"ref": "draft"}},
  "prompt": "读取 draft 文件，润色叙事节奏，保留核心事件。",
  "output": {"file": "${run.dir}/final.md"},
  "review": {"requirements": ["是否达到预期风格，可以定稿"]}
}
```

`review` 在产物上报后请用户验收，批准后才结束；不需要人工验收就省略它。
需要自动有限返工时添加 `check`，需要途中提问时添加 `user_input_schema`。
这两项的完整写法见主入口“输入与验收”，不必先学习路由。

### 4. 验证、交付与启动

```bash
aiwf validate .aiwf/workflows/opening.json
aiwf validate --project
aiwf run opening --title "末世重生" -p idea="末世里每次重生都失去一段记忆" --json
```

启动输入复杂时写 JSON 文件，改用 `--input-file inputs.json`。
单文件校验检查结构与路由；项目校验额外检查子工作流是否存在、必需入参、
调用环等。校验不执行 prompt，也不保证所有运行路径上的 ref 都有值。

仅创建时，交付定义路径、workflow id、启动输入、产物位置、验收点，不启动运行。
执行已获授权时加载执行指南，处理 `run` 返回的第一份任务包。

修改定义后重新校验。新运行使用定义快照，源文件修改只影响以后启动的运行。
运行记录位于 `.aiwf/runs/`，应保留并纳入版本管理，以便恢复和审计。

## 字段与数据作用域

仅在编写定义或排查数据引用时读取。以下以 1.1 为准。

### 字段按用途查

| 位置 | 字段 | 用途 |
|---|---|---|
| 工作流 | schema_version、id、name、nodes | 必填；新定义版本 1.1，id 与定义文件名一致 |
| 工作流 | description | 业务说明 |
| 工作流 | input_schema | 启动 / 子流程调用入参的 JSON Schema |
| 工作流 | max_steps | 单次运行的推进预算，默认 200；按 node_runs 条目计，有循环、返工时显式放大 |
| 工作流 | exports | 子流程返回键 → JSON Schema 片段；省略时不返回业务 context |
| 工作流 | default_model、edges | 模型提示、显式连线；见高阶集成指南 |
| task | id、type、prompt | 必填；prompt 描述任务与验收目标 |
| task / branch | inputs | 材料名称 → 字面值或引用，渲染后出现在任务包 |
| task | user_input_schema | 执行前向用户收集的信息；不是 inputs 的校验 Schema |
| task | output | 可选，仅支持单个主文件声明 `{"file":"路径"}` |
| task | output_schema | 校验 complete 上报的完整结果 |
| task / subflow | output_key | 结果写入 context 的键，默认节点 id |
| task | check、review | Agent 有限质检、人工验收 |
| task | consumes、model | 补充依赖、模型提示；见高阶指南 |
| subflow | workflow、inputs | 子定义 id、显式调用入参 |
| foreach | items、nodes 或 workflow | 条目源（数组 / ref / `$file` / `{"queue": true}`）、内联体或可复用子定义，二选一 |
| foreach | inputs、exports | 子级入参；内联体导出的键（数组或 Schema 映射） |
| foreach | as、index_as、id_field | item/index 别名、静态清单的稳定实例标识（队列模式恒用 `id`） |
| foreach | outputs_key | 结果清单在 context 中的键，默认 foreach id，注意是复数 |
| foreach | max_instances、on_fail | 默认最多 1000 项；失败 abort（默认）或 continue |
| branch | when 或 prompt、routes、default | 确定性条件或 Agent 判断；见分支指南 |
| 独立 review | requirements、routes | 人工检查点；必须明确 APPROVE 去向 |

JSON Schema 本身不填默认值。未列出的任意自定义节点字段不受支持。
建议节点 id 和输出键统一用 `snake_case`，便于 ref：
工作流 id 可含连字符，但 ref 的第一段不能有连字符或以数字开头。
节点若叫 `write-code`，可以设 `output_key: "write_code"` 后引用它。

### inputs 的值可以是什么

同名字段可先用 `use` 简写，再按需覆盖：

```json
{
  "use": ["idea", "tone"],
  "inputs": {"idea": {"ref": "seed"}},
  "prompt": "用 ${tone} 改写 seed"
}
```

上面等价于 `inputs: {"idea": {"ref":"seed"}, "tone": {"ref":"tone"}}`；
显式 `inputs` 在重名时优先。`use` 只用于 task/branch，且只能引用 context 里已有的键。

完整映射写法：

```json
{
  "inputs": {
    "limit": 3,
    "enabled": true,
    "options": {"style": "冷峻", "labels": ["悬念", "反转"]},
    "plan": {"ref": "plan"},
    "first_title": {"ref": "plan.chapters.0.title"},
    "source": {"$file": "notes/source.md"},
    "caption": "第 ${item.number} 章"
  }
}
```

这是字段片段；使用它的流程必须先提供 plan 和 item。
字面值支持对象、数组、数字、布尔、null；嵌套对象/数组中的 ref 和插值也会解析。
`ref` 对象必须只有 `ref` 一个键，不可加 default、类型声明或说明。
`{"$file":"..."}` 只是文件引用数据；Agent 应自行读取文件。

| 写法 | 结果 |
|---|---|
| `{"ref":"plan.chapters"}` | 原始数组的值，保留类型 |
| `{"ref":"tests.exit_code"}` | 数字，例如 0 |
| `"退出码：${tests.exit_code}"` | 字符串，例如“退出码：0” |
| `{"ref":"draft"}`，draft 为文件引用 | `{"$file":"..."}` 对象，不是文件正文 |
| `{"ref":"plan.chapters.0.title"}` | 数组第 0 项的 title |

不支持 `chapters[0]`、JSONPath、表达式求值、通配符或 ref 默认值。
缺失路径报错，不能以空串兜底。已有键的值为 null 则原样返回 null。
要提供可选值，先由启动输入或前置任务输出显式填好。

### ref 从哪里取值

ref 只查**当前运行的 context**，不查完整任务包、文件系统或全局变量。

| 来源 | 可引用示例 | 何时可用 |
|---|---|---|
| 启动输入 | idea、settings.tone | 启动校验通过后 |
| 用户回答 | tone、audience | provide 成功后；答案键写入当前 context |
| task 输出 | plan、plan.chapters | 任务成功上报后；设 output_key 时改用该键 |
| subflow 输出 | analysis.report | 子流程结束并导出 report 后 |
| foreach 输出 | chapters | 批量结束后，值为结果清单 `$file` |
| 当前运行元信息 | run.id、run.dir | 运行时自动提供 |
| foreach 子级 | item、index | 每个实例创建后；可用 as/index_as 改名 |
| foreach 父级 inputs 映射 | previous | 第一项 null；以后为前一实例 exports 对象 |

启动输入本身可以有默认来源：项目的 `.aiwf/config.json` 里 `run_defaults` 为它提供值
（合并序 `run_defaults` → `--input-file` → `-p`，逐键覆盖；只注入定义声明过的键）。
机制与用法见 [模板库](templates.md)。

`run` 是保留键，不要用用户输入、材料键、输出键或循环别名覆盖。
输出会更新同名 context 键，因此输入键与节点输出键应避免无意碰撞。
循环重复执行同一节点会覆盖 context 最新值；旧值保存在审计记录。
当前节点第一次执行时不能引用自己的输出；分支未经过的节点也没有输出。

以下都不是自动可用的 ref 来源：

- `inputs.x`：inputs 只是任务包的材料映射，没有这个通用 context 命名空间。
- `parent.x`、其他 run、兄弟实例：通过父级 inputs 显式传递。
- `node.previous_output`、`feedback`、`check_request`：Agent 从任务包读取。
- shell 环境变量（含 LOCAL_REPO_ROOT）：Agent 先读取，再作为启动输入传入。
- `$file` 的正文或文件里的字段：Agent 读取、解析后输出短结构，或直接消费文件。
- 历史事件、旧版输出：通过审计命令读取，不作为 ref 自动注入。

### 材料别名不会创建变量

```json
{
  "inputs": {"outline": {"ref": "plan.outline"}},
  "prompt": "读取 outline 材料，按其结构创作。"
}
```

这里任务包 `node.inputs.outline` 可供 Agent 使用，但 context 不会新增 outline。
若需插值，写 `${plan.outline}`；不要因为声明了材料就写 `${outline}`。
文件与复杂对象优先放 inputs，prompt 直接说材料名称，避免长 JSON 挤入提示词。

### 父子作用域

子流程仅得到父级显式映射的 inputs、自己的 run 元信息；
foreach 另加当前 item/index。父级 plan、run.dir 不会整体继承。

`run_defaults` 也一样：它只补**顶层 run** 的启动输入，不直注子流程。要让子流程拿到项目固有值
（知识库路径、测试命令等），父级需**自己声明该入参**并在 subflow 节点显式映射
`{"knowledge_dir": {"ref": "knowledge_dir"}}`。漏映射会被 `validate --project` 拦下
（`does not map required inputs [...]`），不会静默变成空值。

```json
{
  "inputs": {
    "outline": {"ref": "plan.outline"},
    "previous": {"ref": "previous"},
    "shared_dir": "${run.dir}"
  }
}
```

此片段放在 foreach 上时：outline/previous/shared_dir 进入子 context。
子级 `${run.dir}` 仍是自己的目录，`${shared_dir}` 才是传入的父目录。
previous 只在 foreach 入参映射阶段额外提供；若未传递，子任务不能引用它。
完整流程见主入口“批量与子流程”。

## 产物设计：对象、文件与代码修改

### 先选结果形状

`node.output` 是**可选的预期文件声明**，`complete` 的 output 是**实际结果**。
两者不是同一个字段的两种写法，省略声明也可以上报结果。

| 任务产物 | 推荐定义 | 实际结果 |
|---|---|---|
| 分类、小规划、短统计 | output_schema；省略 output | 短 JSON 对象 / 数组 |
| 小说正文、长报告 | output.file | 顶层 `$file` 引用 |
| 多个文件 | output_schema；按需另声明一个主文件 | 对象 / 数组内多个 `$file` |
| 修改已有代码、场景、配置 | 省略 output，写 output_schema | 摘要、修改路径、验证结果 |
| 超过 2000 字符的清单 | 将清单本身写 JSON 文件 | 指向清单的 `$file` |

单次实际结果序列化后 ≤2000 字符，包含键名和所有引用，不能通过拆成嵌套字段
绕过。长代码、diff、日志、正文都外置。文件名使用稳定、安全的业务标识，
不要直接拼接未经检查的用户文本。

### 1. 短对象

节点片段：

```json
{
  "id": "plan",
  "type": "task",
  "prompt": "输出两章规划，每项含 number 与 title。",
  "output_schema": {
    "type": "object",
    "required": ["chapters"],
    "properties": {
      "chapters": {
        "type": "array",
        "maxItems": 2,
        "items": {
          "type": "object",
          "required": ["number", "title"],
          "properties": {"number": {"type": "integer"}, "title": {"type": "string"}}
        }
      }
    }
  }
}
```

上报 `{"chapters":[{"number":1,"title":"醒来"},{"number":2,"title":"代价"}]}`，
下游用 `{"ref":"plan.chapters"}` 取得数组。需要更大规划时外置清单。

### 2. 一个主文件

定义写：

```json
"output": {"file": "${run.dir}/chapter.md"}
```

执行时先读任务包中渲染后的 `node.output.file`，创建必要目录并写文件，
再上报 `{"$file":"任务包中的实际路径"}`。用 CLI 时直接：

```bash
aiwf submit <run_id> --file "任务包里的 node.output.file"
```

`--file` 会自动包成 `{"$file": …}`，路径反斜杠/正斜杠都接受。
`submit.json` 的 `output` 通常已经预填这个路径；写完文件直接 `aiwf submit` 即可。
不要把 `${run.dir}` 原样放进上报 JSON：引擎不会对实际结果再做模板渲染。
任务包 `next.text` 已经把这行命令算好，照抄即可。

`output.file` 是**模板**，用当前 run 的 context 渲染，变量域同 inputs
（见[字段与数据作用域](#字段与数据作用域)）：`${run.dir}`、`${run.id}`、
`${<节点输出键>.<路径>}` 与启动输入名都可用。渲染结果若是**相对路径，按项目
工作目录解析**，所以产物可以声明到 run 目录之外，例如落到业务目录：

```json
"output": {"file": "plan/${intake.plan_name}/plan.md"}
```

路径里引用某个节点（如 `${intake.plan_name}`）会**形成一条依赖边**，该节点
必定排在它之前；`${run.*}` 是运行元信息，不产生依赖。
一个节点只能声明**一个**主文件（见下节），要产出两个各自受契约的文件就拆成
两个节点。

引擎检查顶层 `$file` 与声明路径一致、文件存在；可在结果旁附加少量统计。
如果同时定义 output_schema，它校验包含 `$file` 在内的**整个结果**，
不是正文。文件内容、字数、可运行性须由 Agent 实际检查。

### 3. 多个文件

省略 output，用 output_schema 约束多个引用；实际结果例如：

```json
{
  "script": {"$file": "artifacts/episode.md"},
  "shots": {"$file": "artifacts/shots.json"},
  "summary": "剧本与分镜已对应"
}
```

后续材料写 `{"ref":"write.script"}` 或 `{"ref":"write.shots"}`。
可采用文件引用 Schema 片段：

```json
{"type":"object","required":["$file"],"properties":{"$file":{"type":"string"}}}
```

如果必须有一个主文件，也可声明 output.file，并上报：

```json
{
  "$file": "artifacts/episode.md",
  "attachments": [{"$file": "artifacts/shots.json"}]
}
```

此时只对顶层主文件应用 output.file 契约。
**不支持** `output.files`、output 数组或在 output 内声明任意具名文件。
Schema 只检查引用形状，不自动验证所有嵌套文件是否存在；Agent 须逐个核对。
context.md 会收录嵌套 `$file`，普通字符串路径则不会被当作产物引用。

### 4. 修代码 / 修改 Godot 项目

代码落原项目路径，运行目录存测试证据和变更报告。不要为了满足 output.file
再生成一份完整代码副本。推荐省略 output，实际上报：

```json
{
  "summary": "修复空输入导致的越界，并补充边界验证",
  "changed_files": ["src/parser.py", "tests/test_parser.py"],
  "validation": {
    "exit_code": 0,
    "report": {"$file": "artifacts/validation.txt"}
  }
}
```

`changed_files` 是位置索引，不是完整 diff；太多时外置变更清单。
`validation.exit_code` 只能记录真实命令结果，未运行不可填 0。
需要区分跳过时在自己的 output_schema 中设计 status/reason。
若把源文件加入 `$file` 索引，要知道它指向当前文件，后续修改会改变内容；
需要留存版本证据时把 diff/报告另存到当前 run.dir。

完整修复定义在范例目录的 `code-change.json`，它用 check 检查变更证据，
再等待人工验收。其他工作流也可拆成“修复 → 测试 → 条件路由”，不要同时
用两套返工机制重复控制同一件事。

### 5. 检查失败如何处理

output_schema 不符、文件契约不符会驳回产物，任务保持开放并留审计记录。
修正结果或文件，再读取最新动作并上报。声明文件存在不意味着内容通过质检，
`check` 通过也不等于用户已经验收。
`--output-file` / `--json-file` 读取的是**上报结果 JSON**，不是正文文件；
正文用 `--file`。`submit.json` 的 `hint` 只是说明，不属于 payload——只改 `output`。
**驳回时退出码是 3**：状态没有前进，按 `rejected.expected/got` 修产物后重报。

## 分支与路由

任务质检返工优先 task.check；只有业务去向不同才增加 branch。
条件能由已有数据直接判断时用 when；需要 Agent 理解内容时用 prompt。

### 确定性分支

```json
{
  "id": "gate",
  "type": "branch",
  "when": {"eq": [{"ref": "tests.exit_code"}, 0]},
  "routes": {"TRUE": "deliver", "FALSE": "diagnose"}
}
```

这是节点片段，tests 必须先输出 exit_code，deliver/diagnose 必须存在。
引擎自动计算 when 并记录决策，不产生等待 Agent 判断的任务。
完整定义见范例 `condition-routing.json`，两条路径各自产出报告后结束。

支持条件运算：

| 运算 | 写法 |
|---|---|
| eq / ne / gt / gte / lt / lte | `{"gte":[{"ref":"score"},80]}`，恰好两个操作数 |
| and / or | `{"and":[条件1,条件2]}`，非空条件数组 |
| not | `{"not":条件}` |

操作数用字面标量或 typed ref，引用可返回对象/数组用于相应比较。
不用字符串表达式（如 `"score > 80"`）或旧 `var`。
when 必须同时声明 TRUE/FALSE 路由，目标为节点 id。

### Agent 主观分类

```json
{
  "id": "choose",
  "type": "branch",
  "inputs": {"idea": {"ref": "idea"}},
  "prompt": "根据 idea 的核心冲突选择更合适的叙事节奏。只返回 FAST 或 SLOW。",
  "routes": {"FAST": "fast_script", "SLOW": "slow_script"}
}
```

引擎返回 `action.kind=complete`；Agent 根据材料选择标签：

```bash
aiwf submit <root_run_id> --output FAST --json
```

输出标签字符串，不是 task.check 的 verdict 对象。
大小写保持与 routes 一致。未知标签且无 default 时失败；
default 是兜底目标节点，不是一个标签列表，也不要配置为质量检查的自动放行。
完整短剧范例见 `subjective-routing.json`。

### 多出口不要靠数组排列猜路径

显式 edges 出现时按 edges 连接，不再把遗漏连线理解为默认顺序。
路由节点（branch、独立 review）只用 routes/default，不能再有出边。
普通 task 最多一条出边，不能通过多条 edges 做并行。

独立的两条业务路径通常用显式 end 汇合：

```json
"edges": [
  {"from": "tests", "to": "gate"},
  {"from": "deliver", "to": "end"},
  {"from": "diagnose", "to": "end"}
]
```

注意这里没有 gate 的出边，它的去向由 routes 决定。
顶层定义需有 `{"id":"end","type":"end"}` 节点。
foreach 内联体可以路由到自动生成的 `end`，但不得手写 start/end 节点，
内联 edges 的端点只写体内节点。

### 独立人工检查点

多个阶段共同验收时可以插入：

```json
{
  "id": "accept",
  "type": "review",
  "requirements": ["需求和实现是否匹配", "测试证据是否充分"],
  "routes": {"APPROVE": "deliver", "REJECT": "fix"}
}
```

APPROVE 必须有明确目标。task 内嵌 review 更简单，单任务验收优先内嵌。
任何 REJECT 回路都要评估总预算 max_steps；条件可能永久为 false 时应有
终止 / 人工处理路径。不要把业务质量问题只交给死循环保护处理。

条件分支只决定路径，不会生成未执行分支的输出。汇合任务不能无条件 ref
两条互斥分支的结果；可让两条路径使用相同 output_key，或分别交付。
