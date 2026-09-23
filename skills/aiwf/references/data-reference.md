# 字段与数据作用域

仅在编写定义或排查数据引用时读取。以下以 1.1 为准。

## 字段按用途查

| 位置 | 字段 | 用途 |
|---|---|---|
| 工作流 | schema_version、id、name、nodes | 必填；新定义版本 1.1，id 与定义文件名一致 |
| 工作流 | description | 业务说明 |
| 工作流 | input_schema | 启动 / 子流程调用入参的 JSON Schema |
| 工作流 | max_steps | 单次运行的推进预算，默认 50；有循环、返工时显式放大 |
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
| foreach | items、nodes 或 workflow | 条目源、内联体或可复用子定义，二选一 |
| foreach | inputs、exports | 子级入参；内联体导出的键（数组或 Schema 映射） |
| foreach | as、index_as、id_field | item/index 别名、稳定实例标识 |
| foreach | outputs_key | 结果清单在 context 中的键，默认 foreach id，注意是复数 |
| foreach | max_instances、on_fail | 默认最多 1000 项；失败 abort（默认）或 continue |
| branch | when 或 prompt、routes、default | 确定性条件或 Agent 判断；见分支指南 |
| 独立 review | requirements、routes | 人工检查点；必须明确 APPROVE 去向 |

JSON Schema 本身不填默认值。未列出的任意自定义节点字段不受支持。
建议节点 id 和输出键统一用 `snake_case`，便于 ref：
工作流 id 可含连字符，但 ref 的第一段不能有连字符或以数字开头。
节点若叫 `write-code`，可以设 `output_key: "write_code"` 后引用它。

## inputs 的值可以是什么

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

## ref 从哪里取值

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

## 材料别名不会创建变量

```json
{
  "inputs": {"outline": {"ref": "plan.outline"}},
  "prompt": "读取 outline 材料，按其结构创作。"
}
```

这里任务包 `node.inputs.outline` 可供 Agent 使用，但 context 不会新增 outline。
若需插值，写 `${plan.outline}`；不要因为声明了材料就写 `${outline}`。
文件与复杂对象优先放 inputs，prompt 直接说材料名称，避免长 JSON 挤入提示词。

## 父子作用域

子流程仅得到父级显式映射的 inputs、自己的 run 元信息；
foreach 另加当前 item/index。父级 plan、run.dir 不会整体继承。

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
