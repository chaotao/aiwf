# aiwf 执行卡（一页）

入口：项目内 `./bin/aiwf`（Windows `.\bin\aiwf.cmd`，由 `aiwf init` 生成）；
没有就用 PATH 的 `aiwf`；拿不准先跑 `aiwf doctor`。状态变更命令加 `--json`。

## 正常循环：只有两条命令

```bash
aiwf                 # 读任务书（等同 aiwf status）
# …执行 node.prompt：写文件、跑工具、问用户…
aiwf submit          # 交当前步骤的结果
```

`aiwf`（无参）或 `aiwf status [RUN]` 会给出：标题/状态、当前节点、材料、产物路径、
check/review 要求、`submit.json` 路径、`next` 命令。**照 `next` 做**，不要自己选动词。

`aiwf submit` 按当前阶段自动分派：

| 当前步骤 | 怎么交 |
|---|---|
| task（有 `output.file`） | 把文件写到 `artifact` 路径，直接 `aiwf submit`（模板已指向它） |
| task（只有 `output_schema`） | 改 `submit.json` 的 `output`，或 `--json-file result.json` |
| check | `aiwf submit --verdict PASS --reason "实测依据"`（或 REVISE） |
| review | `aiwf submit --verdict APPROVE --feedback "用户结论"`（或 `--approve` / `--reject`） |
| provide | `aiwf submit -p tone=冷峻`（或 `--data-file answers.json`） |
| 队列 awaiting_items | 改 `submit.json` 的 `output`（条目数组）/ `seal`，或 `--item-file items.json` |
| 失败 | `aiwf submit --fail "具体原因"` |

`submit.json` 里带 action 指纹和预填 payload；**只改 `output`，不要改 `action`/`kind`**。
旧模板重放会退 4，此时 `aiwf status` 后 `aiwf submit --print` 重建。

**旧 run 没有 `submit.json`**（L2 之前启动的）：`aiwf status` 会标 `(missing)`；
首次 `aiwf submit` 会自动补建——单个文件任务且文件已存在时会直接完成，否则按提示
编辑/加 flag 后重跑。也可以显式 `aiwf status --ensure` 补建。

## 启动与准备

```bash
aiwf init                                  # .aiwf + ./bin/aiwf
aiwf new my-flow --kind checked            # 生成可运行骨架：sequential|checked|batch
aiwf wf list                               # 有没有现成模板
aiwf wf install std.review-loop            # 装模板（可选）
aiwf validate --project                    # 装/改定义后必跑
aiwf run <wf> --title "这次做什么" -p idea="…" --json
```

## 逃生舱（不在正常循环里）

```bash
aiwf resume <run>                          # 失败后重开
aiwf revise <run> --target <node> --reason "…"   # 计划外回跳/重做
aiwf runs list --status running             # 有哪些在跑
aiwf runs show <run>                        # 审计一次运行
aiwf status --json                          # 机器可读任务包
aiwf submit --print                         # 重建 submit.json
```

## 禁止

- 手拼 `--output '{"…"}'`（PowerShell 会剥引号）；结构化结果用 `--json-file`。
- 跳过 check 直接报 PASS；替用户做 review 结论。
- 把正文/长日志粘进 `output`；大产物写文件，用 `$file`。
- 在多个 run 并行时省略 `run_id`（CLI 会报错并列出候选）。
- 读 `engine/` 源码猜行为；先看 `aiwf --help-all` 或 `TROUBLESHOOT.md`。
