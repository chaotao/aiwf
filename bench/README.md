# 接口基准（两命令循环）

这个脚本验证的不是"模型有多聪明"，而是**接口是否只靠两条命令就能走完流程**。
它用一个"盲跟随者"代替 Agent：只读 `aiwf status --json`、只调 `aiwf submit`，
从不选动词、不复制 action id、不手写 payload。所有节点产物都是固定夹具。

```bash
python bench/run_bench.py
```

输出每个场景的命令数 / 错误数 / traceback 数，全绿才退 0。

## 场景

| 场景 | 覆盖 |
|---|---|
| `sequential` | 两个顺序 task、文件产物 |
| `checked` | task → check → review |
| `batch` | 队列初始为空 → `--item-file ... --seal` → 两个实例 |
| `asked` | 执行前向用户提问（provide） |
| `sequential`（删模板） | 模拟 L2 之前启动的旧 run：没有 `submit.json`，首次 `submit` 自动补建并完成 |

## 指标

- **completed**：最终 `status=completed`；
- **commands**：本场景的 `run + status + submit` 次数（不含场景搭建）；
- **errors**：`status`/`submit` 非 0 的次数，目标 0；
- **traceback**：stderr 出现 `Traceback` 的次数，目标 0。

## 它证明了什么 / 不证明什么

证明：正常路径不需要 `complete/provide/review/fail/items`，不需要 action id，
不需要手写 JSON；旧 run 缺 `submit.json` 时能在首次 `submit` 被补齐。

不证明：模型能理解 prompt、产物质量合格、人工验收真实。要做真实模型基准，
把同一个场景交给一个只读 `RUN-CARD.md` 的 Agent，记录同一组指标 + 文档读取量。

## 扩展

新增场景：在 `run_bench.py` 里加一个返回 workflow 的函数，并把 `drive(...)`
加进 `main()` 的 `results`。夹具 Agent 已覆盖 complete/check/review/provide/items
五种分派；新增场景通常只需换 workflow。
