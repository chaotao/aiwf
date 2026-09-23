# AI 工作流引擎实施计划（已交付）

## 1. 需求概述
基于 Python 的轻量级 AI 工作流引擎：JSON Schema 驱动、JSON 文件存储（无数据库）、
执行全程可审计；交付形态为 **CLI + SDK + agent skills**，skills 可安装到具体项目。
工作流由 agent 通过 skill 生成。

**核心架构：引擎是纯状态机**。引擎不调用 LLM、不执行工具；由外部 AI agent
驱动，并在需要时把人接入：agent 向引擎获取当前状态 → 自己执行，或向用户收集
信息 / 请人验收 / 计划外回跳 → 上报，引擎负责审计、上下文更新与节点推进。

## 2. 已交付结构

```
aiwf/
├── pyproject.toml                     # 可选 pip 打包配置（package 位于 engine/）
├── engine/
│   ├── bin/aiwf                       # 可执行入口（自动设置 PYTHONPATH）
│   └── aiwf/                          # Python 包
│       ├── schema.py                  # 结构校验（jsonschema，惰性导入）+ 语义检查
│       ├── storage.py                 # .aiwf 目录；每次运行一个文件夹（record.json + context.md）
│       ├── engine.py                  # 状态机：依赖图、三阶段、provide/review/revise、输出长度校验
│       └── cli.py                     # CLI 命令
├── schemas/                           # 核心 JSON Schema（仓库根）
│   ├── workflow.schema.json           # 工作流定义契约（1.0）
│   └── run_record.schema.json         # 运行审计记录契约（1.0）
├── skills/                            # 单个 agent skill（渐进式披露）
│   └── aiwf/
│       ├── SKILL.md                   # 精简入口：概述 + 命令约定 + 场景路由
│       └── references/                # 详细指南，agent 按需加载
│           ├── create-workflow.md
│           ├── run-workflow.md
│           └── inspect-runs.md
├── examples/
│   ├── short-drama.json               # 示例：自动质检返工的短剧流程
│   └── novel-demo/                    # 示例项目：灵感收集/人工验收/回跳修订
└── tests/test_engine.py               # 16 个单测（模拟 agent + 人，无网络）
```

路径解析：代码默认按自身位置定位仓库根的 `schemas/`、`skills/`，
也可用 `AIWF_SCHEMAS_DIR` / `AIWF_SKILLS_DIR` 覆盖。

## 3. 核心设计

### 节点类型（5 种）

| type | 执行/决策者 | 语义 |
|------|------------|------|
| `start` / `end` | — | 入口 / 结束；无输出 |
| `task` | agent | 一个工作步骤；工具调用由 agent 自行组织，输出写入 context（output_key 默认=id） |
| `branch` | LLM 自动 | 回答标签匹配 routes，不停顿；用于设计内自动返工循环 |
| `review` | 人 | 纯人工检查点；不产出，流程暂停等验收结论 |

`edges` 省略时节点按数组顺序串联；branch/review 不连 edges，路由由 routes/default
决定。字符串模板 `${var}` 引用上下文，变量缺失运行时报错。

### 产物外置：文件引用 + 长度校验

- **记录不塞完整产出**：正文/剧本/长报告等由执行 agent 写入文件，context 与
  node_run 中只保存引用 `{"$file": "路径"}`。
- 引擎对单个节点输出有 **2000 字符**（序列化后）硬上限，超限 `complete` 报错、
  不保存，提示改用引用——防止记录膨胀的最后约束。
- 引用在模板中渲染为**路径**，需要正文的节点（质检/续写/汇总）在 prompt 中要求
  先用读文件工具读取；引擎不读文件，仍是纯状态机。

### 运行目录与共享上下文（context.md）

- 每次运行独立成目录：`runs/<workflow_id>/<run_id>/`，含 `record.json` 与
  `context.md`；同一工作流多次执行互不覆盖。
- `context.md` 由引擎在每次落盘时确定性重写（无 LLM）：状态/当前节点、输入、
  内联上下文、全部 `$file` 产物索引及来源节点，供人和 agent 共享与交接。

### 运行三阶段（status=running 内的 phase）

- `executing`：正常执行节点 spec；
- `awaiting_input`：task 的节点级 `input_schema` 有 required 字段缺失，暂停并下发
  问题清单；agent 提问后 `provide` 回报，校验写入 context；
- `awaiting_review`：task 完成后命中内嵌 `review`，或流程走到独立 review 节点，
  暂停；人给结论后 `review --verdict APPROVE|REJECT` 回报。

task 内嵌 `review`：`requirements`（字符串/检查项，支持模板）+ 可选 `routes`，
缺省 APPROVE 沿 edges、REJECT 重做本节点。节点输入与验收均可在 run 启动输入中预填。

### 计划外回跳修订（revise）

执行到中途想改某个**已完成 task**，无需修改工作流定义：

- 引擎静态扫描所有节点的 `${var}` 引用构建**数据依赖图**；
- `--scope auto`（默认）：从变更键沿依赖图求传递闭包，闭包内已执行节点标 stale
  重做，其余下游节点自动复用（审计记 skipped）；
- `--scope all` / `manual`：全部下游 / 指定节点重做；
- `--mode cascade`（默认）：沿 edges 重做 stale、跳过复用；`--mode return`：只改
  target，完成后跳回发起节点；
- 修改已完成 task 的节点输入（provide --node）自动开启 cascade 修订。

### 审计记录（两层）

- **events**：不可变事件流——input_provided/input_changed、review_requested/
  decided、revise_requested、node_reused、revision_completed、运行生命周期；
- **node_runs**：每节点时间戳/duration_ms、渲染后 input、output（短值或 `$file`
  引用，≤2000 字符）、≤160 字摘要、agent 自报 model/tokens、branch decision、
  feedback、revision_seq；skipped 节点复用旧输出。context 始终为最新值，演变
  历史只在 events；产物位置另见同目录 context.md 索引。

### CLI 全集
`init / validate / workflows / run / current / complete / provide / review /
revise / fail / resume / runs list|show|events|search / skills list|install`

## 4. 验证结果
- 单测 16/16 通过：线性审计、branch 返工、节点输入收集、内嵌验收打回/通过、
  独立验收点、revise 级联（stale 重做/reused 跳过）、return 模式、
  闭包含验收点时正确关闭、改已完成节点输入、fail+resume、启动输入校验，以及
  超长输出被拒且运行不中断、引用渲染为路径、context.md 产物索引。
- 两个示例定义 validate 通过；skill 正确安装到项目 `.trae/skills/`（含 references）。
- novel-demo：在临时副本中由桩 agent 完整驱动 15 节点真实定义——awaiting_input
  灵感收集、三章写文件回引用、qc 引用路径、人工/卷末验收、assemble 汇总，最终
  completed；record.json/context.md 位于 per-run 目录，无超长内联值。验证副本
  已清理，真实 demo 与已外置的 novel/*.md 保留。

## 5. 演进过程
1. 初稿：引擎自己执行 LLM/工具 → 反转为纯状态机 + agent 驱动；
2. 删除 tool 节点（工具是 agent 自身能力）、llm 更名 task、删除 system；
   新增节点级 input_schema（awaiting_input）、内嵌 review 与独立 review
   （awaiting_review）、基于数据依赖图的 revise 级联/return 修订与 events 审计。
3. 本轮：大产物外置为 `$file` 引用 + 2000 字符输出校验；运行存储由
   `runs/<wf>/<run_id>.json` 改为 `runs/<wf>/<run_id>/{record.json,context.md}`；
   引擎每次落盘确定性生成 context.md 共享上下文。
