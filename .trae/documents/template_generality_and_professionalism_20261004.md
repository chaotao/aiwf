# 模板的通用性与专业性：诊断与方案

日期：2026-10-04
状态：**决策 6（通用性）与决策 7（专业性）均已拍板 A 并落地**——项目级 `run_defaults` + 模板写法约定（第 5、6 节）。
前置文档：`workflow_template_library_20261004.md`（模板库机制本身，已落地）。

---

## 0. 结论

主人指出的两个问题成立，且在机制层各有明确落点：

| 问题 | 本质 | 机制现状 | 后果 |
|---|---|---|---|
| **通用性不足** | 项目之间的**固有差异**（项目目录、知识库路径、测试命令）无处存放 | 项目级常量**只有一个入口：每次启动手填入参**；引擎无项目配置层，且**明确不支持**环境变量注入 | 同一模板换项目，值要重填；写死就只能改模板 |
| **专业性不足** | 模板里没有承载"**这个领域该怎么做**"的位置 | 领域步骤只能塞进 `prompt` 散文或 `check.criteria`，二者都不是"步骤清单"的合适位置 | 模板质量**完全取决于作者**，无法积累、无法校验、无法复用 |

一句话：**专业性不足让模板不值得复用，通用性不足让模板无法复用。** 前者是内容问题，后者是机制问题——必须先解决机制问题，否则每个模板都得靠项目改。

---

## 1. 实证：现状机制能做什么、不能做什么

### 1.1 变量只有两个来源

`code-change.json` 的写法是当前唯一的"通用化"手段：

```json
"input_schema": {
  "type": "object",
  "required": ["project_dir", "request"],
  "properties": {"project_dir": {"type": "string"}, "request": {"type": "string"}}
}
```

`project_dir` 被声明为**必填启动入参**，每次运行由人提供：

```
aiwf run code-change -p project_dir=F:/data/project/github/xxx -p request="..."
#                                              ^^^^^^^^ 项目固有常量，每换一个项目就要重填一次
```

入口只有这两个（`cli.py:691-693`）：`-p/--param`、`--input-file`。

### 1.2 引擎没有项目配置层

grep 全仓 `config.json` / `run_defaults` / `load_config`：**无匹配**。项目级配置这一层目前不存在。

### 1.3 环境变量这条路是明确堵死的

`data-reference.md:95`（"都不是自动可用的 ref 来源"清单）：

> - shell 环境变量（含 LOCAL_REPO_ROOT）：Agent 先读取，再作为启动输入传入。

即：平台**有意**不让定义直接读环境变量。这个设计本身是对的（同一个定义在不同 shell 下行为不同，会与 run 启动快照的复现性冲突）。所以通用性不能靠"透传 env"解决。

### 1.4 `${run.dir}` 是唯一自动可用的路径类变量

`data-reference.md:81` 只列了两个运行时自动提供的键：`run.id`、`run.dir`。`run.dir` 是**当前运行目录**，与"项目目录"无关。因此现在能自动拿到的只有"我这次运行放东西的地方"，拿不到"我在哪个项目里工作"。

### 1.5 专业性当前只有两个落点

看 `code-change.json` 的"专业"部分：

```json
"prompt": "读取目标项目约定并定位 request 的问题，修改真实源文件，运行与变更相称的验证。……不要虚构未执行的验证。",
"check": {
  "criteria": ["修改解决了原始问题且没有无关变更",
               "验证报告文件存在，记录真实命令与结果",
               "validation.exit_code 为 0 且验证覆盖关键边界"],
  "max_attempts": 3
}
```

写得不错，但注意：**"读取目标项目约定"这句所依赖的"项目约定"是什么、在哪、怎么读，模板无法声明**。它全靠 agent 自己临场判断。同理"运行与变更相称的验证"——哪些命令算相称，模板里没有位置可写。

`check.criteria` 表达的是**合格标准**，不是**应执行的步骤**。把步骤塞进 criteria 会让它语义混杂（既当 checklist 又当验收线）。

---

## 2. 问题一：通用性 —— 三个方案

### 方案 A：项目级默认值（`.aiwf/config.json` 的 `run_defaults`）

模板写法不变（仍声明 `input_schema` 必填），值改由项目提供：

```json
// <project>/.aiwf/config.json
{
  "run_defaults": {
    "project_dir": "F:/data/project/github/xxx",
    "knowledge_dir": "docs/knowledge",
    "test_cmd": "python -m pytest -q"
  }
}
```

合并序：`run_defaults` ← `--input-file` ← `-p`（后者覆盖前者）。
`validate --project` 顺带报告：模板要求的必填键，项目配置里有没有。

- **好在哪**：零语义变更（context 的内容和来源不变，只是"默认从哪来"多了一层）；改动小（读取 + 合并，约 20 行）；跨模板天然共享（同一个键被多个模板用到时只写一次）。
- **代价**：模板仍必须把这类键声明成 `input_schema` 必填，否则拿不到。
- **适合**：现在。它正好解决"同项目重复填同样的值"，而不引入任何新的命名空间。

### 方案 B：A + 引擎注入保留命名空间 `${project.*}`

模板可直接写 `${project.knowledge_dir}`，不必声明为启动入参：

```json
"prompt": "先读 ${project.knowledge_dir} 下的约定，再改 ${project.dir} 里的代码。"
```

- **好在哪**：模板表达力最强；"项目固有差异"与"本次运行参数"在语义上彻底分开。
- **代价**：新增保留键（要与 `run` 并列）；要定义缺键行为（报错还是空值——按现有"缺失路径报错"的规矩，只能报错）；模板仍需声明"我依赖哪些 project 键"，否则运行到一半才炸；`validate --project` 要跟着扩展。
- **适合**：等出现 ≥2 个模板共用同一个项目级键（例如 `knowledge_dir`），并且这个键在模板里出现多次。**现在上，是在给一个还没出现的模式造机制。**

### 方案 C：不加配置层，模板自带"项目适配清单"

在 meta 里声明模板对项目的假设，首次运行时由 agent 询问用户并落到本地输入。

```json
// library/meta/std.xxx.json
"requires": {"project_dir": "目标项目根目录", "knowledge": "项目知识库位置（可选）"}
```

- **好在哪**：无引擎改动；`wf show` 能提前暴露"装之前先确认你有没有这些"。
- **代价**：值散落在每次 run 的输入里，**不可复用**——这次填了下次还要填，正是要解决的问题；每个新项目多一轮人机交互。
- **适合**：作为 A 的**补充**（用 `requires` 做文档与提示），不适合作为替代。

### 推荐

**先做 A。** 判据：A 恰好消灭"同项目重复填值"，而它不引入新语义；B 是 A 的超集，等出现真实的跨模板共享键（有 ≥2 个实例）再上，那时才知道保留命名空间该怎么设计。C 的信息可以并进 `meta`，与 A 不冲突。

---

## 3. 问题二：专业性 —— 为什么机制解决不了，以及候选沉淀位

**先说清楚：这一项不是靠加字段能解决的。** 一个模板专不专业，取决于作者是否知道该领域的做法。机制能做的是**给专业内容一个可积累、可校验的位置**。

现在这类知识只有两个去处，都不合适：

| 现位置 | 它表达什么 | 塞进去会怎样 |
|---|---|---|
| `prompt` 散文 | 任务描述 + 临场要求 | 步骤埋在自然语言里，不可枚举、不可校验、下次要重写一遍 |
| `check.criteria` | 合格标准 | 语义混杂：既要写明"做什么"，又当验收线 |

候选沉淀位置（按"是否值得现在做"排序，**本次不拍板**）：

1. **`prompt` 里显式的步骤清单**（零机制改动）——把"读知识库 → 定位 → 改 → 跑与变更相称的验证"写成有序步骤，而不是让它隐含在句子里。先用现有字段把质量做出来，成本最低。
2. **`meta` 增加 `assumes`（模板对项目的假设）**——例如"假定项目有自动化测试""假定有 AGENTS.md/知识库"。让 `wf show` 能在装之前告诉你"这个模板假设你有 X，没有就别装"。这既服务专业性（把假设说清楚），也服务通用性（A 的补充）。
3. **`check` 增加独立的步骤字段**（新 schema 字段）——例如 `steps: [...]`，与 `criteria` 分开：前者是"要做哪些事"，后者是"做到什么算好"。**代价是改 schema 与快照兼容**，且现在还没有足够的真实模板来证明这个字段的形状——容易设计错。

**建议**：在解决通用性（A）之后，先用 1 + 2 做 2–3 个真实模板；如果那时反复发现"步骤"无处安放且形态稳定，再谈 3。

---

## 4. 顺带发现（待确认，本次不改）

`inputs` 字段的描述三处不一致：

| 位置 | 说法 |
|---|---|
| `schemas/workflow.schema.json:253-255` | `"[subflow\|foreach] Values passed into the child"` |
| `skills/aiwf/references/data-reference.md:16` | `task / branch \| inputs \| 材料名称 → 字面值或引用，渲染后出现在任务包` |
| 实际用法 | `code-change.json:18`、`game-resources.json:31/36`、`multi-file.json:15`、`condition-routing.json:15` 都在 **task 节点**上用 `inputs` |

schema 的 `node.properties` 里列了 `inputs`，所以 task 用它不会被拒（`additionalProperties: false` 只拦未列出的键）；但描述文字说只给 subflow/foreach，与文档和四个演示件的实际用法矛盾。这是**描述文本**的不准确，不影响校验行为——但会误导模板作者。

---

## 5. 决策 6：已拍板 A 并落地（2026-10-04）

主人选 **A**。以下是**已实施**内容，不是计划。

**决策 6：模板怎么拿到"项目固有常量"？** —— A（项目级 `run_defaults`，合并序覆盖，`validate --project` 顺带检查）。

### 落地的文件

| 文件 | 改动 |
|---|---|
| `engine/aiwf/storage.py` | 新增 `config_path` / `load_config()` / `run_defaults()`；布局文档补 `config.json`；新增 `CONFIG_NAME` 常量 |
| `engine/aiwf/cli.py` | 新增 `_declared_inputs()` 与 `_report_run_defaults()`；`_collect_inputs(args, workflow, storage)` 加 defaults 层；`cmd_validate --project` 追加建议性提示 |
| `skills/aiwf/references/templates.md` | 新增「项目固有的值：run_defaults」一节 |
| `skills/aiwf/references/data-reference.md` | 「ref 从哪里取值」表下补一句默认来源 |
| `tests/test_run_defaults.py` | 9 个用例 |

### 实现时定下的三个细节（原方案未写明）

1. **过滤**：只注入定义 `input_schema` 的 `properties` / `required` 声明过的键。一个项目的配置不会串进
   另一个模板的 context；同一个 `project_dir` 可被多个模板共用。
2. **逐键覆盖**：`-p` 只覆盖它指定的那一个键，其余键仍取配置值——实测 `-p subject=...` 后 `criteria` 仍来自配置。
3. **提示不是失败**：两条建议都以普通行打印，退出码仍是 0；完全覆盖时 `validate --project` 保持安静
   （不出现任何 `run_defaults` 字样）。

### 实证

- `tests/test_run_defaults.py` **9/9 通过**；全量 **89/89 通过**（原 80 + 新 9）。
- 手工核过 context：未声明键（`project_dir` / `typo_key`）**确实没有进入** context，只剩 `criteria` / `run` / `subject`。
- 必填项未被覆盖时 run 仍然失败——校验没有被放松。
- 坏配置：非 JSON → `error: invalid config.json: ...`；非对象 → `must contain a JSON object`；均 rc=2。

### 明确未做

- 不新增 `${project.*}` 保留命名空间（方案 B）。触发条件：出现 ≥2 个模板共用同一项目级键。
- 不改 run 记录格式、journal、启动快照；`run_defaults` 只影响**顶层 run 的启动输入**。

---

## 6. 决策 7：已拍板 A 并落地（2026-10-04）

主人选 **A：只做写法约定**（`prompt` 写有序步骤 + 项目固有值抽成入参），零机制改动。

### 为什么不是 A+/B/C

三个候选都指向"新增机制"，但实测发现**运行期这条路已经通了**：入参 → `run_defaults` → `${}` 插值，三段俱全。
缺的不是机制，是**写法**——现有模板把 6 件事塞进一句散文，专业细节（约定在哪、验证怎么做）无处安放。
所以本轮产出是**示范 + 约定**，不是新字段。

### 改了什么

| 文件 | 改动 |
|---|---|
| `library/workflows/std.review-check.json` | `prompt` 改写成 4 步有序步骤（先完整读 → 逐条指位置 → 判满足 → 输出 JSON） |
| `library/workflows/std.review-loop.json` | 新增 `conventions_file` / `test_cmd` 项目固有入参；`prompt` 改 5 步；`check.criteria` 加一条"实际执行了验证" |
| `skills/aiwf/assets/examples/code-change.json` | 同上（演示件 → 生产写法的首个样本） |
| `skills/aiwf/references/examples.md` | `code-change` 启动输入速查补两个新键；注明项目固有值应放 `run_defaults` |
| `skills/aiwf/references/templates.md` | 新增「写模板：专业性与通用性落在哪」——步骤写法、`"none"` 约定、材料别名不能插值 |
| `tests/test_skill_examples.py` | `code-change` 用例补两个新必填入参 |

### 一条被强制的约定：用 `"none"` 代替"可选"

引擎不区分"键没给"与"键给了空值"——被 `${}` / `ref` 引用的键缺值就硬失败；而 `validate --project`
对只写在 `properties`、没进 `required` 的键**完全静默**。所以"项目可能没有的能力"不能用 optional 表达，
必须放 `required` + 描述写"没有就填 `"none"`" + prompt 给 `none` 分支。

**副作用（正面的）**：这样 `validate --project` 会提示它未被配置覆盖——恰好补上了上一轮发现的"optional 检测静默"缺口。

### 实证

- 全量 **89/89 通过**（`test_skill_examples` 的 `code-change` 用例已同步补参）。
- 端到端：临时项目 `wf install std.review-loop` → 配 `run_defaults`(project_dir/conventions_file/test_cmd)
  → `run -p request=...`。命令只传 `request`，渲染出的 prompt 里是**实际路径与实际命令**：
  `在项目 C:/.../aiwf-pro-1 中完成…` / `执行 python -m pytest -q`。
- `std.review-check` 独立跑，多行 `criteria` 正确插入，步骤完整。

### 本轮新暴露的一处提示噪音（未改，待定）

`validate --project` 把 `std.review-check.subject` / `criteria` 也列进"required inputs not covered by run_defaults"。
但它们是**子流程入参**，由父级 `inputs` 映射提供，`run_defaults` 按设计不作用于子流程（templates.md 已写明）。
即：对"被当作子流程使用的模板"，这条提示是**噪音**，会误导人去 config 里加无效值。

修法（待拍板）：`_report_run_defaults` 只对**顶层可运行**的定义报缺，或跳过被任何 `subflow` 节点引用的 id。

第 4 节那条 `inputs` 描述不一致仍未处理（属描述文本，不影响校验行为）。
