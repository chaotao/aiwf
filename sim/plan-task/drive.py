#!/usr/bin/env python3
"""Simulation driver for the `plan-task` workflow.

Stands in for the agent that would normally run each node: it turns the user's
requirement into a brief, *plans* the task list, executes every task (with a
self-check), passes the agent quality checks (PASS) and approves the human
gates (APPROVE). It exists so the *view* can be reproduced on demand without a
real model in the loop -- nothing here is part of the engine.

Artifact layout (the point of this demo):

    plan/<plan_name>/            <- business artifacts, human-facing
        plan.md                  <- the plan book (the plan node's output)
        tasks/TASK-0NN.md        <- per-task change note + self-check
        acceptance.md            <- overall acceptance report
        plan-final.md            <- delivered plan
    .aiwf/runs/<wf>/<run_id>/    <- engine bookkeeping only
        record.json workflow.json context.md
        foreach-work-items.json foreach-work-results.json

The task list is **not a file**: it is the engine-owned queue behind
`items: {"queue": true}`, which lives inside `record.json` (`queues.work`).
The plan node pushes one item per task through `aiwf items … add`, then
`seal`s it; every later reader asks the engine for the same list
(`aiwf items … list`) instead of following a `$file`. Short reports go inline
via --output, so no scratch file is left behind.

    python drive.py                    # requirement -> plan -> execute -> deliver
    python drive.py --stop-at-task 2   # stop at TASK-002's acceptance gate
    python drive.py --stop-at-accept   # stop right before the overall gate
    python drive.py --continue <run_id>
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.normpath(os.path.join(HERE, "..", "..", "engine"))
PY = sys.executable
ENV = dict(
    os.environ,
    PYTHONPATH=ENGINE,
    AIWF_HOME=".aiwf",
    PYTHONIOENCODING="utf-8",
)


# ---- engine boundary -----------------------------------------------------
def cli(*args):
    proc = subprocess.run(
        [PY, "-m", "aiwf", *args], cwd=HERE, env=ENV,
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        sys.exit(f"! aiwf {' '.join(args)}\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


def write(path, content):
    """Write an artifact. Paths are project-relative (the engine resolves the
    declared `output.file` the same way, against the project dir)."""
    target = path if os.path.isabs(path) else os.path.join(HERE, path)
    target = os.path.normpath(target)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)

# ---- what the "planner" derives from the requirement ---------------------
# The brief and the task list below are what the intake/plan nodes would
# produce for the requirement in inputs.json -- canned so the demo is
# deterministic without a model in the loop.
PLAN_NAME = "wu-gang-ch1"          # the slug intake would choose
GOAL = "做出《雾港》第一章可玩的垂直切片"
SCOPE = "码头探索 + 潮汐谜题 + 道具合成 + 关门通关，一条单章节闭环"
CONSTRAINTS = "两周内完成；只用现有美术素材；不引入新的第三方库"
ACCEPTANCE = "玩家能从码头走到通关且全程无阻断；谜题 / 合成 / 关门三条链路可独立复现；手感与氛围达到可展示标准"

PLANNED_TASKS = [
    {"id": "TASK-001", "name": "核心循环 spec",
     "goal": "把探索-解谜-合成-通关的核心循环写成可执行规格",
     "deps": "无", "acceptance": "循环每一环都有明确的输入输出与状态迁移"},
    {"id": "TASK-002", "name": "潮汐谜题与关卡原型",
     "goal": "搭建潮汐水位机制与首个谜题关卡原型",
     "deps": "TASK-001", "acceptance": "水位随机关状态变化；谜题有唯一解且可通过"},
]


def requirement():
    with open(os.path.join(HERE, "inputs.json"), encoding="utf-8") as fh:
        return json.load(fh)["requirement"]


def queue_rows(rid, node="work"):
    """The task list is engine state now: ask the engine for it instead of
    following a file, exactly what a real agent does."""
    return cli("items", "list", rid, "--node", node, "--json")["queue"]["items"]


# ---- simulated node bodies ----------------------------------------------
def do_intake(node):
    requirement()  # the brief is derived from it; text is canned for the demo
    # short object -> inline, no file (create.md 产物设计: short objects stay in context)
    out = {
        "goal": GOAL, "scope": SCOPE,
        "constraints": CONSTRAINTS, "acceptance": ACCEPTANCE,
        "plan_name": PLAN_NAME,
    }
    print(f"  intake   -> {PLAN_NAME}：{GOAL}")
    return out


def do_plan(spec):
    """One node produces the plan. The task list is *pushed* into the
    engine-owned queue -- the agent writes no queue file at all -- and the
    human-facing plan book is what the node declares as its output."""
    node, rid = spec["node"], spec["run_id"]
    tasks = [dict(t) for t in PLANNED_TASKS]
    for task in tasks:
        cli("items", "add", rid, "--node", "work",
            "--item", json.dumps(task, ensure_ascii=False), "--json")
    cli("items", "seal", rid, "--node", "work", "--json")

    doc = [f"# 计划：{GOAL}", "",
           f"- 范围：{SCOPE}", f"- 约束：{CONSTRAINTS}",
           f"- 成功标准：{ACCEPTANCE}", "", "## 任务", ""]
    for t in tasks:
        doc.append(f"- [{t['id']}] {t['name']}｜依赖：{t['deps']}")
        doc.append(f"    - 目标：{t['goal']}")
        doc.append(f"    - 验收：{t['acceptance']}")
    out = {
        "$file": node["output"]["file"],
        "summary": f"计划已制定：{len(tasks)} 个任务，队列已封口",
        "task_count": len(tasks),
    }
    write(out["$file"], "\n".join(doc) + "\n")
    print(f"  plan     -> 队列 {len(tasks)} 项已入队并 seal；计划书 {out['$file']}")
    return out


def do_execute(node, task):
    """One node does the work *and* the self-check; the human gate is the
    node's embedded review."""
    body = (
        f"# {task['id']} {task['name']}\n\n"
        f"## 改动说明\n\n"
        f"- 目标：{task['goal']}\n"
        f"- 做了什么：按目标实现主体改动，落在模块 `{task['id'].lower()}`。\n"
        f"- 关键决定：沿用 TASK-001 定下的核心循环，未引入计划外依赖。\n"
        f"- 遗留风险：边界输入与异常路径待回归覆盖。\n\n"
        f"## 自检结论\n\n"
        f"- 验收点：{task['acceptance']}\n"
        f"- 结论：逐条核对，达标，可提交人工验收。\n"
    )
    out = {"$file": node["output"]["file"], "summary": f"{task['name']} 完成并自检达标"}
    write(out["$file"], body)
    print(f"  execute  {task['id']}")
    return out


def do_accept(spec):
    node, rid = spec["node"], spec["run_id"]
    brief = node["inputs"]["intake"]
    rows = queue_rows(rid)                 # the queue, not a file
    body = (
        "# 整体验收报告\n\n"
        f"- 计划目标：{brief['goal']}\n"
        f"- 成功标准：{brief['acceptance']}\n"
        f"- 任务结果：队列 {len(rows)} 项，"
        f"{sum(1 for r in rows if r['status'] == 'done')} 项 success，已封口。\n"
        "- 结论：跨任务无冲突，第一章垂直切片的闭环成立，可交付。\n"
    )
    out = {"$file": node["output"]["file"], "summary": "整体回归通过，闭环成立"}
    write(out["$file"], body)
    print("  accept   （按队列核对，未读任何清单文件）")
    return out


def do_finalize(spec):
    node, rid = spec["node"], spec["run_id"]
    brief = node["inputs"]["brief"]
    rows = queue_rows(rid)
    doc = [f"# 计划完成报告：{brief['goal']}", "",
           f"- 范围：{brief['scope']}", f"- 成功标准：{brief['acceptance']}", "",
           f"## 任务（已完成 {len(rows)}/{len(rows)}）", ""]
    for r in rows:
        deps = r["fields"].get("deps", "无")
        doc.append(f"- [x] {r['id']} {r['name']}｜状态：{r['status']}｜依赖：{deps}")
    doc += ["", "## 结论", "", "- 目标达成，垂直切片可玩；遗留风险见各任务改动说明。"]
    out = {"$file": node["output"]["file"], "summary": "计划完成，已交付完成态计划"}
    write(out["$file"], "\n".join(doc) + "\n")
    print("  finalize -> 完成态计划已交付")
    return out


# ---- resuming an existing run -------------------------------------------
def done_count(rid):
    """How many queue items already succeeded -- read off the view, which uses
    the engine's own instance vocabulary (``success``, not a second one)."""
    view = cli("runs", "show", rid, "--view", "--json")
    for stage in view.get("stages", []):
        queue = stage.get("queue")
        if queue:
            return sum(1 for it in queue.get("items", []) if it.get("status") == "success")
    return 0


# ---- main loop -----------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop-at-task", type=int, default=0,
                    help="stop at the acceptance gate of the Nth task (0 = run to completion)")
    ap.add_argument("--stop-at-accept", action="store_true",
                    help="stop at the overall acceptance gate")
    ap.add_argument("--continue", dest="continue_run", metavar="RUN_ID",
                    help="keep driving an existing run instead of starting a new one")
    args = ap.parse_args()

    approved_tasks = 0
    if args.continue_run:
        root = args.continue_run
        spec = cli("status", root, "--json")
        approved_tasks = done_count(root)
    else:
        spec = cli("run", "plan-task", "--title", "plan-task 驱动演练",
                   "--input-file", "inputs.json", "--json")
        root = spec["run_id"]
    print(f"run {root}")

    while spec.get("status") not in ("completed", "failed"):
        act = spec.get("action") or {}
        kind, node_id = act.get("kind"), act.get("node_id")

        if kind == "complete":
            node = spec["node"]
            if node_id == "intake":
                out = do_intake(node)
            elif node_id == "plan":
                out = do_plan(spec)
            elif node_id == "execute":
                out = do_execute(node, node["inputs"]["task"])
            elif node_id == "accept":
                out = do_accept(spec)
            elif node_id == "finalize":
                out = do_finalize(spec)
            else:
                sys.exit(f"! unexpected node {node_id!r}")
            # the reported object is short by design -> inline, no scratch file
            spec = cli("complete", root, "--action", act["id"],
                       "--output", json.dumps(out, ensure_ascii=False), "--json")

        elif kind == "check":
            spec = cli("complete", root, "--action", act["id"],
                       "--output", '{"verdict":"PASS","reason":"逐条核对验收点，达标"}', "--json")

        elif kind == "review":
            if args.stop_at_accept and node_id == "accept":
                print(f"\n停在整体验收门；run {root} 仍在运行。")
                return root
            if (args.stop_at_task and node_id == "execute"
                    and approved_tasks >= args.stop_at_task - 1):
                print(f"\n停在 TASK-{args.stop_at_task:03d} 的验收门；run {root} 仍在运行。")
                return root
            spec = cli("review", root, "--action", act["id"],
                       "--verdict", "APPROVE", "--feedback", "仿真验收通过", "--json")
            if node_id == "execute":
                approved_tasks += 1

        else:
            sys.exit(f"! unexpected action kind {kind!r}")

    print(f"\nrun {root} -> {spec.get('status')}")
    return root


if __name__ == "__main__":
    print(main())
