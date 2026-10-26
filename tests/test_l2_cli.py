"""L2 submit protocol: one template, one write verb, state-driven dispatch."""
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


def file_task(workflow_id="doc", *, check=False, review=False, prompt="写点东西"):
    node = {
        "id": "write", "type": "task", "prompt": prompt,
        "output": {"file": "${run.dir}/doc.md"},
    }
    if check:
        node["check"] = {"criteria": ["非空"], "max_attempts": 2}
    if review:
        node["review"] = {"requirements": ["可以定稿"]}
    return {
        "schema_version": "1.1", "id": workflow_id, "name": workflow_id,
        "input_schema": {"type": "object", "required": ["idea"],
                         "properties": {"idea": {"type": "string"}}},
        "nodes": [node],
    }


def two_step(workflow_id="two"):
    return {
        "schema_version": "1.1", "id": workflow_id, "name": workflow_id,
        "input_schema": {"type": "object", "required": ["idea"],
                         "properties": {"idea": {"type": "string"}}},
        "nodes": [
            {"id": "first", "type": "task", "prompt": "第一步。",
             "output": {"file": "${run.dir}/first.md"}},
            {"id": "second", "type": "task", "prompt": "第二步。",
             "output": {"file": "${run.dir}/second.md"}},
        ],
    }


def queue_workflow(workflow_id="batch"):
    return {
        "schema_version": "1.1", "id": workflow_id, "name": workflow_id,
        "nodes": [{
            "id": "work", "type": "foreach", "items": {"queue": True},
            "nodes": [{
                "id": "step", "type": "task", "prompt": "处理条目。",
                "output": {"file": "${run.dir}/${item.id}.md"},
            }],
        }],
    }


def ask_workflow(workflow_id="ask"):
    return {
        "schema_version": "1.1", "id": workflow_id, "name": workflow_id,
        "input_schema": {"type": "object", "required": ["idea"],
                         "properties": {"idea": {"type": "string"}}},
        "nodes": [{
            "id": "draft", "type": "task",
            "user_input_schema": {
                "type": "object", "required": ["tone"],
                "properties": {"tone": {"type": "string", "enum": ["冷峻", "温暖"]}},
            },
            "inputs": {"idea": {"ref": "idea"}, "tone": {"ref": "tone"}},
            "prompt": "用 ${tone} 写。",
            "output": {"file": "${run.dir}/draft.md"},
        }],
    }


class L2CliTest(unittest.TestCase):
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

    def start(self, workflow_id="doc", *extra, title="L2"):
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

    def template_path(self, spec):
        workflow = spec.get("root_workflow_id") or spec["workflow_id"]
        return (self.base / ".aiwf" / "runs" / workflow / spec["run_id"]
                / "submit.json")

    def template(self, spec):
        return json.loads(self.template_path(spec).read_text(encoding="utf-8"))

    def write_template(self, spec, value):
        self.template_path(spec).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- template lifecycle ---------------------------------------------
    def test_run_writes_a_prefilled_template(self):
        self.save(file_task())
        spec = self.start()
        template = self.template(spec)
        self.assertEqual(template["action"], spec["action"]["id"])
        self.assertEqual(template["kind"], "complete")
        self.assertEqual(template["output"]["$file"],
                         spec["node"]["output"]["file"].replace("\\", "/"))
        self.assertIn("hint", template)

    def test_submit_from_template_completes_and_archives(self):
        self.save(file_task())
        spec = self.start()
        self.artifact(spec)
        proc = self.cli("submit", spec["run_id"], "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "completed")
        self.assertFalse(self.template_path(spec).is_file())
        archive = self.template_path(spec).parent / "submissions"
        entries = sorted(archive.glob("*.json"))
        self.assertEqual(len(entries), 1)
        recorded = json.loads(entries[0].read_text(encoding="utf-8"))
        self.assertEqual(recorded["node"], "write")
        self.assertEqual(recorded["kind"], "complete")

    def test_submit_refreshes_the_template_for_the_next_step(self):
        self.save(two_step())
        spec = self.start("two")
        self.artifact(spec)
        after = self.cli("submit", spec["run_id"], "--json")
        self.assertEqual(after.returncode, 0, after.stderr)
        spec2 = json.loads(after.stdout)
        self.assertEqual(spec2["node"]["id"], "second")
        template = self.template(spec2)
        self.assertEqual(template["node"], "second")
        self.assertEqual(template["action"], spec2["action"]["id"])

    def test_missing_template_is_created_then_editable(self):
        self.save(file_task())
        spec = self.start()
        self.template_path(spec).unlink()
        proc = self.cli("submit", spec["run_id"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("template created", proc.stderr)
        self.assertTrue(self.template_path(spec).is_file())
        self.artifact(spec)
        done = self.cli("submit", spec["run_id"], "--json")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_direct_file_works_without_a_template(self):
        self.save(file_task())
        spec = self.start()
        self.template_path(spec).unlink()
        path = self.artifact(spec)
        proc = self.cli("submit", spec["run_id"], "--file", path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "completed")

    def test_stale_template_is_rejected(self):
        self.save(two_step())
        spec = self.start("two")
        old = self.template(spec)
        self.artifact(spec)
        self.assertEqual(self.cli("submit", spec["run_id"]).returncode, 0)
        self.write_template(spec, old)
        replay = self.cli("submit", spec["run_id"], "--json")
        self.assertEqual(replay.returncode, 4)
        self.assertEqual(json.loads(replay.stdout)["code"], "E_STALE_ACTION")

    def test_print_rebuilds_the_template(self):
        self.save(file_task())
        spec = self.start()
        self.template_path(spec).unlink()
        proc = self.cli("submit", spec["run_id"], "--print")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("submit.json", proc.stdout)
        self.assertEqual(self.template(spec)["action"], spec["action"]["id"])

    def test_status_reports_a_missing_template_and_ensure_creates_it(self):
        self.save(file_task())
        spec = self.start()
        self.template_path(spec).unlink()
        status = self.cli("status", spec["run_id"])
        self.assertIn("missing", status.stdout)
        self.assertFalse(self.template_path(spec).is_file())

        payload = json.loads(self.cli("status", spec["run_id"], "--json").stdout)
        self.assertTrue(payload["submit_missing"])
        self.assertIsNone(payload["submit_file"])

        ensured = self.cli("status", spec["run_id"], "--ensure")
        self.assertEqual(ensured.returncode, 0, ensured.stderr)
        self.assertTrue(self.template_path(spec).is_file())

    def test_submit_backfills_an_old_run_and_finishes_when_ready(self):
        self.save(file_task())
        spec = self.start()
        self.artifact(spec)
        self.template_path(spec).unlink()
        proc = self.cli("submit", spec["run_id"], "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "completed")

    def test_submit_backfills_a_check_and_asks_for_an_edit(self):
        self.save(file_task(check=True))
        spec = self.start()
        self.artifact(spec)
        checked = json.loads(self.cli("submit", spec["run_id"], "--json").stdout)
        self.assertEqual(checked["phase"], "awaiting_check")
        self.template_path(checked).unlink()
        proc = self.cli("submit", spec["run_id"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("template created", proc.stderr)
        self.assertEqual(self.template(checked)["kind"], "check")

    # ---- phase dispatch --------------------------------------------------
    def test_check_then_review_through_submit(self):
        self.save(file_task(check=True, review=True))
        spec = self.start()
        self.artifact(spec)
        checked = json.loads(self.cli("submit", spec["run_id"], "--json").stdout)
        self.assertEqual(checked["phase"], "awaiting_check")
        self.assertEqual(self.template(checked)["kind"], "check")

        reviewed = json.loads(self.cli(
            "submit", spec["run_id"], "--verdict", "PASS",
            "--reason", "实测非空", "--json").stdout)
        self.assertEqual(reviewed["phase"], "awaiting_review")
        self.assertEqual(self.template(reviewed)["kind"], "review")

        done = json.loads(self.cli(
            "submit", spec["run_id"], "--approve",
            "--feedback", "用户认可", "--json").stdout)
        self.assertEqual(done["status"], "completed")

    def test_unfilled_check_template_is_rejected(self):
        self.save(file_task(check=True))
        spec = self.start()
        self.artifact(spec)
        checked = json.loads(self.cli("submit", spec["run_id"], "--json").stdout)
        proc = self.cli("submit", spec["run_id"], "--json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("verdict and a reason", proc.stdout)

    def test_provide_template_and_direct_answer(self):
        self.save(ask_workflow())
        spec = self.start("ask")
        self.assertEqual(spec["phase"], "awaiting_input")
        self.assertEqual(self.template(spec)["kind"], "provide")
        self.assertIn("tone", self.template(spec)["output"])
        proc = self.cli("submit", spec["run_id"],
                        "-p", "tone=冷峻", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["phase"], "executing")

    def test_items_can_be_submitted_from_the_template(self):
        self.save(queue_workflow())
        spec = self.start("batch")
        self.assertEqual(spec["phase"], "awaiting_items")
        template = self.template(spec)
        self.assertEqual(template["kind"], "items")
        template["output"] = [{"id": "A", "name": "a"}]
        template["seal"] = True
        self.write_template(spec, template)
        proc = self.cli("submit", spec["run_id"], "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["node"]["id"], "step")
        archive = self.template_path(spec).parent / "submissions"
        recorded = json.loads(sorted(archive.glob("*.json"))[0].read_text(
            encoding="utf-8"))
        self.assertEqual(recorded["kind"], "items")
        self.assertTrue(recorded["seal"])

    def test_seal_only_closes_an_empty_queue(self):
        self.save(queue_workflow())
        spec = self.start("batch")
        proc = self.cli("submit", spec["run_id"], "--seal", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "completed")

    def test_submit_fail_marks_the_run_failed(self):
        self.save(file_task())
        spec = self.start()
        proc = self.cli("submit", spec["run_id"], "--fail", "工具崩了")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(self.template_path(spec).is_file())
        status = self.cli("status", spec["run_id"])
        self.assertIn("failed", status.stdout)
        self.assertIn("resume", status.stdout)

    def test_flag_mismatch_is_rejected(self):
        self.save(file_task(check=True))
        spec = self.start()
        proc = self.cli("submit", spec["run_id"], "--approve", "--json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("review", json.loads(proc.stdout)["error"])

    # ---- status / interface ---------------------------------------------
    def test_status_prints_the_full_packet(self):
        self.save(file_task(prompt="写第三章，保持人物一致"))
        spec = self.start(title="状态")
        proc = self.cli("status", spec["run_id"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("写第三章", proc.stdout)
        self.assertIn("artifact:", proc.stdout)
        self.assertIn("next  : aiwf submit", proc.stdout)
        self.assertIn("file  :", proc.stdout)

        payload = json.loads(self.cli("status", spec["run_id"], "--json").stdout)
        self.assertEqual(payload["title"], "状态")
        self.assertIn("写第三章", payload["node"]["prompt"])
        self.assertEqual(payload["next"]["text"], "aiwf submit")
        self.assertTrue(payload["submit_file"].endswith("submit.json"))

    def test_no_argument_aiwf_is_status(self):
        self.save(file_task())
        spec = self.start(title="无参")
        proc = self.cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(spec["run_id"], proc.stdout)
        self.assertIn("next  : aiwf submit", proc.stdout)

    def test_no_argument_with_json_flag_returns_the_packet(self):
        self.save(file_task())
        spec = self.start(title="无参JSON")
        proc = self.cli("--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["run_id"], spec["run_id"])
        self.assertEqual(payload["title"], "无参JSON")
        self.assertEqual(payload["next"]["text"], "aiwf submit")

    def test_default_help_hides_advanced_but_help_all_lists_them(self):
        normal = self.cli("--help")
        self.assertEqual(normal.returncode, 0)
        self.assertIn("submit", normal.stdout)
        self.assertNotIn("complete", normal.stdout)
        self.assertNotIn("provide", normal.stdout)
        full = self.cli("--help-all")
        self.assertEqual(full.returncode, 0)
        self.assertIn("complete", full.stdout)
        self.assertIn("items", full.stdout)

    def test_current_does_not_write_the_template(self):
        self.save(file_task())
        spec = self.start()
        before = self.template_path(spec).read_bytes()
        self.cli("current", spec["run_id"], "--json")
        self.assertEqual(self.template_path(spec).read_bytes(), before)

    def test_deprecated_complete_still_works_with_a_note(self):
        self.save(file_task())
        spec = self.start()
        path = self.artifact(spec)
        proc = self.cli("complete", spec["run_id"], "--file", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("deprecated", proc.stderr)
        self.assertIn("aiwf submit", proc.stderr)


if __name__ == "__main__":
    unittest.main()
