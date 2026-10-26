# 模板库：找现成流程，而不是每次自己写

本仓库的 `library/workflows/` 预置了一批通用工作流。项目通过 `aiwf wf install`
把它们**复制**到自己的 `.aiwf/workflows/` 使用——复制进来就是你自己的文件，可以随意改。

## 何时用

先查库，再决定自建：

1. 流程有恢复、复用、批量或验收价值（一次性短任务仍然直接做完，不固化成工作流）；
2. `aiwf wf list` 看库里有没有覆盖这个场景的模板；
3. 命中就装，**不命中才自己写**（自建流程走[创建入门](create.md#创建入门)）。

`wf list` 的 MATURITY 列先看：`experimental` 表示结构可能还会变，装了要当作起点读一遍，
不要当成稳定依赖。

## 何时不该用

- 模板的 `not_for` 明确排除的场景（`aiwf wf show <id>` 会打印）。硬套会浪费时间。
- 只需要跑一次的短任务——装模板比直接做完更慢。

## 怎么用

```bash
aiwf wf list                      # 库里有哪些：id / 成熟度 / 是否已装 / 一句话场景
aiwf wf show std.review-loop      # 单个模板：适用、不适用、依赖、必填入参、本地状态
aiwf wf install std.review-loop   # 装到 .aiwf/workflows/（默认连依赖一起装）
aiwf validate --project           # 装完必须跑：校验子流程调用图与必需入参
```

其余参数：

| 参数 | 语义 |
|---|---|
| `--as <new-id>` | 换个 id 安装（会同时改写定义里的 `id`，保持文件名与定义一致） |
| `--force` | 覆盖本地已改动的副本；不加则拒绝覆盖 |
| `--no-deps` | 只装本体，不带依赖。装完 `validate --project` 会报缺子流程 |

**装完通常要改什么**（复制式安装的固有动作）：先 `aiwf wf show <id>` 看必填入参，
再打开 `.aiwf/workflows/<id>.json` 看三处——`input_schema` 的必填项、写死的路径或标准文本、
`check.criteria` / `check.max_attempts` 这类按项目调整的阈值。
具体某个模板该改哪里，目前要读模板本体判断（元信息里还没有"常见本地改动项"字段）。

## 项目固有的值：run_defaults

模板的 `input_schema` 声明它需要哪些入参，但有些值属于**项目**、不属于某一次运行——
目标项目目录、知识库路径、测试命令。这些值写在项目的 `.aiwf/config.json` 里，一次即可：

```json
{
  "run_defaults": {
    "project_dir": "F:/data/project/github/xxx",
    "knowledge_dir": "docs/knowledge",
    "test_cmd": "python -m pytest -q"
  }
}
```

**装完模板先做这件事**，而不是改模板本体：改模板，下次库里有更新就合不上（见下面三条约定）；
只有模板逻辑本身不合用时才动模板。

- 合并序，后者覆盖前者：`run_defaults` → `--input-file` → `-p`。**逐键覆盖**——被覆盖的只是那一个键，
  其余键仍来自配置。
- 只注入**模板自己声明过**的键（`input_schema` 的 `properties` / `required`），
  一个模板的配置不会串进另一个模板的 context。所以同一个 `project_dir` 供多个模板共用是安全的。
- **校验不放松**：必填项既没被配置覆盖、运行也没给，照旧失败。
- `aiwf validate --project` 顺带报两句：哪些必填项还没被 `run_defaults` 覆盖、`run_defaults` 里哪些键
  没有任何定义声明（多半是拼错）。**两句都是提示，不影响退出码。**
- 只注入**顶层 run** 的启动输入；子流程的入参由父级 `inputs` 映射决定，不由项目配置直注。
- 但项目固有值会**顺着父级映射穿透**：父级写 `{"knowledge_dir": {"ref": "knowledge_dir"}}`，
  子流程实例拿到的就是同一个项目值——项目侧仍然只配一次 config。
  所以通用子流程要把项目固有值声明成入参、由父级映射下去；**漏映射不会静默**，
  `validate --project` 会报 `does not map required inputs [...]`。

## 复制式安装的三条约定

1. **装进来的是副本，本地改动不会回传库。** 想让别的项目也吃到你的改动，得回到仓库改
   `library/workflows/` 的源文件。
2. **不会悄悄覆盖你改过的东西。** 安装时把内容哈希记在 `.aiwf/installed.json`；
   重装时发现本地已改动就拒绝，需要 `--force`。项目自己的同名文件（不是从库装的）同样拒绝。
3. **判断上游有没有更新**：`aiwf wf status` 看两列——LOCAL 是 `edited` 表示你改过，
   UPSTREAM 是 `updated` 表示库里有新版本；具体差异用 `aiwf wf diff <id>` 看。

```bash
aiwf wf status                    # 已装模板 vs 库版本对照
aiwf wf diff std.review-loop      # 本地副本与库版本的差异
```

## 写模板：专业性与通用性落在哪

自己写模板（或把演示件升级成模板）时，同时盯两条，否则复用价值不大：

- **专业性**——把"该做哪些事、按什么标准"写出来，而不是甩一句需求让 Agent 自己猜；
- **通用性**——同一个定义在不同项目里都能跑，换项目只换配置、不改定义。

### 专业性：把步骤写进 prompt

`prompt` 是**唯一**能放"步骤"的槽位。`check.criteria` 是合格线、`review.requirements` 是人工验收标准，
它们回答"做到什么算好"，**不回答**"先做什么、再做什么"。

写的时候用有序步骤，一步一个动作，每步都能落到实际内容上：

```
按以下步骤执行，每步都要落到实际动作：
1. 读约定：约定文件为 ${conventions_file}——不是 none 就先读它；是 none 就看项目根的 README。
2. 定位：……  3. 改动：……  4. 验证：……  5. 报告：……
```

反例（读不出该做什么）："读取目标项目约定并定位问题，修改源文件，运行与变更相称的验证。"
——"约定是什么、在哪、怎么读"、"相称的验证"由谁判定，全都没说。

### 通用性：项目固有值走入参 + run_defaults

凡是**项目之间不同、同一项目内固定**的值——目标目录、约定文件路径、知识库路径、测试命令——
都声明成 `input_schema` 的入参，值由项目在 `.aiwf/config.json` 的 `run_defaults` 里配一次（见上一节）。
**定义本体不写死任何路径或命令。** 换项目只换配置，定义照装照用。

### 一条机制约束：没有"可选入参"，用 `"none"` 约定

引擎不区分"键没给"和"键给了空值"：任何被 `${}` 或 `ref` 引用的键，context 里没有就硬失败
（`template variable ${x} not found in workflow context`）。而 `validate --project` 对**只写在
`properties`、没进 `required`** 的键完全静默——安装时看不见，跑起来才炸。

所以"项目可能没有的能力"（没有约定文件、没有自动化测试）**不要**声明成 optional，而要：

- 放进 `required`，描述里写明"没有就填 `"none"`"；
- prompt 里对 `none` 分支给出替代动作。

好处有两个：`validate --project` 会提示它没被配置覆盖（optional 不会），prompt 也不会因缺值崩。
示范见 `library/workflows/std.review-loop.json`。

### 语法提醒：材料别名不能插值

`inputs` 里 `{"alias": {"ref": "..."}}` 建的是**任务包里的材料名**，不是 context 变量——
写 `${alias}` 会报 `undefined context key`。要插值就用原始入参键：

```jsonc
"inputs": {"kb": {"ref": "knowledge_dir"}}   // kb 只是材料名
"prompt": "读 ${knowledge_dir} 下的资料"      // 插值用原始键，不是 ${kb}
```

由 `${run.dir}` 派生出的材料键同理，只能在 prompt 里作为**文字**提到。

## 库在哪、怎么换

默认是本仓库的 `library/workflows/`。结构：

```
library/
  INDEX.md                 # 模板目录，由 `aiwf wf index` 生成，勿手改
  workflows/<id>.json      # 工作流定义，只放定义
  meta/<id>.json           # 适用/不适用/依赖等元信息，不会被装进项目
```

`meta/` 与 `workflows/` 分开放，是因为 `list_workflows` 把 `.aiwf/workflows/` 下所有
`*.json` 都当定义读，元信息文件一旦被复制进去会让 `validate --project` 直接失败。

换库位置用 `AIWF_LIBRARY_DIR` 环境变量，或给任一 `wf` 命令加 `--library <dir>`。
新增模板：写 `library/workflows/<id>.json` 与 `library/meta/<id>.json`，
再跑 `aiwf wf index` 更新目录。库内模板必须是 `schema_version: "1.1"`。

模板本体只在 `library/workflows/` 存一份，**不随 skill 包分发**——
否则每个 agent 目录都会有一份副本，改一处漏一处。
