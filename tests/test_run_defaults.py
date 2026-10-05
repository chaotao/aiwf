"""Project-level run defaults: ``<root>/config.json`` -> ``run_defaults``.

A template declares the inputs it needs (``input_schema``), but values like the
project dir, a knowledge base path or a test command are properties of the
*project*, not of the run - retyping them on every ``aiwf run -p`` is how a
template stops being reusable. ``run_defaults`` holds them once per project.

Merge order is later-wins::

    run_defaults  ->  --input-file  ->  -p/--param

and defaults are filtered to keys the definition declares, so one project's
settings never leak into unrelated workflows. Nothing here weakens validation:
a required input that the project does not configure still fails the run.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aiwf import Storage


ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "aiwf"]
BASE_ENV = dict(os.environ, PYTHONPATH=str(ROOT / "engine"))
HOME = ".aiwf"

# Deliberately local to the test: the library may change, the mechanism may not.
DEFINITION = {
    "schema_version": "1.1",
    "id": "needs-input",
    "name": "需要入参",
    "input_schema": {
        "type": "object",
        "required": ["subject"],
        "properties": {
            "subject": {"type": "string"},
            "criteria": {"type": "string"},
        },
    },
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "work", "type": "task", "prompt": "处理 ${subject}",
         "output_key": "work"},
        {"id": "end", "type": "end"},
    ],
}


class RunDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        (self.project / HOME / "workflows").mkdir(parents=True)
        self.write_json(
            self.project / HOME / "workflows" / "needs-input.json", DEFINITION
        )

    # ---- helpers ---------------------------------------------------------
    def write_json(self, path, value):
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def config(self, value):
        self.write_json(self.project / HOME / "config.json", value)

    def cli(self, *args):
        return subprocess.run(
            [*CLI, *args], cwd=self.project, env=BASE_ENV,
            text=True, capture_output=True,
        )

    def context_of(self, stdout):
        run_id = ""
        for line in stdout.splitlines():
            if line.startswith("run_id"):
                run_id = line.split(":", 1)[1].strip()
                break
        self.assertTrue(run_id, f"no run_id in output:\n{stdout}")
        record = Storage(str(self.project / HOME)).find_run(run_id)
        return record.get("context") or {}

    # ---- the problem this solves -----------------------------------------
    def test_required_inputs_still_fail_without_a_config(self):
        result = self.cli("run", "needs-input")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'subject' is a required property", result.stderr)

    def test_config_defaults_fill_required_inputs(self):
        self.config({"run_defaults": {"subject": "from-config"}})
        result = self.cli("run", "needs-input")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.context_of(result.stdout)["subject"], "from-config")

    # ---- merge order -----------------------------------------------------
    def test_param_wins_over_config_and_other_defaults_survive(self):
        self.config({"run_defaults": {
            "subject": "from-config", "criteria": "from-config",
        }})
        result = self.cli("run", "needs-input", "-p", "subject=from-param")
        self.assertEqual(result.returncode, 0, result.stderr)
        context = self.context_of(result.stdout)
        self.assertEqual(context["subject"], "from-param")
        # per-key merge, not a wholesale replacement of the defaults layer
        self.assertEqual(context["criteria"], "from-config")

    def test_input_file_sits_between_config_and_params(self):
        self.config({"run_defaults": {
            "subject": "from-config", "criteria": "from-config",
        }})
        inputs = self.project / "inputs.json"
        self.write_json(inputs, {"subject": "from-file", "criteria": "from-file"})

        from_file = self.cli("run", "needs-input", "--input-file", str(inputs))
        self.assertEqual(from_file.returncode, 0, from_file.stderr)
        context = self.context_of(from_file.stdout)
        self.assertEqual(context["subject"], "from-file")
        self.assertEqual(context["criteria"], "from-file")

        param_wins = self.cli(
            "run", "needs-input", "--input-file", str(inputs),
            "-p", "subject=from-param",
        )
        self.assertEqual(param_wins.returncode, 0, param_wins.stderr)
        context = self.context_of(param_wins.stdout)
        self.assertEqual(context["subject"], "from-param")
        self.assertEqual(context["criteria"], "from-file")

    # ---- filtering -------------------------------------------------------
    def test_undeclared_defaults_are_not_injected(self):
        self.config({"run_defaults": {
            "subject": "ok", "project_dir": "D:/somewhere", "typo_key": 1,
        }})
        result = self.cli("run", "needs-input")
        self.assertEqual(result.returncode, 0, result.stderr)
        context = self.context_of(result.stdout)
        self.assertEqual(context["subject"], "ok")
        self.assertNotIn("project_dir", context)
        self.assertNotIn("typo_key", context)

    # ---- validate --project advisories -----------------------------------
    def test_validate_project_reports_uncovered_and_unused_keys(self):
        self.config({"run_defaults": {"criteria": "covered", "typo_key": 1}})
        result = self.cli("validate", "--project")
        # advisory: gaps are notes, not failures
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("needs-input.subject", result.stdout)
        self.assertNotIn("needs-input.criteria", result.stdout)
        self.assertIn("typo_key", result.stdout)

    def test_validate_project_is_quiet_when_fully_covered(self):
        self.config({"run_defaults": {"subject": "covered"}})
        result = self.cli("validate", "--project")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("run_defaults", result.stdout)

    # ---- broken config ---------------------------------------------------
    def test_broken_config_is_reported_not_swallowed(self):
        (self.project / HOME / "config.json").write_text(
            "{ not json", encoding="utf-8"
        )
        broken = self.cli("run", "needs-input")
        self.assertEqual(broken.returncode, 2)
        self.assertIn("config.json", broken.stderr)

        self.write_json(self.project / HOME / "config.json", [1, 2])
        wrong_shape = self.cli("run", "needs-input")
        self.assertEqual(wrong_shape.returncode, 2)
        self.assertIn("must contain a JSON object", wrong_shape.stderr)

    def test_run_defaults_must_be_an_object(self):
        self.config({"run_defaults": ["not", "a", "mapping"]})
        result = self.cli("run", "needs-input")
        self.assertEqual(result.returncode, 2)
        self.assertIn("run_defaults", result.stderr)


if __name__ == "__main__":
    unittest.main()
