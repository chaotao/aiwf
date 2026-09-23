# 仿真测试预期基线（Oracle）：3 章短篇小说创作工作流

> 本文是测试基准：子 Agent 仅依据项目内安装的 aiwf skill 创建/执行工作流，
> 测试者据此核对「工作流是否符合理想形态」「执行是否顺畅」。
> 仿真策略：人工验收节点一律由 Agent 代审批（APPROVE），保证全自动闭环。

## 一、工作流定义的理想形态

### 1. 顶层结构
- `schema_version="1.0"`，`id` 小写合法、与文件名一致；有 `name`。
- `input_schema`：至少要求启动输入（如 `idea`/题材方向）。
- `max_steps`：考虑 3 章循环 + 返工，应 **≥ 60**（默认 50 偏紧，理想是显式放大到 80~200）。
- 无 subflow/foreach 时单文件即可；`validate` 必须通过。

### 2. 节点拓扑（按阶段）

线性前置阶段：
1. `start`
2. 立项/概念 task：建议用节点级 `input_schema` 收集题材、基调、目标章节数（=3）；产出概念简报，`$file` 外置
3. 世界观 task（读取概念；理想态带验收或由大纲验收统一覆盖）
4. 人物 task（读取概念/世界观）
5. 总大纲+分章规划 task：恰好 3 章规划；理想态带 review

章节循环（必须成环）：
6. 章节细纲/准备 task：通过列目录统计已完成章数 N，本次写第 N+1 章（引擎不维护业务计数）
7. 章节正文 task：正文写文件、只回 `{"$file": ...}`；理想态挂 `output_schema`（required [$file]）
8. 自动质检 branch（LLM 判定，不停顿）：
   - PASS → 人工验收
   - REVISE → 章节修订 task → **回到质检**（回环边）
9. 章节修订 task：`output_key` 与正文节点相同（覆盖同一 context key）
10. 人工验收：独立 `review` 节点（或正文 task 内嵌 review）：
    - APPROVE → 继续判断
    - REJECT → 回到正文节点
11. 是否继续 branch：
    - CONTINUE → **回到第 6 步（循环边）**
    - DONE → 汇总

收尾：
12. 汇总定稿 task：理想态 `consumes: [{"glob": "novel/chapters/*.md"}]`，组装全书
13. `end`

### 3. 关键质量项
- 所有大产物（简报/设定/大纲/正文/全书）一律 `$file` 引用，节点输出不内联正文。
- 依赖通过 prompt 中 `${var}` 表达；汇总类节点用 `consumes` glob。
- 模板变量每个都有来源，不存在缺失变量。
- branch/review 节点不再连 edges，路由完全由 routes/default 决定。
- 章节序号/计数由 Agent 在执行中通过文件系统自行维护，不依赖引擎业务计数。

## 二、执行过程的理想表现

- 启动只需一次 `run`；之后 Agent 按信封 phase 驱动，不重复试错。
- phase 序列符合预期：前置线性阶段 → 每章 `executing → awaiting_review（→APPROVE）`。
- 路由正确：
  - 第 1 章：质检经历 **REVISE 一次**（测试脚本故意在首稿制造章尾钩子缺陷）→ 修订后 PASS
  - 第 2、3 章：质检 PASS
  - 第 1、2 章验收后走 CONTINUE 回环；第 3 章走 DONE
- 全部人工验收节点由 Agent 代批 APPROVE，不真正停顿。
- 全程无命令报错、无失败节点、无需要 resume 的失败；最终 `status=completed`。

## 三、记录与产出的理想表现

- 每个 node_run：有 `started_at/ended_at/duration_ms/summary`，input 为渲染后 prompt，output 为短值或 `$file` 引用。
- events 完整：run_started、input_provided（如触发）、review_requested/decided（每章）、以及质检分支决策。
- `context.md` 每次状态变更同步更新，产物索引覆盖全部文件，无失效路径。
- 文件产出齐全：概念/世界观/人物/大纲各 1 份、章节 3 份、全书 1 份；全书含 3 章、排序正确。

## 四、问题分类口径

- A 类（skill 引导缺陷）：skill 没写清，导致节点选型/字段用法错误
- B 类（执行协议体验）：CLI/phase 机制理解成本高、信息位置难找、靠重试才走对
- C 类（复杂流程控制）：循环/分支/回溯路由走错或状态异常
