# 执行与接管工作流

你执行业务、使用工具或向用户提问；引擎负责状态、校验和审计，不调用 LLM。
默认以下为 1.1 协议。始终加 `--json`，使用每次返回的最新任务包继续。

## 开始与接管

```bash
aiwf run <workflow_id> --title "这次要做什么" --input-file inputs.json --json
aiwf current                 # 省略 run_id：唯一运行中的 run，否则最近一个
aiwf status                  # 更短的一行：标题 / 当前节点 / next 命令
```

`--title` **必填**：它认领这次运行，`aiwf runs list` 靠它辨认是哪个实例。定不出正式名字
就给个能认出来的（「末世/重生」即可）；定义里若声明了 `view.dynamic_title`，等它引用的
变量有值时会自动换成正式名。

**id 都可以少抄**：`run_id` 可省略——有运行中的 run 就取最近那个，没有才取最近一条记录；
`current`/`status` 优先显示还在运行的 run，`runs show` 无参则显示最近一次活动。
也可以只给唯一前缀。`--action` 可省略（用当前 action），也可给唯一前缀或当前节点 id。
只有在"同时跑多个 run"或"确认提交的不是当前节点"时才需要完整 id；会改状态的命令
（complete/provide/review/fail/items 写操作）遇到多个 running 且没指定时会报错并列出候选。

任务包的 `run_id` 是根运行；`action` 指向当前真实目标，`next` 直接给出下一行命令：

```json
{
  "run_id": "root-id",
  "status": "running",
  "phase": "executing",
  "action": {
    "id": "opaque-action-id",
    "run_id": "child-or-root-id",
    "node_id": "write",
    "kind": "complete"
  },
  "next": {
    "kind": "complete",
    "text": "aiwf complete root-id --action opaque-action-id --file \"…/chapter.md\""
  },
  "node": {
    "id": "write",
    "type": "task",
    "prompt": "按本章规划创作正文。",
    "inputs": {"chapter": {"number": 1}},
    "output": {"file": ".aiwf/runs/.../chapter.md"},
    "previous_output": null,
    "feedback": null
  }
}
```

**看到 `next.text` 就照抄执行**，不要自己从 `kind` 推命令。文本模式同样在最后打印一行 `next     : …`。
无需查子 run 或维护调用栈；子 run_id 供审计定位。`workflow_id` 对应叶子定义，
`root_workflow_id` 用于根运行 resume。`current` 只读；若 action.kind 为 resume，
先分辨是不是队列在等投喂（`awaiting_items`）——是则用 `aiwf items add`，它会自动推进；
否则执行明确的 resume 命令以恢复调度。所有状态变更命令返回下一任务包。

## 按 action.kind 行动

| kind | 执行 |
|---|---|
| complete | 按 node.prompt 执行，使用 node.inputs 材料，按 node.output/output_schema 交付 |
| check | 按 check_request.criteria 审查 check_request.output，返回 verdict 与 reason |
| provide | 根据 input_request 向用户收集答案 |
| review | 展示 review_request 的要求与产物，等待用户真实结论 |
| resume | 队列在等投喂 / 调度恢复；队列场景改用 `aiwf items add`（停在 awaiting_items 时它会自动推进） |

执行 task 时，先读取材料中 `$file` 指向的文件。`previous_output` 非空表示该任务
已有版本，可结合 `feedback` 修订。文件按 `node.output.file` 写入，完成后上报
相同路径的 `$file`；引擎不会替你创建正文。别把正文粘贴进 complete。

```bash
# 单个产物文件：最常用，不需要手写 JSON
aiwf complete <root_run_id> --action <最新action.id> --file "实际路径" --json
# 结构化结果：写一个 JSON 文件再交（数组/对象一律走这个）
aiwf complete <root_run_id> --action <最新action.id> --json-file result.json --json
# 少量标量字段（值是 JSON）
aiwf complete <root_run_id> --action <最新action.id> --set goal=短规划 --set count=3 --json
```

有 `check` 的任务，上报作品后不会直接过关。先实际检查，再上报：

```bash
aiwf complete <root_run_id> --action <最新action.id> \
  --verdict REVISE --reason "缺少本章要求的转折，需要补充主角选择及其代价" --json
aiwf complete <root_run_id> --action <最新action.id> \
  --verdict PASS --reason "关键事件完整，冲突推进且章尾钩子明确" --json
```

REVISE 保持同一任务与实例，反馈自动进入下一任务包；达到 max_attempts 则失败。
PASS 后若有 review，仍须用户批准。人工验收的拒绝开启新一轮创作与质检；
max_steps 对整个运行继续生效。结构错误和文件不存在会留下 rejected 记录；
按反馈修正后用新 action.id 重报。**驳回时命令退出码是 3**（状态没有前进），
不要把它当成功继续下一步。

```bash
aiwf provide <root_run_id> --action <最新action.id> --data-file answers.json --json
aiwf review <root_run_id> --action <最新action.id> --verdict APPROVE --feedback "用户的意见" --json
aiwf review <root_run_id> --action <最新action.id> --verdict REJECT --feedback "用户要求修改的内容" --json
```

质检结论是 Agent 判断，人工 review 必须来自用户；测试夹具中的模拟审批不可用于
真实运行。`status=completed` 才交付最终成果；failed 时查看 error 并处理原因。
收到 stale action（退出码 4）不要重试旧结果，应重新 current，确认现在的任务与产物是否匹配。
动作编号防顺序误报，不提供多进程并发写保护；同一根运行由一个执行者驱动。

**Windows / PowerShell**：不要用 `--output '{"k":...}'`，shell 会剥掉双引号；
统一用 `--file` / `--json-file` / `--verdict` / `--set`。`--output-file` 能读带 BOM 的 JSON。

## 临时失败与等待

```bash
aiwf fail <root_run_id> --action <最新action.id> --error "具体失败原因" --json
aiwf resume <root_run_id> --json          # workflow id 自动推断
```

临时工具失败修好后 resume；嵌套子运行一起恢复。质检次数耗尽不能靠 resume
清零或直接补报 PASS；需要明确修订时加载主入口“修订与重做”。
定义错误不能靠反复 resume 修好：1.1 运行使用定义快照，修正源定义后新建运行。

awaiting_items 表示引擎队列在等投喂或封口，不能忙循环 resume；
正等投喂时优先 `aiwf items add`——run 停在 awaiting_items 时 add 会自动推进；
业务结束时 `aiwf items seal`。只有一个队列时 `--node` 可省，条目用
`--item-file items.json` 投喂（数组或单个对象），避免在 shell 里拼 JSON。
汇总 foreach 时读取结果清单并检查失败项，不数目录。
实际处理时加载主入口“运行时队列”。

## 交接与交付

交接时读取根运行 `context.md`，再 `current --json`。文件索引定位产物，
`runs show/events` 查看历史；不要从目录里推断哪个章节已验收。

交付时报告状态、关键产物链接、实际验证结果；未完成时明确停在哪个动作。
流程完成只代表状态机走完，不自动证明正文质量、代码正确或所有批量项成功。
等待真实用户回答/验收时保留根 run_id、问题或要求，收到回复后继续。

## 常见误用速查

| 现象 | 处理 |
|---|---|
| complete 要求 action | 使用本次 current 返回的 action.id（`next.text` 已带） |
| action stale（退出码 4） | 重读 current，确认任务和结果匹配后再上报 |
| ref missing | 查实际 context；确认父子 inputs 和前置节点是否执行 |
| 文件输出被驳回（退出码 3） | 按 `rejected.expected/got` 修文件，用新 action.id 重报 |
| 把正文传 --output-file 后解析失败 | 该参数要求结果 JSON；正文改用 `--file` |
| PASS 后还在 awaiting_review | 等待用户结论，再用 review，不重复提交 PASS |
| 不知道子 run 怎么切换 | 1.1 使用根 run_id，任务包直接给叶子动作 |
| 旧 run 没有 action | 加载主入口“集成与兼容”的 1.0 协议 |

状态变更和 current 使用 `--json`；`runs show/list/events/search` 都支持 `--json`
（文本输出仍适合人读）。`--json` 放在子命令前后都行。
