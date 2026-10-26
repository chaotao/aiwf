# 执行与接管工作流

你执行业务、使用工具或向用户提问；引擎负责状态、校验和审计，不调用 LLM。
默认以下为 1.1 协议。始终加 `--json`，使用每次返回的最新任务包继续。

## 主循环：两条命令

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

## submit.json

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

## 接管与交接

交接时读根运行 `context.md`，再 `aiwf status --json`。文件索引定位产物；
`runs show/events` 查看历史；不要从目录里推断哪个章节已验收。

## 失败与等待

```bash
aiwf submit --fail "具体失败原因"
aiwf resume <run>                         # workflow id 自动推断
```

临时工具失败修好后 resume；嵌套子运行一起恢复。质检次数耗尽不能靠 resume
清零或直接补报 PASS；需要明确修订时加载“修订与重做”。

`awaiting_items` 表示引擎队列在等投喂或封口：改 `submit.json` 的 `output`/`seal`
后 `aiwf submit`；或 `--item-file items.json`。只有一个队列时 `--node` 可省。
汇总 foreach 时读取结果清单并检查失败项，不数目录。

## 旧命令（保留兼容，已隐藏）

`complete` / `provide` / `review` / `fail` 仍可用，但已弃用；文本模式会打印
`deprecated: use aiwf submit`。新代码不要再用它们。`current` 保留为机器可读的
任务包（SDK/旧脚本），`aiwf status --json` 是它的超集。

## Windows / PowerShell

不要用 `--output '{"k":...}'`，shell 会剥掉双引号；统一用 `--file` / `--json-file` /
`--verdict` / `--set`。`submit.json` 用 UTF-8 读，带 BOM 也可以。

## 常见误用

| 现象 | 处理 |
|---|---|
| action stale（退出码 4） | `aiwf status` 后 `aiwf submit --print`，确认产物仍适用再交 |
| 文件输出被驳回（退出码 3） | 按 `rejected.expected/got` 修文件，重跑 `aiwf submit` |
| check 占位符没改 | verdict 只能是 PASS/REVISE，reason 必须非空 |
| review 占位符没改 | verdict 只能是 APPROVE/REJECT，结论必须来自用户 |
| 多个 run 并行 | `aiwf runs list --status running`，显式给 `run_id` 或唯一前缀 |
