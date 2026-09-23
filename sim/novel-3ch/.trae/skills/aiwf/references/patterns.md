# 批量任务与子工作流

固定几个阶段用顺序 task；对每个条目执行同样步骤用 foreach；
相同步骤需多处复用时抽成独立工作流，通过 subflow 调一次或 foreach 调多次。
foreach 顺序执行，不能用它承诺并行加速。

## 有清单的批量任务：foreach

下面是节点片段，假设 plan 已输出 chapters 数组与 outline 文件引用。
完整可运行版本在范例目录的 `novel-chapters.json`。

```json
{
  "id": "chapters",
  "type": "foreach",
  "items": {"ref": "plan.chapters"},
  "id_field": "number",
  "max_instances": 100,
  "inputs": {
    "outline": {"ref": "plan.outline"},
    "previous": {"ref": "previous"}
  },
  "nodes": [
    {
      "id": "draft",
      "type": "task",
      "inputs": {
        "chapter": {"ref": "item"},
        "outline": {"ref": "outline"},
        "previous": {"ref": "previous"}
      },
      "prompt": "按本章规划写作；previous 非空时先读上一实例导出的正文，保持连续性。",
      "output": {"file": "${run.dir}/chapter-${item.number}.md"},
      "check": {
        "criteria": ["关键事件完整", "与大纲及前章连续", "冲突推进且章尾有钩子"],
        "max_attempts": 3
      },
      "review": {"requirements": ["本章是否可以定稿"]}
    }
  ],
  "exports": ["draft"]
}
```

- items 可以是数组、`{"ref":"path"}` 指向数组或文件引用、`{"$file":"清单路径"}`。
- 子级 context 只含显式 inputs，加当前 `item` 和从零开始的 `index`；
  不会自动继承父 context。
- 父级 inputs 解析时可用 `previous`：前一个实例的 exports 对象，第一项为 null。
  需显式映射后才传给子级。previous 不来自目录、不会携带任意兄弟 context。
- `id_field` 是稳定实例标识；重做保持实例身份，不自行数目录。
- `as`/`index_as` 可重命名 item/index，避免复杂复用时冲突。
- exports 声明子级要返回的 context 键；内联时可用数组，也可用 key→Schema 映射。
- 父级输出是引擎写的结果清单 `$file`，默认 context 键为 foreach id；
  清单每项含 key/status/exports。汇总读取清单，失败项不得当作成稿。
- `on_fail: "abort"` 默认中止；`continue` 留存失败再处理下一项。
- foreach 是顺序执行；级联重做命中 foreach 时整体重做。
- 先前实例更新后，仅 redo 本实例不重做其他实例；有衔接依赖时应整体重做。
- `max_steps` 是每个运行的推进预算，默认 50；内联体继承父定义预算。
  预算包括实际推进与返工，需为循环和验收拒绝留余量，不等于最大条目数。

## 汇总批量产物

父级 foreach 完成后，下一个任务写：

```json
{
  "id": "assemble",
  "type": "task",
  "inputs": {"manifest": {"ref": "chapters"}},
  "prompt": "读取 manifest 清单，按顺序读取 success 项的 exports.draft 文件并汇总；列明失败项，不把失败当成成稿。",
  "output": {"file": "${run.dir}/novel.md"}
}
```

manifest 是文件引用，需要 Agent 读文件；不是内存中的结果数组，
不能直接 `{"ref":"chapters.0.exports.draft"}`。
结果 JSON 清单的每项包含 key/status/exports。
`on_fail: "continue"` 时即使父流程完成也可能有失败项，交付前须明确数量及影响。

## 单次复用：subflow

子工作流 `analyze-piece` 的顶层声明：

```json
{
  "input_schema": {
    "type": "object",
    "required": ["text"],
    "properties": {"text": {"type": "string"}}
  },
  "exports": {
    "analysis": {
      "type": "object",
      "required": ["summary"],
      "properties": {"summary": {"type": "string"}}
    }
  }
}
```

以上是顶层字段片段；子定义仍需 schema_version/id/name/nodes，
且实际节点必须输出 analysis。父节点调用：

```json
{
  "id": "analyze",
  "type": "subflow",
  "workflow": "analyze-piece",
  "inputs": {"text": {"ref": "text"}}
}
```

子流程结束后，父 context.analyze 包含 exports 对象，
下游取 `{"ref":"analyze.analysis.summary"}`。不是直接 `analyze.summary`。
顶层 exports 只能是“键 → Schema”的对象，不能写数组；不约束类型可用 `{}`。
两个完整定义见范例目录的 `subflow-parent.json` 与 `analyze-piece.json`。

## 批量调用复用体

将 foreach 的内联 nodes 换成 `"workflow":"analyze-piece"`，
在 foreach.inputs 中把条目映射为子流程要求的 text：

```json
{
  "id": "analyses",
  "type": "foreach",
  "items": ["第一段", "第二段"],
  "workflow": "analyze-piece",
  "inputs": {"text": {"ref": "item"}}
}
```

此时 exports 在被调用定义的顶层配置，不在 foreach 上配置。
nodes 与 workflow 必须二选一；内联体自动包 start/end，不可手写。
内联 exports 可用数组 `["draft"]`，也可用 Schema 映射。
被调用工作流的 input_schema 会校验子级实际入参；嵌套最多八层。
完成后执行 `aiwf validate --project`，检查子定义、必需入参和调用环。

每项 run.dir 独立，避免所有实例覆盖同一文件；需要共享目录时显式传入，
同时保证文件名有稳定实例标识。未知数量的批量任务另读高阶“动态清单”。
