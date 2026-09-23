# aiwf 仿真测试报告：3 章短篇小说创作

- 日期：2026-09-23
- 被测对象：engine（纯状态机）+ 随包 skill `aiwf`
- 仿真项目：[sim/novel-3ch](file:///Users/bytedance/myjobs/aiwf/sim/novel-3ch)，基线：[sim/oracle.md](file:///Users/bytedance/myjobs/aiwf/sim/oracle.md)
- 方法：4 个独立"干净"子 Agent 顺序扮演创建者/执行者（只能读项目内 skill，禁看引擎源码与示例），人工审批全部由 Agent 代批 APPROVE；主控侧独立审计。

## 一、总体结论

| 环节 | 结果 | 说明 |
|------|------|------|
| 创建工作流 | ⚠️ 通过但 3 次试错 | validate 最终通过；但产出含 2 处"校验通过、运行必炸"的点路径 |
| 执行（首次） | ❌ gate 处 2 次失败 | 失败后 Agent 能正确判定为定义层问题并停止，未蛮干 |
| 修复后续跑 | ✅ completed | 经 resume 续跑，foreach 三实例 + 返工循环 + 终审全部走通 |
| 记录与产出 | ⚠️ 基本齐全，1 处索引缺陷 | 时间线/节点/事件完整；context.md 产物索引漏收章节文件 |

**最终运行**：[20260923-174619-840522](file:///Users/bytedance/myjobs/aiwf/sim/novel-3ch/.aiwf/runs/novel-3ch/20260923-174619-840522)，status=completed；产出 outline/chapters.json/3 章正文/汇编成书（正文合计约 3098 汉字）。

**核心判断**：引擎状态机本身（路由、审计、foreach、resume、自动传播）工作正常；问题高度集中在 **skill 文档示例错误 + 关键机制未文档化**，导致 Agent 创建期埋雷、执行期踩雷。

## 二、问题清单

### A 类：skill 引导缺陷（创建期）
- A1【最严重】官方 foreach 示例 `${item.outline}` 与引擎实现不一致：模板正则只捕获单层变量名，点路径模板**不会被渲染**。
- A2 未说明 context 寻址语义：task 产物整体挂在 `output_key` 下（嵌套对象），`when` 示例只有顶层键，也未声明"不支持点路径"。Agent 据此写出 `{"var":"plan.chapter_count"}`，validate 抓不到、运行时 KeyError。
- A3 `branch.routes` 值必须是节点 id、不允许 null（与内嵌 review 的 null 语义易类推混淆），无警示。
- A4 `default` 字段语义缺失：值是"兜底目标节点 id"，不是默认标签。
- A5 内联体不能放 start/end，且没说"branch 想结束循环体时该指向谁"。
- A6 缺少端到端复杂示例（foreach + branch 返工循环），最小示例过简。
- A7 SKILL.md 称"三种 phase"，实际四种（漏 `awaiting_items`）；两个不同子 Agent 独立发现。

### B 类：执行协议体验
- B1 run-workflow.md 的 branch 小节未区分模型分支与 `when` 确定性分支；`complete --empty` 只写在 create-workflow.md。渐进披露下执行 Agent 几乎必然误用 `--output TRUE`（实测首次即错）。
- B2 失败信封不带 error 原文，必须额外 `runs show` 才能定位。
- B3 `runs events` 不支持 `--json`，与"信封始终加 --json"口径冲突。
- B4 foreach 接管无 checklist：应父信封取 `node.child.run_id` → 切子级驱动。
- B5 独立 review 节点不入 node_runs（只在事件流），易误判终审未发生。
- B6 `output_schema` 驳回行为（节点不关闭、可重试）未文档化。
- B7 失败处理不区分"可恢复失败"与"定义/条件类失败"；盲目 resume 原样再炸（gate 实测连失败 2 次）。
- B8 JSON 信封 items 预览被截断；结构化对象在 context.md 只显示 `$file` 路径、不显示键清单。

### C 类：复杂流程控制
- C1 修复定义后，循环/分支路由全部正确：第 1 章 `QC REVISE → 重写 → QC PASS`、foreach 三实例顺序推进、子 end 自动传播、终审与交付衔接。
- C2 根因仍在文档：复杂流程本身可行，但 Agent 正确理解的成本过高。
- C3（引擎）gate 类问题"定义期合法、运行期才炸"，validate 缺少引用可解析性静态检查。

### 审计附带发现
- D1 context.md 产物索引只收顶层直接 `$file`，漏收对象内嵌路径（chapters_file）与子运行产物（3 个章节文件），与 skill 承诺的"每个 $file 路径"不符。
- D2 节点 duration 实为节点开放墙钟时长（含 Agent 思考/等待），并非纯执行耗时，建议注明。
- D3 成书书名与第一章标题同名（Agent 产出质量，非引擎问题）。

## 三、优化建议（按优先级）

**P0（让"文档写的"真正合法）**
1. 模板引擎支持点路径：渲染 `${item.number}`、条件 `{"var":"plan.chapter_count"}`；或立即修正官方示例与文档（二者必做其一，建议做前者）。
2. 修正 SKILL.md phase 数量为四种。

**P1（把错误拦在运行前）**
3. `validate` 增强：检查 `when.var` / 模板变量是否有 context 来源；branch routes 的 null/未知目标在单文件校验即报错。
4. run-workflow.md branch 小节补："deterministic=true 时执行 `complete --empty`"；失败处理区分可恢复 vs 定义类失败。

**P2（降低 Agent 理解成本）**
5. 补一个端到端范例：foreach（外引子流程）+ 内嵌 QC 返工循环 + 人工验收。
6. 失败信封携带 `error`；context.md 产物索引递归收集内嵌 `$file` 与子运行产物。
7. 明确文档化：default 语义、branch routes 不许 null、内联体结束去向、output_schema 驳回行为、foreach 接管三步 checklist。

**P3（打磨）**
8. JSON 模式 items 完整输出；注明 duration 语义；统一速查表参数口径。
