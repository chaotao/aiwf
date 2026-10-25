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
from aiwf.cli import _cell, _clip, _display_width


REPO = Path(__file__).resolve().parents[1]
# Drive the CLI through the current interpreter (the POSIX engine/bin/aiwf
# cannot be executed directly on Windows).
CLI = [sys.executable, "-m", "aiwf"]
CLI_ENV = {"PYTHONPATH": str(REPO / "engine")}

RUN_COLUMNS = [
    "started_at", "run_id", "workflow_id", "title", "status",
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

    def start(self, title="上线前的最后一遍检查"):
        wf = workflow()
        self.storage.save_workflow(wf)
        return self.engine.start(wf, {}, title=title)

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
        self.assertEqual(row["title"], record["title"])
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

    # ---- the title column ------------------------------------------------
    def test_runs_list_leads_with_the_title(self):
        """The title is what makes a listing readable at a glance, so it comes
        first -- before the id nobody can decode."""
        self.start("第十章 重写")
        proc = self.cli("runs", "list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = proc.stdout.strip().splitlines()[0]
        self.assertTrue(line.startswith("第十章 重写"), line)
        self.assertIn("jsoncheck", line)

    def test_a_run_without_a_title_falls_back_to_its_run_id(self):
        record = self.engine.start(workflow(), {})
        proc = self.cli("runs", "list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(
            proc.stdout.strip().splitlines()[0].startswith(record["run_id"])
        )

    def test_a_long_title_is_clipped_without_pushing_the_columns_apart(self):
        self.start("一个非常非常长的标题" * 4)
        proc = self.cli("runs", "list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = proc.stdout.strip().splitlines()[0]
        self.assertIn("…", line)
        self.assertIn("jsoncheck", line)


class ColumnWidthTest(unittest.TestCase):
    """Titles are routinely written in Chinese, where one glyph occupies two
    columns: padding by ``len()`` would ragged-align every listing."""

    def test_a_wide_glyph_counts_as_two_columns(self):
        self.assertEqual(_display_width("末世"), 4)
        self.assertEqual(_display_width("ab"), 2)

    def test_a_cell_is_exactly_as_wide_as_asked_for(self):
        for text in ("末世", "mixed 混排", "a very long latin title indeed"):
            self.assertEqual(_display_width(_cell(text, 12)), 12)

    def test_clipping_marks_what_was_lost(self):
        self.assertEqual(_clip("abcdef", 4), "abc…")
        self.assertEqual(_clip("abc", 4), "abc")
        self.assertEqual(_display_width(_cell("末世重生攻略大纲", 6)), 6)


if __name__ == "__main__":
    unittest.main()
