"""Execute the shipped documentation examples with synthetic Agent/human fixtures.

These tests validate engine contracts, not literary quality or real code changes.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aiwf import Storage, WorkflowEngine
from aiwf.schema import validate_run_record, validate_workflow


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "skills/aiwf/assets/examples"
# The POSIX engine/bin/aiwf launcher is not executable on Windows, so drive the
# module through the current interpreter instead.
CLI = [sys.executable, "-m", "aiwf"]
CLI_ENV = dict(os.environ, PYTHONPATH=str(ROOT / "engine"))


class SkillExamplesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.storage = Storage(str(self.base / ".aiwf")).init_dirs()
        self.engine = WorkflowEngine(self.storage)
        self.definitions = {}
        for path in EXAMPLES.glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if "schema_version" in value:
                validate_workflow(value)
                self.definitions[value["id"]] = value
                self.storage.save_workflow(value)

    def start(self, name, inputs):
        self.workflow = self.definitions[name]
        record = self.engine.start(self.workflow, inputs)
        self.rid = record["run_id"]
        self.engine.settle(self.rid)
        return self.engine.action_spec(self.rid)

    def act(self, output=None, command="complete", **kwargs):
        spec = self.engine.action_spec(self.rid)
        if command == "complete":
            kwargs["output"] = output
        return self.engine.act(self.rid, spec["action"]["id"], command, **kwargs)

    def file(self, path, contents="Synthetic test fixture"):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return {"$file": str(path)}

    def result(self, spec, exit_code=0):
        node = spec["node"]
        if node.get("output"):
            out = self.file(node["output"]["file"])
            if "summary" in (node.get("output_schema") or {}).get("required", []):
                out["summary"] = "Synthetic fixture"
            return out
        if node["id"] == "plan":
            return {"goal": "fixture", "steps": ["first", "second"]}
        if node["id"] == "analysis":
            return {"summary": "fixture analysis"}
        if node["id"] == "write":
            return {
                "script": self.file(self.base / "script.md"),
                "shots": self.file(self.base / "shots.json", "[]"),
            }
        if node["id"] == "fix":
            return {
                "summary": "fixture only",
                "changed_files": ["src/example.py"],
                "validation": {
                    "exit_code": exit_code,
                    "report": self.file(self.base / "validation.txt"),
                },
            }
        if node["id"] == "tests":
            return {"exit_code": exit_code, "report": self.file(self.base / "tests.txt")}
        self.fail(f"No synthetic result for {node}")

    def finish(self, spec, label="FAST", exit_code=0):
        seen = []
        for _ in range(40):
            if spec["status"] == "completed":
                validate_run_record(self.storage.find_run(self.rid))
                return seen
            self.assertEqual(spec["status"], "running", spec.get("error"))
            kind = spec["action"]["kind"]
            seen.append((spec["action"]["node_id"], kind))
            if kind == "check":
                spec = self.act({"verdict": "PASS", "reason": "Synthetic fixture approval"})
            elif kind == "review":
                spec = self.act(command="review", verdict="APPROVE", feedback="Synthetic human fixture")
            elif kind == "provide":
                spec = self.act(command="provide", values={"tone": "冷峻"})
            elif kind == "complete":
                output = label if spec["node"]["type"] == "branch" else self.result(spec, exit_code)
                spec = self.act(output)
            else:
                self.fail(f"Unexpected waiting action: {spec}")
        self.fail("Example did not terminate within fixture budget")

    def test_all_static_examples_and_project_contracts(self):
        cases = {
            "sequential": {"idea": "fixture"},
            "user-check-review": {"idea": "fixture"},
            "multi-file": {"idea": "fixture"},
            "code-change": {
                "project_dir": str(self.base), "request": "fixture",
                "conventions_file": "none", "test_cmd": "python -m pytest -q",
            },
            "novel-chapters": {"premise": "fixture 题材", "chapters": [{"number": 1, "title": "a"}, {"number": 2, "title": "b"}]},
            "game-resources": {"project_dir": str(self.base), "resources": [{"key": "player", "path": "res://player.tscn"}]},
            "analyze-piece": {"text": "fixture"},
            "subflow-parent": {"text": "fixture"},
            "condition-routing": {"project_dir": str(self.base)},
            "subjective-routing": {"idea": "fixture"},
        }
        self.assertEqual(set(self.definitions), set(cases) | {"queue-items"})
        validated = subprocess.run(
            [*CLI, "validate", "--project"],
            cwd=self.base, env=CLI_ENV, text=True, encoding="utf-8",
            capture_output=True,
        )
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        for name, inputs in cases.items():
            with self.subTest(name=name):
                self.finish(self.start(name, inputs))

    def test_both_condition_and_subjective_routes(self):
        for exit_code, target in [(0, "deliver"), (1, "diagnose")]:
            seen = self.finish(
                self.start("condition-routing", {"project_dir": str(self.base)}),
                exit_code=exit_code,
            )
            self.assertIn((target, "complete"), seen)
            self.assertNotIn(("gate", "complete"), seen)
        for label, target in [("FAST", "fast_script"), ("SLOW", "slow_script")]:
            seen = self.finish(self.start("subjective-routing", {"idea": "fixture"}), label=label)
            self.assertIn((target, "complete"), seen)
            self.assertIn(("accept", "review"), seen)

    def test_rejection_retry_and_real_input_gate_contract(self):
        spec = self.start("user-check-review", {"idea": "fixture"})
        self.assertEqual(spec["action"]["kind"], "provide")
        with self.assertRaises(Exception):
            self.act(command="provide", values={"tone": "invalid"})
        spec = self.act(command="provide", values={"tone": "冷峻"})
        spec = self.act({"wrong": "missing declared file"})
        self.assertEqual(spec["phase"], "executing")
        record = self.storage.find_run(self.rid)
        self.assertEqual(record["node_runs"][-1]["status"], "rejected")
        spec = self.act(self.result(spec))
        self.assertEqual(spec["action"]["kind"], "check")
        spec = self.act({"verdict": "REVISE", "reason": "Synthetic revision request"})
        self.assertEqual(spec["action"]["kind"], "complete")
        self.assertIsNotNone(spec["node"]["previous_output"])
        self.finish(spec)

    def test_previous_exports_and_child_scope(self):
        spec = self.start("novel-chapters", {
            "premise": "fixture 题材",
            "chapters": [{"number": 1, "title": "a"}, {"number": 2, "title": "b"}],
        })
        # the queue sits behind three preparation nodes; walk them first
        while spec["action"]["node_id"] != "draft":
            if spec["action"]["kind"] == "check":
                spec = self.act({"verdict": "PASS", "reason": "Synthetic fixture"})
            else:
                spec = self.act(self.result(spec))
        self.assertIsNone(spec["node"]["inputs"]["previous"])
        first = self.result(spec)
        self.act(first)
        spec = self.act({"verdict": "PASS", "reason": "Synthetic fixture"})
        self.assertEqual(spec["node"]["inputs"]["previous"], {"draft": first})
        self.assertEqual(spec["node"]["inputs"]["chapter"]["number"], 2)
        self.finish(spec)

    def test_queue_wait_append_and_seal(self):
        """The queue lives in the run record: `aiwf items` (the engine API) is
        the only writer, and `seal` is what ends the wait."""
        spec = self.start("queue-items", {})
        # the plan node is what feeds the queue; walk it, then the run lands
        # on the wait instead of starting instances
        while spec["phase"] != "awaiting_items":
            if spec["action"]["kind"] == "check":
                spec = self.act({"verdict": "PASS", "reason": "Synthetic fixture"})
            else:
                spec = self.act(self.result(spec))
        self.assertEqual(spec["phase"], "awaiting_items")
        self.engine.queue_add(self.rid, "chapters", [
            {"id": "1", "name": "first", "title": "第一章"},
        ])
        self.engine.settle(self.rid)
        spec = self.engine.action_spec(self.rid)
        self.assertEqual(spec["node"]["inputs"]["chapter"]["title"], "第一章")
        spec = self.act(self.result(spec))
        self.assertEqual(spec["action"]["kind"], "check")
        spec = self.act({"verdict": "PASS", "reason": "Synthetic fixture"})
        self.assertEqual(spec["phase"], "awaiting_items")
        self.engine.queue_add(self.rid, "chapters", [
            {"id": "2", "name": "second", "title": "第二章"},
        ])
        self.engine.settle(self.rid)
        spec = self.engine.action_spec(self.rid)
        self.assertEqual(spec["node"]["inputs"]["chapter"]["title"], "第二章")
        spec = self.act(self.result(spec))
        self.assertEqual(spec["action"]["kind"], "check")
        spec = self.act({"verdict": "PASS", "reason": "Synthetic fixture"})
        self.assertEqual(spec["phase"], "awaiting_items")
        self.engine.queue_seal(self.rid, "chapters")
        self.engine.settle(self.rid)
        spec = self.engine.action_spec(self.rid)
        self.assertEqual(spec["node"]["id"], "assemble")
        self.finish(spec)


if __name__ == "__main__":
    unittest.main()
