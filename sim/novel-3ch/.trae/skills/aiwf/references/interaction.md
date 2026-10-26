# 启动入参、途中提问、质检与人工验收

按发生时机选择能力：

| 时机 | 定义字段 | 执行方 / 命令 | 作用 |
|---|---|---|---|
| 启动时 | 顶层 input_schema | Agent 准备 run 入参 | 缺项或类型错误时启动失败 |
| task 执行前 | user_input_schema | 用户回答；provide | 缺 required 信息时暂停提问 |
| task 上报时 | output_schema、output.file | 引擎校验 | 检查结果结构、主文件契约 |
| task 上报后 | check | Agent 检查；complete | 有限次数的当前任务返工 |
| 质检通过后（若有） | review | 用户验收；review | 人工批准后才前进 |

## 启动输入与途中提问

顶层 input_schema 描述整个工作流的调用接口：

```json
"input_schema": {
  "type": "object",
  "required": ["idea"],
  "properties": {"idea": {"type": "string"}}
}
```

task 上的片段描述执行前要收集的信息：

```json
{
  "user_input_schema": {
    "type": "object",
    "required": ["tone"],
    "properties": {
      "tone": {"type": "string", "enum": ["冷峻", "温暖"], "title": "叙事风格"}
    }
  },
  "inputs": {"idea": {"ref": "idea"}, "tone": {"ref": "tone"}}
}
```

tone 缺失时返回 `action.kind=provide`、`phase=awaiting_input`。
按 input_request 中的字段提问，收到答案后提交：

```bash
aiwf submit <root_run_id> -p tone="冷峻" --json
```

复杂答案用 `--data-file answers.json`，或直接改 `submit.json` 的 `output`。
submit 会校验用户答案并写入 context，再渲染任务材料。inputs 本身没有与之配对的
inputs_schema 字段。

预填信息不会重复询问；JSON Schema 的 default 不自动落值。
同一 run 再次进入已收集过信息的节点不会重新激活提问；
每项都要询问时，把任务放进 foreach，每个实例有自己的输入状态。
需要每轮动态更新时，由 Agent 显式收集后更新文件或发起输入修订。

## Agent 有限质检

task 增加：

```json
"check": {
  "criteria": ["字数符合要求", "关键事件完整", "结尾留下明确悬念"],
  "max_attempts": 3
}
```

正常上报后，任务包进入 `action.kind=check`、`phase=awaiting_check`。
读取 `check_request` 的实际产物与检查项，执行检查后上报：

```bash
aiwf submit <run_id> --verdict REVISE \
  --reason "实测正文仅 620 字，低于 800 字要求" --json
aiwf submit <run_id> --verdict PASS \
  --reason "按要求口径统计 986 字，关键事件与悬念均已核对" --json
```

也可以改 `submit.json` 的 `output`，或写 `--json-file verdict.json`。
reason 必须非空且说明依据。
“自动”只指引擎安排检查动作，质量仍由 Agent 判断。字数、测试、资源加载等
可测项目用工具验证，证据较长时写报告并在 reason 中指出位置。

max_attempts 包括初稿：3 表示初稿及最多两次返工。
REVISE 在当前任务重做；previous_output 和 feedback 进入下一任务包。
次数耗尽运行失败，resume 不会清零；按高阶修订指南处理，不能直接补报 PASS。
Schema 驳回不是质检结论，先满足结构与文件契约才能进入质量检查。

## 人工验收

task 增加：

```json
"review": {"requirements": ["风格符合预期，可以定稿"]}
```

有 check 时先质检；没有 check 时产物成功上报后直接进入人工验收。
展示产物位置、关键摘要及 review_request，再等待用户明确结论：

```bash
aiwf submit <root_run_id> --verdict APPROVE --feedback "用户认可" --json
aiwf submit <root_run_id> --verdict REJECT --feedback "用户要求增强冲突" --json
```

默认 APPROVE 前进，REJECT 重做当前任务。人工拒绝会开启新的质检预算，
整个运行仍受 max_steps 保护。质检通过不能代替人工批准；
测试夹具里的模拟审批只能用于测试。

可以在同一 task 同时写 user_input_schema、inputs、output、check、review；
它们分别控制前置提问、材料、产物、Agent 检查和人的确认。
完整组合见范例目录的 `user-check-review.json`。

## 非默认验收路由

需要打回更早阶段才添加：

```json
"review": {
  "requirements": ["是否符合大纲"],
  "routes": {"APPROVE": null, "REJECT": "outline"}
}
```

这里 null 表示批准后按正常顺序继续；outline 必须存在。
这只是显式跳转，不是依赖感知的计划外修订；回路应有 max_steps 保护。
跨多个阶段的独立验收点可用 review 节点，具体见分支与路由指南。
