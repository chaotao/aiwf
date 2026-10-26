# 执行：两命令循环与验收

把定义跑起来：任务包怎么读、结果怎么交，以及入参、途中提问、Agent 质检和人工验收分别在哪一步发生。

## 目录

- [执行与接管工作流](#执行与接管工作流)
- [启动入参、途中提问、质检与人工验收](#启动入参途中提问质检与人工验收)

## 执行与接管工作流

你执行业务、使用工具或向用户提问；引擎负责状态、校验和审计，不调用 LLM。
默认以下为 1.1 协议。始终加 `--json`，使用每次返回的最新任务包继续。

### 主循环：两条命令

```bash
aiwf run <workflow_id> --title "这次要做什么" --input-file inputs.json --json
aiwf                      # 读任务包（等同 aiwf status）
# …执行 node.prompt：写文件、跑工具、问用户…
aiwf submit               # 交当前步骤的结果
```

`aiwf`（无参）或 `aiwf status [RUN]` 给出：标题/状态、当前节点、材料、产物路径、
check/review 要求、`submit.json` 路径、`next` 命令。**照 `next` 做**。

`aiwf submit` 按当前阶段分派：

| 当前步骤 | 怎么交 |
|---|---|
| task（有 `output.file`） | 写文件到 `artifact` 路径，直接 `aiwf submit`（模板已指向它） |
| task（只有 `output_schema`） | 改 `submit.json` 的 `output`，或 `--json-file result.json` |
| check | `aiwf submit --verdict PASS --reason "实测依据"`（或 REVISE） |
| review | `aiwf submit --verdict APPROVE --feedback "用户结论"`（或 `--approve` / `--reject`） |
| provide | `aiwf submit -p tone=冷峻`（或 `--data-file answers.json`） |
| 队列 awaiting_items | 改 `submit.json` 的 `output` / `seal`，或 `--item-file items.json` |
| 失败 | `aiwf submit --fail "具体原因"` |

### submit.json

每个会停下来的动作，引擎在根 run 目录写一份 `submit.json`：

```json
{
  "action": "c5c2e2715306c71491734ffb",
  "action_run_id": "20261025-165913-0758df",
  "run_id": "20261025-165913-0758df",
  "node": "outline",
  "kind": "complete",
  "hint": "把文件写到 .aiwf/runs/.../outline.md；写完直接 aiwf submit。",
  "output": {"$file": ".aiwf/runs/.../outline.md"}
}
```

- **只改 `output`（items 还可改 `seal`）**；`action`/`kind`/`node` 由引擎维护。
- check 的 `output` 是 `{"verdict":"PASS|REVISE","reason":""}`；
  review 是 `{"verdict":"APPROVE|REJECT","feedback":""}`；
  provide 是字段名到值；queue 是条目数组 + `seal`。
- 模板里的 action 是"你看到的是哪一步"的指纹：run 前进后旧模板会被拒（退出码 4），
  不会把旧结果交给新步骤。此时 `aiwf status` 后 `aiwf submit --print` 重建。
- 提交成功后引擎归档 payload 到 `submissions/NNNN-<node>.json`，并写下一份模板；
  run 完成/失败时删除 `submit.json`。
- **旧 run 可能没有 `submit.json`**（L2 之前启动，或 SDK 驱动）：`aiwf status` 会标
  `(missing)`；`aiwf status --ensure` 补建，或直接 `aiwf submit`——单个文件任务且文件
  已存在时首次 submit 会补建并提交（若还有 output_schema 额外要求，会以驳回返回）；
  其余情况会以退出码 2 提示先编辑模板。

### 接管与交接

交接时读根运行 `context.md`，再 `aiwf status --json`。文件索引定位产物；
`runs show/events` 查看历史；不要从目录里推断哪个章节已验收。

### 失败与等待

```bash
aiwf submit --fail "具体失败原因"
aiwf resume <run>                         # workflow id 自动推断
```

临时工具失败修好后 resume；嵌套子运行一起恢复。质检次数耗尽不能靠 resume
清零或直接补报 PASS；需要明确修订时加载“修订与重做”。

`awaiting_items` 表示引擎队列在等投喂或封口：改 `submit.json` 的 `output`/`seal`
后 `aiwf submit`；或 `--item-file items.json`。只有一个队列时 `--node` 可省。
汇总 foreach 时读取结果清单并检查失败项，不数目录。

### 旧命令（保留兼容，已隐藏）

`complete` / `provide` / `review` / `fail` 仍可用，但已弃用；文本模式会打印
`deprecated: use aiwf submit`。新代码不要再用它们。`current` 保留为机器可读的
任务包（SDK/旧脚本），`aiwf status --json` 是它的超集。

### Windows / PowerShell

不要用 `--output '{"k":...}'`，shell 会剥掉双引号；统一用 `--file` / `--json-file` /
`--verdict` / `--set`。`submit.json` 用 UTF-8 读，带 BOM 也可以。

### 常见误用

| 现象 | 处理 |
|---|---|
| action stale（退出码 4） | `aiwf status` 后 `aiwf submit --print`，确认产物仍适用再交 |
| 文件输出被驳回（退出码 3） | 按 `rejected.expected/got` 修文件，重跑 `aiwf submit` |
| check 占位符没改 | verdict 只能是 PASS/REVISE，reason 必须非空 |
| review 占位符没改 | verdict 只能是 APPROVE/REJECT，结论必须来自用户 |
| 多个 run 并行 | `aiwf runs list --status running`，显式给 `run_id` 或唯一前缀 |

## 启动入参、途中提问、质检与人工验收

按发生时机选择能力：

| 时机 | 定义字段 | 执行方 / 命令 | 作用 |
|---|---|---|---|
| 启动时 | 顶层 input_schema | Agent 准备 run 入参 | 缺项或类型错误时启动失败 |
| task 执行前 | user_input_schema | 用户回答；provide | 缺 required 信息时暂停提问 |
| task 上报时 | output_schema、output.file | 引擎校验 | 检查结果结构、主文件契约 |
| task 上报后 | check | Agent 检查；complete | 有限次数的当前任务返工 |
| 质检通过后（若有） | review | 用户验收；review | 人工批准后才前进 |

### 启动输入与途中提问

顶层 input_schema 描述整个工作流的调用接口：

```json
"input_schema": {
  "type": "object",
  "required": ["idea"],
  "properties": {"idea": {"type": "string"}}
}
```

task 上的片段描述执行前要收集的信息：

```json
{
  "user_input_schema": {
    "type": "object",
    "required": ["tone"],
    "properties": {
      "tone": {"type": "string", "enum": ["冷峻", "温暖"], "title": "叙事风格"}
    }
  },
  "inputs": {"idea": {"ref": "idea"}, "tone": {"ref": "tone"}}
}
```

tone 缺失时返回 `action.kind=provide`、`phase=awaiting_input`。
按 input_request 中的字段提问，收到答案后提交：

```bash
aiwf submit <root_run_id> -p tone="冷峻" --json
```

复杂答案用 `--data-file answers.json`，或直接改 `submit.json` 的 `output`。
submit 会校验用户答案并写入 context，再渲染任务材料。inputs 本身没有与之配对的
inputs_schema 字段。

预填信息不会重复询问；JSON Schema 的 default 不自动落值。
同一 run 再次进入已收集过信息的节点不会重新激活提问；
每项都要询问时，把任务放进 foreach，每个实例有自己的输入状态。
需要每轮动态更新时，由 Agent 显式收集后更新文件或发起输入修订。

### Agent 有限质检

task 增加：

```json
"check": {
  "criteria": ["字数符合要求", "关键事件完整", "结尾留下明确悬念"],
  "max_attempts": 3
}
```

正常上报后，任务包进入 `action.kind=check`、`phase=awaiting_check`。
读取 `check_request` 的实际产物与检查项，执行检查后上报：

```bash
aiwf submit <run_id> --verdict REVISE \
  --reason "实测正文仅 620 字，低于 800 字要求" --json
aiwf submit <run_id> --verdict PASS \
  --reason "按要求口径统计 986 字，关键事件与悬念均已核对" --json
```

也可以改 `submit.json` 的 `output`，或写 `--json-file verdict.json`。
reason 必须非空且说明依据。
“自动”只指引擎安排检查动作，质量仍由 Agent 判断。字数、测试、资源加载等
可测项目用工具验证，证据较长时写报告并在 reason 中指出位置。

max_attempts 包括初稿：3 表示初稿及最多两次返工。
REVISE 在当前任务重做；previous_output 和 feedback 进入下一任务包。
次数耗尽运行失败，resume 不会清零；按高阶修订指南处理，不能直接补报 PASS。
Schema 驳回不是质检结论，先满足结构与文件契约才能进入质量检查。

### 人工验收

task 增加：

```json
"review": {"requirements": ["风格符合预期，可以定稿"]}
```

有 check 时先质检；没有 check 时产物成功上报后直接进入人工验收。
展示产物位置、关键摘要及 review_request，再等待用户明确结论：

```bash
aiwf submit <root_run_id> --verdict APPROVE --feedback "用户认可" --json
aiwf submit <root_run_id> --verdict REJECT --feedback "用户要求增强冲突" --json
```

默认 APPROVE 前进，REJECT 重做当前任务。人工拒绝会开启新的质检预算，
整个运行仍受 max_steps 保护。质检通过不能代替人工批准；
测试夹具里的模拟审批只能用于测试。

可以在同一 task 同时写 user_input_schema、inputs、output、check、review；
它们分别控制前置提问、材料、产物、Agent 检查和人的确认。
完整组合见范例目录的 `user-check-review.json`。

### 非默认验收路由

需要打回更早阶段才添加：

```json
"review": {
  "requirements": ["是否符合大纲"],
  "routes": {"APPROVE": null, "REJECT": "outline"}
}
```

这里 null 表示批准后按正常顺序继续；outline 必须存在。
这只是显式跳转，不是依赖感知的计划外修订；回路应有 max_steps 保护。
跨多个阶段的独立验收点可用 review 节点，具体见分支与路由指南。
