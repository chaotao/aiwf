# 易用性改造实施约定

已获用户授权，基于同日分析落地。

- 新定义版本 `1.1`；旧 `1.0` 继续使用低级逐节点协议。`${...}` 始终是文本插值。
- 数据用 `{"ref":"a.b"}`；task/branch 的 `inputs` 是显式材料映射，子流程的 `inputs` 是入参映射。文件引用保持引用，只有 foreach items 自动读取 JSON 清单。
- task `output: {"file":"..."} ` 声明预期文件；Agent 写文件，引擎核对路径及存在性。`run.id`、`run.dir` 是每次运行专属路径材料。
- task `check: {"criteria":[...],"max_attempts":3}`：上报作品后进入 `awaiting_check`，Agent 返回 `{"verdict":"PASS|REVISE","reason":"..."}`。返工保持实例身份，达到上限失败；PASS 后仍需独立人工 review。
- 1.1 顺序末节点自然结束；inline 路由可使用自动生成的 `end`。确定性推进仅发生于状态变更入口，`current` 只读。
- `action_spec` 返回根运行当前叶子动作，含 `action.id`、目标 run、node、kind；`act` 校验动作再上报，CLI 1.1 必须携带 `--action`。低级 SDK 保留。
- 新运行保存定义快照，旧运行继续按旧协议，不能通过覆盖源定义静默升级旧记录。
- 不新增数据库、UI、YAML、LLM 调用、通用表达式或并发调度。沿用动态 foreach manifest；跨章材料必须显式传递。
- 验证包含类型、缺失引用、条件依赖、自然结束、文件契约、返工上限、人审、过期动作、嵌套、恢复与 CLI 案例；历史仿真报告保留。
