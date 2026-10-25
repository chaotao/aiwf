# 创建入门

用最少的可验收阶段表达业务。一个节点至少应有独立验收、重试、复用或交接价值。
先列出阶段、材料来源、交付物，再决定哪些阶段需要用户参与。
默认只写顺序 task，不为简单流程添加 start/end、分支或子流程。

## 1. 初始化与选择范例

在目标项目执行 `aiwf init`，定义放 `.aiwf/workflows/<id>.json`。
`aiwf skills install` 将本 Skill 安装到该项目的 `.trae/skills/aiwf`。
若命令不在 PATH，使用引擎脚本的绝对路径；相对文件路径按命令工作目录解释。
可从主入口的“范例目录”选择完整定义；先复制再按需求修改，勿修改 Skill 的模板。

## 2. 写第一个工作流

```json
{
  "schema_version": "1.1",
  "id": "opening",
  "name": "小说开篇",
  "input_schema": {
    "type": "object",
    "required": ["idea"],
    "properties": {"idea": {"type": "string"}}
  },
  "nodes": [{
    "id": "draft",
    "type": "task",
    "inputs": {"idea": {"ref": "idea"}},
    "prompt": "基于 idea 材料写小说开篇，建立冲突与悬念。",
    "output": {"file": "${run.dir}/opening.md"}
  }]
}
```

这里的 `idea` 来自启动输入；`inputs` 将它显式交给任务。
Agent 执行 prompt 并写文件，再上报 `{"$file":"任务包给出的实际路径"}`。
引擎不会创作正文，启动成功也不代表任务已执行。

## 3. 扩展为顺序阶段

在 `nodes` 数组末尾追加任务，后续任务用节点 id 引用前一任务结果：

```json
{
  "id": "polish",
  "type": "task",
  "inputs": {"draft": {"ref": "draft"}},
  "prompt": "读取 draft 文件，润色叙事节奏，保留核心事件。",
  "output": {"file": "${run.dir}/final.md"},
  "review": {"requirements": ["是否达到预期风格，可以定稿"]}
}
```

`review` 在产物上报后请用户验收，批准后才结束；不需要人工验收就省略它。
需要自动有限返工时添加 `check`，需要途中提问时添加 `user_input_schema`。
这两项的完整写法见主入口“输入与验收”，不必先学习路由。

## 4. 验证、交付与启动

```bash
aiwf validate .aiwf/workflows/opening.json
aiwf validate --project
aiwf run opening --title "末世重生" -p idea="末世里每次重生都失去一段记忆" --json
```

启动输入复杂时写 JSON 文件，改用 `--input-file inputs.json`。
单文件校验检查结构与路由；项目校验额外检查子工作流是否存在、必需入参、
调用环等。校验不执行 prompt，也不保证所有运行路径上的 ref 都有值。

仅创建时，交付定义路径、workflow id、启动输入、产物位置、验收点，不启动运行。
执行已获授权时加载执行指南，处理 `run` 返回的第一份任务包。

修改定义后重新校验。新运行使用定义快照，源文件修改只影响以后启动的运行。
运行记录位于 `.aiwf/runs/`，应保留并纳入版本管理，以便恢复和审计。
