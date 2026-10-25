# 让"不太聪明的模型"也用对 aiwf：易用性诊断与改造建议

日期：2026-10-25
状态：L0 已于同日实施（见 §9）；L1–L3 仍是待评估建议。
范围：引擎 / CLI 协议 / 定义格式 / Skill 文档 / 分发。
方法：通读实现（`cli.py` 1198 行、`engine.py` 2990 行、`schema.py`、`storage.py`、`view.py`、14 份 reference），
在中文 Windows + PowerShell 5.1 下逐条实跑命令，并让一个只读 Skill 文档的 Agent（环境变量由外部给出）完整跑一遍两步流程。

## 0. 结论摘要

aiwf 的"顺路"是通的：弱 Agent 只读技能文档（环境变量由外部给出），12 条命令、0 报错、5 份文档（SKILL + 4 份 reference），完成了「大纲 → 正文 + 两次人工验收」。
问题不在顺路，而在**正确性目前依赖执行者"谨慎 + 读得懂 1900 行文档"**。弱模型会死在四个地方：

1. **错误是 traceback 或 schema 回显**：少写一个 `name` 字段，错误对象 68 KB；一份多处笔误的定义，PowerShell 落盘 141 KB；漏一个启动入参、枚举值填错、verdict 少个字段，全是 Python traceback。
2. **每一步都要自己翻译**：`action.kind` → 命令、产物 → 手拼 JSON、动作 → 精确复制 24 位 action id、运行 → 精确复制 `20261025-...` run id。
3. **中文 Windows 上必踩的平台坑**：PowerShell 剥掉内联 JSON 的双引号；`Set-Content -Encoding utf8` 带 BOM 被 `--output-file` 拒绝；`node.output.file` 是反斜杠，塞进 JSON 会变成转义符。
4. **文档是"预读式"的**：1900 行、14 份 reference，弱模型要么全读（超上下文）要么乱读；而真正的操作指令（`next`）不在输出里，只在文档里。

改造方向不是加功能，而是把正确性从"知识"挪到"接口"：**每一步只留一个显而易见的动作；错误自带修复指令；文档从"先读后用"变成"出错再查"。**

如果只做三件事：

| 优先级 | 事情 | 一句话 |
|---|---|---|
| P0 | 错误协议 + UTF-8 硬保证 | 任何用户错误都不出现 traceback，stderr ≤ 10 行，带 `code / at / hint / next` |
| P0 | spec 自带 `next` 命令 + 免引号提交 | 输出里直接给可复制命令；`--file` / `--verdict` 取代手写 JSON |
| P1 | 一页 RUN-CARD + 错误码速查 | 顺路 5 条命令；出错按 code 查一行，不再预读 1900 行 |

### 0.1 对"不太聪明的模型"的能力假设

这不是贬低模型，而是把设计建立在可验证的行为上：

| 能做 | 不擅长 |
|---|---|
| 照抄一行命令并执行 | 在 5 个动词里选对下一个 |
| 读一个文件、按模板填字段 | 精确复制 24 位 action id / 22 字符 run id |
| 复制路径、改一个值 | 手写合法 JSON 并处理 PowerShell 引号 |
| 执行一步、汇报结果 | 从 140 KB 错误里定位那一句 |
| 按清单逐条核对 | 预读 1900 行后正确回忆该用哪条规则 |

由此得到四条设计原则：**识别优于回忆**（命令放进输出）、**模板优于创作**（给待填文件而不是空 JSON）、**错误即指令**（code → 一行动作）、**一次只做一个决定**（每个 phase 只有一个显而易见的动作）。

## 1. 实测数据

| 指标 | 实测值 | 说明 |
|---|---|---|
| 默认中文 Windows 下 `pytest` | **166 passed / 12 failed** | 设 `PYTHONUTF8=1` 后 166 全过 |
| 少写 `name` 的定义 → `validate` | Python 侧错误对象 **67,992 字符**；PowerShell 捕获落盘 **141 KB** | 有用信息只有第一行 |
| `output` 写成字符串 → `validate` | 470 字符 | 正常可读 |
| 未知字段 `output_file` → `validate` | 14,814 字符 | 有用信息只有第一行 |
| 常见误用抽查 | **7 例中 5 例是 traceback** | 缺入参、枚举不符、verdict 形状不符、`-p` 少 `=`、定义不合法 |
| CLI 命令面 | **16 个顶层 + 18 个二级 = 34** | 顺路只需其中 ~8 个 |
| Skill 文档 | SKILL + 参考 + 范例合计 **1,909 行**；14 份 reference 占 **1,334 行** | `SKILL.md` 仅 47 行 |
| 弱 Agent 试跑 | 12 命令 / 0 报错 / 3 次犹豫 / 运行 23.8 s | 顺路可学，但正确性靠"读得细" |

弱 Agent 的三条 `CONFUSION`（原文摘录）：

```
CONFUSION: 任务包渲染出的路径是 Windows 反斜杠（.aiwf\runs\...），直接塞进 JSON 会被当成转义符
           （\r 变成回车、\s 非法）。我猜必须写成双反斜杠才能和声明路径逐字一致。
CONFUSION: 状态显示成 completed / executing，已完成的运行 phase 还停在 executing；
           我猜只是 phase 没归到终态，不影响结果，但读起来像"还在跑"。
CONFUSION: 任务说工作目录已存在，实际不存在，得先自己建。
```

结论：**顺路不需要改造就能走通；需要改造的是"走错一步之后的恢复成本"，以及"顺路之外的每个选择点"。**

## 2. 分层诊断

按 Agent 一次循环的认知顺序分层：环境 → 提交 → 协议 → 观测 → 定义 → 文档。
每层给：现象 / 证据 / 弱模型的后果 / 建议。

### 2.1 环境层：还没开始就断

**E1. 输出编码与 locale 耦合（P0）**

- 现象：`print()` 走 locale 编码（中文 Windows = cp936）。任何被管道/子进程捕获的输出都会变成乱码；`json.dumps(..., ensure_ascii=False)` 的中文同样。
- 证据：`PYTHONUTF8=1` 时 166 项测试全过；不设时 12 项失败，报 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xb8`；`test_usability` 另有 `Path.read_text()` 用 GBK 读中文 JSON 直接崩。
- 后果：Agent 读到的 prompt、feedback、title 全是 `����`；它会在"看不懂任务"上浪费整轮。
- 建议：`main()` 入口统一 `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")`；所有 `open()` 读取用 `utf-8-sig`；测试显式传 `encoding="utf-8"`；CI 增加 `PYTHONUTF8=0` / `LANG=C` 矩阵。

**E2. 命令入口在目标项目里不存在（P0）**

- 现象：Skill 说"优先用本仓库启动入口 `engine/bin/aiwf`"（`SKILL.md:58-60`），但工作流定义和运行记录都在**目标项目**里；`cd` 到项目后这个相对路径就不存在了。
- 证据：本次实测中，`cd` 到项目目录后 `.\engine\bin\aiwf.cmd` 直接 `CommandNotFoundException`；弱 Agent 是靠外部给的绝对路径才跑起来的。
- 后果：弱模型会在"命令找不到"上循环，或误用系统里另一个版本。
- 建议：`aiwf init` 在项目里生成 `bin/aiwf.cmd` / `bin/aiwf` 启动器（写死解释器与 engine 路径），Skill 只教 `./bin/aiwf`；或 `aiwf skills install` 一并装启动器。再加 `aiwf doctor` 一条命令自检（python、编码、schema 目录、library、skill 版本、`.aiwf`）。

**E3. 打包缺 `schemas/`、`library/`、`skills/`（P0）**

- 证据：`engine/aiwf.egg-info/SOURCES.txt` 只含 `engine/aiwf/*.py` 与 tests；`pyproject.toml` 没有 package-data；`schema._default_schema_dir()`（`schema.py:9-12`）和 `library.library_dir()`（`library.py:45-55`）都相对**仓库根**解析。
- 后果：`pip install` 后第一条 `aiwf validate` 就 `FileNotFoundError: .../schemas/workflow.schema.json`（非 editable 安装时）。Skill 里"或 pip install"这条路是断的。
- 建议：把 schema 收进包内（`engine/aiwf/schemas/`），library/skills 作为 package data 或安装时显式报"当前是精简安装，模板库不可用"；`--version` 同时打印 schema 目录。

**E4. 文件读取不容错 BOM（P0）**

- 现象：`--input-file`、`--output-file`、`--data-file`、`--tokens-file` 全部 `json.load(open(..., encoding="utf-8"))`。
- 证据：PowerShell 5.1 `Set-Content -Encoding utf8` 写出的文件带 BOM → `error: Unexpected UTF-8 BOM (decode using utf-8-sig)`。而 `view.md:224-227` 给 Windows 用户的**唯一替代方案**正是 `--output-file`，等于把用户从坑里指到另一个坑。
- 建议：所有 JSON 读取统一 `utf-8-sig`；BOM 不算错误。

**E5. Skill 安装只认 `.trae`（P1）**

- 现象：`aiwf skills install` 默认 `--target .trae/skills`（`cli.py:832-848`），Skill 正文也写 `.trae/skills/aiwf`。
- 后果：换一个 agent 宿主（Claude Code / Codex / DSH 等）就要人肉找目录；弱模型会装到错地方，或以为"技能已安装"其实没生效。
- 建议：`--target` 支持预设（`trae` / `claude` / `codex` / `dsh` / `custom:<dir>`）或自动探测已存在的 skills 目录；文档不再写死 `.trae`。

### 2.2 提交层：产物怎么交

**S1. 内联 JSON 在 PowerShell 必炸，而这是文档的主推写法（P0）**

- 证据：照抄 `run-workflow.md:70` 的 `--output '{"verdict":"REVISE","reason":"..."}'`，到 CLI 变成 `{verdict:REVISE,reason:...}` → `jsonschema.ValidationError` traceback。`view.md:224` 已承认这个现象，但纠正措施写在"视图的已知边界"里，执行指南没有。
- 后果：弱模型会认为"工具坏了"，或反复重试同一行。
- 建议：**顺路命令不出现内联 JSON**。新增一等参数：
  - `--file PATH` → 自动 `{"$file": PATH}`（并做路径规范化与契约校验）
  - `--verdict PASS|REVISE --reason "..."`（check）
  - `--set k=v`（可重复，值按 JSON 解析）（通用对象）
  - `--json-file F`（复杂对象；`--output-file` 保留为别名并明确"这是结果 JSON，不是正文"）
  - `--output` 保留给高级用户；若文本以 `{`/`[` 开头却解析失败，报 **"看起来是 JSON 但被 shell 改了引号，请改用 --json-file"**，而不是落到"字符串"再被 schema 打回。

**S2. 最常用的"一个文件"没有专用入口（P0）**

- 现象：`output.file` 声明的任务，实际结果要写成 `{"$file": "..."}`；`--output-file` 名字看起来像"把结果写到这个文件"，实际是"从这个文件读结果 JSON"（`outputs.md:143-144` 专门用一句纠正）。
- 证据：弱 Agent 为两次上报各写了一个 `result-*.json`，并卡在反斜杠转义上。
- 建议：`aiwf submit --file <产物路径>`（见 §4.2）。文件名/参数名要"望文生义"，不要靠文档纠正直觉。

**S3. `$file` 路径的反斜杠与相对路径（P1）**

- 现象：`node.output.file` 由 `os.path.join` 渲染，Windows 上是 `.aiwf\runs\.../opening.md`（正反斜杠混用）；直接进 JSON 会被 `\r` 吃掉。
- 证据：弱 Agent 的 `CONFUSION` 第 1 条；我实测把该路径原样塞进 `--output`，引擎报 `expected existing file '...', got None`（`\r` 被解析成回车）。
- 建议：spec 里统一输出**正斜杠**路径（或同时给 `file` 与 `file_posix`）；`--file` / `$file` 一律 `normpath` 后比较；拒绝时打印"你给的是 X，声明的是 Y，差在第 N 个字符"。

**S4. 产物被驳回仍 exit 0（P0）**

- 现象：`_reject_output`（`engine.py:2223-2246`）把原因塞进 `last_feedback`，run 保持 running，CLI 返回 0。
- 证据：`complete --empty` 打在有 `output.file` 的任务上，输出一整份新 spec、`feedback: "expected existing file ..., got None"`、**exit 0**。
- 后果：弱模型用 `&&` 或看退出码判断成功，会把"原地踏步"当"过关"，继续下一步或结束。
- 建议：驳回用独立退出码（如 3）并在文本输出首行写 `REJECTED: 产物未通过契约` + 期望路径 + 一条可执行命令。

### 2.3 协议层：下一步做什么

**P1. `action.kind` → 命令的翻译全靠人（P0）**

- 现状：spec 只给 `{"kind": "check", "id": "...", "node_id": "..."}`（`engine.py:1673-1676`），没有命令、没有期望载荷。
- 证据：`print_spec` 在 `awaiting_check` 时只打印 `action : check --action <id>` 这一行片段和 `check_request` JSON 就 `return`（`cli.py:95-101`），**不给完整的 `aiwf complete ...` 命令**；`awaiting_input` / `awaiting_review` 只给 "then: aiwf provide/review ..." 的省略号；只有 `runs show` 的 view 门命令（`_gate_command`，`cli.py:301-318`）是完整的。同一个"下一步"，三个地方三种完整度。
- 建议：action 统一携带 `next`（见 §4.1）；文本输出每种 phase 都打印可直接复制的一行；view 的门命令与 spec 复用同一函数。

**P2. action id 不可读、不可省（P0）**

- 现状：24 位 sha256 前缀；`act()` 严格要求 `--action` 与当前一致（`engine.py:1715-1723`）。
- 后果：漏传、旧传、复制错一位，都会得到同一句 `missing or stale action; fetch current --json again`——不说是哪个 run、哪个节点。
- 建议（三档，可组合）：
  1. 报错带上下文与可执行命令：`run <id> 的 action 已变（当前节点 write，第 2 次尝试）；重新取：aiwf current <id>`；
  2. `--action` 接受**唯一前缀**或当前节点 id；
  3. 允许省略 `--action`：引擎每次状态变更时把当前 action 写进 run 目录的 `submit.json`（`current` 仍只读，只打印它）；`submit` 用模板里的 action 与当前状态比对，不一致才拒绝。这样既保留防误报，又不需要复制。

**P3. run_id 必须精确复制，选择器缺失（P0）**

- 现状：所有命令都要完整 `run_id`；`find_run` 精确匹配（`storage.py:529-534`）；`resume` 还要再给 `workflow_id`（`cli.py:1044-1048`）。
- 建议：`current` / `complete` / `submit` / `show` 的 run 参数改可选——唯一 running → 用它；否则用最近一个；多个才报错并列出 title。支持唯一前缀。`resume RUN_ID`（workflow 从 record 推）。`runs show` 无参 → 最近一次。

**P4. 一个动词干三种事，退出码不表达结果（P1）**

- 现状：`complete` 同时承担 task 产物、check verdict、start/end 空推进；`provide`/`review` 各管一种。`main()` 只捕获 4 类异常（`cli.py:1189`），其余 traceback。
- 建议：保留 `complete` 兼容，新增按 kind 命名的入口（`task` / `check` / `accept` / `answer`），或统一 `submit`；退出码表见 §4.3。

**P5. 重复提交无幂等提示（P2）**

- 现状：completed 之后再 `complete` → "missing or stale action"（误导）；网络重试/并发重放无法区分。
- 建议：action 已被消费且输出与审计中最后一次一致 → 返回 `already_applied: true` + 当前 spec；不一致才报 stale。

**P6. `run --title` 必填（P2，需主人拍板）**

- 现状：`--title` 是 argparse 必填（`cli.py:982-984`）；这是 2026-10-11 定下的语义——它认领这次运行。
- 后果：弱模型漏传就整条命令失败，要重试一次；而它其实给不出有意义的标题。
- 建议：保留 `--title` 语义，但允许省略：默认 `<workflow name> · <日期>`，并在输出里标明 `title auto`；`view.dynamic_title` 有值时仍会覆盖。是否放开需要拍板。

**P7. `--json` 不是全局参数（P2）**

- 现状：`--json` 只加在部分命令上；`aiwf runs events <id> --json` 报 `unrecognized arguments: --json`，并把 16 个命令全打一遍。
- 后果：弱模型容易"给所有命令机械追加 --json"（`run-workflow.md:129-130` 甚至专门提醒不要这么做——这本身说明接口不直觉）。
- 建议：根解析器接受任意位置的 `--json`（argv 归一化），并让所有只读命令都支持它。

### 2.4 观测层

| 现象 | 证据 | 建议 |
|---|---|---|
| 运行中 `runs list` 打印 `None ms` | `cmd_runs_list` `str(dur)` 直接拼接 | `-` 或实时耗时 |
| 完成态 `runs show` 打印 `completed / executing` | `cli.py:211` 无条件打 phase | 非 running 不显示 phase |
| 没有 `.aiwf` 时 `validate --project` 打印 `valid project: 0 workflow(s)` 并 exit 0 | `cli.py:458-465` | 明确 "未找到 .aiwf/workflows，先 aiwf init" |
| `runs list` 无 limit，历史多时刷屏 | 无 `--limit` | 默认 10，`--all` 展开 |
| 失败 run 的 `error` 只在 `runs show` 里 | — | `runs list` 失败行尾带 error 摘要 |
| `context.md` 没有验收结论 | 弱 Agent 反馈 | 产物索引 + 最近一次 review/feedback 摘要 |

### 2.5 定义层：作者侧

**D1. 没有脚手架（P1）**

- 现状：新建 = 从 0 写 JSON，7 种节点类型、约 24 个字段、`additionalProperties: false`。
- 证据：`create-workflow.md` 的 30 行例子可用，但"改一个字段名"就会得到 68 KB 回显；模板库只有 3 个 `experimental` 模板，且都是完整业务流，不是"最小骨架"。
- 建议：`aiwf new <id> --kind sequential|checked|batch` 生成 10–20 行可运行骨架；库补 3 个最小模板（不带业务味道）。

**D2. 本该有默认值的必填项（P1）**

| 字段 | 现状 | 建议 |
|---|---|---|
| `name` | 必填 | 默认 = `id` |
| `check.max_attempts` | 必填 | 默认 3（schema 描述里已写 default，只是没生效） |
| `output` | 只收 `{"file": ...}` | 接受字符串简写 `"path"` |
| `inputs` | `{"a": {"ref": "a"}}` | 支持 `"use": ["a", "b"]` 同名字段简写 |
| `schema_version` | 必填 1.0/1.1 | 保持必填，但错误信息说明"新定义写 1.1" |

`use` 简写能直接消灭 `ref` 与 `${}` 的最大二义：`inputs` 只声明材料，`${}` 才插值，`use` 只做同名搬运。

**D3. 校验错误无定位（P0，同 E1 的错误协议）**

- 现状：jsonschema 原始错误 + schema 回显；`validate` 不说是哪个文件、哪个节点、哪个字段（多文件项目尤其难）。
- 建议：统一格式 `文件:路径 → 一句话 + 期望 + 提示`，例如：
  `nodes[0].output: 需要对象 {"file": "..."}，收到字符串 "out.md"；改成 {"file":"out.md"}`。
  常见笔误做一次"did you mean"（`output_file` → `output`，`inputs_schema` → `input_schema`）。

### 2.6 文档层

- **预读成本**：`SKILL.md` 47 行 + 14 份 reference 1,300+ 行；弱模型没有"读到哪算够"的判据。
- **平台差异放错位置**：PowerShell 引号坑写在 `view.md` 的"已知边界"，执行指南照旧给 POSIX 内联 JSON。
- **占位符要模型替换**：`<root_run_id>`、`<最新action.id>`、`<最新id>`；弱模型容易原样执行或错配。
- **副本漂移**：同一份技能在 `sim/*/.trae/skills/aiwf` 有安装副本，历史上出现过 9/12 文件 md5 不一致（见 `.workbuddy/memory/2026-10-11.md`）。
- **未测弱模型**：`sim/report-usability-20260923.md:10-11` 明确"没有开展无上下文 Agent 创建/接管实验"，这正是当前目标的缺口。

建议文档结构：

```
skills/aiwf/
  SKILL.md            # 30 行：要不要用 + 顺路 5 条命令 + 出错看 TROUBLESHOOT
  RUN-CARD.md         # 60 行：创建/启动/循环/质检/验收/批量 的完整命令
  TROUBLESHOOT.md     # 错误码 → 一行动作（按 code 索引）
  references/         # 现有 14 份，标注"遇到 X 才读"，不再要求预读
```

加 `aiwf skills status` / 版本戳：`SKILL.md` 带 `min_cli_version`，`--version` 不匹配就提示，不再靠人 md5。

## 3. 改造路线

### L0 热修（不改协议，约 1 天，收益最大）

| # | 改动 | 位置 |
|---|---|---|
| 1 | 强制 UTF-8 stdout/stderr；所有 JSON 读取 `utf-8-sig` | `cli.py:main`、各 `_read_*` |
| 2 | 异常边界：捕获 `jsonschema.ValidationError`、`argparse.ArgumentTypeError`；输出 `code/at/hint/next`；`--json` 下也输出结构化错误；**绝不打印 traceback / schema** | `cli.py:1184-1194`、`schema.py:24-31` |
| 3 | 退出码：0 接受 / 2 用法与输入 / 3 产物驳回 / 4 stale | `cli.py`、`engine.py:2223` |
| 4 | `--file`、`--verdict/--reason`、`--set`、`--json-file` | `cli.py` 参数与 `_read_output` |
| 5 | spec 文本输出每种 phase 都给一行可复制命令 | `cli.py:print_spec` |
| 6 | `runs list` 的 `None ms`、`runs show` 的 `completed / executing`、空项目 `valid project: 0` | `cli.py` |
| 7 | `--version`、`aiwf doctor` | `cli.py` |
| 8 | 打包补 schemas/library/skills；`init` 写项目内启动器 | `pyproject.toml`、`cmd_init` |
| 9 | 测试在 `PYTHONUTF8=0` 下也全绿 | `tests/*` |

验收：任一用户错误路径都满足 `"Traceback" not in stderr`、`len(stderr) < 2000`，且 stderr 里有 `hint:`。

参考改法（约 20 行，不动协议）：

```python
def _force_utf8() -> None:                      # main() 第一行
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

def _one_line(err) -> str:                      # 替代 str(ValidationError)
    path = "/".join(str(p) for p in err.absolute_path) or "(root)"
    return f"{path}: {err.message}"

# schema.py: _jsonschema_validate 把 jsonschema.ValidationError 包成 SchemaError(ValueError)，
# 这样 cli 不必在模块级 import jsonschema，也能统一捕获。
try:
    return args.func(args, storage)
except schema.SchemaError as e:                 # dry-run 校验、入参、verdict 都走这里
    return _error("E_INPUT_INVALID", str(e), hint="检查类型/必填/枚举后重试")
except argparse.ArgumentTypeError as e:         # _parse_param 目前漏在这里
    return _error("E_USAGE", str(e), hint="参数写成 key=value，例如 -p idea=末世")
```

`--file` 的最小实现就是：`normpath(abspath(path))` → `{"$file": path}`，再交给现有 `complete` 做契约校验。

### L1 协议人机工程（约 2–3 天）

- `action.next`（见 §4.1）；`print_spec` 与 view 共用命令生成。
- `--action` 可省（`submit.json` 承载）/ 唯一前缀 / 节点名。
- run 选择器：无参 → 唯一 running → 最近；唯一前缀。
- `resume RUN_ID`；`items --node` 在只有一个队列时可省。
- `runs list --limit`；`status` 一条命令（§4.5）。

### L2 submit 协议（约 3–5 天，弱模型收益最大）

- 引擎在每次状态变更时写 `submit.json` 模板（`current` 仍只读）；`aiwf submit` 自带 action、免 JSON 引号、自动归档（§4.2）。
- 保留 `complete`，SDK 不变。

### L3 定义与文档（约 1 周）

- `aiwf new` 脚手架 + 3 个最小模板；`name`/`max_attempts`/`output` 默认值；`use` 简写。
- 文档三件套（RUN-CARD / TROUBLESHOOT / 精简 SKILL）；示例命令纳入 Windows PowerShell 实跑测试。
- 弱模型基准（§7）。

## 4. 关键设计草案

### 4.1 spec 里的 `next`

```json
"action": {
  "id": "c5c2e2715306c71491734ffb",
  "run_id": "20261025-165913-0758df",
  "node_id": "outline",
  "kind": "complete",
  "expects": {"file": ".aiwf/runs/story-two-step/20261025-165913-0758df/outline.md"},
  "attempt": 1,
  "next": {
    "argv": ["aiwf", "submit", "--file", ".aiwf/runs/.../outline.md"],
    "text": "aiwf submit --file .aiwf/runs/.../outline.md"
  }
}
```

- `next` 由引擎算，不由模型拼；`check` 给 `--verdict PASS|REVISE --reason "..."`，`review` 给 `--approve|--reject --feedback`，`provide` 给 `-p key=value`，`fail` 给 `--error`。
- 文本模式直接打印 `next : <text>`；模型只需"看到就执行"。

### 4.2 `submit.json` + `aiwf submit`

每个会停下来的动作，引擎在 run 目录写一份**待填模板**：

```json
{
  "$hint": "把正文写到下面这个路径，保留 $file 不变；写完执行 aiwf submit",
  "$file": ".aiwf/runs/story-two-step/20261025-165913-0758df/outline.md"
}
```

check 时模板是 `{"$hint":"逐条核对 criteria 后填","verdict":"PASS|REVISE","reason":""}`。

```bash
aiwf submit                 # 唯一 running run + 当前 action + submit.json
aiwf submit --run <id>      # 多个 run 时指定
aiwf submit --file <path>   # 不用模板，直接交一个产物文件
```

- `submit` 读取 `submit.json` 里的 action（引擎写），与当前状态比对；校验载荷（剥掉 `$` 开头的提示键），成功后归档为 `submissions/0003.json`，再打印下一份 spec 与模板。
- 好处：消灭 action id 复制、JSON 引号、BOM、`$file` 形状四类错误，且不削弱审计。
- 错误码：`E_STALE_ACTION`（上次看到的 action 与现在不一致）、`E_OUTPUT_FILE`、`E_OUTPUT_SHAPE`、`E_INPUT_MISSING`、`E_CHECK_EXHAUSTED`、`E_UNKNOWN_RUN`、`E_AMBIGUOUS_RUN`。

### 4.3 错误与退出码

文本：

```
REJECTED 产物未通过契约（run 20261025-...，节点 outline，第 1 次）
  at   : node.output.file
  want : .aiwf/runs/.../outline.md
  got  : (没有 $file)
  hint : 先写文件，再执行下面这行
  next : aiwf submit --file .aiwf/runs/.../outline.md
```

JSON：

```json
{"ok": false, "error": {"code": "E_OUTPUT_FILE", "message": "...", "at": "node.output.file",
 "hint": "...", "next": {"argv": [...], "text": "..."}}}
```

| 退出码 | 含义 | Agent 应对 |
|---|---|---|
| 0 | 已接受（含推进到等待门、含 check 判 REVISE） | 读下一份 spec |
| 2 | 用法/定义/入参错误，状态未变 | 按 `hint` 改，重试 |
| 3 | 产物被驳回，状态未变 | 按 `want/got` 修产物，重试 |
| 4 | action 过期/不匹配 | 重新 `current`，确认节点与产物 |
| 1 | 内部错误 | 报告，不重试 |

文案约定：`code` 用稳定英文枚举；`message / hint` 用中文（与文档一致），不要中英混排；
`at` 用 JSON Pointer 或点路径（`nodes[0].output`），保证可 grep。

### 4.4 `aiwf new` 与最小模板

```bash
aiwf new my-flow --kind sequential   # 两个 task，自然结束
aiwf new my-flow --kind checked      # task + check + review
aiwf new my-flow --kind batch        # foreach + 单文件产物
```

生成 10–20 行、`validate` 必过、可直接 `run` 的骨架。弱模型改模板的成功率远高于从 0 写 schema。

### 4.5 `aiwf status`：弱模型唯一需要的只读入口

```text
run  20261025-165913-0758df   《钟表匠与怀表》   running
now  [write] 写第三章
do   写 .aiwf/runs/.../ch3.md，然后执行下面这行
next aiwf submit --file .aiwf/runs/.../ch3.md
note 质检 REVISE（第 2 次，剩 1 次）：字数不足
```

一行 title、一行当前节点、一行 `do`、一行 `next`、一行"为什么停在这"。
`--json` 时就是完整 spec 加 `title` 与 `next`。它把 `current` / `runs show` / `view` 的信息按"现在该干什么"重排，
弱模型不需要在三个命令之间选。

### 4.6 一页 RUN-CARD（草稿）

```markdown
# aiwf 执行卡
入口：本项目 ./bin/aiwf（没有就 aiwf init）；始终加 --json。

创建：aiwf new my-flow --kind checked → 改 prompt/产物/验收 → aiwf validate --project
启动：aiwf run my-flow --title "这次做什么" -p idea="..." --json
循环：
  1) 读 spec 的 action.next，照抄执行
  2) 要交文件：写 node.output.file，再 aiwf submit --file <该路径>
  3) 要交结论：aiwf submit --verdict PASS --reason "依据"
  4) 人工验收：把 review_request 给用户，按结论 aiwf submit --approve/--reject
  5) 出错：读 error.code → TROUBLESHOOT.md 对应一行
禁止：手拼 --output JSON；跳过 check；替用户 APPROVE；读 engine 源码猜行为。
```

### 4.7 命令面收缩

| 场景 | 现在 | 建议 |
|---|---|---|
| 顺路（建/验/跑/交/验收） | 8 个动词 + 大量参数 | `new` `validate` `run` `current` `submit` `answer` `accept` `runs` |
| 高级（修订/队列/模板） | 26 个 | 保留，文档降级为"遇到才读" |

## 5. 该保留的设计（别在简化时拆掉）

这套东西里有一半是"正确但难用"，需要的是换界面，不是换内核：

| 保留 | 理由 |
|---|---|
| action 指纹防误报 | 弱模型最容易把上一步的结果交给下一步；这个保护要留，只是改由 `submit.json` 承载，不让人复制 |
| 根 run 直达叶子任务包 | `current` 不要求执行者维护父子栈，是对的；继续把"叶子动作"作为唯一入口 |
| view 的门命令 | 已经是"把命令算好给人"的正确做法，把它推广到所有 phase 即可 |
| typed ref + 显式材料映射 | 类型保留、材料可见是引擎相对 prompt 编排的核心价值；补 `use` 简写降低写法成本 |
| 定义快照 + 事件流审计 | 恢复与追责的基础；弱模型出错时这是唯一能复盘的东西 |
| 复制式模板安装 + 来源记录 | 让项目可改、可对比上游；保留，补最小骨架 |

## 6. 不建议做的

- **不要加 YAML / 表达式语言 / 通用 DSL**：不解决"错误不可读、下一步不可见"的根因。
- **不要用更多文档弥补**：1900 行已经超出弱模型的预读预算；应改成"错误码 → 一行"。
- **不要隐藏错误或自动重试**：驳回、stale、审核未过必须显式失败，弱模型最怕"看起来成功了"。
- **不要自动批准 review / 自动 PASS check**：这是产品底线。
- **不要为了简化删掉审计字段**：run_id/action/事件流是恢复与追责的基础，保持，只是不要求人来搬运。

## 7. 怎么验收"确实更简单"

固定 3 个任务（顺序 + 文件产物；带 check + review；foreach 批量），每个任务换一个**无上下文** Agent 只读 `RUN-CARD.md` 执行 3 次，记录：

| 指标 | 目标 |
|---|---|
| 完成率 | 3/3 任务 × 3 次 |
| traceback 次数 | 0 |
| 单任务 aiwf 命令数 | ≤ 节点数 × 2 + 3 |
| 需要读 RUN-CARD 以外的文档 | ≤ 1 次（且因错误码触发） |
| 人工介入次数 | 只在实际验收点 |
| 平均 token（含读文档） | 比现在低 50% |

另外保留当前弱 Agent 的日志（本次 12 命令 / 0 报错）作为基线，防止"改简单了但完成率下降"。

## 8. 附：本次实测的关键输出

```
$ aiwf validate bad1.json
Traceback (most recent call last):
  ...
jsonschema.exceptions.ValidationError: 'name' is a required property
Failed validating 'required' in schema: { ... 67,992 字符 ... }
（PowerShell 落盘合计 141 KB）

$ aiwf complete <run> --action <id> --output '{"verdict":"PASS","reason":"ok"}'
jsonschema.exceptions.ValidationError: '{verdict:pass,reason:ok}' is not of type 'object'
（PowerShell 剥引号）

$ aiwf current <run>          # phase=awaiting_check
action   : check --action 153e54ee0b40dde019950629 (target ...)
quality check (agent):
{ ... criteria ... }
（没有给下一步命令）

$ aiwf runs list
末世3   2026-10-25T16:56:13  20261025-165613-822d33  opening  running  None ms  0 nodes
```

> 说明：上面的输出是**改造前**的实测证据（L0 之前的基线）。`.tmp-*` 是当时的临时目录，已清理。

## 9. L0 实施记录（2026-10-25）

L0（不改协议的热修）已落地，验证为 **182 passed / 20 subtests**，且在
`PYTHONUTF8=1` 与默认中文 Windows（不设该变量）两种环境下都全绿。

| L0 项 | 实施 |
|---|---|
| UTF-8 输出 | `main()` 先 `reconfigure(encoding="utf-8")`；所有 JSON 读取改 `utf-8-sig`（含 `--input-file` / `--output-file` / `--json-file` / storage / library / schema） |
| 错误协议 | 新增 `aiwf/errors.py`（`AiwfError` / `SchemaError` / `UsageError` / `StaleActionError` / `NotFoundError` / `InstallError`）；`main()` 统一格式化并保证不打印 traceback（`AIWF_DEBUG=1` 才显示）；jsonschema 错误只保留一句 + `at`，不再回显 schema |
| 退出码 | 0 接受 / 2 用法与输入 / 3 产物驳回 / 4 stale / 1 运行失败 |
| 提交参数 | 新增 `--file`、`--verdict/--reason`、`--set`、`--json-file`；`--output` 保留，但"看起来是 JSON 却解析失败"会提示 shell 剥引号 |
| `next` 命令 | 每个 spec 新增 `next: {kind, text}`；文本模式每个 phase 末行打印 `next     : …` |
| 观测修正 | `runs list` 运行中耗时显示 `-`；`runs show` 完成态只显示 `completed`；空项目 `validate --project` 退 2 并说明 |
| 新命令 | `--version`；`aiwf doctor`（python/jsonschema/schemas/library/skills/encoding）；`runs events/search --json`；`skills list --json` |
| 入口 | `aiwf init` 生成项目内 `bin/aiwf`（POSIX）与 `bin/aiwf.cmd`（Windows），固定解释器与 engine 路径 |
| 分发 | `pyproject.toml` 修 `readme` 并加 data-files；新增 `aiwf/paths.py` 解析 `<prefix>/share/aiwf/{schemas,library,skills}`；wheel 安装实测可 `validate` / `run` |
| 测试 | 新增 `tests/test_l0_cli.py`（16 项）；既有测试补显式 UTF-8 编码；`uv build` 产出 wheel 可在干净 venv 运行 |

与提案的两处刻意偏差：

1. **错误 JSON 仍保留字符串 `error`**（同时加 `code/at/hint/next`），避免破坏已有消费者；
   提案里的嵌套结构留到 L1 再统一。
2. **`--action` 缺失仍是用法错误（2）**，只有"给了但与当前状态不符"才是 stale（4）；
   L1 再做"可省略 / 前缀匹配"。

L1 未做：`action` 可省、run 选择器、`resume RUN_ID`、`items --node` 可省、`runs list --limit`、
`aiwf status`、`submit.json` 工作流。L3 未做：`aiwf new` 脚手架、`use` 简写、文档三件套、弱模型基准。

