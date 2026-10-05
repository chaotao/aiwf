# 游戏开发工作流模板：设计提案 v1

日期：2026-10-04
状态：**提案**。模板与代码均未写；当前只抛出一项待拍板（决策 9：第一个模板固定哪条流程）。
关联文档：`workflow_template_library_20261004.md`（模板库机制，已落地）、`template_generality_and_professionalism_20261004.md`（通用性/专业性，决策 6/7/8 已落地）。

---

## 1. 试验田勘察（全部为读码/实测事实，附证据）

### 1.1 `godot_agent` 仓库现状

试验田 = `F:/data/project/github/godot_agent`（下称本仓库）。

```text
AGENTS.md            # 权威约定（技能路由 / 事实唯一来源 / Godot 实施约束 / 变更范围）
skills/              # 11 个 g-* 技能：project brainstorm design spec plan do dev ui clarify inbox cc0
scripts/             # manage_project.py / manage_ui_assets.py / manage_cc0_assets.py / sync_skills.py
scaffolds/godot/     # 新项目脚手架
cc0-assets/          # 已核验 CC0 素材包（供占位）
ideas/               # 仓库级点子库（当前为空）
project/<game-id>/   # 3 个游戏：digital-citizen、mc-plus、project-01-action-roguelite
```

**事实唯一来源表**（`AGENTS.md:95-108`，原文列出）：`.active-project`（当前项目/阶段/计划）、
`.project-focus`（各项目上次焦点）、`design/index.md`、`spec/<scope>-spec.md`、`plan/<id>/index.md`、
`plan/<id>/context.md`（代码基线）、`tasks/TASK-NNN.md`、`inbox/inbox.md`。

**当前真实状态**（实测）：
- `project/.active-project` = `project: mc-plus` / `stage: plan` / `plan: machine-core`
- `plan/machine-core/`：`index.md` + `context.md` + `tasks/TASK-001..007.md`
- 进度 3/7 已验收；**TASK-004 待验收**（"代码完成，274 测试全绿；手动实机验收"）；TASK-005 待开始
- `mc-plus/tests/` 下 40+ 个 `.gd` 测试（含 `headless_*.gd`）、`fixtures/`、`_headless_suite_runner.gd`

**`g-do` 的执行纪律**（本仓库流程的权威定义，`skills/g-do/SKILL.md`）：
- 状态机（原文标注"唯一权威"）：`待开始 → 进行中 → 待验收 → 已完成`；`验收未通过 → 进行中`；
  `待开始/进行中 → 阻塞 → 进行中`；`已拆分`/`已取消` 仅 `g-plan`/`g-clarify` 可设。
- 默认读取有**硬上限**：只读 `.active-project`、`index.md` 当前焦点、当前 `TASK-NNN.md`、`context.md` 基线。
- 实施前有"任务就绪检查"，未就绪须分流并暂停，**不提前标 `进行中`**。
- 验证纪律：纯逻辑补 `tests/`；**交互与画面必须运行游戏**；读 editor / game 日志；不得靠静态判断完成。
- 验收：**"Agent 自检通过不等于用户验收通过"**；用户明确通过后标 `已完成`，
  并对 `project/<game-id>/` **整个目录**做一次 `git add` + commit（含文档，不拆多笔，不 push）。

### 1.2 能力约束（本节决定闭环的形状）

| 检查项 | 实测结果 |
|---|---|
| Godot 是否在 PATH | **否**（`which godot` 无输出） |
| 扫描盘根与常见目录找 Godot 可执行 | **未找到**。扫过 `C/D/E/F/G:/` 根目录、`C:/Program Files`、`%LOCALAPPDATA%/Programs`、`F:/SteamLibrary/steamapps/common`、`F:/tools`、`F:/temp`、`F:/switch`、`F:/FeverApps`、`F:/pcgames`、Downloads、Desktop |
| 本会话的 MCP 配置 | `~/.workbuddy/mcp.json` **只有 blender**，无 Godot MCP |
| mc-plus 侧线索 | 有 `addons/godot_ai`（编辑器插件）与 `.godot/`（说明曾在编辑器中打开过） |
| 现有 headless 跑法 | `tests/_headless_suite_runner.gd` 头部自述：*"Temporary scaffold … Delete once the MCP editor session is available again"*，且注释 *"Needs a live editor viewport; passes under the MCP editor runner"* |
| 连接器市场搜 Godot | **无 Godot 相关连接器** |

→ **硬结论：在当前会话里，"运行 Godot 做验证"这一步执行不了。** 它必须要么交给人，要么先补齐运行能力。

### 1.3 aiwf 侧：人验收是引擎原生能力（这条让闭环成立）

实测 `engine.py` / `cli.py`：

- 运行相位含 `awaiting_input`（任务节点向人提问）与 **`awaiting_review`（"acceptance requirements for a human"）**
  （`engine.py:9-10`）；另有 `awaiting_items`、`awaiting_check`（`:1306-1307`）。
- task 节点可内嵌人工验收门：`if node["type"] == "task" and node.get("review")` → 渲染 `review.requirements`
  进 spec（`engine.py:1799-1800`）。
- 裁决与回退命令（实测参数）：
  - `aiwf review <run_id> --verdict APPROVE|REJECT [--feedback "..."]`（`cli.py:770-777`）
  - `aiwf revise <run_id> --target <task-node> --reason "..." [--mode cascade|return]`（`cli.py:779-787`）
  - `aiwf resume <workflow_id> <run_id>`（`cli.py:796-800`）
- 运行记录 + journal 落盘，`aiwf current <run_id>` 可随时打印当前 spec（跨会话可续）。

→ **Godot 跑不起来并不阻塞闭环**：把"运行验证"明确设计成 `review` 门即可。
这正是本仓库既有实践（`g-do` 的"Agent 自检通过不等于用户验收通过"）的引擎化表达。

---

## 2. 模板定位：与 `g-*` 体系的分工（避免双份维护）

本仓库已有一套成熟流程。模板若把 `g-do`/`g-dev` 的规则文本抄进来，立刻出现两份事实、必然漂移。
所以定位是**薄编排**：模板只负责"把已确认的流程变成引擎强制的节点序列"，知识本体仍留在 `skills/`。

| aiwf 提供（`g-*` 没有的） | `g-*` 提供（模板不该重写的） | 模板的做法 |
|---|---|---|
| 引擎强制的节点边界、`check` 重试、`max_steps` 预算 | 就绪检查的具体判据、五类分流 | `prompt` 写"执行 `g-do` 的实施前就绪检查"，不抄判据 |
| 可恢复 + 可审计：run 目录 / journal / 定义快照（1.1） | Godot 工程结构与 GDScript 规范（`g-dev`） | 节点写"写代码前加载 `g-dev`" |
| 结构化人 gate：`awaiting_review` + APPROVE/REJECT | 验收标准怎么写、状态机怎么流转 | 用 `review.requirements` 引用 TASK 的验收清单 |

判据：**模板负责"流程被强制执行"，技能负责"流程内容正确"。** 两者都不接管对方的职责。

---

## 3. 关键设计点：事实不重复维护

- **`game_id` / `plan_id` 不进 `run_defaults`。** 它们已经是 `project/.active-project` 的内容，
  而 `AGENTS.md:95-98` 指定它是唯一来源。模板第一步用 `scripts/manage_project.py current`
  读取当前项目/阶段/计划，而不是让配置再声明一遍（两份事实必然打架）。
  → 实测该脚本可用：`manage_project.py {create,ensure,switch,focus,current,list}`，默认 `--repo-root` 即本仓库。
- **`run_defaults` 只放"项目固有、别处没有"的值**，例如 `repo_dir`（将来若走 headless，再加 `godot_exe`）。
  写在 `godot_agent/.aiwf/config.json`。
- 模板本体按决策 1（A 安装式 copy）落到 `godot_agent/.aiwf/workflows/`；库内源文件强制 `schema_version: "1.1"`。

---

## 4. 候选流程（第一个模板固定哪一条）

| # | 流程 | 专业性来源 | 本会话能否跑通 | 固化价值 |
|---|---|---|---|---|
| **A** | **任务实施 + 人工验收 + 收口闭环**：读 `.active-project`/TASK/基线 → 就绪检查 → 实施 → 自检取证 → 提交 `review` 门（给人实机验收）→ APPROVE 后收口（状态/记录/基线 + 整目录提交） | `g-do` 全套 + `g-dev` | **能**（实机验证本来就在人这一侧） | **最高**：这是每天重复的动作 |
| B | 验收收口专件：验收清单核对 → 证据归集 → 标 `已完成` → 整目录提交 | `g-do`「用户验收」节 | 能 | 高，但窄（流程的一半） |
| C | 计划巡检（只读）：当前该做什么 / 就绪缺口 / 阻塞点 | `g-do`「实施前就绪检查」 | 能，且端到端最快 | 中：轻，更像巡检工具而非生产流程 |
| D | 资源与许可检查（把现有演示件 `game-resources` 泛化成 `std.*`） | 无（自造） | 能 | 中低：一次性成分大 |

**推荐 A。** 三条理由：
1. 它是"从实践中来"的——本仓库 3 个游戏、十几份 plan、每条 TASK 都走这条；不是猜出来的流程。
2. 它把**专业性**落在实处：就绪检查、实施前分流、验证纪律、验收后整目录提交，全部是既有的、写得极细的规则。
3. 它把**通用性**落在实处：换游戏换计划不需要改模板——`.active-project` 决定上下文，配置只放仓库路径。

B/C 是 A 的真子集，A 跑通后它们要么自然覆盖、要么拆出来当轻量入口，不值得先做。

---

## 5. 推荐项（A）的骨架草案

> 草案，供讨论粒度用。**选完流程后逐节点过一遍再定稿**，此处不作为已定设计。

| 节点 | 类型 | 目的 | 读什么 | 产出 / 门 |
|---|---|---|---|---|
| `prepare` | task | 确定目标 + 就绪检查 | `.active-project` → `plan/<id>/index.md` 焦点 → 当前 TASK → `context.md` 基线 | 就绪结论 + 实施要点；`check` 只准读点名的文件（对齐 `g-do` 的硬上限） |
| `implement` | task（带 `review`） | 实施并取证 | 任务契约、按需加载 `g-dev`、相关现有代码 | 真实改动 + 验证证据落 run 目录；**`review.requirements` 写"请实机验收"的具体步骤** → 挂 `awaiting_review` |
| `finalize` | task | 收口 | 验收裁决与反馈 | 更新 `index.md` 状态/进度、TASK 记录、`context.md` 基线；对 `project/<game-id>/` 整目录一次提交（不 push） |

`REJECT` 走 `aiwf revise --target implement`（`g-do` 的"验收未通过 → 进行中"）。

---

## 6. 明确不做

- 不把 `g-do` / `g-dev` 的规则文本抄进模板（双份维护）。
- 不在模板里绕开 plan 建立平行执行路径（`AGENTS.md` 核心原则 2/3）。
- 不自动提交其他游戏目录或仓库级文件（`g-do` 提交范围限定 `project/<game-id>/`）。
- 不假定 `godot` 在 PATH（当前不在；任何运行命令都不得写死）。

---

## 7. 待拍板（当前只这一项）

**决策 9：第一个模板固定哪条流程。**
选项见第 4 节；推荐 **A**（任务实施 + 人工验收 + 收口闭环）。

---

## 8. 后续待议（不占本次拍板）

1. **模板落点**：进 aiwf 库（`library/workflows/`）还是留 `godot_agent` 项目本地。
   张力点：`AGENTS.md:142`「项目特有逻辑不得伪装为通用工具」——本流程深度绑定 `g-*` 与 `.active-project`；
   若进库，等于承认这是"跨项目通用的游戏开发流程"。**这一项等流程定了再谈。**
2. **Godot 运行能力怎么补**：配 Godot MCP / 给我 `godot_exe` 路径走 headless / 一直接受"实机验证由人做"。
   不补的代价：模板里不能有任何自动运行游戏的节点，验证全部经人 gate。
3. **节点粒度与 `check` 阈值**：`prepare`／`implement`／`finalize` 是否要再拆（例如把"取证"独立成节点）。
4. 本仓库 `ideas/` 当前为空、`project/mc-plus/tasks/` 顶层为空（任务实际在 `plan/<id>/tasks/`）——不影响本设计，
   记录备查。
