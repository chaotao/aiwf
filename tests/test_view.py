"""The declared business view is *derived*, never stored: these tests pin the
derivation down, including the mid-flight cases that motivated it."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aiwf import Storage, WorkflowEngine
from aiwf import view as view_mod
from aiwf.cli import _print_view
from aiwf.schema import validate_workflow


REPO = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "aiwf"]
CLI_ENV = {"PYTHONPATH": str(REPO / "engine")}


def task(node_id):
    return {"id": node_id, "type": "task", "prompt": "Execute the task."}


def foreach(node_id, items, body=("draft",)):
    return {"id": node_id, "type": "foreach", "items": items,
            "nodes": [task(n) for n in body]}


def workflow(nodes, view=None, **extra):
    wf = {"schema_version": "1.1", "id": "w", "name": "test",
          "max_steps": 50, "nodes": nodes, **extra}
    if view is not None:
        wf["view"] = view
    return wf


def record(node_runs=(), **extra):
    base = {
        "schema_version": "1.1", "run_id": "R1", "workflow_id": "w",
        "status": "running", "phase": "executing",
        "started_at": "2026-10-05T10:00:00+08:00",
        "next_node": None, "node_runs": list(node_runs),
        "context": {}, "events": [],
    }
    base.update(extra)
    return base


def node_run(node_id, status="success", output=None, summary="dump", **extra):
    row = {"node_id": node_id, "node_name": node_id, "type": "task",
           "status": status, "duration_ms": 5, "summary": summary,
           "output": output}
    row.update(extra)
    return row


def instance(key, index, status, run_id=None):
    return {"key": str(key), "index": index, "status": status,
            "run_id": run_id, "started_at": None, "ended_at": None,
            "error": None, "exports": {}}


THREE = workflow(
    [task("prepare"), task("implement"), task("audit")],
    view={"stages": [
        {"name": "确定目标", "nodes": ["prepare"]},
        {"name": "实施改动", "nodes": ["implement"]},
        {"name": "独立复检", "nodes": ["audit"]},
    ]},
)


class DeriveTest(unittest.TestCase):
    """Pure derivation from a record: no engine, no filesystem."""

    def statuses(self, rec):
        derived = view_mod.derive(THREE, rec)
        return [s["status"] for s in derived["stages"]]

    def test_no_view_declared_is_not_an_error(self):
        self.assertIsNone(view_mod.derive(workflow([task("a")]), record()))
        self.assertIsNone(view_mod.derive(None, record()))

    def test_standing_on_the_first_node_makes_it_active(self):
        self.assertEqual(
            self.statuses(record(next_node="prepare")),
            ["active", "pending", "pending"],
        )

    def test_finished_prefix_is_done_and_the_rest_pending(self):
        rec = record(node_runs=[node_run("prepare")], next_node="implement")
        self.assertEqual(self.statuses(rec), ["done", "active", "pending"])

    def test_completed_run_marks_every_stage_done(self):
        rec = record(
            node_runs=[node_run(n) for n in ("prepare", "implement", "audit")],
            status="completed", next_node=None,
        )
        self.assertEqual(self.statuses(rec), ["done", "done", "done"])

    def test_failure_marks_that_stage_and_leaves_the_rest_pending(self):
        rec = record(
            node_runs=[node_run("prepare"), node_run("implement", status="failed")],
            status="failed", next_node="implement",
        )
        self.assertEqual(self.statuses(rec), ["done", "failed", "pending"])

    def test_awaiting_review_is_waiting_and_offers_the_command(self):
        rec = record(node_runs=[node_run("prepare")], next_node="implement",
                     phase="awaiting_review")
        stage = view_mod.derive(THREE, rec)["stages"][1]
        self.assertEqual(stage["status"], "waiting")
        self.assertEqual(stage["action"]["kind"], "review")
        self.assertEqual(stage["action"]["run_id"], "R1")

    def test_a_gate_whose_node_already_succeeded_is_still_waiting(self):
        """The node feeding a review gate has produced its output; nobody has
        ruled on it yet. Judging "every mapped node succeeded" first would
        call the stage done and hide the one state a human must act on."""
        rec = record(node_runs=[node_run("prepare"), node_run("implement")],
                     next_node="implement", phase="awaiting_review")
        stage = view_mod.derive(THREE, rec)["stages"][1]
        self.assertEqual(stage["status"], "waiting")
        self.assertEqual(stage["action"]["kind"], "review")
        self.assertEqual(stage["action"]["run_id"], "R1")

    def test_awaiting_check_offers_the_check_command(self):
        rec = record(node_runs=[node_run("prepare")], next_node="implement",
                     phase="awaiting_check")
        stage = view_mod.derive(THREE, rec)["stages"][1]
        self.assertEqual(stage["action"]["kind"], "check")
        self.assertEqual(stage["action"]["run_id"], "R1")

    def test_gate_flag_is_carried_through(self):
        wf = workflow(
            [task("a")],
            view={"stages": [{"name": "验收", "nodes": ["a"], "gate": True}]},
        )
        derived = view_mod.derive(wf, record(next_node="a"))
        self.assertTrue(derived["stages"][0]["gate"])

    def test_one_node_can_carry_two_stages(self):
        wf = workflow(
            [task("a")],
            view={"stages": [
                {"name": "实施", "nodes": ["a"]},
                {"name": "人验", "nodes": ["a"], "gate": True},
            ]},
        )
        rec = record(node_runs=[node_run("a")], status="completed")
        self.assertEqual(
            [s["status"] for s in view_mod.derive(wf, rec)["stages"]],
            ["done", "done"],
        )


class DetailTest(unittest.TestCase):
    def derive_one(self, detail=None, rec=None, view_extra=None):
        stage = {"name": "S", "nodes": ["a"]}
        if detail is not None:
            stage["detail"] = detail
        spec = {"stages": [stage]}
        spec.update(view_extra or {})
        return view_mod.derive(workflow([task("a")], view=spec), rec)

    def test_engines_dump_is_the_fallback(self):
        rec = record(node_runs=[node_run("a", output={"k": 1}, summary="dump 1")])
        self.assertEqual(self.derive_one(rec=rec)["stages"][0]["detail"], "dump 1")

    def test_the_agents_own_summary_wins_when_present(self):
        rec = record(node_runs=[node_run("a", output={"summary": "人话结论"},
                                         summary="{\"a\":1}")])
        self.assertEqual(self.derive_one(rec=rec)["stages"][0]["detail"], "人话结论")

    def test_node_placeholder_reads_a_dotted_path(self):
        rec = record(node_runs=[node_run("a", output={"review": {"verdict": "PASS"}})])
        derived = self.derive_one("结论 ${node.a.review.verdict}", rec)
        self.assertEqual(derived["stages"][0]["detail"], "结论 PASS")

    def test_phase_and_status_placeholders(self):
        rec = record(phase="awaiting_review", status="running")
        derived = self.derive_one("${status}/${phase}", rec)
        self.assertEqual(derived["stages"][0]["detail"], "running/awaiting_review")

    def test_unknown_placeholder_renders_as_a_dash(self):
        rec = record(node_runs=[node_run("a", output={"k": 1})])
        derived = self.derive_one("x=${node.a.nope}", rec)
        self.assertEqual(derived["stages"][0]["detail"], "x=—")

    def test_a_template_still_renders_for_an_unreached_stage(self):
        derived = self.derive_one("never=${node.a.summary}", record())
        self.assertEqual(derived["stages"][0]["detail"], "never=—")


class LoopTest(unittest.TestCase):
    WF = workflow(
        [foreach("q", [1, 2, 3]), task("assemble")],
        view={"stages": [
            {"name": "逐章", "nodes": ["q"]},
            {"name": "合稿", "nodes": ["assemble"]},
        ]},
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snapshot = Path(self.tmp.name) / "items.json"

    def rec(self, instances, snapshot=None, **extra):
        state = {"node_id": "q", "kind": "foreach", "status": "active",
                 "items_snapshot": str(snapshot) if snapshot else None,
                 "instances": instances}
        return record(next_node="q", subflow_state=state, **extra)

    def test_counts_and_default_sentence(self):
        rec = self.rec([instance(0, 0, "success", "c0"),
                        instance(1, 1, "running", "c1"),
                        instance(2, 2, "pending")])
        derived = view_mod.derive(self.WF, rec)
        self.assertEqual(derived["stages"][0]["queue"]["done"], 1)
        self.assertEqual(derived["stages"][0]["queue"]["total"], 3)
        self.assertEqual(derived["stages"][0]["queue"]["left"], 2)
        self.assertEqual(derived["stages"][0]["status"], "active")
        self.assertEqual(
            derived["stages"][0]["detail"], "已完成 1/3 · 当前 1 · 还剩 2"
        )
        self.assertEqual(
            [r["status"] for r in derived["stages"][0]["queue"]["items"]],
            ["success", "running", "pending"],
        )
        self.assertEqual([r["current"] for r in derived["stages"][0]["queue"]["items"]],
                         [False, True, False])

    def test_a_finished_queue_says_so_without_a_running_item(self):
        rec = self.rec([instance(i, i, "success", f"c{i}") for i in range(3)])
        derived = view_mod.derive(self.WF, rec)
        self.assertEqual(derived["stages"][0]["detail"], "已完成 3/3 · 全部完成")
        self.assertIsNone(derived["stages"][0]["queue"]["current"])

    def test_item_label_template_reads_the_snapshot(self):
        self.snapshot.write_text(
            json.dumps([{"number": 1, "title": "第七日重生"}]), encoding="utf-8"
        )
        wf = workflow(
            [foreach("q", [{"number": 1, "title": "第七日重生"}])],
            view={"stages": [{"name": "逐章", "nodes": ["q"]}],
                  "items": {"label": "第${item.number}章 ${item.title}"}},
        )
        derived = view_mod.derive(
            wf, self.rec([instance(1, 0, "success", "c0")], snapshot=self.snapshot)
        )
        self.assertEqual(derived["stages"][0]["queue"]["items"][0]["label"], "第1章 第七日重生")

    def test_a_missing_snapshot_falls_back_to_the_instance_key(self):
        missing = Path(self.tmp.name) / "gone.json"
        derived = view_mod.derive(
            self.WF, self.rec([instance(7, 0, "success", "c0")], snapshot=missing)
        )
        self.assertEqual(derived["stages"][0]["queue"]["items"][0]["label"], "7")

    def test_a_single_subflow_is_not_a_queue(self):
        rec = record(next_node="q", subflow_state={
            "node_id": "q", "kind": "single", "status": "active",
            "instances": [instance(0, 0, "running", "c0")]})
        derived = view_mod.derive(self.WF, rec)
        self.assertNotIn("loop", derived)


class ChildStageTest(unittest.TestCase):
    """The one part that needs real files: a queue item's own stage."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.storage = Storage(str(self.base / ".aiwf")).init_dirs()
        self.child_wf = workflow(
            [task("draft"), task("review_it")],
            view={"stages": [{"name": "草拟", "nodes": ["draft"]},
                             {"name": "审阅", "nodes": ["review_it"]}]},
            id="draft-loop",
        )
        self.storage.save_workflow(self.child_wf)
        self.parent = record(
            next_node="q",
            subflow_state={"node_id": "q", "kind": "foreach", "status": "active",
                           "items_snapshot": None,
                           "instances": [instance(1, 0, "running", "CHILD")]},
        )
        self.wf = workflow(
            [foreach("q", [1])],
            view={"stages": [{"name": "逐章", "nodes": ["q"]}]},
        )

    def write_child(self, phase, next_node, status="running"):
        directory = Path(self.storage.child_dir(self.parent, "q", "1"))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "record.json").write_text(json.dumps({
            "schema_version": "1.1", "run_id": "CHILD",
            "workflow_id": "draft-loop", "workflow_version": "1.1",
            "status": status, "phase": phase,
            "started_at": "2026-10-05T10:00:00+08:00",
            "next_node": next_node, "node_runs": [], "context": {}, "events": [],
        }, ensure_ascii=False), encoding="utf-8")

    def test_child_stage_name_and_gate_bubble_up_to_the_parent(self):
        self.write_child("awaiting_review", "review_it")
        derived = view_mod.derive(self.wf, self.parent, self.storage)
        self.assertEqual(derived["stages"][0]["queue"]["current_stage"], "审阅")
        self.assertEqual(derived["stages"][0]["status"], "waiting")
        self.assertEqual(derived["stages"][0]["action"]["kind"], "review")
        self.assertEqual(derived["stages"][0]["action"]["run_id"], "CHILD")

    def test_a_child_check_is_a_check_gate(self):
        self.write_child("awaiting_check", "draft")
        derived = view_mod.derive(self.wf, self.parent, self.storage)
        self.assertEqual(derived["stages"][0]["queue"]["current_stage"], "草拟")
        self.assertEqual(derived["stages"][0]["action"]["kind"], "check")

    def test_a_missing_child_record_is_simply_unknown(self):
        derived = view_mod.derive(self.wf, self.parent, self.storage)
        self.assertIsNone(derived["stages"][0]["queue"]["current_stage"])
        self.assertEqual(derived["stages"][0]["status"], "active")


class PrintTest(unittest.TestCase):
    def render(self, derived):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_view(derived)
        return buf.getvalue()

    def test_queue_rows_indent_under_their_stage_and_the_gate_follows(self):
        wf = workflow(
            [foreach("q", [1]), task("assemble")],
            view={"stages": [{"name": "逐章", "nodes": ["q"]},
                             {"name": "合稿", "nodes": ["assemble"], "gate": True}]},
        )
        rec = record(
            next_node="assemble", phase="awaiting_review",
            node_runs=[node_run("q")],
            subflow_state={"node_id": "q", "kind": "foreach", "status": "completed",
                           "items_snapshot": None,
                           "instances": [instance(1, 0, "success", "c0"),
                                         instance(2, 1, "pending")]},
        )
        lines = self.render(view_mod.derive(wf, rec)).splitlines()
        stage_at = next(i for i, l in enumerate(lines) if "逐章" in l)
        self.assertIn("已完成 1/2 · 当前 2 · 还剩 1", lines[stage_at])
        self.assertTrue(lines[stage_at].startswith("  ["))
        rows = [l for l in lines if l.startswith("    [")]
        self.assertEqual(len(rows), 2)
        self.assertTrue(any("<- 当前" in l for l in rows))
        # the rows belong to the stage: no sibling queue block any more
        self.assertFalse(any(l.startswith("queue") for l in lines))
        gate = next(l for l in lines if "->" in l)
        self.assertIn("aiwf review R1", gate)
        self.assertIn("--action ", gate)  # without it the engine calls it stale
        self.assertIn("--verdict APPROVE|REJECT", gate)


class ViewSchemaTest(unittest.TestCase):
    def check(self, wf):
        validate_workflow(wf)

    def reject(self, wf, needle):
        # Structural violations arrive as jsonschema.ValidationError, semantic
        # ones as ValueError; both must stop the definition.
        with self.assertRaises(Exception) as caught:
            validate_workflow(wf)
        self.assertIn(needle, str(caught.exception))

    def test_a_covering_view_is_valid(self):
        self.check(workflow([task("a")], view={"stages": [{"name": "S", "nodes": ["a"]}]}))

    def test_unknown_stage_node_is_rejected(self):
        self.reject(
            workflow([task("a")], view={"stages": [{"name": "S", "nodes": ["zzz"]}]}),
            "unknown node",
        )

    def test_an_uncovered_node_is_rejected(self):
        self.reject(
            workflow([task("a"), task("b")],
                     view={"stages": [{"name": "S", "nodes": ["a"]}]}),
            "missing: ['b']",
        )

    def test_unknown_placeholder_root_is_rejected(self):
        self.reject(
            workflow([task("a")], view={"stages": [
                {"name": "S", "nodes": ["a"], "detail": "${oops.x}"}]}),
            "unknown placeholder",
        )

    def test_unknown_node_in_a_template_is_rejected(self):
        self.reject(
            workflow([task("a")], view={"stages": [
                {"name": "S", "nodes": ["a"], "detail": "${node.zzz.summary}"}]}),
            "unknown node 'zzz'",
        )

    def test_loop_fields_are_whitelisted(self):
        self.reject(
            workflow([foreach("q", [1])], view={"stages": [
                {"name": "S", "nodes": ["q"], "detail": "${loop.wat}"}]}),
            "must be one of",
        )

    def test_item_is_only_legal_in_the_label_template(self):
        self.reject(
            workflow([task("a")], view={"stages": [
                {"name": "S", "nodes": ["a"], "detail": "${item.name}"}]}),
            "only valid inside view.items.label",
        )
        self.check(workflow(
            [task("a")],
            view={"stages": [{"name": "S", "nodes": ["a"]}],
                  "items": {"label": "${item.name}"}},
        ))

    def test_unknown_key_inside_a_stage_is_rejected(self):
        self.reject(
            workflow([task("a")], view={"stages": [
                {"name": "S", "nodes": ["a"], "bogus": 1}]}),
            "bogus",
        )


class ViewCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.storage = Storage(str(self.base / ".aiwf")).init_dirs()
        self.engine = WorkflowEngine(self.storage)

    def cli(self, *args):
        env = dict(os.environ, AIWF_HOME=str(self.base / ".aiwf"), **CLI_ENV)
        return subprocess.run([*CLI, *args], env=env, cwd=str(self.base),
                              text=True, capture_output=True)

    def start(self, wf, complete=False):
        """A fresh run has no node_runs yet, so complete one when a test needs
        the node stream to actually be present."""
        self.storage.save_workflow(wf)
        record = self.engine.start(wf, {})
        run_id = record["run_id"]
        self.engine.settle(run_id)
        if complete:
            spec = self.engine.action_spec(run_id)
            self.engine.act(run_id, spec["action"]["id"], "complete", output="ok")
        return run_id

    def test_a_definition_without_a_view_prints_no_view_section(self):
        run_id = self.start(workflow([task("a")]))
        proc = self.cli("runs", "show", run_id)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("view       :", proc.stdout)
        self.assertIn(f"run_id     : {run_id}", proc.stdout)

    def test_the_view_is_rendered_above_the_node_stream(self):
        run_id = self.start(
            workflow([task("a")], view={"stages": [{"name": "做", "nodes": ["a"]}]}),
            complete=True,
        )
        proc = self.cli("runs", "show", run_id)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("nodes:", proc.stdout)
        self.assertLess(proc.stdout.index("view       :"),
                        proc.stdout.index("nodes:"))

    def test_view_json_alone_is_machine_readable(self):
        run_id = self.start(workflow(
            [task("a")], view={"stages": [{"name": "做", "nodes": ["a"]}]}))
        proc = self.cli("runs", "show", run_id, "--view", "--json")
        self.assertEqual(proc.returncode, 0)
        derived = json.loads(proc.stdout)
        self.assertEqual(derived["stages"][0]["name"], "做")
        self.assertEqual(derived["stages"][0]["status"], "active")

    def test_asking_for_a_view_that_does_not_exist_fails_loudly(self):
        run_id = self.start(workflow([task("a")]))
        proc = self.cli("runs", "show", run_id, "--view")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("declares no view", proc.stderr)

    def test_the_gate_command_the_view_prints_actually_runs(self):
        """A command that looks runnable but comes back "stale action" is worse
        than printing none -- so the printed command is executed here verbatim."""
        wf = {
            "schema_version": "1.1", "id": "gate", "name": "gate",
            "max_steps": 20,
            "nodes": [{"id": "a", "type": "task", "prompt": "do",
                       "review": {"requirements": ["looks right"]}}],
            "view": {"stages": [{"name": "验收", "nodes": ["a"], "gate": True}]},
        }
        self.storage.save_workflow(wf)
        run_id = self.engine.start(wf, {})["run_id"]
        self.engine.settle(run_id)
        spec = self.engine.action_spec(run_id)
        self.engine.act(run_id, spec["action"]["id"], "complete", output="ok")
        self.engine.settle(run_id)

        proc = self.cli("runs", "show", run_id, "--view")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        line = next(l for l in proc.stdout.splitlines() if l.strip().startswith("->"))
        command = line.split("->", 1)[1].strip().replace("APPROVE|REJECT", "APPROVE")

        argv = command.split()
        self.assertEqual(argv[0], "aiwf")
        ran = self.cli(*argv[1:] or ["--help"])
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(self.storage.find_run(run_id)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
