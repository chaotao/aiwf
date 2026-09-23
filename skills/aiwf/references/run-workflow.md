# 驱动 aiwf 工作流运行

**核心概念**：引擎不调用 LLM、不执行任务。你（agent）是执行者，必要时把人
接入；引擎负责校验、渲染、审计与推进。每次查看状态，信封都带 `status` 和
`phase`，按 phase 行动。

## 1. 启动 / 接管

```bash
aiwf run <wf_id> --input-file inputs.json --json
aiwf run <wf_id> -p title="书名" --json
aiwf resume <wf_id> <run_id> --json       # 续跑失败运行
aiwf current <run_id> --json              # 中断后重新查看
```

信封（始终加 `--json`）：

```json
{ "run_id": "...", "workflow_id": "...",
  "status": "running|completed|failed",
  "phase": "executing|awaiting_input|awaiting_review|awaiting_items",
  "revision": { "...当前修订信息，无则 null" },
  "error": "失败原因（status=failed 时直接在此，无需另查 runs show）",
  "node": { "..." }, "input_request": null, "review_request": null }
```

**接管正在跑的 foreach（中途换 agent 最常见）**，三步：

1. `aiwf current <父run_id> --json`：信封节点是 foreach 时看 `node.child.run_id`
   （或父 `context.md` 的「子工作流」小节）；
2. `aiwf current <child_run_id> --json`：拿到子级当前节点；
3. 按子级自己的 phase 驱动，直到子级 end——会自动传播回父级并创建下一实例。

## 2. 按 phase / node 行动

### phase=executing：执行当前节点

- **task**：
  - 用渲染后的 `node.prompt` 调用**你自己的模型**；`model_hint` 仅供参考。
  - 是否使用工具（写文件、查 API 等）由你自行组织，引擎不感知工具。
  - **大产物（正文/剧本/长报告）写入文件**，不要把全文作为输出；完成后只上报
    引用 `{"$file": "路径"}`。prompt 里出现的路径（如 `路径：novel/x.md`）是
    上游产物，先用读文件工具读取再使用。
  - `previous_output`（修订/重做时为上次原稿或原引用）在此基础上改，不要从零生成。
  - 短结果（一句话、小 JSON）可直接作为输出；产物最终存入 `output_key`。
- **branch**：分两种，看信封字段：
  - **模型分支**（无 `deterministic`）：用 `node.prompt` 调用你自己的模型。若
    prompt 给的是文件路径，先读文件；回答必须是 `routes` 中的标签（取首行、忽略
    标点大小写），`complete --output 标签`；你不需要决定下一节点，引擎算路由。
  - **确定性分支**（`deterministic=true`，配了 `when`）：引擎自己求值，不要回答
    标签，直接 `complete <run_id> --empty`，结果按 TRUE/FALSE 路由。
- **start / end**：无需执行。

然后上报：

```bash
aiwf complete <run_id> --output "文本或标签" [--model m] [--tokens '{...}'] --json
aiwf complete <run_id> --output '{"$file": "novel/chapter-1.md"}' --json   # 大产物
aiwf complete <run_id> --output-file result.json --json
aiwf complete <run_id> --empty --json    # start/end
```

> 单个节点输出超过 **2000 字符**会被引擎拒绝；遇到该报错就把内容写文件、改报引用。

### phase=awaiting_input：向用户收集信息

信封的 `input_request` 列出需要回答的字段：每个字段的 title/description/type/
enum。向用户提问（可一次问完），回答后：

```bash
aiwf provide <run_id> -p inspiration="赛博雨夜" --json
aiwf provide <run_id> --data-file answers.json --json
```

引擎校验回答并写入 context；若同一节点还有未答字段会继续等待，全部答完后返回
渲染好的正常 task spec。用户修改已答内容时重新 provide 即可。

### phase=awaiting_review：请人验收

信封的 `review_request` 包含：`kind`（内嵌/独立检查点）、渲染后的
`requirements`（字符串或检查项清单）、`routes`（APPROVE/REJECT 去向）、
`attempt`。把验收要求和待审产物展示给人，拿到结论后：

> 注意：独立 `review` 检查点本身**不出现在 node_runs**，只在事件流留
> `review_requested/review_decided`；内嵌验收则在被审 task 的记录里体现。

```bash
aiwf review <run_id> --verdict APPROVE --feedback "可选意见" --json
aiwf review <run_id> --verdict REJECT --feedback "需要改什么" --json
```

- APPROVE：按 routes 前进；
- REJECT：指针回到 routes 指定节点（缺省重做被验收的 task），反馈随下次 spec
  下发（`node.feedback`），按意见修改。
- 若 complete 时产物不符节点的 `output_schema`，引擎直接**驳回**（run 不失败、
  不走人工）：node_runs 记 `rejected`、`node.feedback` 给原因，按反馈改完重报即可。

### 子工作流节点：转去驱动子级

当前节点是 `subflow` / `foreach` 时，引擎已自动建好子运行；信封的 `node.child`
给出子 `run_id`（foreach 还带进度 `node.subflow_state.progress`）：

- **不要 complete 父节点**——子级进行中父级会拒绝。直接用现有命令驱动子级：
  ```bash
  aiwf current <child_run_id> --json
  # 按子级自己的 phase 行动（多为普通 task），直到子级走到 end
  aiwf complete <child_run_id> --empty --json
  ```
- 子级 end 的那次 complete 会被引擎**自动传播**：校验子级 exports 快照；foreach
  重读 items、创建下一实例或写结果清单收尾。返回的信封已经是**父级的下一规格**
  （foreach 会显示新的 `node.child`，继续驱动即可）。
- foreach 的结果清单位于父运行目录 `foreach-<node>-results.json`，父 context 只有
  `{"$file":路径}`，需要汇总时读该清单。
- **动态 items**：父级进入 `awaiting_items` 表示当前条目已跑完、源里还没有新条目。
  外部追加条目（或设 `done=true`）后执行 `aiwf resume <wf_id> <run_id>` 继续。
- 父节点已完成后只想重做某一实例：
  ```bash
  aiwf redo <run_id> --node chapters --instance 0 --reason "重写第一章" --json
  ```
  子级沿用原目录与 run_id；完成后清单整体刷新、其余实例保留。
- 子级失败（含 exports 契约不满足）：abort 时父级 failed，`aiwf resume` 会同时
  重开该子级；on_fail=continue 时 foreach 自动推进下一条，失败留在清单。

## 3. 计划外回跳修订（revise）

执行中途发现某个**已完成的 task** 内容需要调整（而非验收打回），无需改定义：

```bash
aiwf revise <run_id> --target worldview --reason "补充一条设定规则" \
  [--mode cascade] [--scope auto] --json
```

**重做范围如何确定**：引擎根据节点 prompt/requirements 中的 `${var}` 引用及 task
显式声明的 `consumes`（context key 或按 `$file` 路径匹配的 glob）构建数据依赖图。

- `--scope auto`（默认）：从 target 的输出键沿依赖图求传递闭包——引用了变更内容
  的节点标 stale 重做；其余已完成的下游节点自动复用（审计记 skipped），不重做。
- `--scope all`：target 之后所有节点重做。
- `--scope manual --nodes a,b`：只重做指定节点。

**两种模式**：

- `--mode cascade`（默认）：沿 edges 前进，stale 节点重做，非 stale 节点自动跳过，
  走完 stale 清单后修订关闭、流程恢复正常。
- `--mode return`：只重做 target 本身，完成后直接跳回发起修订时所在节点。

**修改已完成节点的输入**（如世界观的灵感从"玄幻"改成"未来世界"）：直接对该
节点 provide 并加 `--node`，引擎会自动开启 cascade 修订：

```bash
aiwf provide <run_id> --node worldview -p inspiration="未来世界" --reason "换方向"
```

约束：target 必须是已成功执行过的 task；同一时间只允许一个活跃修订。

## 4. 失败处理

```bash
aiwf fail <run_id> --error "失败原因"
aiwf resume <wf_id> <run_id>
```

失败节点的记录入审计；续跑从失败节点重新执行，已完成节点不重跑，上下文从记录恢复。
进程可随时中断，`aiwf current` 查看当前 phase 后继续。

先看失败信封的 `error` 判断类型，再决定动作：

- **可恢复失败**（工具临时故障、执行步骤出错）：排除原因后 `aiwf resume`，从失败
  节点重跑。
- **定义/条件类失败**（`condition variable ... not found`、模板变量无来源、
  routes 目标错误）：resume 会原样再炸——需要修正工作流定义（变量写法见
  [create-workflow.md](create-workflow.md)）或改用 revise/正确的协议命令，不要
  盲目重试。
- **子流程失败**：abort 时父级随之 failed，resume 父级会同时重开失败实例；
  on_fail=continue 时已自动跳过，失败留在 foreach 清单。

## 5. 共享与交接：context.md

每次状态变更，引擎都会把运行概况自动写入该次运行文件夹的 `context.md`：状态与
当前节点、输入、共享上下文（内联短值）、以及全部 `$file` 产物的**索引与来源
节点**。它无需 LLM 生成、始终与记录同步：

- 中途换人/换 agent 时，先读 `context.md` 即可拿到当前进度和所有产物位置；
- 不确定某个产物路径时，查它的「产物索引」，再用读文件工具打开。

审计检索见 [inspect-runs.md](inspect-runs.md)。
