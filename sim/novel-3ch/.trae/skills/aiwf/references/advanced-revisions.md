# 高阶：修订、依赖级联与实例重做

仅在用户改变已完成内容、已有结果必须返修时使用。
当前任务的常规质量返工用 check / review；临时工具失败修好后 resume。

## 先选操作

| 需求 | 操作 |
|---|---|
| 改已完成 task，自动重做依赖它的结果 | revise --mode cascade（默认） |
| 仅改目标 task，完成后返回发起位置 | revise --mode return |
| 更改已完成 task 的用户答案并启动修订 | provide --node |
| 只重做 foreach / subflow 的一个实例 | redo --node --instance |
| 代码或工具临时失败，定义本身正确 | 修好外部条件后 resume |
| 定义写错、需要改变节点图 | 修改源定义、校验并启动新 run |

普通叶子动作使用根 run_id 和 action.id；下列是**显式管理操作**，
必须使用目标所在的实际 run_id。通过 action.run_id 或审计记录定位，
不要把根 id 当成子任务 id。

## 修订已完成任务

```bash
aiwf revise <target_run_id> --target outline --reason "用户调整主线" --mode cascade --json
aiwf revise <target_run_id> --target outline --reason "仅修正错字" --mode return --json
```

目标必须是已成功执行的 task。cascade 按依赖关系标记 stale，
未受影响的节点可复用，审计中记录为 skipped/node_reused。
return 只重做目标，不保证下游结果与它保持业务一致；确有内容影响时使用 cascade。

显式控制范围：

```bash
aiwf revise <target_run_id> --target outline --reason "全篇受影响" --scope all --json
aiwf revise <target_run_id> --target outline --reason "指定修订范围" --scope manual --nodes chapter_a,final --json
```

自动依赖来源包括 inputs 的 ref/插值、prompt 插值、when、output 路径、
check 检查项与 consumes。它是数据依赖分析，不理解隐含剧情或代码业务影响。
Agent 私自从目录读取但未声明的材料，可能不会被正确判为依赖。

必要时补充 task 字段：

```json
"consumes": ["outline", {"glob": "chapters/*.md"}]
```

glob 匹配被引用产物的文件路径，不是让引擎执行文件搜索或生成材料。
通常直接声明 inputs 就够了；使用共享可变文件时尤其要核对依赖。

## 修改历史用户输入

```bash
aiwf provide <target_run_id> --node outline -p tone="冷峻" --reason "用户调整风格" --json
```

values 仍按该 task 的 user_input_schema 校验，成功后开启修订。
这与当前 awaiting_input 的 `provide --action` 不同；不要混用。

## 单实例重做

```bash
aiwf redo <parent_run_id> --node chapters --instance 1 --reason "第一章需要重写" --json
```

instance 是 id_field 产生的稳定 key，没有 id_field 时通常是从零开始的索引键。
单实例 redo 不自动重做后面的兄弟实例，也不自动重写所有下游成品。
章节依赖 previous 时，改第一章通常影响后续章节；应明确扩大修订范围，
或从上游任务发起能覆盖整个 foreach 的级联修订。
整体 foreach 重做会保留子运行审计，汇总应重新读取结果清单，不能数目录。

## 检查预算耗尽与恢复

check 拒绝耗尽后 run 为 failed。resume 不清零 attempt，也不能强行补报 PASS。
用户决定继续返修时，可先 resume 恢复运行，再对已有成功上报的该 task 显式
revise，开启新的检查预算；若没有可修订的成功记录，则修正定义 / 输入后新建运行。
记录具体原因，不能无限自动重置预算逃过终止保护。

## 修订完成与审计

revision 不是“当前正在执行旧节点”的同义词。
级联修订要等受影响范围被重做或按规则复用后才结束，
所以已经进入新节点时，修订状态仍可能存在。
用 runs show/events 核对 stale/reused 与 revision_completed，
不要仅凭当前 node id 判断是否已关闭。

1.1 run 使用定义快照。修改源 JSON 不会修复正在运行的图；
不要直接编辑 record.json、workflow.json 快照来绕过契约。
