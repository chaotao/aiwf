---
name: aiwf
description: 创建、运行与审计 aiwf 工作流。用户要求创建或修改工作流、执行或续跑流程、查看运行记录时使用；一次性短任务无需固化为工作流。
---

# aiwf 工作流

先判断流程是否有恢复、复用、批量或验收价值；一次性短任务直接完成。
引擎是纯状态机，不调用 LLM 或业务工具。由你获取任务包、执行任务、报告结果，
引擎负责推进、校验与文件审计。

## 加载顺序

只读当前意图对应的指南。创建时先读入门，再从范例目录选最接近的一份；
遇到字段或能力问题才补读专题。执行时优先读执行指南，不预读全部创作指南。
修改本 Skill 时更新仓库的 `skills/aiwf` 源目录，再同步项目安装副本。

### 入门与日常使用

| 意图 | 读取 |
|---|---|
| 想找现成流程；不确定要不要自己写 | [模板库](references/templates.md) — 先 `aiwf wf list` 查有没有可装的 |
| 新建、修改定义；第一次使用 | [创建入门](references/create-workflow.md) |
| 找完整范例；小说、短剧、游戏、修代码 | [范例目录](references/examples.md) — 演示件，选中后再读取或复制 assets 中的 JSON；可直接安装的通用模板见模板库 |
| 启动、执行、接管、临时失败续跑 | [执行指南](references/run-workflow.md) |
| 查看进度、记录、产物、耗时、失败原因 | [审计与检索](references/inspect-runs.md) |
| 想知道「做到哪一步、还剩什么」而不是「哪些节点跑完了」 | [业务进展视图](references/view.md) — 定义里声明 `view` 段，`runs show` 按业务阶段渲染并给出下一步命令 |

### 常用专题

| 疑问 / 能力 | 读取 |
|---|---|
| inputs 能放什么、ref 引用范围、变量与字段命名 | [字段与数据作用域](references/data-reference.md) |
| 单文件、多文件、结构化结果、修代码节点 output 怎么写 | [产物设计](references/outputs.md) |
| 运行前入参、途中提问、Agent 质检、人工验收 | [输入与验收](references/interaction.md) |
| 批量章节 / 资源、上一项结果、复用子工作流 | [批量与子流程](references/patterns.md) |
| 条件判断、Agent 分类、多出口 | [分支与路由](references/routing.md) |

### 高阶玩法（有需要再读）

| 需求 | 读取 |
|---|---|
| 数量事先未知、运行中追加章节 / 条目 | [运行时队列](references/advanced-dynamic.md) — `items: {"queue": true}`，用 `aiwf items add/seal` 投喂 |
| 改已完成任务、依赖级联、局部重做、修改历史输入 | [修订与重做](references/advanced-revisions.md) |
| SDK 驱动、显式 edges、模型提示、旧 1.0 迁移 | [集成与兼容](references/advanced-integration.md) |

## 始终遵守

- 新定义用 `schema_version: "1.1"`；从顺序 task 开始，末任务自然结束。
- `inputs` 是材料；顶层 `input_schema` 校验启动入参；task `user_input_schema` 收集用户信息。
- `{"ref":"key"}` 保留数据类型，`${key}` 仅文字插值；大产物落文件，结果回 `$file`。
- 单节点输出 ≤2000 字符；优先把运行产物放 `${run.dir}`，代码修改仍落实际项目文件。
- 使用根 run_id 和最新 `action.id` 驱动；依据 `action.kind` 选择命令，不把 phase 当命令。
- `check` 由 Agent 实际检查；`review` 的结论来自用户，不能自行代批。
- 当前节点的缺陷走 `check` / `fail` / `revise`，不许绕过质检。
- `.aiwf/runs/` 保留并纳入版本管理；交接先看 `context.md`，再 `current --json`。

命令优先用本仓库启动入口：POSIX 用 `engine/bin/aiwf`，Windows 用
`engine/bin/aiwf.cmd`；其次 PATH 的 `aiwf`，或在仓库中
`PYTHONPATH=engine python -m aiwf`。下文统一写 `aiwf`；状态变更命令加 `--json`。
