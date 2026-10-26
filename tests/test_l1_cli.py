"""L1 protocol ergonomics: fewer ids to copy, same state-machine safety.

The run/action selectors are conveniences; when they are ambiguous the CLI
must say so instead of guessing.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "aiwf"]
ENV = {k: v for k, v in os.environ.items() if k != "PYTHONUTF8"}
ENV["PYTHONPATH"] = str(REPO / "engine")


def two_step(workflow_id="two"):
    return {
        "schema_version": "1.1",
        "id": workflow_id,
        "name": workflow_id,
        "input_schema": {
            "type": "object", "required": ["idea"],
            "properties": {"idea": {"type": "string"}},
        },
        "nodes": [
            {"id": "first", "type": "task", "prompt": "第一步。",
             "output": {"file": "${run.dir}/first.md"}},
            {"id": "second", "type": "task", "prompt": "第二步。",
             "output": {"file": "${run.dir}/second.md"}},
        ],
    }


def queue_workflow():
    return {
        "schema_version": "1.1",
        "id": "batch",
        "name": "batch",
        "nodes": [{
            "id": "work", "type": "foreach", "items": {"queue": True},
            "nodes": [{
                "id": "step", "type": "task", "prompt": "处理条目。",
                "output": {"file": "${run.dir}/${item.id}.md"},
            }],
        }],
    }


class L1CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.assertEqual(self.cli("init").returncode, 0)

    def cli(self, *args):
        return subprocess.run(
            [*CLI, *args], cwd=self.base, env=ENV,
            text=True, encoding="utf-8", capture_output=True,
        )

    def save(self, workflow):
        target = self.base / ".aiwf" / "workflows" / f"{workflow['id']}.json"
        target.write_text(json.dumps(workflow, ensure_ascii=False), encoding="utf-8")

    def start(self, workflow_id="two", title="L1", *extra):
        proc = self.cli("run", workflow_id, "--title", title,
                        "-p", "idea=x", *extra, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def artifact(self, spec):
        path = spec["node"]["output"]["file"]
        target = Path(path) if Path(path).is_absolute() else self.base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        return path

    # ---- action selectors ------------------------------------------------
    def test_action_can_be_omitted(self):
        self.save(two_step())
        spec = self.start()
        path = self.artifact(spec)
        proc = self.cli("submit", spec["run_id"], "--file", path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["node"]["id"], "second")

    def test_action_accepts_a_unique_prefix(self):
        self.save(two_step())
        spec = self.start()
        path = self.artifact(spec)
        prefix = spec["action"]["id"][:6]
        proc = self.cli("submit", spec["run_id"], "--action", prefix,
                        "--file", path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["node"]["id"], "second")

    def test_action_accepts_the_current_node_id(self):
        self.save(two_step())
        spec = self.start()
        path = self.artifact(spec)
        proc = self.cli("submit", spec["run_id"], "--action", "first",
                        "--file", path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["node"]["id"], "second")

    # ---- run selectors ---------------------------------------------------
    def test_run_id_and_action_can_both_be_omitted(self):
        self.save(two_step())
        spec = self.start()
        path = self.artifact(spec)
        proc = self.cli("submit", "--file", path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["node"]["id"], "second")

    def test_status_without_a_run_id_picks_the_running_run(self):
        self.save(two_step())
        spec = self.start()
        proc = self.cli("status", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["run_id"], spec["run_id"])

    def test_run_id_accepts_a_unique_prefix(self):
        self.save(two_step())
        spec = self.start()
        proc = self.cli("status", spec["run_id"][:12], "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["run_id"], spec["run_id"])

    def test_multiple_running_runs_require_an_explicit_id(self):
        self.save(two_step())
        self.start(title="first")
        self.start(title="second")
        proc = self.cli("submit", "--file", "x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("multiple runs", proc.stderr)

    def test_runs_show_without_an_id_uses_the_latest(self):
        self.save(two_step())
        self.start(title="older")
        latest = self.start(title="newer")
        proc = self.cli("runs", "show", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["run_id"], latest["run_id"])

    def test_runs_list_limit(self):
        self.save(two_step())
        self.start(title="one")
        self.start(title="two")
        proc = self.cli("runs", "list", "--limit", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(proc.stdout.strip().splitlines()), 1)

    def test_status_prefers_running_over_a_newer_finished_run(self):
        """`status` answers "what am I working on"; `runs show` answers
        "what happened last". With two live runs and a newer finished one, the
        two commands must disagree."""
        self.save(two_step())
        self.start(title="live A")
        live_b = self.start(title="live B")
        finished = self.start(title="finished C")
        first = self.artifact(finished)
        step2 = json.loads(self.cli(
            "submit", finished["run_id"], "--file", first, "--json").stdout)
        second = self.artifact(step2)
        self.cli("submit", finished["run_id"], "--file", second)

        current = self.cli("status", "--json")
        self.assertEqual(json.loads(current.stdout)["run_id"], live_b["run_id"])
        latest = self.cli("runs", "show", "--json")
        self.assertEqual(json.loads(latest.stdout)["run_id"], finished["run_id"])

    # ---- resume ----------------------------------------------------------
    def test_resume_takes_only_the_run_id(self):
        self.save(two_step())
        spec = self.start()
        failed = self.cli("submit", spec["run_id"], "--action",
                          spec["action"]["id"], "--fail", "temporary")
        self.assertEqual(failed.returncode, 1, failed.stderr)
        proc = self.cli("resume", spec["run_id"], "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "running")

    # ---- status ----------------------------------------------------------
    def test_status_reports_title_and_next_command(self):
        self.save(two_step())
        spec = self.start(title="状态测试")
        proc = self.cli("status", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["run_id"], spec["run_id"])
        self.assertEqual(payload["title"], "状态测试")
        self.assertEqual(payload["next"]["text"], "aiwf submit")

        text = self.cli("status")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("状态测试", text.stdout)
        self.assertIn("next  : aiwf submit", text.stdout)

    def test_status_after_completion_says_finished(self):
        self.save(two_step())
        spec = self.start()
        first = self.artifact(spec)
        step2 = json.loads(self.cli(
            "submit", spec["run_id"], "--file", first, "--json").stdout)
        second = self.artifact(step2)
        self.cli("submit", spec["run_id"], "--file", second)
        proc = self.cli("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("(none; run finished)", proc.stdout)

    # ---- items -----------------------------------------------------------
    def test_items_infer_the_only_queue_and_accept_a_file(self):
        self.save(queue_workflow())
        spec = self.start("batch")
        self.assertEqual(spec["phase"], "awaiting_items")
        items = self.base / "items.json"
        items.write_text(json.dumps([
            {"id": "A", "name": "a"},
            {"id": "B", "name": "b"},
        ]), encoding="utf-8")
        added = self.cli("items", "add", spec["run_id"],
                         "--item-file", str(items), "--json")
        self.assertEqual(added.returncode, 0, added.stderr)
        table = json.loads(added.stdout)["queue"]
        self.assertEqual(table["node"], "work")
        self.assertEqual(table["total"], 2)

        listed = self.cli("items", "list", spec["run_id"], "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)["queue"]["total"], 2)


if __name__ == "__main__":
    unittest.main()
