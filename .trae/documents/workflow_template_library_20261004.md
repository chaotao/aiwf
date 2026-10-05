# aiwf 工作流模板库机制 — 方案 v2（决策 1 已定，其余待拍板）

日期：2026-10-04（v3：已实施）
状态：**决策 1–4、6 已定并落地**；P0 + P1 完成，全量测试 80/80 通过。剩余项见第 7 节，实施记录见第 8 节。

---

## 0. 目标

预先制作通用工作流，保存在本仓库；其他项目一条命令装到本地使用，不必自己从零创建。

---

## 1. 已定决策

| # | 决策 | 结论 | 依据 |
|---|---|---|---|
| 1 | 复用语义 | **A 安装式（copy 到项目本地）** | 主人 2026-10-04 拍板：前期模板不成熟，项目自己调整本地工作流更方便 |
| 2 | 模板条目构成 | **c：定义 JSON + 元信息 JSON** | 主人 2026-10-04「按推荐来改」。元信息落在 `library/meta/`，不在定义旁——见第 3.2 节理由 |
| 3 | 命名前缀 | **`std.`**，做成常量 `library.PREFIX`，不强制校验 | 同上；A 下模板与项目自建定义同处一目录，前缀让外来件一眼可辨 |
| 4 | install 依赖 | **默认带依赖**，`--no-deps` 关闭 | 同上 |
| 6 | 命令组名 | **`wf`** | 同上 |
| 5 | 首批入库哪些模板 | **未定** | 需主人单独拍板；当前库里只有 2 个验证用试点模板 |

**A 的含义**：不做运行时分层解析、不做 fork/遮蔽。

**A 的代价（必须配套解决，否则机制会坑人）**：
- **依赖链被切断**：模板 A 通过 `subflow` 调模板 B，只装 A 则 `validate --project` 直接报 missing workflow。
- **上游更新不可见**：本地改过之后，无法判断库里的版本是否已更新、本地改了哪些。

本方案用「依赖闭包安装 + 安装台账（provenance）」两件事把这两个坑补上。

---

## 2. 现状（实测事实，含证据）

| # | 事实 | 证据 |
|---|---|---|
| 1 | 定义解析单目录无分层：`workflows_dir = <AIWF_HOME \| .aiwf>/workflows` | `storage.py:238-239`、`263-277` |
| 2 | 所有入口共用 `load_workflow`：`run`、`validate --project`、subflow/foreach 解析 | `cli.py:307`、`engine.py:461-466`、`734/811/1122` |
| 3 | 1.1 每个 run（含子流程实例）启动时写定义快照 `workflow.json`，读取时快照优先 | `engine.py:1263-1273`、`engine.py:911`、`storage.py:502-504` |
| 4 | 1.0 定义**无快照**，且有 `schema_version` 一致性检查 | `storage.py:505-512` |
| 5 | 现有"分发"机制先例：`_bundled_skills_dir()` 按 `AIWF_SKILLS_DIR` env 覆盖，否则由 `cli.py` 位置推出 `<repo>/skills` | `cli.py:521-527` |
| 6 | 现有跨项目复用实为整包 `copytree`，且已产生漂移：两份 `.trae/skills/aiwf/` 与事实来源分歧 12/25 文件 | `cli.py:541-557`；git status + 前次记忆 |

**第 3 条决定了本方案不需要碰 run 记录、journal 或复现逻辑**——快照机制已经存在。

---

## 3. 方案（A 版）

### 3.1 库的位置与发现

- 物理位置：本仓库 `library/workflows/`，与 `skills/` 平级。
- 发现方式：**与 `skills/` 完全同构**——`AIWF_LIBRARY_DIR` env 覆盖，否则由 `cli.py` 相对位置推出 `<repo>/library/workflows`。零新概念。

### 3.2 模板条目的构成（决策 2 已定：c）

一个模板条目 = 定义 + 元信息，**两者分目录存放**：

```
library/
  INDEX.md                # 由 `aiwf wf index` 生成，勿手改
  workflows/<id>.json     # 工作流定义，只放定义
  meta/<id>.json          # summary / maturity / scenarios / not_for / deps
```

**元信息不放定义旁，改放兄弟目录 `meta/`**：`Storage.list_workflows()` 把 `.aiwf/workflows/`
下所有 `*.json` 都当定义读，而 `<id>.meta.json` 同样以 `.json` 结尾——一旦被复制进项目，
`aiwf validate --project` 会把它当工作流加载并直接校验失败。分目录从结构上消除这个隐患，
代价只是 `library.py` 里一处路径推导。

最小字段集（只定这 5 个，字段集本身保持可演进）：

| 字段 | 用途 |
|---|---|
| `summary` | `wf list` 的一句话，并进 `INDEX.md` |
| `maturity` | `experimental` / `stable` / `deprecated`，直接服务"何时用" |
| `scenarios` | 适用场景，进 `INDEX.md` 与 `wf show` |
| `not_for` | 不适用场景，进 `INDEX.md` 与 `wf show` |
| `deps` | 文档化依赖，由测试强制与定义里实际调用的 workflow 一致 |

### 3.3 命名与冲突

库模板统一加前缀（`std.` 或 `lib.`）。理由：A 模式下模板与项目自建定义**同在 `.aiwf/workflows/` 一个目录**，冲突是真实风险；前缀让"外来模板"一眼可辨，也让 `wf status` 只需扫目录前缀即可列出已安装项，无需额外登记。

项目自建定义沿用现约定（如 `novel-writing`），几乎不可能撞名。

### 3.4 依赖闭包安装（补 A 的第一个坑）

`install` 需静态扫描该定义里的 `subflow` / `foreach.workflow` 节点，递归收集库内依赖：

- 默认 `--with-deps`（带上全部依赖），并在输出里逐条列出装了哪些；
- 依赖若在库中缺失 → 显式报错，不静默跳过；
- 装完提示跑 `aiwf validate --project`（该命令已能覆盖调用图与必需入参，`engine.py:461-498`）。

### 3.5 覆盖保护与上游更新感知（补 A 的第二个坑）

安装时写台账 `.aiwf/workflows/.installed.json`：

```json
{
  "std.batch-generate": {
    "source": "library/workflows/std.batch-generate.json",
    "installed_hash": "<装入内容当时的 sha256>",
    "upstream_hash":  "<库文件当时的 sha256>",
    "installed_at":   "2026-10-04T11:00:00+08:00",
    "maturity":       "experimental"
  }
}
```

由此得到两个能力，且**不改动 run 记录、不改动引擎状态机**：

- **覆盖保护**：`install` 发现本地已存在且内容与 `installed_hash` 不一致（= 项目改过）→ **默认拒绝覆盖**，需显式 `--force`。这正对应主人选 A 的理由（要能自由改本地）。
- **上游更新感知**：`wf status` / `wf diff` 对比三方——当前内容、`installed_hash`、库文件当前 hash，回答「本地改过没」「上游更新没」。

### 3.6 库模板硬约束

库内模板**强制 `schema_version: "1.1"`**（由第 2 节第 3/4 条：1.0 无 run 快照，库升级会让旧 run 撞版本检查）。

---

## 4. CLI 表面

```bash
aiwf wf list                    # 库中可用模板：id / 名称 / 成熟度 / 一句话场景
aiwf wf show <id>               # 元信息 + input_schema 摘要 + 依赖的其他模板 + 本地是否已装
aiwf wf install <id>            # 装到 .aiwf/workflows/（默认含依赖、拒绝覆盖已改动的本地文件）
        [--as <new-id>]         #   改名安装
        [--force]               #   覆盖本地已改动的副本
        [--no-deps]             #   只装本体
aiwf wf diff <id>               # 本地副本 vs 库版本
aiwf wf status                  # 已装模板 vs 库版本对照表（本地改动 / 上游更新）
aiwf wf index                   # 由元信息重新生成 library/INDEX.md
```

`aiwf workflows` 保持现状（列本地可用 id），可酌情加标记区分外来/自建。

---

## 5. Skill 配套（对应主人第 2 条：怎么用 / 何时用 / 用哪些）

改动点：

1. **`skills/aiwf/SKILL.md` 的「入门与日常使用」表新增一行**：
   `| 想找现成流程、不确定要不要自己写 | 模板库 |`
   这是"何时用"的入口——先查库再决定自建，避免重复造。

2. **新增 `skills/aiwf/references/templates.md`**，内容：
   - **何时用**：沿用已有原则——一次性短任务不用工作流；流程有恢复/复用/批量/验收价值才考虑固化。在此之上补：**先 `wf list` 查库，命中就用，不命中才自建**。
   - **何时不该用**：库模板元信息里的 `not_for` 抄进这一节（避免用错的场景）。
   - **怎么用**：`list → show → install → validate --project` 四步，含"装完通常要改什么"（`input_schema` 必填值、路径类输入、`max_steps`）。
   - **用哪些**：由 `wf list` 输出 + `library/INDEX.md` 承担，skill 文档只讲判据、不抄模板清单（避免文档漂移）。
   - **A 模式特有提醒**：装进来的是**副本**，本地修改不会回传库；判断上游有无更新用 `wf diff`。

3. **库与 skill 包的边界**：库**不进** skill 包。理由：`skills/` 现在已被整包 copy 到每个 agent 目录并产生 12/25 文件漂移（第 2 节第 6 条）；把库塞进去等于把同一个问题搬家。模板本体只存一份。

---

## 6. 落地顺序

**P0 — 机制跑通**
1. `library/workflows/` 建目录；`AIWF_LIBRARY_DIR` 解析（照抄 `_bundled_skills_dir()` 模式）。
2. CLI：`wf list` / `wf show` / `wf install`（含依赖闭包、覆盖保护、台账）。
3. **2 个试点模板**：一个"装了直接用"，一个"有 subflow 依赖 + 需要本地改"，用来验证依赖闭包与覆盖保护。
4. `tests/test_wf_library.py`：装本体、装依赖闭包、依赖缺失报错、覆盖保护拒绝 / `--force` 通过、台账正确。

**P1** — `wf diff` / `wf status` / `wf index`；skill 文档（SKILL.md 表 + `references/templates.md`）。

**P2** — 模板 lint（强制 1.1、必填字段有 description、`max_steps` 显式）；`--as` 改名后的台账追踪。

---

## 7. 剩余项（未定，等主人拍板）

### 决策 5：首批入库哪些模板

当前 `library/workflows/` 只有 2 个**验证用试点模板**，不等于首批生产模板：

| 模板 | 角色 |
|---|---|
| `std.review-check` | 「装了直接用」：无依赖，可单独使用，也作为下面那个的依赖体 |
| `std.review-loop` | 「有依赖 + 需要本地改」：subflow 调 `std.review-check`，用来验证依赖闭包与本地改动保护 |

真正的首批清单需要主人定：现有 `skills/aiwf/assets/examples/` 里 12 个演示件哪些升级为
生产模板、是否新增（批量内容生成 / 代码修改 / 素材生产 / 调研评审）。**本方案不预设。**

### 决策 5b：元信息是否加 `local_edits` 字段

当前"装完通常要改什么"只能靠读模板本体判断，`wf show` 给不出。若首批模板定下来后
这类信息反复出现，再加 `local_edits`（常见本地改动项）字段。**不提前加。**

### 仍未决定的实现项

- 模板 lint（决策 5 定后才值得做）：强制 1.1、必填字段带 description、`max_steps` 显式。

---

## 8. 实施记录（2026-10-04）

**新增**

| 文件 | 内容 |
|---|---|
| `engine/aiwf/library.py` | 库路径解析（`AIWF_LIBRARY_DIR` → `<repo>/library/workflows`）、定义与元信息加载、依赖闭包、覆盖保护安装、台账、`status` / `diff` / `render_index` |
| `library/workflows/std.review-check.json`、`std.review-loop.json` | 2 个试点模板 |
| `library/meta/*.json` | 对应元信息 |
| `library/INDEX.md` | `aiwf wf index` 生成 |
| `skills/aiwf/references/templates.md` | 何时用 / 何时不该用 / 怎么用 / 用哪些 / 复制式安装的三条约定 |
| `tests/test_wf_library.py` | 10 个用例 |

**改动**

- `engine/aiwf/cli.py`：docstring 命令表 + `from . import library as template_library` + 6 个
  处理器（`cmd_wf_list/show/install/diff/status/index`）+ `wf` 子命令组接线。
- `skills/aiwf/SKILL.md`：入门表新增「想找现成流程 → 模板库」一行；范例行补注"演示件"与模板库的分工。

**未改动**：`engine/aiwf/engine.py`、`storage.py`、`schemas/`、run 记录格式、journal 机制。
设计上就不需要动——1.1 的 run 启动快照（第 2 节第 3 条）已把运行时与定义源解耦。

**实证**

- 新增 `tests/test_wf_library.py`：**10/10 通过**。覆盖元信息与定义一致（含 `deps` 与
  实际调用一致）、依赖闭包安装、`--no-deps` 后 `validate --project` 失败、本地改动拒绝覆盖
  与 `--force`、项目自有同名文件拒绝覆盖、`--as` 改名并改写定义 `id`、缺依赖显式报错、
  未知模板报出搜索路径、`status`/`diff` 追踪本地与上游变化、`INDEX.md` 与元信息一致。
- 全量 `tests/`：**80/80 通过**（此前 70 个 + 新增 10 个），无回归。
- 运行方式：`C:/Python314/python.exe`（该解释器有 `jsonschema` 4.17.3；仓库自带的
  `C:\Users\tao\.workbuddy\binaries\python\versions\3.13.12\python.exe` 未装 jsonschema）。

**已知遗留**：`skills/aiwf/` 的两份项目副本（`examples/novel-demo/.trae/skills/aiwf/`、
`sim/novel-3ch/.trae/skills/aiwf/`）本次**未同步**。实测（`diff -rq`，2026-10-04）两份副本
与事实来源各有 **13 处差异**（26 个源文件中的 12 处内容分歧 + 仅存在于源目录的
`references/templates.md`），两份副本彼此行为一致。改动前该数字为 12——`SKILL.md` 本就在
差异集内，故本次净增 1（新增的 `templates.md`）。同步方向仍是主人未确认事项。

---

## 9. 明确不做

- 不做运行时分层解析 / fork / 遮蔽（决策 1 选 A 后作废）。
- 不做远程库（`wf add <git-url>`）。
- 不把库塞进 `skills/` 分发。
- 不改 run 记录格式、journal 机制、定义快照逻辑。
