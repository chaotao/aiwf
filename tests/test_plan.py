"""Project plan: a long-running task list at ``<root>/plans/<id>/index.md``.

A plan spans many runs -- one task may take several, one plan many workflows --
so it cannot be derived from any single run. Like the inbox it is project level
and never part of run state; unlike the inbox it *reads* runs, to derive the
execution status (进行中 / 待验收 / 已完成 / 验收未通过) that prose cannot keep
honest.

The Markdown index is the source of truth. These tests pin the four things the
thin layer adds on top of it:

* the status vocabulary is enforced (the drift that motivated the module was a
  ``已验收`` where the vocabulary only has ``已完成``);
* the dependency graph is checked (existence, no cycle);
* execution status is derived from run records;
* goal / scope / focus / backlog stay readable for a one-glance overview.

The last group deliberately covers shapes found in real plan files, so the
parser cannot quietly drop a task: link-less rows, extra ``｜`` annotations
after the dependency, range shorthand, and progress rows that point at another
plan.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aiwf import plan as plan_store

ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "aiwf"]
BASE_ENV = dict(os.environ, PYTHONPATH=str(ROOT / "engine"))
HOME = ".aiwf"

INDEX = """# 测试计划

## 计划信息
- 游戏：`t`
- 计划 ID：demo
- 状态：规划中
- 目标：做出可演示的核心闭环
- 范围：只做核心闭环，不做内容扩展

## 当前焦点
- 当前任务：TASK-001
- 当前状态：待开始
- 下一步：实现最小可运行切片
- 阻塞：无
- 必读文件：
  - [tasks/TASK-001.md](tasks/TASK-001.md)

## 任务
- [TASK-001](tasks/TASK-001.md)｜建立最小可运行切片｜状态：待开始｜依赖：无
- [TASK-002](tasks/TASK-002.md)｜接入核心闭环｜状态：待开始｜依赖：TASK-001

## 进度
- 已验收：0 / 2
- 进行中：无
- 待验收：无
- 阻塞：无
- 已取消或拆分：无
"""

#: Minimal definition, local to the test: the library may change, the
#: link between a run and a task (inputs.plan / inputs.task) may not.
WORKFLOW = {
    "schema_version": "1.1",
    "id": "work",
    "name": "工作",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "t", "type": "task", "prompt": "干活", "output_key": "t"},
        {"id": "end", "type": "end"},
    ],
}


def index_with(tasks, progress=None, info_extra="", header="# 测试计划\n"):
    """Build an index.md around a task block, for the shape-specific cases."""
    progress = progress if progress is not None else (
        "## 进度\n- 已验收：0 / 2\n- 进行中：无\n- 待验收：无\n"
        "- 阻塞：无\n- 已取消或拆分：无\n"
    )
    return (
        f"{header}\n## 计划信息\n- 游戏：`t`\n- 计划 ID：demo\n- 状态：规划中\n"
        f"- 目标：做出可演示的核心闭环\n- 范围：只做核心闭环{info_extra}\n\n"
        f"## 当前焦点\n- 当前任务：无\n- 当前状态：待开始\n\n"
        f"## 任务\n{tasks}\n{progress}"
    )


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        (self.project / HOME / "workflows").mkdir(parents=True)
        self.plans = self.project / HOME / "plans"
        self.write_plan(INDEX)

    # ---- helpers ---------------------------------------------------------
    def write_plan(self, text, plan_id="demo"):
        path = self.plans / plan_id / "index.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_json(self, path, value):
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def cli(self, *args):
        return subprocess.run(
            [*CLI, *args], cwd=self.project, env=BASE_ENV,
            text=True, capture_output=True,
        )

    def check(self, *args):
        return self.cli("plan", "check", *args)

    # ---- parsing ---------------------------------------------------------
    def test_parses_goal_scope_focus_and_tasks(self):
        plan = plan_store.load(str(self.plans), "demo")
        self.assertEqual(plan["name"], "测试计划")
        self.assertEqual(plan["info"]["目标"], "做出可演示的核心闭环")
        self.assertEqual(plan_store.focus_task_id(plan), "TASK-001")
        self.assertEqual([t["id"] for t in plan["tasks"]], ["TASK-001", "TASK-002"])
        self.assertEqual(plan["tasks"][1]["deps"], ["TASK-001"])
        self.assertEqual(plan["tasks"][0]["deps"], [])

    def test_bare_task_rows_without_a_link_are_parsed(self):
        """Real files mix ``- [TASK-001](link)｜…`` and ``- TASK-001｜…``."""
        self.write_plan(index_with(
            "- TASK-001｜建立最小可运行切片｜状态：待开始｜依赖：无\n"
            "- [TASK-002](tasks/TASK-002.md)｜接入核心闭环｜状态：待开始｜依赖：TASK-001"
        ))
        plan = plan_store.load(str(self.plans), "demo")
        self.assertEqual([t["id"] for t in plan["tasks"]], ["TASK-001", "TASK-002"])
        self.assertEqual(plan["tasks"][0]["link"], "")
        self.assertEqual(plan["tasks"][1]["link"], "tasks/TASK-002.md")

    def test_segment_after_deps_is_a_note_not_a_dependency(self):
        self.write_plan(index_with(
            "- [TASK-001](tasks/TASK-001.md)｜预览｜状态：已取消｜"
            "依赖：TASK-002｜并入计划 `other`（取代）\n"
            "- [TASK-002](tasks/TASK-002.md)｜本体｜状态：已完成｜依赖：无",
            progress="## 进度\n- 已验收：1 / 2\n- 进行中：无\n- 待验收：无\n"
                     "- 阻塞：无\n- 已取消或拆分：TASK-001\n",
        ))
        plan = plan_store.load(str(self.plans), "demo")
        self.assertEqual(plan["tasks"][0]["deps"], ["TASK-002"])
        self.assertEqual(plan["tasks"][0]["note"], "并入计划 `other`（取代）")
        # the note must not be mistaken for a dependency
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_range_shorthand_expands_to_each_task(self):
        self.assertEqual(
            plan_store.parse_deps("TASK-003～005"),
            ["TASK-003", "TASK-004", "TASK-005"],
        )
        self.assertEqual(plan_store.parse_deps("TASK-001、TASK-004"), ["TASK-001", "TASK-004"])
        self.assertEqual(plan_store.parse_deps("无"), [])

    # ---- check: the dependency graph -------------------------------------
    def test_check_accepts_a_clean_plan(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("plan ok", result.stdout)

    def test_check_reports_an_unknown_dependency(self):
        self.write_plan(index_with(
            "- [TASK-001](tasks/TASK-001.md)｜A｜状态：待开始｜依赖：TASK-404"
        ))
        result = self.check()
        self.assertEqual(result.returncode, 2)
        self.assertIn("TASK-001 depends on unknown TASK-404", result.stdout)

    def test_check_detects_a_dependency_cycle(self):
        self.write_plan(index_with(
            "- [TASK-001](tasks/TASK-001.md)｜A｜状态：待开始｜依赖：TASK-002\n"
            "- [TASK-002](tasks/TASK-002.md)｜B｜状态：待开始｜依赖：TASK-001"
        ))
        result = self.check()
        self.assertEqual(result.returncode, 2)
        self.assertIn("dependency cycle", result.stdout)

    # ---- check: vocabulary ------------------------------------------------
    def test_check_rejects_a_status_outside_the_vocabulary(self):
        self.write_plan(index_with(
            "- [TASK-001](tasks/TASK-001.md)｜A｜状态：已验收｜依赖：无"
        ))
        result = self.check()
        self.assertEqual(result.returncode, 2)
        self.assertIn("'已验收' is outside the vocabulary", result.stdout)

    def test_check_requires_goal_and_scope(self):
        self.write_plan(
            "# 没有目标\n\n## 计划信息\n- 状态：规划中\n\n## 当前焦点\n- 当前任务：无\n\n"
            "## 任务\n- [TASK-001](tasks/TASK-001.md)｜A｜状态：待开始｜依赖：无\n\n"
            "## 进度\n- 已验收：0 / 1\n"
        )
        result = self.check()
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing 目标", result.stdout)
        self.assertIn("missing 范围", result.stdout)

    # ---- check: the progress block ---------------------------------------
    def test_check_reports_a_progress_numerator_drift(self):
        self.write_plan(index_with(
            "- [TASK-001](tasks/TASK-001.md)｜A｜状态：已完成｜依赖：无",
            progress="## 进度\n- 已验收：5 / 5\n",
        ))
        result = self.check()
        self.assertEqual(result.returncode, 2)
        self.assertIn("进度 已验收 says 5", result.stdout)

    def test_progress_may_point_at_another_plan_without_a_false_alarm(self):
        """Real rows read ``TASK-002→demo01 TASK-009``: only ids of *this* plan
        are compared, so the cross-plan reference is not an unknown task."""
        self.write_plan(index_with(
            "- [TASK-001](tasks/TASK-001.md)｜A｜状态：已取消｜依赖：无\n"
            "- [TASK-002](tasks/TASK-002.md)｜B｜状态：已拆分｜依赖：无",
            progress="## 进度\n- 已验收：0 / 2\n"
                     "- 已取消或拆分：TASK-001 已取消；TASK-002→other TASK-009\n",
        ))
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout)

    # ---- show / list ------------------------------------------------------
    def test_show_surfaces_goal_scope_focus_and_backlog(self):
        result = self.cli("plan", "show", "demo", "--no-runs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("做出可演示的核心闭环", result.stdout)
        self.assertIn("当前焦点", result.stdout)
        self.assertIn("TASK-001", result.stdout)
        self.assertIn("待办  TASK-001, TASK-002", result.stdout)

    def test_missing_plan_is_reported_with_the_known_ids(self):
        result = self.cli("plan", "show", "nope")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no plan 'nope'", result.stderr)
        self.assertIn("demo", result.stderr)

    def test_list_json_reports_counts_and_outstanding(self):
        result = self.cli("plan", "list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["plans"][0]["id"], "demo")
        self.assertEqual(payload["plans"][0]["counts"]["total"], 2)
        self.assertEqual(payload["plans"][0]["outstanding"], ["TASK-001", "TASK-002"])

    def test_an_empty_plans_directory_is_healthy(self):
        """Like a missing inbox ledger: nothing planned yet is not an error."""
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        result = self.check("--dir", str(empty))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("plan ok: 0 plan(s)", result.stdout)

    # ---- derived status ---------------------------------------------------
    def test_status_is_derived_from_a_run(self):
        self.write_json(self.project / HOME / "workflows" / "work.json", WORKFLOW)
        started = self.cli("run", "work", "-p", "plan=demo", "-p", "task=TASK-001")
        self.assertEqual(started.returncode, 0, started.stderr)

        result = self.cli("plan", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TASK-001", result.stdout)
        self.assertIn("进行中", result.stdout)
        # document still says 待开始 -> the difference is called out
        self.assertIn("drift", result.stdout)

    def test_show_annotates_a_task_whose_run_disagrees(self):
        self.write_json(self.project / HOME / "workflows" / "work.json", WORKFLOW)
        self.cli("run", "work", "-p", "plan=demo", "-p", "task=TASK-001")
        result = self.cli("plan", "show", "demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("→ 进行中", result.stdout)

    def test_a_run_without_plan_inputs_links_to_nothing(self):
        self.write_json(self.project / HOME / "workflows" / "work.json", WORKFLOW)
        self.cli("run", "work")
        result = self.cli("plan", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no runs linked", result.stdout)


#: A file shaped like the real ones -- prose, a 必读文件 list, a cancelled task
#: and a progress block that agrees with the task list. Every write test runs
#: against it, because "only the right line changed" is the property that
#: matters and a minimal fixture cannot show it.
RICH_INDEX = """# 机器核心（控制台统一编排）

## 计划信息
- 游戏：`mc-plus`
- 计划 ID：demo
- 状态：规划中
- 目标：装上驾驶控制台的作品成为一台「可编机器」
- 范围：控制台承载统一控制方案
- 创建日期：2026-09-17
- 最近更新：2026-09-21
- 稳定上下文：[context.md](context.md)

## 当前焦点
- 当前任务：TASK-002
- 当前状态：待验收（代码完成，274 测试全绿；手动实机验收）
- 下一步：按 [tasks/TASK-002.md](tasks/TASK-002.md) 验收
- 阻塞：无
- 必读文件：
  - [tasks/TASK-002.md](tasks/TASK-002.md)

## 任务
- [TASK-001](tasks/TASK-001.md)｜机器核心 spec（g-spec）｜状态：已完成｜依赖：无
- [TASK-002](tasks/TASK-002.md)｜控制台控制方案｜状态：待验收｜依赖：TASK-001
- [TASK-003](tasks/TASK-003.md)｜调试台骨架｜状态：已取消｜依赖：TASK-002

## 进度
- 已验收：1 / 3
- 进行中：无
- 待验收：TASK-002
- 阻塞：无
- 已取消或拆分：TASK-003
"""


class PlanWriteTest(unittest.TestCase):
    """new / task add / focus / sync: a plan is written by a human, so a writer
    may only splice the line it must -- never re-render the file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        (self.project / HOME / "workflows").mkdir(parents=True)
        (self.project / HOME / "runs").mkdir(parents=True)
        self.plans = self.project / HOME / "plans"
        path = self.plans / "demo" / "index.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(RICH_INDEX, encoding="utf-8")

    # ---- helpers ---------------------------------------------------------
    def cli(self, *args):
        return subprocess.run(
            [*CLI, *args], cwd=self.project, env=BASE_ENV,
            text=True, capture_output=True,
        )

    def read_index(self, plan_id="demo"):
        return (self.plans / plan_id / "index.md").read_text(encoding="utf-8")

    def write_index(self, text, plan_id="demo"):
        path = self.plans / plan_id / "index.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def diff_lines(before, after):
        return [
            (a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b
        ]

    def write_run(self, run_id, task, status="completed", plan="demo"):
        """A run record is all ``derive_from_runs`` reads: no engine needed."""
        path = self.project / HOME / "runs" / "work" / run_id / "record.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(path, {
            "workflow_id": "work",
            "run_id": run_id,
            "status": status,
            "started_at": "2026-09-20T10:00:00+08:00",
            "inputs": {"plan": plan, "task": task},
            "context": {},
            "node_runs": [],
            "events": [],
        })

    def write_json(self, path, value):
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # ---- new -------------------------------------------------------------
    def test_new_writes_a_skeleton_that_becomes_a_plan_with_a_task(self):
        result = self.cli("plan", "new", "fresh",
                          "--goal", "做出闭环", "--scope", "只做闭环")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.read_index("fresh")
        self.assertIn("# fresh", text)
        self.assertIn("- 目标：做出闭环", text)
        self.assertIn("- 范围：只做闭环", text)
        self.assertIn("## 任务", text)

        # A plan with no task is not yet a plan -- check says so, it is not a bug
        empty = self.cli("plan", "check", "fresh")
        self.assertEqual(empty.returncode, 2)
        self.assertIn("no tasks listed", empty.stdout)

        added = self.cli("plan", "task", "add", "fresh", "TASK-001", "--name", "第一件事")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(self.cli("plan", "check", "fresh").returncode, 0)

    def test_new_refuses_to_touch_an_existing_plan(self):
        result = self.cli("plan", "new", "demo", "--goal", "G", "--scope", "S")
        self.assertEqual(result.returncode, 2)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(self.read_index(), RICH_INDEX)

    def test_new_rejects_an_id_that_is_not_path_safe(self):
        result = self.cli("plan", "new", "Not A Plan", "--goal", "G", "--scope", "S")
        self.assertEqual(result.returncode, 2)
        self.assertIn("plan id must match", result.stderr)

    # ---- task add --------------------------------------------------------
    def test_task_add_appends_after_the_last_row(self):
        result = self.cli("plan", "task", "add", "demo", "TASK-004",
                          "--name", "整体回归", "--deps", "TASK-002,TASK-003")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.read_index().splitlines()
        row = ("- [TASK-004](tasks/TASK-004.md)｜整体回归｜状态：待开始｜"
               "依赖：TASK-002、TASK-003")
        self.assertIn(row, lines)
        self.assertGreater(lines.index(row), lines.index(
            "- [TASK-003](tasks/TASK-003.md)｜调试台骨架｜状态：已取消｜依赖：TASK-002"))
        self.assertLess(lines.index(row), lines.index("## 进度"))
        self.assertEqual(self.cli("plan", "check", "demo").returncode, 0)

    def test_task_add_expands_range_shorthand_in_deps(self):
        result = self.cli("plan", "task", "add", "demo", "TASK-004",
                          "--name", "回归", "--deps", "TASK-001～003")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("依赖：TASK-001、TASK-002、TASK-003", self.read_index())

    def test_task_add_rejects_an_unknown_dependency(self):
        result = self.cli("plan", "task", "add", "demo", "TASK-004",
                          "--name", "X", "--deps", "TASK-404")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown dependency TASK-404", result.stderr)

    def test_task_add_rejects_a_taken_id(self):
        result = self.cli("plan", "task", "add", "demo", "TASK-001", "--name", "X")
        self.assertEqual(result.returncode, 2)
        self.assertIn("already in plan", result.stderr)

    def test_task_add_guards_the_vocabulary_at_the_module_boundary(self):
        """The CLI's --status uses choices=; the module is the real gate."""
        with self.assertRaises(ValueError) as caught:
            plan_store.add_task(str(self.plans), "demo", "TASK-004",
                                name="X", status="已验收")
        self.assertIn("task status must be one of", str(caught.exception))

    # ---- focus -----------------------------------------------------------
    def test_focus_moves_the_pointer_and_follows_the_task_status(self):
        before = self.read_index()
        result = self.cli("plan", "focus", "demo", "TASK-001")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.read_index()
        self.assertIn("- 当前任务：TASK-001", after)
        self.assertIn("- 当前状态：已完成", after)
        self.assertEqual(
            [old for old, _ in self.diff_lines(before, after)],
            ["- 当前任务：TASK-002",
             "- 当前状态：待验收（代码完成，274 测试全绿；手动实机验收）"],
        )

    def test_focus_none_clears_the_pointer(self):
        result = self.cli("plan", "focus", "demo", "none")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- 当前任务：无", self.read_index())

    def test_focus_reports_an_unknown_task(self):
        result = self.cli("plan", "focus", "demo", "TASK-404")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no task TASK-404", result.stderr)

    # ---- sync ------------------------------------------------------------
    def test_sync_is_a_dry_run_without_write(self):
        self.write_run("RUN-1", "TASK-002")
        before = self.read_index()
        result = self.cli("plan", "sync", "demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would write", result.stdout)
        self.assertIn("dry run", result.stdout)
        self.assertEqual(self.read_index(), before)

    def test_sync_write_folds_run_facts_into_task_and_progress(self):
        self.write_run("RUN-1", "TASK-002")
        result = self.cli("plan", "sync", "demo", "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.read_index()
        self.assertIn("｜状态：已完成｜依赖：TASK-001", after)
        self.assertIn("- 已验收：2 / 3", after)
        self.assertIn("- 待验收：无", after)

    def test_sync_only_splices_the_lines_it_must(self):
        """The property that makes this safe on a hand-written file."""
        self.write_run("RUN-1", "TASK-002")
        before = self.read_index()
        self.cli("plan", "sync", "demo", "--write")
        after = self.read_index()
        self.assertEqual(len(before.splitlines()), len(after.splitlines()))
        diff = self.diff_lines(before, after)
        self.assertEqual([old for old, _ in diff], [
            "- [TASK-002](tasks/TASK-002.md)｜控制台控制方案｜状态：待验收｜依赖：TASK-001",
            "- 已验收：1 / 3",
            "- 待验收：TASK-002",
        ])
        self.assertEqual([new for _, new in diff], [
            "- [TASK-002](tasks/TASK-002.md)｜控制台控制方案｜状态：已完成｜依赖：TASK-001",
            "- 已验收：2 / 3",
            "- 待验收：无",
        ])
        # the 当前焦点 prose is not a run fact; the parenthetical must survive
        self.assertIn("- 当前状态：待验收（代码完成，274 测试全绿；手动实机验收）", after)

    def test_sync_leaves_a_cancelled_task_alone(self):
        """A run must never resurrect a task somebody deliberately dropped."""
        self.write_run("RUN-2", "TASK-003")
        self.cli("plan", "sync", "demo", "--write")
        self.assertIn("调试台骨架｜状态：已取消", self.read_index())

    def test_sync_follows_the_focus_when_it_is_a_bare_status_word(self):
        self.cli("plan", "focus", "demo", "TASK-002")   # -> 当前状态：待验收
        self.write_run("RUN-1", "TASK-002")
        self.cli("plan", "sync", "demo", "--write")
        self.assertIn("- 当前状态：已完成", self.read_index())

    def test_sync_repairs_a_stale_focus_even_when_the_row_is_already_current(self):
        """The row was reconciled by an earlier run and the focus is behind; a
        check tied to "did this row change just now" would never catch it."""
        self.write_index(self.read_index()
                         .replace("- 当前任务：TASK-002", "- 当前任务：TASK-001")
                         .replace("- 当前状态：待验收（代码完成，274 测试全绿；手动实机验收）",
                                  "- 当前状态：待开始"))
        result = self.cli("plan", "sync", "demo", "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- 当前状态：已完成", self.read_index())

    def test_sync_leaves_a_focus_whose_status_carries_a_note(self):
        """待验收（代码完成，274 测试全绿）is the author explaining something,
        not a status word; swapping the head would make the note a lie."""
        self.write_run("RUN-1", "TASK-002")
        self.cli("plan", "sync", "demo", "--write")
        self.assertIn("- 当前状态：待验收（代码完成，274 测试全绿；手动实机验收）",
                      self.read_index())

    def test_sync_does_not_relist_a_progress_row_that_is_already_right(self):
        """A real row reads ``TASK-002→demo01 TASK-009``: the ids are correct,
        so a sync must leave the note alone instead of relisting them."""
        self.write_index(self.read_index().replace(
            "- 已取消或拆分：TASK-003",
            "- 已取消或拆分：TASK-003（并入 `machine-core`）"))
        self.write_run("RUN-1", "TASK-002")
        result = self.cli("plan", "sync", "demo", "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- 已取消或拆分：TASK-003（并入 `machine-core`）",
                      self.read_index())

    def test_sync_keeps_a_parenthetical_on_the_accepted_row(self):
        self.write_index(self.read_index().replace(
            "- 已验收：1 / 3", "- 已验收：1 / 3（TASK-001）"))
        self.write_run("RUN-1", "TASK-002")
        self.cli("plan", "sync", "demo", "--write")
        self.assertIn("- 已验收：2 / 3（TASK-001）", self.read_index())

    def test_sync_renumber_resets_the_denominator(self):
        self.cli("plan", "task", "add", "demo", "TASK-004", "--name", "回归")
        self.assertIn("- 已验收：1 / 3", self.read_index())
        result = self.cli("plan", "sync", "demo", "--write", "--renumber")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- 已验收：1 / 4", self.read_index())

    def test_sync_keeps_the_denominator_by_default(self):
        self.cli("plan", "task", "add", "demo", "TASK-004", "--name", "回归")
        self.cli("plan", "sync", "demo", "--write")
        self.assertIn("- 已验收：1 / 3", self.read_index())

    def test_a_new_plan_has_no_denominator_and_sync_does_not_invent_one(self):
        """A baked-in 0 / 0 would go stale on the first task add (1 / 0)."""
        self.cli("plan", "new", "fresh", "--goal", "G", "--scope", "S")
        self.assertIn("- 已验收：0\n", self.read_index("fresh"))
        self.cli("plan", "task", "add", "fresh", "TASK-001", "--name", "第一件事")
        self.write_run("RUN-9", "TASK-001", plan="fresh")
        self.cli("plan", "sync", "fresh", "--write")
        self.assertIn("- 已验收：1\n", self.read_index("fresh"))

    def test_sync_renumber_seeds_a_missing_denominator(self):
        self.cli("plan", "new", "fresh", "--goal", "G", "--scope", "S")
        self.cli("plan", "task", "add", "fresh", "TASK-001", "--name", "第一件事")
        self.write_run("RUN-9", "TASK-001", plan="fresh")
        self.cli("plan", "sync", "fresh", "--write", "--renumber")
        self.assertIn("- 已验收：1 / 1\n", self.read_index("fresh"))

    def test_sync_on_a_clean_plan_writes_nothing(self):
        result = self.cli("plan", "sync", "demo", "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to do", result.stdout)

    def test_sync_reports_an_unknown_plan(self):
        result = self.cli("plan", "sync", "nope")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no plan 'nope'", result.stderr)

    def test_sync_json_reports_each_change(self):
        self.write_run("RUN-1", "TASK-002")
        result = self.cli("plan", "sync", "demo", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["plans"][0]["written"])
        self.assertEqual(
            [c["field"] for c in payload["plans"][0]["changes"]],
            ["TASK-002 状态", "进度 已验收", "进度 待验收"],
        )
        self.assertEqual(payload["plans"][0]["changes"][0]["run_id"], "RUN-1")


if __name__ == "__main__":
    unittest.main()
