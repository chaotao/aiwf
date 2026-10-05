# godot_agent → aiwf：资产分类与替代边界

日期：2026-10-04
状态：**勘察完成，待拍板**。本轮零代码改动。
前置文档：`workflow_template_library_20261004.md`（模板库机制）、`template_generality_and_professionalism_20261004.md`（通用性/专业性）、`game_dev_template_proposal_20261004.md`（游戏模板提案）
试验田：`F:/data/project/github/godot_agent`

---

## 0. 结论

**aiwf 应当替代的是 godot_agent 的「编排与状态」，不是它的「知识与文档」。**

实测下来有一条极干净的对应关系，它直接决定替代边界：

> godot_agent 的任务状态机有 9 个态，其中 **6 个是运行态**（进行中 / 待验收 / 已完成 / 验收未通过 / 阻塞 + 未启动），**3 个是规划态**（已拆分 / 已取消 / 计划收缩）。
> **aiwf 原生覆盖全部 6 个运行态（含 `awaiting_review` 人工门禁与 `revise` 回退），对 3 个规划态零覆盖。**

所以边界是现成的：**运行态归 aiwf，规划态留文档。** 不需要新造机制，也不需要改造 aiwf 的引擎。

（下表为逐态实证，证据在 §3。）

---

## 1. 实测盘点

### 1.1 规模

| 项 | 数量 | 备注 |
|---|---|---|
| 技能 | **11 个** `g-*` | 30 份 md，共约 180 KB |
| 脚本 | 4 个可执行 | `manage_project.py` / `manage_cc0_assets.py` / `manage_ui_assets.py` / `sync_skills.py` |
| 游戏项目 | **3 个** | `mc-plus` / `digital-citizen` / `project-01-action-roguelite` |
| 计划 | **15 个** | mc-plus **12**、roguelite 2、digital-citizen 1 |
| TASK | **106 个** | mc-plus **93**、roguelite 9、digital-citizen 4 |
| 脚手架 | `scaffolds/godot/` | 含 godot-ai 插件全量客户端（20+ 个 agent 适配） |

`mc-plus` 一个游戏就有 12 计划 / 93 任务。**这个量级是判断的第一手依据**——它说明现状的"任务状态靠 `index.md` 手工维护"已经承担了相当大的负荷。

### 1.2 当前仓库把三类东西混在一起

```
godot_agent/
├── AGENTS.md                # 方法总纲 + 技能路由表（唯一权威）
├── skills/                  # ① 知识：11 个 g-*
├── scripts/                 # ③ 机械执行
├── scaffolds/godot/         # ④ 产物结构
├── cc0-assets/              # ⑤ 共享素材库（跨项目）
├── ideas/                   # 跨项目点子库
└── project/
    ├── .active-project      # ② 编排状态：当前项目/阶段/计划
    ├── mc-plus/             # ⑥ 游戏产物（12 计划 93 任务）
    ├── digital-citizen/
    └── project-01-action-roguelite/
```

**"一项目 = 一个独立游戏"的本质，是把 ⑥ 拆出去。** 拆出去之后，② 必须有个新家——**那个新家就是 aiwf**；① 要继续共享，但不能复制进每个游戏仓库（那正是 aiwf 模板库要消灭的漂移）。

---

## 2. 判断轴

用主人自己在 `AGENTS.md:30` 定的口径，加一条 aiwf 的适用判据：

| 桶 | 判据 | 归属 |
|---|---|---|
| **知识** | 是判据、规范、经验，agent 读了才知道怎么做；无状态 | **Skill**（保留） |
| **编排与状态** | 有跨步骤的状态、有人工 checkpoint、需要可恢复/可审计 | **aiwf workflow**（替代） |
| **机械执行** | 稳定、确定、可独立运行、无判断 | **Script**（保留） |
| **结构** | 项目骨架、可复制的初始形态 | **Scaffold / aiwf 库模板** |

主人既有偏好一致：「简单任务倾向 skill / script 解决，workflow 留给需要 state persistence 与人工 checkpoint 的长周期任务」。

---

## 3. 结构性发现：运行态 6/9 已被 aiwf 原生覆盖

这是本方案成立的核心证据。逐态实测：

| godot_agent 状态 | 归属层 | aiwf 对应 | 证据 |
|---|---|---|---|
| 待开始 | 规划态 | —（run 未启动） | `g-do`："`待开始` 由 `g-plan` 创建任务时写入" |
| **进行中** | 运行态 | `running` + `phase=executing` | `engine.py:889` |
| **待验收** | 运行态 | **`running` + `phase=awaiting_review`** | `engine.py:1810`；`engine.py:10` 源码注释原文 *"awaiting_review -> acceptance requirements for a human"* |
| **已完成** | 运行态 | `completed` | `engine.py:1081` |
| **验收未通过 → 进行中** | 运行态 | `aiwf review <run> --verdict REJECT` + `aiwf revise <run> --target <node>` | CLI 命令面实测 |
| **阻塞** | 运行态 | `failed` / `aiwf fail` | `engine.py:1003` |
| 已拆分 | 规划态 | — | `g-plan`："仅 `g-plan`／`g-clarify` 设置" |
| 已取消 | 规划态 | — | 同上 |
| 计划已完成 | 规划态 | — | `g-plan`「计划收口」 |

**结论**：aiwf 覆盖的恰好是"每次实施都会发生"的那 6 个态，缺的恰好是"规划阶段才会发生"的那 3 个。这不是巧合——**aiwf 天生是运行器，plan 文档天生是规划器**。硬把规划态塞进 aiwf 需要给引擎加"计划容器"概念，而按主人的"不提前造机制"原则，那应当等真的疼了再说。

另外两条支撑事实：

- aiwf 的 review 门是**原生的**，且有路由：`review_config` 支持 `requirements`（渲染进 spec 给审阅人）+ `routes`（`APPROVE`/`REJECT` → 各指向一个节点 id）。
- aiwf 的 `task` 节点还有 `check`（agent 自检，`criteria` + `max_attempts` 有界重试）+ `consumes`（显式数据依赖，支撑级联返工）。**`check` 与 `review` 并存，正好对应 `g-do` 那句"Agent 自检通过不等于用户验收通过"。**

---

## 4. 逐技能处置表

11 个技能。**替代 = 该技能中存在应转入 aiwf 的成分**，不等于整技能废弃。

| 技能 | 本质 | 该技能内的三桶拆分 | 处置 |
|---|---|---|---|
| **g-do** | 任务实施入口（14.8 KB，最大） | ② 任务状态机 9 态 + 实施流程 8 步 + 验收 gate + 收尾提交<br>① 就绪检查 5 类分流判据、默认读取硬上限、文档写入范围、功能影响写法<br>③ — | **② 大部分 → aiwf**<br>① → 保留 skill（瘦身） |
| **g-plan** | 计划拆解与进度 | ② 任务状态（待开始/已拆分/已取消）、依赖图、进度分母<br>① 力度判断、纵切片拆法、编号稳定、执行期修复判据 | **② 规划态 → 留文档；但其"任务状态"应由 run 状态派生**（见 §8.1）<br>① 保留 skill |
| **g-project** | 多项目/阶段切换 | ② `.active-project`、`.project-focus`、`switch`/`focus`/`current`/`list`<br>④ `create`/`ensure --godot` 脚手架复制 | **② 在"一项目=一游戏"下整体消失 → 废弃**<br>③④ 脚手架部分保留 |
| **g-brainstorm** | 仓库级 `ideas/` 点子库 | ① 脑暴方法与成熟度门槛<br>② 点子状态（孵化中/已转化/已采纳/搁置） | **不被 aiwf 替代**；但需**迁出游戏仓库**（跨项目性质不变） |
| **g-design** | 策划文档写作 | ① 全部（玩家规则、循环、范围） | **保留 skill**（零状态） |
| **g-spec** | 系统核心设计 | ① 全部（选题判据 + 写法） | **保留 skill**（零状态） |
| **g-clarify** | 跨层文档写回 | ① 事实归属表、上游→下游顺序<br>② 多文档写回本身 | **保留 skill**（判据价值 > 编排价值；体量不足以支撑引入 workflow） |
| **g-inbox** | 想法收集与消化 | ① 内聚原则、`为何停放` 判据<br>③ 编号分配、索引更新、归档（机械） | **→ skill 瘦身 + script**；不被 aiwf 替代（它是跨计划备忘池，不是执行流程） |
| **g-ui** | UI 视觉生产（第二大） | ② **10 步生产流水线 + 人工评审门 + 资产生命周期**（`pending-generation→generated→approved→rejected/stale`）<br>① 生成规范（无文字/透明底/nearest/lanczos/margins 人工确认）、契约字段 | **② → aiwf（subflow + review 的强候选）**<br>① 保留 skill（含 3 份 references） |
| **g-dev** | Godot 工程规范 | ① 全部（8 份 references，约 46 KB） | **保留 skill，100% 不动**（纯知识库，零状态） |
| **g-cc0** | CC0 素材库 | ① 许可核验判据、包说明撰写<br>③ 下载/解压/复制（已是脚本） | **保留 skill + script**；共享库定位需重议（见 §7） |

### 汇总

- **应被 aiwf 替代**：`g-do` 的状态机与流程骨架、`g-ui` 的生产流水线与评审门、`g-project` 的切换/焦点机制（直接消亡）。
- **保留为 skill**：`g-dev`（全）、`g-design`、`g-spec`（全）、`g-clarify`、`g-brainstorm`、`g-plan` 的方法论部分。
- **保留为 script**：`manage_cc0_assets.py`、`manage_ui_assets.py`、`sync_skills.py`；`manage_project.py` 仅留脚手架部分。
- **需要新落点（不属 aiwf）**：`ideas/`、`cc0-assets/`（都是跨项目共享，天然不该在单个游戏仓库里）。

---

## 5. 被 aiwf 替代的具体清单

### P0 — 运行态与门禁（改动面最小，收益最大）

| # | 替代物 | 从哪来 | aiwf 承载方式 |
|---|---|---|---|
| 1 | **TASK 的运行态** | `g-do`「状态流转」9 态中的 6 个运行态 | run 的 `status`/`phase`（`awaiting_review` 原生） |
| 2 | **人工验收门** | `g-do`「用户验收」全节 | `task.review`（`requirements` 渲染进 spec + `routes` 分流 APPROVE/REJECT） |
| 3 | **验收未通过 → 回退修原任务** | `g-do`「修复」1–5 步 | `aiwf review --verdict REJECT` → `aiwf revise --target <node>` |
| 4 | **阻塞态** | `g-do`「失败与阻塞」 | `failed` / `aiwf fail` / `aiwf resume` |
| 5 | **agent 自检（有界）** | `g-do`「按风险自检」 | `task.check`（`criteria` + `max_attempts`） |
| 6 | **执行期可恢复** | `g-do` 隐式（靠人重读文档） | run 目录 + journal + 1.1 定义快照 |

### P1 — 流程骨架

| # | 替代物 | 说明 |
|---|---|---|
| 7 | **TASK 实施流程 8 步** | `g-do`「执行流程」→ 一条 workflow 定义（节点序列固定，prompt 保留判据） |
| 8 | **验收后整目录提交** | `g-do` 固定收尾 → workflow 末节点（确定性步骤，可用 script 节点化） |
| 9 | **UI 生产流水线 10 步** | `g-ui`「生产流程」→ `subflow` + 内嵌 `review` 门（基准页评审 / 组件评审） |
| 10 | **UI 资产生命周期** | `g-ui`「状态」→ run 内实例状态 + `redo`（重做单个实例，对应资产 `stale` 重生成） |

### P2 — 结构性收口

| # | 替代物 | 说明 |
|---|---|---|
| 11 | **`.active-project` 焦点机制** | 一项目=一游戏后 **直接废弃**，项目根即身份 |
| 12 | **多项目 switch/focus/list** | 同上，由目录结构替代 |
| 13 | **TASK 编号与依赖** | `plan/tasks/TASK-NNN.md` 的编号稳定 + 依赖 → 若引入"计划容器"则由引擎管；否则保留文档 |
| 14 | **跨技能路由表** | `AGENTS.md:38-50` 的 11 行路由 → 部分变为 workflow 定义（哪条链），部分保留为 skill 索引 |

---

## 6. 明确不被 aiwf 替代的

这一半同样重要——**越界会把 aiwf 变成万能容器，反而失去价值**。

| 资产 | 为什么不该进 aiwf |
|---|---|
| `g-dev` 全部 8 份 references | **纯知识**。aiwf 的 task 节点只能把 prompt 交给 agent，知识本体就在 agent 侧。塞进 workflow = 把知识库拆碎进流程，没法被"按需加载"（`g-dev` 的核心纪律就是"默认只读本文件，最多再开 1–2 份"，aiwf 表达不了这种渐进加载） |
| `g-design` / `g-spec` 文档写作 | **文档方法论**。aiwf 不会写文档，只编排 prompt。文档路径与结构属项目约定，不是流程 |
| `g-clarify` 的事实归属表 | **判据**。决定"这次变更动哪几层"需要理解语义，引擎无法表达 |
| `g-brainstorm` | **跨项目**，且是对话式孵化，无固定步骤可编排 |
| `g-inbox` 的消化判断 | 按去向分组 = 语义判断；机械部分（编号/索引/归档）应走 script |
| `g-cc0` 许可核验 | 需要读 Preview/许可文件后**撰写说明**，是判断 + 生产，不是编排 |
| `verification-harness.md` 全部 | **纯 Godot/MCP 操作经验**（合成输入、`push_input` 坐标陷阱、macOS App Nap 假通过…）。零状态，纯知识——**100% 保留** |
| `check.criteria` 的**内容** | 判据文本由 skill 提供，aiwf 只负责"有这个门、有界重试" |

---

## 7. 目标形态

```
# ① 每个游戏 = 一个独立 aiwf 项目
mc-plus/
├── .aiwf/
│   ├── workflows/      # 从 aiwf 模板库 install 的流程定义（std.godot-task / std.godot-ui…）
│   ├── config.json     # run_defaults：repo_dir / godot_bin / test_cmd / conventions_file…
│   └── runs/           # 每次执行的记录（对应 g-do 的每次任务实施）
├── project.godot
├── design/ spec/ plan/ inbox/     # 规划态文档（g-design/g-spec/g-plan 维护）
├── scenes/ scripts/ resources/ assets/ tests/
└── ui-workspace/

# ② 方法与知识留在独立源仓库（仍叫 godot_agent，但不再是 monorepo）
godot_agent/
├── skills/             # 11 个 g-* 保留（去掉 monorepo 管理成分）
├── scripts/            # manage_cc0_assets / manage_ui_assets / sync_skills / 脚手架部分
├── scaffolds/godot/
├── cc0-assets/         # 跨项目共享素材库
└── ideas/              # 跨项目点子库
   # 不再有 project/ ，不再有 .active-project

# ③ 通用流程 → aiwf 库模板
aiwf/library/workflows/
├── std.godot-task-deliver.json   # 从 g-do 抽出的编排（状态机 + 验收门 + 收尾提交）
└── std.godot-ui-assets.json      # 从 g-ui 抽出的流水线（含中间评审门）
```

**三层职责**：

- **aiwf 库模板** = 流程骨架（跨项目相同）
- **项目 `.aiwf/config.json`** = 项目固有值（`repo_dir`、`test_cmd`…，配一次）
- **skill** = 每一步的判据与知识（agent 按需加载，如 `g-dev` 的渐进加载图）

这三层的关系此前已实测打通（`run_defaults` 注入顶层 → `${}` 插值 → 子流程靠父级映射穿透）。

---

## 8. 缺口与风险（必须先说清）

### 8.1 最大缺口：aiwf 没有"计划容器"

`plan/<id>/index.md` 维护的是**跨多个 run 的规划态**（15 个计划 / 106 个 TASK 的任务清单、依赖、当前焦点、进度分母）。而 aiwf 的顶层键只有 `default_model / description / edges / exports / id / input_schema / max_steps / name / nodes / schema_version`——**没有 plan 这一层**。

三条出路：

| 出路 | 做法 | 代价 |
|---|---|---|
| **A（推荐，先这样）** | plan 文档保留，**任务状态改为从 run 汇总派生**（新增一条只读命令，扫 `.aiwf/runs/` 生成"哪些 TASK 处于哪个态"），`index.md` 只写规划态 | 需要一个派生视图工具（几十行）；`index.md` 手工状态与 run 状态可能短暂不一致 |
| B | 给引擎加"计划容器"概念（plan → tasks → runs 三级） | 改引擎核心数据结构，**属重大改动**，按"不提前造机制"暂不宜 |
| C | 不管，两套状态各维护各的 | 必然漂移，且 106 个 TASK 的量级下维护成本高——**这正是要解决的问题** |

推荐 A：它不动引擎，且方向正确（**运行态只有一个来源**）。

### 8.2 其它风险

1. **run 数量膨胀**：mc-plus 93 个 TASK 若各一 run，就是 93+ 个 run 目录。需要定归档/清理约定（此前沙箱那条"优先归档而非删除"的经验在这里适用）。
2. **Godot 跑不起来**：本会话扫盘未找到 Godot 可执行文件，无 Godot MCP，连接器市场也无。**这不阻塞 aiwf 的 gate 机制**（人工验收是引擎原生的），但会让 workflow 里的"自动验证"节点退化成"给出手动验证指令"。
3. **文档写回不在 aiwf 能力内**：aiwf 只编排 prompt，`design/`/`spec/` 由 agent 按 skill 写。所以"跨层写回"（`g-clarify`）仍必须靠 skill。
4. **skill 分发仍然必要**：即便流程进了 aiwf，`g-dev` 这类知识仍要靠 `sync_skills.py` 分发到 8 个 agent 目录。**aiwf 的 `wf install` 只装流程定义，不装知识**——这个边界要守住。
5. **`g-do` 瘦身的风险**：它当前 14.8 KB，装的判据密度很高。拆要走一部分，但不该伤到"就绪检查 5 类分流"——那是它最值钱的部分。

---

## 9. 待拍板（决策 10，当前只抛这一项）

**替代的边界划在哪。**

| 选项 | 内容 | 代价 |
|---|---|---|
| **A（推荐）** | **只替代「编排与状态」**：`g-do` 运行态 + 验收门 + 流程骨架、`g-ui` 生产流水线、`g-project` 焦点机制（消亡）。规划态留在 `plan/` 文档，但任务状态改为从 run 派生（§8.1 出路 A）。知识、文档写作、脚本、判据**全部保留** | 需一个"run → TASK 状态"派生视图工具；`index.md` 与 run 状态需约定权威顺序 |
| B | A + 把 plan/task 结构也纳入 aiwf（给引擎加计划容器） | **改引擎核心**；106 个 TASK 的历史数据要迁移；按"不提前造机制"暂不宜 |
| C | 更激进：整条开发链（g-design→g-spec→g-plan→g-do→g-ui）都编成 aiwf workflow | 把纯知识环节也塞进流程，与 §6 的"不该替代"清单冲突；`g-dev` 的渐进加载无法表达 |

推荐 **A**。判据是 §3 那条实测对应关系——**aiwf 覆盖的 6 个态恰好全是运行态，而它不覆盖的 3 个恰好全是规划态**。边界是引擎给的，不是我们划的。

A 定了之后再逐项走：**P0 的 6 项先做哪一项**（我建议从 #1+#2 起，即"一个 TASK = 一个 run + 一个 review 门"，因为它同时验证状态替代与门禁替代）、以及"run → TASK 状态"派生工具的形状。

---

## 附：本轮实证清单位置

- 技能与脚本体积：`F:/data/project/github/godot_agent/skills/`（30 份 md）
- 计划/任务量级：`mc-plus` 12 plans / 93 tasks；roguelite 2/9；digital-citizen 1/4
- 状态机权威原文：`skills/g-do/SKILL.md:91-105`
- 路由表权威原文：`AGENTS.md:38-50`
- aiwf 节点能力：`schemas/workflow.schema.json` 的 `$defs.node`（7 种类型 / 26 个键）
- aiwf review 门定义：`$defs.review_config`（`requirements` + `routes`）
- aiwf run 相位：`engine/aiwf/engine.py:1810`（`awaiting_review`）、`:10`（注释原文）
- aiwf CLI 命令面：`init/validate/workflows/run/current/complete/provide/review/revise/fail/resume/redo/runs/skills/wf`（无 cancel/plan）
