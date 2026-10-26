"""L3 authoring: scaffold (`aiwf new`) and the `use` inputs shorthand."""
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


class L3CliTest(unittest.TestCase):
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

    def write(self, workflow):
        target = self.base / ".aiwf" / "workflows" / f"{workflow['id']}.json"
        target.write_text(json.dumps(workflow, ensure_ascii=False), encoding="utf-8")
        return target

    def run_json(self, *args):
        proc = self.cli("run", *args, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    # ---- scaffold --------------------------------------------------------
    def test_new_sequential_is_valid_and_runnable(self):
        created = self.cli("new", "opening")
        self.assertEqual(created.returncode, 0, created.stderr)
        path = self.base / ".aiwf" / "workflows" / "opening.json"
        self.assertTrue(path.is_file())
        validated = self.cli("validate", str(path))
        self.assertEqual(validated.returncode, 0, validated.stderr)
        spec = self.run_json("opening", "--title", "t", "-p", "idea=x")
        self.assertEqual(spec["node"]["id"], "draft")

    def test_new_checked_contains_check_and_review(self):
        self.assertEqual(self.cli("new", "reviewed", "--kind", "checked").returncode, 0)
        data = json.loads((self.base / ".aiwf" / "workflows" / "reviewed.json")
                          .read_text(encoding="utf-8"))
        node = data["nodes"][0]
        self.assertIn("check", node)
        self.assertIn("review", node)

    def test_new_batch_starts_awaiting_items(self):
        self.assertEqual(self.cli("new", "batch", "--kind", "batch").returncode, 0)
        spec = self.run_json("batch", "--title", "t")
        self.assertEqual(spec["phase"], "awaiting_items")

    def test_new_refuses_to_overwrite_without_force(self):
        self.assertEqual(self.cli("new", "opening").returncode, 0)
        again = self.cli("new", "opening")
        self.assertEqual(again.returncode, 2)
        self.assertIn("already exists", again.stderr)
        forced = self.cli("new", "opening", "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)

    # ---- use shorthand ---------------------------------------------------
    def use_workflow(self, *, extra_inputs=None):
        node = {
            "id": "write", "type": "task", "use": ["idea"],
            "prompt": "写 ${idea}", "output": {"file": "${run.dir}/doc.md"},
        }
        if extra_inputs is not None:
            node["inputs"] = extra_inputs
        return {
            "schema_version": "1.1", "id": "use", "name": "use",
            "input_schema": {
                "type": "object", "required": ["idea", "seed"],
                "properties": {"idea": {"type": "string"},
                               "seed": {"type": "string"}},
            },
            "nodes": [node],
        }

    def test_use_expands_to_materials(self):
        self.write(self.use_workflow())
        spec = self.run_json("use", "--title", "t",
                             "-p", "idea=from-idea", "-p", "seed=from-seed")
        self.assertEqual(spec["node"]["inputs"], {"idea": "from-idea"})

    def test_explicit_inputs_wins_over_use(self):
        self.write(self.use_workflow(extra_inputs={"idea": {"ref": "seed"}}))
        spec = self.run_json("use", "--title", "t",
                             "-p", "idea=from-idea", "-p", "seed=from-seed")
        self.assertEqual(spec["node"]["inputs"], {"idea": "from-seed"})

    def test_use_is_rejected_outside_task_and_branch(self):
        bad = {
            "schema_version": "1.1", "id": "bad", "name": "bad",
            "nodes": [{
                "id": "f", "type": "foreach", "use": ["idea"], "items": [1],
                "nodes": [{"id": "s", "type": "task", "prompt": "x"}],
            }],
        }
        path = self.base / "bad.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        proc = self.cli("validate", str(path))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("use is only supported", proc.stderr)


if __name__ == "__main__":
    unittest.main()
