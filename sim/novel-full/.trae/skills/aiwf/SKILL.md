---
name: aiwf
description: 创建、运行与审计 aiwf 工作流。用户要求创建或修改工作流、执行或续跑流程、查看运行记录时使用；一次性短任务无需固化为工作流。
---

# aiwf 工作流

先判断流程是否有恢复、复用、批量或验收价值；一次性短任务直接完成。
引擎是纯状态机，不调用 LLM 或业务工具。由你获取任务包、执行任务、报告结果，
引擎负责推进、校验与文件审计。

## 默认只读这一页 + RUN-CARD

执行已有流程：读 [RUN-CARD](RUN-CARD.md)，只记两条命令 `aiwf` 和 `aiwf submit`。
出错：读 [TROUBLESHOOT](TROUBLESHOOT.md) 的 `code` 行。
其余参考只在对应意图下再读，不要预读。

| 意图 | 读取 |
|---|---|
| 执行 / 接管 / 续跑 | [RUN-CARD](RUN-CARD.md) |
| 找现成流程 | [模板库](references/templates.md) — 先 `aiwf wf list` |
| 新建、修改定义；第一次使用 | [创建](references/create.md#创建入门) — 可先 `aiwf new <id> --kind sequential\|checked\|batch` |
| 找完整范例 | [范例目录](references/examples.md) — 选中后再读 assets 里的 JSON |
| 查看进度、记录、产物 | [审计](references/audit.md#审计--检索工作流运行记录) |
| 业务进展视图 / 标题 | [审计 § 业务进展视图](references/audit.md#业务进展视图view) |

### 常用专题

| 疑问 / 能力 | 读取 |
|---|---|
| inputs 能放什么、ref 引用范围、变量与字段命名 | [创建 § 字段与数据作用域](references/create.md#字段与数据作用域) |
| 单文件、多文件、结构化结果、修代码节点 output 怎么写 | [创建 § 产物设计](references/create.md#产物设计对象文件与代码修改) |
| 运行前入参、途中提问、Agent 质检、人工验收 | [执行 § 启动入参](references/run.md#启动入参途中提问质检与人工验收) |
| 批量章节 / 资源、上一项结果、复用子工作流 | [高阶 § 批量与子流程](references/advanced.md#批量任务与子工作流) |
| 条件判断、Agent 分类、多出口 | [创建 § 分支与路由](references/create.md#分支与路由) |

### 高阶玩法（出错或需要时再读）

| 需求 | 读取 |
|---|---|
| 数量事先未知、运行中追加条目 | [高阶 § 运行时队列](references/advanced.md#高阶运行中追加条目的引擎队列) |
| 改已完成任务、依赖级联、局部重做 | [高阶 § 修订与重做](references/advanced.md#高阶修订依赖级联与实例重做) |
| SDK 驱动、显式 edges、旧 1.0 迁移 | [高阶 § 集成与兼容](references/advanced.md#高阶sdk显式图与兼容) |

## 始终遵守

- 新定义用 `schema_version: "1.1"`；从顺序 task 开始，末任务自然结束。
- 执行循环只有两条命令：`aiwf`（读任务包）→ 做事 → `aiwf submit`（交结果）。
  **照 `next` 行做**，不要自己从 `action.kind` 拼命令。
- 交文件用 `--file`（模板已预填时直接 `aiwf submit`），check 用
  `--verdict PASS|REVISE --reason "..."`，review 用
  `--verdict APPROVE|REJECT --feedback "..."`，结构化结果用 `--json-file`。
- Windows 上不要用内联 `--output '{"…"}'`；shell 会剥掉双引号。
- 退出码：0 已前进；2 用法/入参错；3 产物被驳回（状态未变）；4 action 过期。
  非 0 时先读 `error.code` / `hint` / `next`，不要重试同一条命令。
- check 由 Agent 实际检查；review 的结论来自用户，不能自行代批。
- 当前节点的缺陷走 `check` / `aiwf submit --fail` / `revise`，不许绕过质检。
- 大产物写文件、结果回 `$file`；单节点输出 ≤2000 字符。

命令入口：项目内 `./bin/aiwf`（Windows `bin\aiwf.cmd`，`aiwf init` 生成，固定
解释器与 engine 路径）→ PATH 的 `aiwf` → 本仓库 `engine/bin/aiwf`（Windows
`engine/bin/aiwf.cmd`）→ `PYTHONPATH=engine python -m aiwf`。拿不准先跑 `aiwf doctor`；
`aiwf --help-all` 列出隐藏命令：`current`（已隐藏，兼容旧脚本；`status --json` 是其超集）、
`items`（队列排查/精细编辑）。`complete/provide/review/fail/redo` 已移除，
调用退 2 并指向 `submit` / `revise`。
