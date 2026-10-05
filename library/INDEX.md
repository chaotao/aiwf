# aiwf 模板库目录

由 `aiwf wf index` 生成，请勿手改——改各模板的 `meta/<id>.json` 后重新生成。

安装：`aiwf wf install <id>`（默认连同依赖一起装；装完跑 `aiwf validate --project`）。

## `std.review-check`

- 成熟度：`experimental`
- 一句话：按给定标准对一个对象做评审，输出 PASS/REVISE 与问题清单
- 依赖：无
- 版本：`1.1`
- 适用：
  - 需要一个不参与改动的独立复检环节
  - 同一套评审标准要在多处复用，不想重复写提示词
- 不适用：
  - 本模板是 agent 评审，不含人工验收节点；要人拍板需在流程里另加 task 的 review 节点
  - 一次性短任务

## `std.review-loop`

- 成熟度：`experimental`
- 一句话：改代码 → Agent 自检 → 人工验收 → 独立复检 的开发类返工闭环
- 依赖：`std.review-check`
- 版本：`1.1`
- 适用：
  - 开发类任务，改动质量需要多重把关
  - 需要人工验收之后再补一道机器复检
- 不适用：
  - 非代码类改动（写作、素材生产等）
  - 只需要一次生成、不需要验收的场景
