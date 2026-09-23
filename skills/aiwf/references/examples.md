# 分类范例目录

下面是可复制的完整 1.1 定义，不是伪代码或节点片段。
只加载选中的范例，按业务调整 prompt、Schema、预算和验收点。
模板不包含真实运行记录，也不代表已经完成创作、修改代码或人工审批。

## 按能力选择

| 范例 | 文件 | 展示能力 |
|---|---|---|
| 入门规划 | [sequential.json](../assets/examples/sequential.json) | 顺序 task、短对象、ref、单文件、自然结束 |
| 提问与验收 | [user-check-review.json](../assets/examples/user-check-review.json) | user_input_schema、有限返工、人工验收 |
| 短剧多文件 | [multi-file.json](../assets/examples/multi-file.json) | 具名文件引用、output_schema、下游引用 |
| 代码修复 | [code-change.json](../assets/examples/code-change.json) | 不声明 output、修改文件列表、真实验证证据 |
| 小说逐章写作 | [novel-chapters.json](../assets/examples/novel-chapters.json) | 内联 foreach、previous、exports、清单汇总 |
| Godot 资源检查 | [game-resources.json](../assets/examples/game-resources.json) | item/index 改名、父子材料传递、分类汇总 |
| 单次复用 | [subflow-parent.json](../assets/examples/subflow-parent.json) + [analyze-piece.json](../assets/examples/analyze-piece.json) | 跨文件调用、input_schema、顶层 exports |
| 条件分流 | [condition-routing.json](../assets/examples/condition-routing.json) | when、TRUE/FALSE、显式 edges/end |
| 短剧节奏选择 | [subjective-routing.json](../assets/examples/subjective-routing.json) | Agent 标签决策、共同 output_key、独立 review |
| 高阶动态章节 | [dynamic-items.json](../assets/examples/dynamic-items.json) + [dynamic-queue.json](../assets/examples/dynamic-queue.json) | 动态文件队列、awaiting_items、resume |

小说完整创作通常还需要世界观、人物、大纲等前置任务；
这里的逐章范例聚焦批量写法，不会自动补全所有业务阶段。
游戏资源范例只读检查；需要修资源或场景时，参考代码修复的产物形状。

## 复制与验证

1. 在目标项目执行 `aiwf init`。
2. 将选中的工作流 JSON 复制到 `.aiwf/workflows/`，保持文件名与 id 一致。
3. 子流程父子两份必须一起复制。dynamic-queue.json 是数据文件，
   复制到项目的 `queue/chapters.json`，不要放入 workflows 目录。
4. 修改业务要求与入参；如改 workflow id，也改文件名和调用方的 workflow 字段。
5. 执行单文件校验和 `aiwf validate --project`。
6. 仅用户已要求执行时运行，随后按执行指南驱动任务包。

安装后的路径示例：

```bash
aiwf init
cp .trae/skills/aiwf/assets/examples/sequential.json .aiwf/workflows/sequential.json
aiwf validate .aiwf/workflows/sequential.json
aiwf run sequential -p idea="设计一个末世小说开篇" --json
```

在引擎仓库直接取模板时，源路径为 `skills/aiwf/assets/examples/`。
相对项目路径从运行命令所在目录解释；建议从固定项目根目录执行所有命令。

## 启动输入速查

将下面选中的 JSON 写入 inputs.json，再运行
`aiwf run <对应id> --input-file inputs.json --json`。

| workflow id | inputs.json 内容 |
|---|---|
| sequential / user-check-review / multi-file / subjective-routing | `{"idea":"每次重生都会失去一段记忆"}` |
| code-change | `{"project_dir":"/实际项目绝对路径","request":"修复空输入导致的越界"}` |
| novel-chapters | `{"chapters":[{"number":1,"title":"醒来"},{"number":2,"title":"代价"}]}` |
| game-resources | `{"project_dir":"/实际Godot项目","resources":[{"key":"player","path":"res://scenes/player.tscn"}]}` |
| subflow-parent / analyze-piece | `{"text":"每次重生都会失去一段记忆。"}` |
| condition-routing | `{"project_dir":"/实际项目绝对路径"}` |
| dynamic-items | `{"queue":{"$file":"queue/chapters.json"}}` |

带“实际”的路径必须替换。代码项目路径由 Agent 从用户指定位置或
LOCAL_REPO_ROOT 下已定位的项目取得，再明确传入；ref 不读取环境变量。
user-check-review 会继续问 tone，也可在启动输入中预填合法 tone。
动态范例首次队列未封口，会在已知项结束后等待，需按动态清单指南追加或封口。

## 选择产物和完成条件

| 场景 | 交付与验收建议 |
|---|---|
| 小说 | 正文外置；check 实测字数与连续性；用户指定章节或全书验收 |
| 短剧 | 剧本、分镜分文件；检查场次对应；人审节奏与可拍摄性 |
| 游戏 | 场景 / 脚本修改留在工程；运行目录放导入、加载或试玩证据 |
| 软件 | 输出修改路径和真实测试结果；必要时人审变更范围 |

模板中的检查项由 Agent 实际执行，引擎只保存决策。
condition-routing 的失败路径交付诊断后也会 completed；
这表示诊断流程完成，不代表测试通过。
