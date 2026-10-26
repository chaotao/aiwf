# aiwf 剩余工作移交文档

日期：2026-10-26
基线提交：`266d9db`（此前 `abf30de` / `f2c3a3e` / `d16f974` / `449a99a`）
当前状态：L0–L3 主要项已完成；本文只列**还没做**的事，供后续接手。
工作区：`F:\data\project\github\aiwf`。未跟踪的 `.acl-diagnostics/` 是 2026-10-25
修复工作区 Windows 权限时留下的恢复记录，**不要提交**。

## 0. 接手先跑这三条

```powershell
cd F:\data\project\github\aiwf
$env:PYTHONPATH='engine'
.\.venv\Scripts\python.exe -m pytest -q          # 期望 226 passed / 20 subtests
.\.venv\Scripts\python.exe bench\run_bench.py    # 期望 5 个场景全 True，0 错误
git log --oneline -5
```

两种编码环境都要过：不设 `PYTHONUTF8`、以及 `$env:PYTHONUTF8='1'`。
如果 `pytest` 失败先修测试/环境，不要在被破坏的基线上继续。

## 1. 当前接口基线（不要无意破坏）

正常执行循环只有两条命令：

```bash
aiwf                 # 无参 = status：完整任务包
aiwf submit          # 暂存 submit.json 的 action + output，按 phase 分派
```

公开命令（`aiwf --help`）：`status`、`submit`、`new`、`run`、`runs`、`resume`、
`revise`、`validate`、`doctor`、`wf`。

隐藏但仍可用（`aiwf --help-all`）：`current`、`complete`、`provide`、`review`、
`fail`、`redo`、`items`、`workflows`、`skills`、`init`、`wf index`。
其中 `complete` / `provide` / `review` / `fail` 文本模式会打印 `deprecated: use aiwf submit`。

必须保持的约束：

- `engine.act` 的 action 严格校验；`submit` 用 `submit.json` 的 action 恢复这道保护；
- `current` / `status` 默认只读（`status --ensure` 是显式写）；
- 不提供 `--force` 绕过 stale；
- review 结论必须来自用户，不能自动批；
- 输出 UTF-8、JSON 读取容忍 BOM；
- 旧 run 缺 `submit.json` 时：`status` 标 missing；`status --ensure` 补建；
  首次 `submit` 自动补建，单文件 task 且产物已存在时直接提交（若还有 output_schema
  的额外字段要求，会以驳回 3 返回，而不是卡在“缺模板”）。

提交模板与分派细节见 `skills/aiwf/RUN-CARD.md` 与 `skills/aiwf/references/run-workflow.md`。

## 2. R1：物理合并 14 份 reference（P1，纯文档）

### 现状

`skills/aiwf/references/` 共 14 份、约 1,323 行：

```
advanced-dynamic.md (90)     advanced-integration.md (61)
advanced-revisions.md (66)   create-workflow.md (62)
data-reference.md (126)      examples.md (79)
inspect-runs.md (136)        interaction.md (95)
outputs.md (121)             patterns.md (125)
routing.md (73)              run-workflow.md (63)
templates.md (84)            view.md (142)
```

默认路径已经只读 `SKILL.md` → `RUN-CARD.md` / `TROUBLESHOOT.md`，其余按需；
所以这项不影响弱模型的默认行为，属于编辑整理。

### 建议分组（4 份合并 + 2 份独立入口）

| 新文件 | 合并内容 | 约行数 |
|---|---|---|
| `create.md` | create-workflow + data-reference + outputs + routing | 382 |
| `run.md` | run-workflow + interaction | 158 |
| `advanced.md` | patterns + advanced-dynamic + advanced-revisions + advanced-integration | 342 |
| `audit.md` | inspect-runs + view | 278 |
| `templates.md` / `examples.md` | 保持独立（模板库与范例目录是入口，不宜埋进大文件） | 163 |

合并后：4 份主题文档 + 2 份入口文档，共 6 份（不是 5 份；如果要严格 5 份，
可把 `examples.md` 并入 `create.md`，`templates.md` 保持独立）。

### 实施步骤

1. 先决定**删除旧文件还是留重定向 stub**（见 §6 决策 1）；推荐删除，并在同一次提交
   改完所有链接。
2. 按上表拼接内容；每份加目录、统一 H1/H2 层级；把跨文件链接改成新文件 + 锚点。
3. 更新 `SKILL.md`、`RUN-CARD.md`、`TROUBLESHOOT.md`、`references/*.md` 中所有
   `references/<old>.md` 链接。
4. 更新 `pyproject.toml` 的 data-files（目前是 `references/*.md`，通配即可，无需改）。
5. 加一个链接检查测试：扫描 `skills/aiwf/**/*.md` 里的相对链接，断言目标文件存在、
   锚点在同文件内存在。可新建 `tests/test_skill_links.py`。
6. 全量 `pytest` + `bench/run_bench.py`；同步两份 `sim` 安装副本（见 §7）。

### 验收

- 只剩 6 份（4 份合并 + 2 份入口；或按决策并成 5 份）；
- `SKILL.md` 仍是一页，默认路径不变；
- 所有相对链接/锚点可解析；
- 内容抽检：原 14 份的每个 H2 都能在新文件中找到；
- `pytest` 226+ 全绿，bench 全绿。

### 风险

- 合并时丢段落、锚点错位；用“先拼接、再 diff 抽检”的方式，不要手抄。
- `sim/` 副本与源目录是逐文件镜像，漏同步会造成漂移（见 §5）。

## 3. R2：`current` 的最终去向（P2，兼容决策）

### 现状

- `current` 已隐藏，但仍被使用：
  - `sim/plan-task/drive.py:223` 用 `current` 读取任务包；
  - `tests/test_l1_cli.py`、`tests/test_l2_cli.py` 用它验证只读；
  - `skills/aiwf/references/advanced-dynamic.md:78` 与
    `skills/aiwf/references/inspect-runs.md:156` 仍以 `current` 举例。
- `status --json` 是它的超集：多了 `title`、`next`、`submit_file`、`submit_missing`。

### 方案（二选一，需要拍板）

甲（推荐，兼容优先）：保留 `current` 作为隐藏的 SDK/旧脚本入口，不删；
把 `drive.py` 和测试逐步改用 `status --json`，文档只写 `status`。

乙（彻底收敛）：删掉 `current` 的 parser/`cmd_current`/`_emit(advance=False)` 分支；
同步改 `drive.py`、测试、文档；在 CHANGELOG/大版本说明中标注移除。

### 验收

- `grep -R "aiwf current"` 只剩“已移除/兼容说明”或为零；
- `drive.py` 能跑通；
- `status --json` 字段覆盖原 `current` 的所有消费点；
- 全量测试 + bench。

## 4. R3：隐藏命令/别名的清理（P2，大版本再做）

现在隐藏但保留：`current complete provide review fail redo items workflows skills init`、`wf index`。
其中 `complete/provide/review/fail` 已弃用并提示；`redo` 已由 `revise --instance` 覆盖。

建议：

1. 本轮只保留现状（给旧脚本一个版本周期）；
2. 下个大版本删除 `complete/provide/review/fail/redo` 的 parser 与实现（或保留为
   “错误 + 指向 submit”的占位），同步测试与文档；
3. `items` 保留为队列排查入口；`workflows/skills/init/wf index` 保留为维护命令；
4. 删除后更新 `_full_help` 与 `--help-all` 的清单。

验收：`--help` 公共面不变；`--help-all` 与实现一致；旧命令给出明确 `E_USAGE`/提示；
全量测试 + bench。

## 5. R4：真实弱模型基准（P1，需要模型/外部执行）

`bench/run_bench.py` 是**接口基准**：一个盲跟随者只用 `status`/`submit` 跑 5 个场景，
证明接口自洽，不证明模型能力。真实模型基准还没做。

建议协议：

- 每个任务用**全新临时项目**，只给 Agent：`SKILL.md` + `RUN-CARD.md` + 任务一句话；
  不给 `references/`，除非它自己按错误码去查。
- 固定 3 个任务 × 3 次：顺序文件任务；check + review；队列（2 条）。
- 记录：完成率、`status`/`submit` 次数、非 0 次数、traceback 次数、读了几份文档、
  token 总量、人工介入次数。
- 人工 review 只能由人给结论；测试夹具要显式标注。
- 通过线建议：3/3 任务完成；traceback 0；单任务命令数 ≤ 节点数×2+3；
  读 RUN-CARD 以外文档 ≤1 次；不出现 `complete/provide/review/fail/items`。

产出：一份 `sim/report-<date>.md`（沿用仓库 `sim/report*.md` 的写法），
不要改写历史报告。

## 6. R5：可选的遗留决策（先记录，不急）

1. **`run --title` 目前必填**。弱模型漏传会多一次 argparse 失败重试。
   可选：默认 `<workflow name> · <日期>`，`view.dynamic_title` 仍可覆盖。
   这会推翻 2026-10-11 “必须给”的拍板，需主人确认。
2. **`submit` 是否吸收 `revise --instance`**：目前 `revise` 是独立逃生舱；
   不建议塞进 submit（修订需要 target/mode/scope）。
3. **`status --ensure` 是否改名**（如 `--repair`）：`--ensure` 语义是“确保存在”，
   保留亦可。
4. **skill 副本漂移**：目前靠人工全量复制 + md5 校验；可加 `aiwf skills status`
   或版本戳，减少后续漏同步。

## 7. 文档与副本同步（每次改 skill 必做）

源目录：`skills/aiwf/`。
安装副本（逐文件镜像）：

```
sim/novel-3ch/.trae/skills/aiwf/
sim/novel-full/.trae/skills/aiwf/
```

同步方式（PowerShell，保留相对路径）：

```powershell
$src='skills\aiwf'
foreach ($target in @('sim\novel-3ch\.trae\skills\aiwf','sim\novel-full\.trae\skills\aiwf')) {
  Get-ChildItem $src -Recurse -File | ForEach-Object {
    $rel=$_.FullName.Substring((Resolve-Path $src).Path.Length+1)
    $dst=Join-Path $target $rel
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    Copy-Item $_.FullName $dst -Force
  }
}
```

同步后逐文件 md5 比对，必须 0 differences。

## 8. 验证与交付清单

每次改动后：

```powershell
$env:PYTHONPATH='engine'
.\.venv\Scripts\python.exe -m pytest -q
$env:PYTHONUTF8='1'; .\.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:PYTHONUTF8
.\.venv\Scripts\python.exe bench\run_bench.py
git diff --check
```

交付：

- 只提交源码/文档/测试；不要提交 `build/`、`dist/`、`.tmp-*`、`.acl-diagnostics/`；
- commit message 写清“改了什么 + 验证命令/结果”；
- 若改了 skill，必须同步两份 `sim` 副本并在提交说明里写“md5 0 differences”。

## 9. 建议接手顺序

1. **R1 reference 合并**（独立、可先做；先拍板删除 vs stub）。
2. **R2 current**（拍板甲/乙；甲只需要改 `drive.py`/测试/文档）。
3. **R4 真实弱模型基准**（需要模型与预算；先写协议再跑）。
4. **R3 别名清理**（等大版本/一个兼容周期）。
5. **R5** 逐项拍板，不阻塞。

## 10. 关键文件索引

| 文件 | 作用 |
|---|---|
| `engine/aiwf/cli.py` | 命令面、`status`/`submit`、`submit.json`、help 分层、`new` |
| `engine/aiwf/engine.py` | 状态机、action 指纹、节点任务包、check/review 注入 |
| `engine/aiwf/schema.py` | 校验、`normalize_workflow`（`use` 展开） |
| `engine/aiwf/storage.py` | 定义/记录读取、`use` 归一化、run 目录 |
| `engine/aiwf/errors.py` | 错误码/退出码 |
| `skills/aiwf/SKILL.md` | 默认入口（一页） |
| `skills/aiwf/RUN-CARD.md` | 两命令循环 + 分派表 |
| `skills/aiwf/TROUBLESHOOT.md` | 错误码 → 动作 |
| `skills/aiwf/references/` | 按需参考（R1 待合并） |
| `tests/test_l0_cli.py` / `l1` / `l2` / `l3` | CLI 契约 |
| `bench/run_bench.py` | 盲跟随者接口基准 |

## 11. 已知不做/不要做

- 不要在 `current`/`status` 默认路径上写盘（`status --ensure` 除外）；
- 不要给 `submit` 加 `--force` 绕过 stale；
- 不要让 `submit --approve` 在没有用户结论时自动批准；
- 不要把 `submit.json` 的 `action`/`kind` 交给调用方维护；
- 不要为了“更简单”删掉 `resume`/`revise`/`runs`/审计事件——它们是引擎的价值；
- 不要把 `bench/run_bench.py` 的结果当成模型质量结论，它只是接口基准。
