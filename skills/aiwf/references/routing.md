# 分支与路由

任务质检返工优先 task.check；只有业务去向不同才增加 branch。
条件能由已有数据直接判断时用 when；需要 Agent 理解内容时用 prompt。

## 确定性分支

```json
{
  "id": "gate",
  "type": "branch",
  "when": {"eq": [{"ref": "tests.exit_code"}, 0]},
  "routes": {"TRUE": "deliver", "FALSE": "diagnose"}
}
```

这是节点片段，tests 必须先输出 exit_code，deliver/diagnose 必须存在。
引擎自动计算 when 并记录决策，不产生等待 Agent 判断的任务。
完整定义见范例 `condition-routing.json`，两条路径各自产出报告后结束。

支持条件运算：

| 运算 | 写法 |
|---|---|
| eq / ne / gt / gte / lt / lte | `{"gte":[{"ref":"score"},80]}`，恰好两个操作数 |
| and / or | `{"and":[条件1,条件2]}`，非空条件数组 |
| not | `{"not":条件}` |

操作数用字面标量或 typed ref，引用可返回对象/数组用于相应比较。
不用字符串表达式（如 `"score > 80"`）或旧 `var`。
when 必须同时声明 TRUE/FALSE 路由，目标为节点 id。

## Agent 主观分类

```json
{
  "id": "choose",
  "type": "branch",
  "inputs": {"idea": {"ref": "idea"}},
  "prompt": "根据 idea 的核心冲突选择更合适的叙事节奏。只返回 FAST 或 SLOW。",
  "routes": {"FAST": "fast_script", "SLOW": "slow_script"}
}
```

引擎返回 `action.kind=complete`；Agent 根据材料选择标签：

```bash
aiwf submit <root_run_id> --output FAST --json
```

输出标签字符串，不是 task.check 的 verdict 对象。
大小写保持与 routes 一致。未知标签且无 default 时失败；
default 是兜底目标节点，不是一个标签列表，也不要配置为质量检查的自动放行。
完整短剧范例见 `subjective-routing.json`。

## 多出口不要靠数组排列猜路径

显式 edges 出现时按 edges 连接，不再把遗漏连线理解为默认顺序。
路由节点（branch、独立 review）只用 routes/default，不能再有出边。
普通 task 最多一条出边，不能通过多条 edges 做并行。

独立的两条业务路径通常用显式 end 汇合：

```json
"edges": [
  {"from": "tests", "to": "gate"},
  {"from": "deliver", "to": "end"},
  {"from": "diagnose", "to": "end"}
]
```

注意这里没有 gate 的出边，它的去向由 routes 决定。
顶层定义需有 `{"id":"end","type":"end"}` 节点。
foreach 内联体可以路由到自动生成的 `end`，但不得手写 start/end 节点，
内联 edges 的端点只写体内节点。

## 独立人工检查点

多个阶段共同验收时可以插入：

```json
{
  "id": "accept",
  "type": "review",
  "requirements": ["需求和实现是否匹配", "测试证据是否充分"],
  "routes": {"APPROVE": "deliver", "REJECT": "fix"}
}
```

APPROVE 必须有明确目标。task 内嵌 review 更简单，单任务验收优先内嵌。
任何 REJECT 回路都要评估总预算 max_steps；条件可能永久为 false 时应有
终止 / 人工处理路径。不要把业务质量问题只交给死循环保护处理。

条件分支只决定路径，不会生成未执行分支的输出。汇合任务不能无条件 ref
两条互斥分支的结果；可让两条路径使用相同 output_key，或分别交付。
