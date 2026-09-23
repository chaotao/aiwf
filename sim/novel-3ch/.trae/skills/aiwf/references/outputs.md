# 产物设计：对象、文件与代码修改

## 先选结果形状

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

## 1. 短对象

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

## 2. 一个主文件

定义写：

```json
"output": {"file": "${run.dir}/chapter.md"}
```

执行时先读任务包中渲染后的 `node.output.file`，创建必要目录并写文件，
再上报 `{"$file":"任务包中的实际路径"}`。
不要把 `${run.dir}` 原样放进上报 JSON：引擎不会对实际结果再做模板渲染。

引擎检查顶层 `$file` 与声明路径一致、文件存在；可在结果旁附加少量统计。
如果同时定义 output_schema，它校验包含 `$file` 在内的**整个结果**，
不是正文。文件内容、字数、可运行性须由 Agent 实际检查。

## 3. 多个文件

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

## 4. 修代码 / 修改 Godot 项目

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

## 5. 检查失败如何处理

output_schema 不符、文件契约不符会驳回产物，任务保持开放并留审计记录。
修正结果或文件，再读取最新动作并上报。声明文件存在不意味着内容通过质检，
`check` 通过也不等于用户已经验收。
`--output-file result.json` 读取的是**上报结果 JSON**，不是正文文件。
