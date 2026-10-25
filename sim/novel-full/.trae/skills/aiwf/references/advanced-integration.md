# 高阶：SDK、显式图与兼容

日常创建和执行优先 CLI；仅自建驱动器、维护旧运行时读取本页。

## SDK 的 1.1 驱动循环

```python
from aiwf import Storage, WorkflowEngine

storage = Storage(".aiwf").init_dirs()
engine = WorkflowEngine(storage)
workflow = storage.load_workflow("opening")
record = engine.start(workflow, {"idea": "一次有代价的重生"})
root_id = record["run_id"]
engine.settle(root_id)
packet = engine.action_spec(root_id)
# 外部 Agent 执行业务、生成 result；不要让引擎调用 LLM。
# packet = engine.act(root_id, packet["action"]["id"], "complete", output=result)
```

start 后 settle 负责推进自动节点；action_spec 只读。
act 按动作代号提交并返回下一任务包，支持 complete/provide/review/fail；
用户回答用 `values`，人工验收用 `verdict` 和 `feedback`。
结果仍需经过文件契约、Schema、质检和人工验收。
不要直接循环低级 complete 来跳过 action 检查或子运行调度。
低级 API 为兼容保留，其职责和 CLI 的自动 settle 边界不同。

action.id 用于拒绝过期结果，不是多进程锁。同一根运行只允许一个驱动者。
外部工具执行中断时，接管者读取 current 并核实文件是否完成，避免重复修改。

## 显式图与预算

省略 edges 时数组顺序连线；一旦给 edges，就明确列出需要的普通连线。
branch / 独立 review 使用 routes，不再写出边。显式 start 最多一个；
无 start 则从第一个节点开始。顶层可写 end 表示多个路径的共同出口。
foreach 内联体自动生成 start/end，不手写它们。

循环和人工拒绝会消耗推进预算 max_steps，默认 200。
它按 node_runs 记录条数计（start/end、failed、rejected 与修订复用的 skipped 都算一条）。
它是最后的终止保护，不应代替具体业务的结束条件。
嵌套深度上限为八层，不支持递归工作流调用；用项目校验检查调用环。

## 模型与用量只是提示 / 审计

顶层 default_model、节点 model 提示外部 Agent 选择模型。
引擎不创建模型客户端、不调用 LLM，也不保证当前 Agent 能切换模型。

```bash
aiwf complete <root_run_id> --action <最新id> --output-file result.json \
  --model <实际模型> --tokens-file usage.json --json
```

usage.json 可包含 prompt/completion/total；不知道的值不编造。
节点 duration_ms 包含思考、工具、等待时间，不等于纯模型耗时。

## 1.0 与 1.1 的边界

| 项目 | 1.0 旧协议 | 1.1 推荐协议 |
|---|---|---|
| 数据引用 | 历史 var / 字符串插值 | ref 保留类型；插值只做文字 |
| 叶子调度 | Agent 跟踪父子 run | 根 run_id 直接得到当前叶子 action |
| 自动节点 | 逐节点 complete | settle 自动推进 start/end/when |
| 上报 | 无 action.id | 必须使用最新 action.id |
| 产物文件声明 / 有限质检 | 不支持 output/check | task.output / check |

旧 task.input_schema 是 user_input_schema 的兼容别名，旧定义和快照仍可读。
新建或编辑 task 时用 user_input_schema，不同时写两者；
**工作流顶层的 input_schema 不改名**。

旧记录的执行方式：

- start/end 与确定性分支按旧协议 `complete --empty`。
- foreach 从 node.child.run_id 取得子运行，分别 current/complete/provide/review。
- 子级完成会回传父级；current 只读，需要重新调度时显式 resume。

不要直接改一个未完成 1.0 run 的 schema_version。
保留旧定义供旧运行恢复，另存新 id 的 1.1 定义并创建新运行。
新运行有 workflow.json 快照，源修改只影响未来运行；
旧记录没有快照且定义版本已改变时，引擎拒绝静默升级。
运行记录自身的 schema_version 与工作流版本分开，勿批量替换 record.json 中版本号。
