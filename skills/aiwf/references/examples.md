# 分类范例目录

下面是可复制的完整 1.1 定义，不是伪代码或节点片段。
只加载选中的范例，按业务调整 prompt、Schema、预算和验收点。
模板不包含真实运行记录，也不代表已经完成创作、修改代码或人工审批。

## 按能力选择

| 范例 | 文件 | 展示能力 |
|---|---|---|
| 入门规划 | [sequential.json](../assets/examples/sequential.json) | 顺序 task、短对象、ref、单文件、自然结束 |
| 提问与验收 | [user-check-review.json](../assets/examples/user-check-review.json) | user_input_schema、有限返工、人工验收 |
| 短剧多文件 | [multi-file.json](../assets/examples/multi-file.json) | 具名文件引用、output_schema、下游引用、仅标题的 `view` |
| 代码修复 | [code-change.json](../assets/examples/code-change.json) | 不声明 output、修改文件列表、真实验证证据 |
| 小说逐章写作 | [novel-chapters.json](../assets/examples/novel-chapters.json) | 内联 foreach、previous、exports、清单汇总、`dynamic_title` |
| Godot 资源检查 | [game-resources.json](../assets/examples/game-resources.json) | item/index 改名、父子材料传递、分类汇总 |
| 单次复用 | [subflow-parent.json](../assets/examples/subflow-parent.json) + [analyze-piece.json](../assets/examples/analyze-piece.json) | 跨文件调用、input_schema、顶层 exports |
| 条件分流 | [condition-routing.json](../assets/examples/condition-routing.json) | when、TRUE/FALSE、显式 edges/end |
| 短剧节奏选择 | [subjective-routing.json](../assets/examples/subjective-routing.json) | Agent 标签决策、共同 output_key、独立 review |
| 高阶运行时队列 | [queue-items.json](../assets/examples/queue-items.json) | 引擎维护的队列、`aiwf submit` 投喂/封口、awaiting_items、自动推进 |

小说完整创作通常还需要世界观、人物、大纲等前置任务；
这里的逐章范例聚焦批量写法，不会自动补全所有业务阶段。
游戏资源范例只读检查；需要修资源或场景时，参考代码修复的产物形状。

### 范例里的标题：只有需要辨认才写

10 个范例里有 **2 个**声明了 `view.dynamic_title`：

- **小说逐章写作**：`${node.direction.title}`——方向选型节点在工作末尾报告书名，此后
  `runs list` 显示作品名而不是启动时的临时名（这个范例同时有 `stages`）；
- **短剧多文件**：`${node.write.title}`——**只声明 `dynamic_title`、没有 `stages`**，演示
  「流程短到没有阶段可讲，照样能给 run 起名」。

**不必每个范例都加**：标题只解决「在 `runs list` 里认不出这次是哪个实例」。产物本身没有名字的
流程（修代码、资源检查、评审）写了也只是把启动标题换个说法的重复，不用写。

**模板引用的键，节点报告里得真有**——两个范例都在对应节点的 prompt 里要求报告 `title`
（短剧那个还把它加进了 `output_schema`）。只写模板、不让节点产出该字段，等于没写：
永远回落到启动标题。字段、优先级与占位符见[业务进展视图](view.md)。

## 复制与验证

1. 在目标项目执行 `aiwf init`。
2. 将选中的工作流 JSON 复制到 `.aiwf/workflows/`，保持文件名与 id 一致。
3. 子流程父子两份必须一起复制。队列范例**没有**附带数据文件——
   队列是 run 状态，`items: {"queue": true}` 的条目由 `aiwf submit` 的模板/`--item-file` 投喂，
   不要复制任何清单进 workflows 目录。
4. 修改业务要求与入参；如改 workflow id，也改文件名和调用方的 workflow 字段。
5. 执行单文件校验和 `aiwf validate --project`。
6. 仅用户已要求执行时运行，随后按执行指南驱动任务包。

安装后的路径示例：

```bash
aiwf init
cp .trae/skills/aiwf/assets/examples/sequential.json .aiwf/workflows/sequential.json
aiwf validate .aiwf/workflows/sequential.json
aiwf run sequential --title "末世小说开篇" -p idea="设计一个末世小说开篇" --json
```

在引擎仓库直接取模板时，源路径为 `skills/aiwf/assets/examples/`。
相对项目路径从运行命令所在目录解释；建议从固定项目根目录执行所有命令。

## 启动输入速查

将下面选中的 JSON 写入 inputs.json，再运行
`aiwf run <对应id> --title "..." --input-file inputs.json --json`（`--title` 必填）。

| workflow id | inputs.json 内容 |
|---|---|
| sequential / user-check-review / multi-file / subjective-routing | `{"idea":"每次重生都会失去一段记忆"}` |
| code-change | `{"project_dir":"/实际项目绝对路径","request":"修复空输入导致的越界","conventions_file":"none","test_cmd":"python -m pytest -q"}` |
| novel-chapters | `{"chapters":[{"number":1,"title":"醒来"},{"number":2,"title":"代价"}]}` |
| game-resources | `{"project_dir":"/实际Godot项目","resources":[{"key":"player","path":"res://scenes/player.tscn"}]}` |
| subflow-parent / analyze-piece | `{"text":"每次重生都会失去一段记忆。"}` |
| condition-routing | `{"project_dir":"/实际项目绝对路径"}` |
| queue-items | `{}`（队列由 `aiwf submit` 投喂，不经 input） |

带“实际”的路径必须替换。代码项目路径由 Agent 从用户指定位置或
LOCAL_REPO_ROOT 下已定位的项目取得，再明确传入；ref 不读取环境变量。
`project_dir` / `conventions_file` / `test_cmd` 属于项目固有值，实际使用时写进项目
`.aiwf/config.json` 的 `run_defaults`（见[模板库](templates.md)），每次只传变化的 `request`。
user-check-review 会继续问 tone，也可在启动输入中预填合法 tone。
队列范例没有清单输入：它自己的 `plan` 节点把条目写进 `submit.json` 的 `output`（或
`aiwf submit --item-file`），`seal` 后队列关闭；`chapters` 节点起步时队列若仍空且未封口，
run 会停在 `awaiting_items`（`submit` 会自动推进）。

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
