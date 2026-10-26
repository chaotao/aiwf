# aiwf 排错速查

先看 `error.code`（文本模式会打印 `code/at/hint/next`，`--json` 时在同一个对象里）。
每条只做一件事，不要重试同一条命令。

| code / 退出码 | 含义 | 做什么 |
|---|---|---|
| `E_STALE_ACTION` / 4 | `submit.json` 或 `--action` 指向的动作已过期 | `aiwf status` 看当前步骤；`aiwf submit --print` 重建模板；确认产物仍适用再交 |
| （无 code）/ 3 | 产物被驳回：schema 或 `output.file` 不符 | 读 `rejected.expected` / `rejected.got`，修文件后重交；模板保留，不用重建 |
| `E_INPUT_INVALID` / 2 | 启动入参、定义、check/review 结果不符合 schema | 按 `at` 定位；入参用 `-p key=value` 或 `--input-file` |
| `E_USAGE` / 2 | 参数组合不对（如 check 用了 `--file`、多个 run 没指定） | 按 `hint`；`aiwf status` 会给出当前步骤该用的命令 |
| `E_NOT_FOUND` / 2 | run / workflow 不存在或前缀不唯一 | `aiwf runs list` / `aiwf workflows`；前缀给长一点 |
| `E_INSTALL` / 2 | 找不到 schemas / library / skills | 设置 `AIWF_SCHEMAS_DIR`，或重装带数据的 wheel；`aiwf doctor` |
| `E_ERROR` / 2 | 其他引擎拒绝 | 读 `message`；常见是 `check attempts exhausted`（转 `aiwf revise`） |
| 退出码 1 | run 已 failed | `aiwf runs show <run>` 看 error；`aiwf resume <run>` 恢复 |

## 常见现象

- **`aiwf submit` 说 template created**：这是正常的第一次；模板已写到 `file` 行，
  填 `output`（或直接给 `--file` / `--verdict`）后再跑一次。
- **旧 run 的 `submit.json` 缺失**：`aiwf status` 显示 `(missing)`；
  `aiwf status --ensure` 补建，或直接 `aiwf submit`（文件任务且产物已就绪会直接完成）。
- **`submit.json` 的 `output` 还是占位符**：check 的 `PASS|REVISE`、review 的
  `APPROVE|REJECT`、branch 的 `TRUE|FALSE` 必须替换成一个值。
- **不知道写哪个文件**：`aiwf status` 的 `artifact:` 就是它；模板里的 `$file` 已经指向同一路径。
- **改了定义但 run 没变**：1.1 run 用启动时的定义快照；修定义后新建 run。
- **多个 run 在跑**：`aiwf runs list --status running`，给 `status/submit`
  显式 `run_id`（或唯一前缀）。
- **命令找不到**：`./bin/aiwf` 不存在就跑 `aiwf init` 生成；再不行 `aiwf doctor`。
- **Windows 中文乱码**：CLI 已强制 UTF-8；若终端仍乱码，先 `chcp 65001` 或用
  `aiwf --json` 让调用方按 UTF-8 解码。

## 不该做的

- 不要用 `--force` 绕过 stale；过期就重新 `status`。
- 不要手改 `submit.json` 的 `action` / `kind`。
- `complete` / `provide` / `review` / `fail` / `redo` 已移除；统一 `submit`
  （改历史输入用 `submit --node`，单实例重做用 `revise --instance`）。
- 不要在 check 未通过时补报 PASS；不要替用户 APPROVE。
