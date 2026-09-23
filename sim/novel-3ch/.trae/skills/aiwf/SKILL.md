---
name: aiwf
description: Create, run, and inspect aiwf workflows in the current project. Use when the user asks to 创建/新建/定义/修改 工作流，运行/执行/跑/续跑 工作流，或查看运行状态、检索历史执行记录、审计回溯. Covers workflow authoring, agent-driven execution with human review, and run-record inspection.
---

# aiwf 工作流

aiwf 是 schema 驱动的 AI 工作流引擎：工作流是一份 JSON 定义，描述一串节点
（`start / task / branch / review / end`），用于把可复用流程固化下来。

**核心架构：引擎是纯状态机**——不调用 LLM、不执行工具。由你（AI agent）
驱动一次运行，并在需要时把人接入：向引擎获取当前状态 → 自己执行，或向用户
收集信息/请人验收 → 上报，引擎负责审计、上下文更新与节点推进。

运行中 `status=running` 时有四种 phase：

- `executing`：正常，按节点 spec 执行；
- `awaiting_input`：任务需要用户先提供信息，你提问后用 provide 回报；
- `awaiting_review`：等待人工验收结论，用 review 回报；
- `awaiting_items`：仅动态 foreach——当前条目跑完、源里还没有新条目，外部补充后 resume。

两条跨场景约定：**大产物（正文/剧本等）写文件、只回引用** `{"$file": "路径"}`
（单节点输出 ≤2000 字符，引擎强校验）；每次运行独立成文件夹，内含自动生成的
`context.md`，是人和 agent 共享进度/产物索引的入口。

## 命令约定

按优先级：仓库中的 `engine/bin/aiwf` → PATH 上的 `aiwf`（pip 安装）→
`PYTHONPATH=engine python3 -m aiwf`，三者等价。状态变更命令建议加 `--json`。

## 按场景加载详细指南（渐进式披露）

先判断用户意图，再读取对应参考文件，不要一次性加载全部：

| 用户意图 | 加载 |
|---------|------|
| 创建 / 新建 / 定义 / 修改工作流，把流程固化为可复用工作流 | [references/create-workflow.md](references/create-workflow.md) |
| 运行 / 执行 / 跑工作流，收集用户输入、人工验收、计划外回跳修订、续跑 | [references/run-workflow.md](references/run-workflow.md) |
| 查看运行状态、列出历史、检索记录、审计、回溯某次运行 | [references/inspect-runs.md](references/inspect-runs.md) |

多个意图时按需依次加载。最小命令速查：

```bash
aiwf init                              # 初始化 .aiwf 存储
aiwf validate <file>                   # 校验工作流定义
aiwf workflows                         # 列出已有工作流
aiwf run <wf_id> --input-file f.json   # 启动运行，返回当前状态
aiwf current <run_id>                  # 重新查看当前状态（任意 phase）
aiwf complete <run_id> --output "..."  # 上报任务/分支结果（--output-file / --empty）
aiwf provide <run_id> -p k=v           # 回报用户提供的信息（--node 改已完成节点）
aiwf review <run_id> --verdict APPROVE # 人工验收结论（--feedback）
aiwf revise <run_id> --target n        # 计划外回跳（--mode cascade|return）
aiwf fail <run_id> --error "..."       # 上报失败
aiwf resume <wf_id> <run_id>           # 续跑失败运行
aiwf runs list [wf_id]                 # 列出运行
aiwf runs show <run_id>                # 查看运行（--node / --json）
aiwf runs events <run_id>              # 查看事件流（纯文本，不支持 --json）
aiwf runs search <query>               # 检索历史
aiwf skills install                    # 安装本 skill 到项目 .trae/skills
```

> `--json`：状态变更命令建议加（信封还带 `error` 字段，失败时直接含原因）；
> `runs show/search` 支持，`runs list/events` 不支持。
