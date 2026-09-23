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
  "phase": "executing|awaiting_input|awaiting_review",
  "revision": { "...当前修订信息，无则 null" },
  "node": { "..." }, "input_request": null, "review_request": null }
```

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
- **branch**：用 `node.prompt` 调用你自己的模型。若 prompt 给的是文件路径，先
  读文件；回答必须是 `routes` 中的标签（取首行、忽略标点大小写）；你不需要决定
  下一节点，引擎算路由。
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

```bash
aiwf review <run_id> --verdict APPROVE --feedback "可选意见" --json
aiwf review <run_id> --verdict REJECT --feedback "需要改什么" --json
```

- APPROVE：按 routes 前进；
- REJECT：指针回到 routes 指定节点（缺省重做被验收的 task），反馈随下次 spec
  下发（`node.feedback`），按意见修改。

## 3. 计划外回跳修订（revise）

执行中途发现某个**已完成的 task** 内容需要调整（而非验收打回），无需改定义：

```bash
aiwf revise <run_id> --target worldview --reason "补充一条设定规则" \
  [--mode cascade] [--scope auto] --json
```

**重做范围如何确定**：引擎静态扫描所有节点的 `${var}` 引用，构建数据依赖图。

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

## 5. 共享与交接：context.md

每次状态变更，引擎都会把运行概况自动写入该次运行文件夹的 `context.md`：状态与
当前节点、输入、共享上下文（内联短值）、以及全部 `$file` 产物的**索引与来源
节点**。它无需 LLM 生成、始终与记录同步：

- 中途换人/换 agent 时，先读 `context.md` 即可拿到当前进度和所有产物位置；
- 不确定某个产物路径时，查它的「产物索引」，再用读文件工具打开。

审计检索见 [inspect-runs.md](inspect-runs.md)。
