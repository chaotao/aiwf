# 高阶：运行中追加条目的动态清单

只有数量事先未知、且需要边执行边补充条目时才使用。
已知清单用静态 foreach；固定次数返工用 task.check。

## 定义与文件

foreach 的 items 指向一个 JSON 文件，该文件内容为：

```json
{"done": false, "items": [{"number": 1, "title": "醒来"}]}
```

节点字段片段：

```json
{
  "items": {"$file": "queue/chapters.json"},
  "id_field": "number",
  "max_instances": 100
}
```

推荐把队列路径作为启动输入传入，避免不同运行共享同一个可变文件。
完整定义见范例目录 `dynamic-items.json`，附带 `dynamic-queue.json` 初始数据。
items 文件若存数组则是静态清单；存 `{done,items}` 才是动态模式。

## 推进顺序

1. 启动前由 Agent 创建队列文件，把当前已知项写入 items，done=false。
2. 引擎在实例边界重读队列，为新增项顺序创建子运行。
3. 当前项已完成且没有新项时返回 awaiting_items，action.kind=resume。
4. Agent 根据业务决定下一项，更新队列，再 `resume <root_workflow_id> <root_run_id> --json`。
5. 业务结束时把 done 改为 true；已有项全部结束后才能汇总收尾。

`current` 只读，不会因为看了状态而消费新条目。
`{"done":false,"items":[]}` 会等待；`{"done":true,"items":[]}` 可直接结束批量。
是否完结、下一章序号和内容由 Agent 决定，引擎不维护业务计数。

## 更新约束

只追加条目，不修改、删除或重排已有条目，不复用 id_field；
引擎会检查已启动项的一致性。需要重写既有条目，使用明确的修订 / 重做流程，
不要偷改队列使审计身份失效。
把 done=true 作为封口，之后不继续追加。

每次先生成完整 JSON 到临时文件，校验后同目录原子替换，避免引擎读到半截。
同一根运行由一个执行者驱动；不要让多个 Agent 同时修改队列和状态。
保存队列、运行记录和产物，恢复时继续使用同一份业务状态。

动态输入文件不是质量检查器。先确认前一实例结果，再决定下一项，
不要单凭目录里出现文件推断上一章成功。连续性依赖用 foreach.inputs 显式传 previous，
其值是前一实例 exports，不是全文。

## 边界和排查

| 现象 | 检查 |
|---|---|
| 一直 awaiting_items | 队列是否有新项；是否已 done=true；是否调用 resume |
| 清单被拒绝 | JSON 格式、重复 key、修改 / 重排已启动项 |
| 很长的故事提前失败 | max_instances 是否足够；max_steps 是否为返工与循环留余量 |
| 子级读不到新材料 | 材料是否显式通过 inputs 传入；文件是否实际存在 |

动态次数虽未知，也要给 max_instances 与 max_steps 合理上限。
内联子运行继承父定义的推进预算；不要把 max_steps 简单当成“最大章节数”。
