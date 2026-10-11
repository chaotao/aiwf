"""CLI JSON surface: `aiwf workflows --json` and `aiwf runs list --json`.

The two list commands were text-only; these tests pin the machine-readable
shape (field-per-column, matching the text table) so downstream consumers can
parse JSON instead of column positions.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aiwf import Storage, WorkflowEngine


REPO = Path(__file__).resolve().parents[1]
# Drive the CLI through the current interpreter (the POSIX engine/bin/aiwf
# cannot be executed directly on Windows).
CLI = [sys.executable, "-m", "aiwf"]
CLI_ENV = {"PYTHONPATH": str(REPO / "engine")}

RUN_COLUMNS = [
    "started_at", "run_id", "workflow_id", "status",
    "duration_ms", "node_count", "parent_run",
]


def workflow(**extra):
    return {
        "schema_version": "1.1", "id": "jsoncheck", "name": "JSON check",
        "max_steps": 100,
        "nodes": [{"id": "draft", "type": "task", "prompt": "Execute the task."}],
        **extra,
    }


class CliJsonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Storage(os.path.join(self.tmp.name, ".aiwf")).init_dirs()
        self.engine = WorkflowEngine(self.storage)

    def cli(self, *args):
        env = dict(os.environ)
        env.update(CLI_ENV)
        return subprocess.run(
            [*CLI, *args], cwd=self.tmp.name, env=env,
            capture_output=True, text=True, encoding="utf-8",
        )

    def start(self):
        wf = workflow()
        self.storage.save_workflow(wf)
        return self.engine.start(wf, {})

    # ---- workflows -------------------------------------------------------
    def test_workflows_json_empty_is_empty_array(self):
        proc = self.cli("workflows", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [])

    def test_workflows_json_lists_id_and_name(self):
        self.storage.save_workflow(workflow())
        proc = self.cli("workflows", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout),
            [{"id": "jsoncheck", "name": "JSON check"}],
        )

    def test_workflows_text_still_works(self):
        self.storage.save_workflow(workflow())
        proc = self.cli("workflows")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("jsoncheck", proc.stdout)
        self.assertIn("JSON check", proc.stdout)

    # ---- runs list -------------------------------------------------------
    def test_runs_list_json_empty_is_empty_array(self):
        proc = self.cli("runs", "list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [])

    def test_runs_list_json_matches_text_columns(self):
        record = self.start()
        proc = self.cli("runs", "list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(proc.stdout)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(sorted(row), sorted(RUN_COLUMNS))
        self.assertEqual(row["started_at"], record["started_at"])
        self.assertEqual(row["run_id"], record["run_id"])
        self.assertEqual(row["workflow_id"], record["workflow_id"])
        self.assertEqual(row["status"], record["status"])
        self.assertEqual(row["duration_ms"], record["duration_ms"])
        self.assertEqual(row["node_count"], len(record["node_runs"]))
        self.assertIsNone(row["parent_run"])

    def test_runs_list_json_honours_status_filter(self):
        self.start()
        hit = self.cli("runs", "list", "--status", "running", "--json")
        miss = self.cli("runs", "list", "--status", "completed", "--json")
        self.assertEqual(hit.returncode, 0, hit.stderr)
        self.assertEqual(miss.returncode, 0, miss.stderr)
        self.assertEqual(len(json.loads(hit.stdout)), 1)
        self.assertEqual(json.loads(miss.stdout), [])

    def test_runs_list_text_still_works(self):
        self.start()
        proc = self.cli("runs", "list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("jsoncheck", proc.stdout)


if __name__ == "__main__":
    unittest.main()
