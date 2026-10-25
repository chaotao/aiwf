"""L0 robustness: the CLI must be safe for a careless (or weak) caller.

Each test pins one promise an agent depends on: errors are compact and typed,
the exit code says whether anything advanced, payload flags remove shell
quoting, and the spec names the next command.
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
# Deliberately without PYTHONUTF8: the CLI itself must guarantee UTF-8 output.
ENV = {k: v for k, v in os.environ.items() if k != "PYTHONUTF8"}
ENV["PYTHONPATH"] = str(REPO / "engine")


def file_workflow(workflow_id="doc", with_check=False):
    node = {
        "id": "write",
        "type": "task",
        "prompt": "写一段正文。",
        "output": {"file": "${run.dir}/doc.md"},
    }
    if with_check:
        node["check"] = {"criteria": ["正文非空"], "max_attempts": 2}
        node["review"] = {"requirements": ["可以定稿"]}
    return {
        "schema_version": "1.1",
        "id": workflow_id,
        "name": workflow_id,
        "input_schema": {
            "type": "object", "required": ["idea"],
            "properties": {"idea": {"type": "string"}},
        },
        "nodes": [node],
    }


def two_step_workflow(workflow_id="two"):
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


class L0CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        proc = self.cli("init")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def cli(self, *args):
        return subprocess.run(
            [*CLI, *args], cwd=self.base, env=ENV,
            text=True, encoding="utf-8", capture_output=True,
        )

    def save(self, workflow):
        target = self.base / ".aiwf" / "workflows" / f"{workflow['id']}.json"
        target.write_text(json.dumps(workflow, ensure_ascii=False), encoding="utf-8")
        return target

    def start(self, workflow_id="doc", *extra):
        return self.cli("run", workflow_id, "--title", "L0", "-p", "idea=x",
                        *extra)

    def start_json(self, workflow_id="doc"):
        proc = self.start(workflow_id, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def artifact(self, spec, text="正文"):
        path = spec["node"]["output"]["file"]
        target = Path(path) if Path(path).is_absolute() else self.base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return path

    # ---- errors ----------------------------------------------------------
    def test_definition_error_is_compact_and_located(self):
        bad = self.base / "bad.json"
        bad.write_text(json.dumps({
            "schema_version": "1.1", "id": "x",
            "nodes": [{"id": "a", "type": "task", "prompt": "p"}],
        }), encoding="utf-8")
        proc = self.cli("validate", str(bad))
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("Failed validating", proc.stderr)
        self.assertLess(len(proc.stderr), 2000)
        self.assertIn("'name' is a required property", proc.stderr)
        self.assertIn("at   :", proc.stderr)
        self.assertIn("hint :", proc.stderr)

    def test_missing_start_input_is_typed_json(self):
        self.save(file_workflow())
        proc = self.cli("run", "doc", "--title", "L0", "--json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr, "")
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "E_INPUT_INVALID")
        self.assertIn("idea", payload["at"])
        self.assertIn("hint", payload)

    def test_malformed_param_does_not_traceback(self):
        self.save(file_workflow())
        proc = self.cli("run", "doc", "--title", "L0", "-p", "broken")
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("key=value", proc.stderr)

    def test_empty_project_validate_is_an_error(self):
        proc = self.cli("validate", "--project")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no workflow definitions", proc.stderr)

    # ---- payload flags ---------------------------------------------------
    def test_output_file_tolerates_a_bom(self):
        self.save(file_workflow())
        spec = self.start_json()
        path = self.artifact(spec)
        result = self.base / "result.json"
        result.write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"$file": path}).encode("utf-8")
        )
        proc = self.cli("complete", spec["run_id"], "--action",
                        spec["action"]["id"], "--output-file", str(result),
                        "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "completed")

    def test_input_file_tolerates_a_bom(self):
        self.save(file_workflow())
        inputs = self.base / "inputs.json"
        inputs.write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"idea": "x"}).encode("utf-8")
        )
        proc = self.cli("run", "doc", "--title", "L0",
                        "--input-file", str(inputs), "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "running")

    def test_file_flag_reports_a_missing_artifact_as_rejected(self):
        self.save(file_workflow())
        spec = self.start_json()
        missing = spec["node"]["output"]["file"] + ".nope"
        proc = self.cli("complete", spec["run_id"], "--action",
                        spec["action"]["id"], "--file", missing, "--json")
        self.assertEqual(proc.returncode, 3)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["rejected"]["node_id"], "write")
        self.assertIn("expected", payload["rejected"])
        # the packet still tells the caller exactly what to do next
        self.assertIn("--file", payload["next"]["text"])

    def test_file_flag_accepts_a_forward_slash_path(self):
        self.save(file_workflow())
        spec = self.start_json()
        path = self.artifact(spec).replace("\\", "/")
        proc = self.cli("complete", spec["run_id"], "--action",
                        spec["action"]["id"], "--file", path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "completed")

    def test_stale_action_has_its_own_exit_code(self):
        self.save(two_step_workflow())
        spec = self.start_json("two")
        path = self.artifact(spec)
        first = self.cli("complete", spec["run_id"], "--action",
                         spec["action"]["id"], "--file", path)
        self.assertEqual(first.returncode, 0, first.stderr)
        again = self.cli("complete", spec["run_id"], "--action",
                         spec["action"]["id"], "--file", path, "--json")
        self.assertEqual(again.returncode, 4)
        self.assertEqual(json.loads(again.stdout)["code"], "E_STALE_ACTION")

    # ---- next command ----------------------------------------------------
    def test_packet_json_names_the_next_command(self):
        self.save(file_workflow(with_check=True))
        spec = self.start_json()
        self.assertEqual(spec["next"]["kind"], "complete")
        self.assertIn("--file", spec["next"]["text"])

        path = self.artifact(spec)
        after = self.cli("complete", spec["run_id"], "--action",
                         spec["action"]["id"], "--file", path, "--json")
        payload = json.loads(after.stdout)
        self.assertEqual(payload["phase"], "awaiting_check")
        self.assertEqual(payload["next"]["kind"], "check")
        self.assertIn("--verdict", payload["next"]["text"])

        review = self.cli("complete", payload["run_id"], "--action",
                          payload["action"]["id"], "--verdict", "PASS",
                          "--reason", "非空", "--json")
        review_payload = json.loads(review.stdout)
        self.assertEqual(review_payload["phase"], "awaiting_review")
        self.assertEqual(review_payload["next"]["kind"], "review")

    def test_text_packet_prints_the_next_line(self):
        self.save(file_workflow())
        proc = self.start()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("next     : aiwf complete", proc.stdout)

    # ---- observation -----------------------------------------------------
    def test_json_flag_is_accepted_before_the_subcommand(self):
        self.save(file_workflow())
        proc = self.cli("--json", "runs", "list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [])

    def test_running_run_has_no_none_duration(self):
        self.save(file_workflow())
        self.start()
        proc = self.cli("runs", "list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("None", proc.stdout)

    def test_completed_run_does_not_report_a_running_phase(self):
        self.save(file_workflow())
        spec = self.start_json()
        path = self.artifact(spec)
        self.cli("complete", spec["run_id"], "--action", spec["action"]["id"],
                 "--file", path)
        proc = self.cli("runs", "show", spec["run_id"])
        self.assertIn("status     : completed", proc.stdout)
        self.assertNotIn("completed / executing", proc.stdout)

    # ---- environment -----------------------------------------------------
    def test_version_and_doctor(self):
        version = self.cli("--version")
        self.assertEqual(version.returncode, 0)
        self.assertIn("aiwf 0.1.0", version.stdout)

        doctor = self.cli("doctor", "--json")
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        payload = json.loads(doctor.stdout)
        self.assertTrue(payload["ok"])
        checks = {row["check"]: row for row in payload["checks"]}
        self.assertEqual(checks["schemas"]["status"], "ok")

    def test_init_writes_project_launchers(self):
        self.assertTrue((self.base / "bin" / "aiwf.cmd").is_file())
        self.assertTrue((self.base / "bin" / "aiwf").is_file())


if __name__ == "__main__":
    unittest.main()
