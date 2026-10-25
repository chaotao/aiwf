"""1.1 contract tests. Agent outputs and human verdicts here are test fixtures."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aiwf import Storage, WorkflowEngine
from aiwf.engine import resolve_data
from aiwf.schema import validate_workflow


REPO = Path(__file__).resolve().parents[1]
# Drive the CLI through the current interpreter: the POSIX engine/bin/aiwf
# cannot be executed directly on Windows.
CLI = [sys.executable, "-m", "aiwf"]
CLI_ENV = {"PYTHONPATH": str(REPO / "engine")}


def workflow(nodes, **extra):
    return {
        "schema_version": "1.1", "id": "usability", "name": "test",
        "max_steps": 100, "nodes": nodes, **extra,
    }


def task(node_id="draft", **extra):
    return {"id": node_id, "type": "task", "prompt": "Execute the task.", **extra}


class UsabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(os.path.join(self.tmp.name, ".aiwf")).init_dirs()
        self.engine = WorkflowEngine(self.storage)

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, wf, inputs=None):
        self.storage.save_workflow(wf)
        record = self.engine.start(wf, inputs)
        self.rid = record["run_id"]
        self.engine.settle(self.rid)
        return self.engine.action_spec(self.rid)

    def act(self, output=None, command="complete", **kw):
        spec = self.engine.action_spec(self.rid)
        if command == "complete":
            kw["output"] = output
        return self.engine.act(self.rid, spec["action"]["id"], command, **kw)

    def test_user_input_schema_is_separate_from_materials_and_start_inputs(self):
        user_schema = {
            "type": "object", "required": ["tone"],
            "properties": {"tone": {"type": "string", "title": "叙事风格"}},
        }
        wf = workflow([
            task(user_input_schema=user_schema,
                 inputs={"seed": {"ref": "seed"}, "tone": {"ref": "tone"}},
                 prompt="写作风格：${tone}"),
            task("hold"),
        ], input_schema={
            "type": "object", "required": ["seed"],
            "properties": {"seed": {"type": "integer"}},
        })
        with self.assertRaises(Exception):
            self.start(wf)  # top-level contract still applies
        spec = self.start(wf, {"seed": 7})
        self.assertEqual(spec["phase"], "awaiting_input")
        self.assertEqual(spec["input_request"]["fields"][0]["name"], "tone")
        self.assertEqual(spec["input_request"]["schema"], user_schema)
        with self.assertRaises(Exception):
            self.act(command="provide", values={"tone": 42})
        spec = self.act(command="provide", values={"tone": "冷峻"})
        self.assertEqual(spec["node"]["inputs"], {"seed": 7, "tone": "冷峻"})
        self.assertEqual(spec["node"]["prompt"], "写作风格：冷峻")
        self.act("draft")
        with self.assertRaises(Exception):
            self.engine.provide(wf, self.rid, {"tone": 42}, node_id="draft")
        self.engine.provide(wf, self.rid, {"tone": "温暖"}, node_id="draft")
        self.assertEqual(self.engine.action_spec(self.rid)["node"]["prompt"], "写作风格：温暖")
        spec = self.start(wf, {"seed": 7, "tone": "预填"})
        self.assertEqual(spec["phase"], "executing")

    def test_legacy_node_input_schema_snapshot_remains_usable(self):
        wf = workflow([task(input_schema={
            "type": "object", "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
        }, prompt="${answer}")])
        self.start(wf)
        wf["nodes"][0]["user_input_schema"] = wf["nodes"][0].pop("input_schema")
        self.storage.save_workflow(wf)
        snapshot = self.storage.load_workflow_for_record(self.storage.find_run(self.rid))
        self.assertIn("input_schema", snapshot["nodes"][0])
        self.assertEqual(self.act(command="provide", values={"answer": "legacy"})["node"]["prompt"], "legacy")
        self.assertEqual(self.act("done")["status"], "completed")

    def test_user_input_schema_rejects_conflicts_and_invalid_placement(self):
        invalid_nodes = [
            task(user_input_schema={}, input_schema={}),
            task(user_input_schema={"type": "array"}),
            task(user_input_schema={"properties": {"run": {}}}),
            {"id": "end", "type": "end", "user_input_schema": {}},
        ]
        for node in invalid_nodes:
            with self.subTest(node=node), self.assertRaises(ValueError):
                validate_workflow(workflow([node]))
        with self.assertRaisesRegex(ValueError, "cannot also specify"):
            validate_workflow(workflow([{
                "id": "batch", "type": "foreach", "items": [1],
                "nodes": [task(user_input_schema={}, input_schema={})],
            }]))

    def test_typed_refs_and_templates_remain_distinct(self):
        ctx = {"item": {"number": 2, "enabled": False, "file": {"$file": "x.md"}},
               "list": [1, 2]}
        data = resolve_data({
            "object": {"ref": "item"}, "number": {"ref": "item.number"},
            "bool": {"ref": "item.enabled"}, "file": {"ref": "item.file"},
            "array": {"ref": "list"}, "text": "${item.number}",
        }, ctx)
        self.assertEqual(data["number"], 2)
        self.assertIs(data["bool"], False)
        self.assertEqual(data["array"], [1, 2])
        self.assertEqual(data["file"], {"$file": "x.md"})
        self.assertEqual(data["text"], "2")
        data["object"]["number"] = 9
        self.assertEqual(ctx["item"]["number"], 2)
        with self.assertRaises(KeyError):
            resolve_data({"ref": "item.missing"}, ctx)

    def test_single_task_natural_end_at_exact_budget_and_snapshot(self):
        wf = workflow([task()], max_steps=1)
        spec = self.start(wf)
        wf["nodes"][0]["prompt"] = "changed source"
        self.storage.save_workflow(wf)
        self.assertEqual(self.engine.action_spec(self.rid)["node"]["prompt"], "Execute the task.")
        spec = self.act({"ok": True})
        self.assertEqual(spec["status"], "completed")

    def test_current_is_read_only_and_old_action_rejected(self):
        spec = self.start(workflow([task("a"), task("b")]))
        old = spec["action"]["id"]
        root = self.storage.find_run(self.rid)
        paths = list(Path(self.storage.run_dir_for_record(root)).rglob("*"))
        before = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in paths if p.is_file()}
        self.assertEqual(self.engine.action_spec(self.rid), spec)
        after = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in paths if p.is_file()}
        self.assertEqual(before, after)
        self.act("first")
        with self.assertRaisesRegex(RuntimeError, "stale action"):
            self.engine.act(self.rid, old, "complete", output="late first")
        self.assertEqual(self.engine.action_spec(self.rid)["node"]["id"], "b")

    def test_material_missing_after_branch_fails_with_location(self):
        wf = workflow([
            {"id": "gate", "type": "branch", "when": {"eq": [1, 1]},
             "routes": {"TRUE": "consume", "FALSE": "source"}},
            task("source"),
            task("consume", inputs={"plan": {"ref": "source.details"}}),
        ])
        spec = self.start(wf)
        self.assertEqual(spec["status"], "failed")
        self.assertIn("consume", spec["error"])
        self.assertIn("plan", spec["error"])

    def test_condition_inputs_and_nested_file_dependencies(self):
        wf = workflow([
            task("plan"),
            {"id": "gate", "type": "branch",
             "when": {"eq": [{"ref": "plan.ready"}, True]},
             "routes": {"TRUE": "consume", "FALSE": "plan"}},
            task("consume", inputs={"material": {"ref": "plan.file"}}),
        ])
        self.assertEqual(self.engine.stale_closure(wf, {"plan"}), {"gate", "consume"})
        spec = self.start(wf)
        spec = self.act({"ready": True, "file": {"$file": "outline.md"}})
        self.assertEqual(spec["node"]["id"], "consume")
        self.assertEqual(spec["node"]["inputs"]["material"], {"$file": "outline.md"})
        root = self.storage.find_run(self.rid)
        self.assertEqual(root["node_runs"][1]["decision"]["answer"], "TRUE")
        wf["nodes"].append(task("glob", consumes=[{"glob": "chapters/*.md"}]))
        self.assertIn("glob", self.engine.stale_closure(
            wf, {"plan"}, {"plan": {"$file": "outline.md", "chapter": {"$file": "chapters/1.md"}}}
        ))

    def test_check_retry_limit_human_gate_and_stale_verdict(self):
        wf = workflow([task(
            check={"criteria": ["conflict advances"], "max_attempts": 2},
            review={"requirements": ["human acceptance"]},
        )])
        self.start(wf)
        check = self.act("draft 1")
        self.assertEqual(check["action"]["kind"], "check")
        with self.assertRaises(Exception):
            self.act({"verdict": "REVISE", "reason": ""})
        spec = self.act({"verdict": "REVISE", "reason": "missing ending"})
        self.assertEqual(spec["node"]["previous_output"], "draft 1")
        self.assertEqual(spec["node"]["feedback"], "missing ending")
        self.act("draft 2")
        spec = self.act({"verdict": "PASS", "reason": "ending added"})
        self.assertEqual(spec["phase"], "awaiting_review")
        with self.assertRaisesRegex(RuntimeError, "requires review"):
            self.act("cannot skip human")
        spec = self.act(command="review", verdict="REJECT", feedback="change voice")
        self.assertEqual(spec["node"]["feedback"], "change voice")
        self.act("draft 3")
        self.act({"verdict": "PASS", "reason": "voice corrected"})
        spec = self.act(command="review", verdict="APPROVE")
        self.assertEqual(spec["status"], "completed")
        record = self.storage.find_run(self.rid)
        checks = [e for e in record["events"] if e["type"] == "check_decided"]
        self.assertEqual(len(checks), 3)
        self.assertTrue(all("duration_ms" in e and "input" in e for e in checks))

    def test_check_exhaustion_cannot_be_bypassed_by_resume(self):
        wf = workflow([task(check={"criteria": ["quality"], "max_attempts": 1})])
        self.start(wf)
        self.act("bad")
        spec = self.act({"verdict": "REVISE", "reason": "incorrect"})
        self.assertEqual(spec["status"], "failed")
        self.engine.resume(wf, self.rid)
        with self.assertRaisesRegex(RuntimeError, "exhausted"):
            self.act({"verdict": "PASS", "reason": "bypass"})

    def test_file_output_wrong_path_and_missing_file_rejected(self):
        path = os.path.join(self.tmp.name, "draft.md")
        self.start(workflow([task(output={"file": path})]))
        spec = self.act({"$file": path})
        self.assertEqual(spec["status"], "running")
        self.assertIn("expected existing file", spec["node"]["feedback"])
        Path(path).write_text("artifact", encoding="utf-8")
        spec = self.act({"$file": path})
        self.assertEqual(spec["status"], "completed")

    def test_foreach_leaf_previous_and_distinct_instance_outputs(self):
        wf = workflow([
            task("plan"),
            {"id": "chapters", "type": "foreach",
             "items": {"ref": "plan.chapters"}, "id_field": "number",
             "inputs": {"previous": {"ref": "previous"}},
             "nodes": [task("write", inputs={
                 "chapter": {"ref": "item"}, "previous": {"ref": "previous"},
             })], "exports": ["write"]},
            task("assemble", inputs={"results": {"ref": "chapters"}}),
        ])
        self.start(wf)
        spec = self.act({"chapters": [{"number": 1}, {"number": 2}]})
        first = spec["action"]["run_id"]
        self.assertNotEqual(first, self.rid)
        self.assertEqual(spec["node"]["inputs"]["chapter"], {"number": 1})
        self.assertIsNone(spec["node"]["inputs"]["previous"])
        spec = self.act({"summary": "chapter one"})
        self.assertNotEqual(first, spec["action"]["run_id"])
        self.assertIsNone(spec["node"]["previous_output"])
        self.assertEqual(spec["node"]["inputs"]["previous"], {"write": {"summary": "chapter one"}})
        spec = self.act({"summary": "chapter two"})
        self.assertEqual(spec["node"]["id"], "assemble")
        manifest = self.storage.read_json_file(spec["node"]["inputs"]["results"]["$file"])
        self.assertEqual([i["key"] for i in manifest["items"]], ["1", "2"])
        self.assertEqual(self.act("book")["status"], "completed")

    def test_inline_branch_can_exit_without_fake_task(self):
        wf = workflow([{
            "id": "batch", "type": "foreach", "items": [1],
            "nodes": [
                task("write"),
                {"id": "gate", "type": "branch", "when": {"eq": [1, 1]},
                 "routes": {"TRUE": "end", "FALSE": "write"}},
            ], "exports": ["write"],
        }])
        self.start(wf)
        self.assertEqual(self.act("ok")["status"], "completed")

    def test_nested_failure_resume_and_leaf_identity(self):
        wf = workflow([{
            "id": "outer", "type": "foreach", "items": [1],
            "nodes": [{
                "id": "inner", "type": "foreach", "items": [1],
                "nodes": [task()],
            }],
        }])
        spec = self.start(wf)
        old = spec["action"]["id"]
        target = spec["action"]["run_id"]
        spec = self.act(command="fail", error="temporary tool fault")
        self.assertEqual(spec["status"], "failed")
        self.engine.resume(wf, self.rid)
        self.engine.settle(self.rid)
        resumed = self.engine.action_spec(self.rid)
        self.assertEqual(resumed["action"]["run_id"], target)
        self.assertNotEqual(resumed["action"]["id"], old)
        self.assertEqual(self.act("fixed")["status"], "completed")

    def test_queue_waits_until_sealed_and_static_empty_ends(self):
        wf = workflow([{
            "id": "batch", "type": "foreach", "items": {"queue": True},
            "nodes": [task()],
        }])
        spec = self.start(wf)
        self.assertEqual(spec["phase"], "awaiting_items")
        self.engine.queue_add(self.rid, "batch", [{"id": "a", "name": "one"}])
        self.engine.settle(self.rid)
        spec = self.act("one")
        self.assertEqual(spec["phase"], "awaiting_items")
        self.engine.queue_seal(self.rid, "batch")
        self.engine.settle(self.rid)
        self.assertEqual(self.engine.action_spec(self.rid)["status"], "completed")
        wf["nodes"][0]["items"] = []
        self.assertEqual(self.start(wf)["status"], "completed")

    def test_auto_branch_loop_obeys_budget(self):
        wf = workflow([{
            "id": "loop", "type": "branch", "when": {"eq": [1, 1]},
            "routes": {"TRUE": "loop", "FALSE": "loop"},
        }], max_steps=3)
        self.assertEqual(self.start(wf)["status"], "failed")
        self.assertEqual(len(self.storage.find_run(self.rid)["node_runs"]), 3)

    def test_old_record_rejects_silent_version_upgrade(self):
        wf = workflow([task()])
        wf["schema_version"] = "1.0"
        self.storage.save_workflow(wf)
        record = self.engine.start(wf)
        record.pop("workflow_version")
        self.storage.save_run(record)
        wf["schema_version"] = "1.1"
        self.storage.save_workflow(wf)
        with self.assertRaisesRegex(ValueError, "version changed"):
            self.storage.load_workflow_for_record(record)

    def test_changed_condition_cannot_reuse_old_branch_values(self):
        wf = workflow([
            task("plan"),
            {"id": "gate", "type": "branch",
             "when": {"eq": [{"ref": "plan.use_source"}, True]},
             "routes": {"TRUE": "source", "FALSE": "consume"}},
            task("source", inputs={"plan": {"ref": "plan"}}),
            task("consume", inputs={"source": {"ref": "source"}}),
            task("hold"),
        ])
        self.start(wf)
        self.act({"use_source": True})
        self.act("first branch data")
        self.act("consumed")
        self.engine.revise(wf, self.rid, "plan", "change branch")
        spec = self.act({"use_source": False})
        self.assertEqual(spec["status"], "failed")
        self.assertIn("source", spec["error"])
        self.assertNotIn("source", self.storage.find_run(self.rid)["context"])

    def test_static_items_are_read_once_from_their_source(self):
        path = os.path.join(self.tmp.name, "source.json")
        self.storage.write_json_file(path, [1, 2])
        wf = workflow([{
            "id": "batch", "type": "foreach", "items": {"$file": path},
            "nodes": [task(inputs={"item": {"ref": "item"}})],
        }])
        self.start(wf)
        # a static source is snapshotted at the node boundary; editing the file
        # afterwards must not reach back into a queue that already exists
        self.storage.write_json_file(path, [9])
        self.assertEqual(self.act("one")["node"]["inputs"]["item"], 2)
        self.assertEqual(self.act("two")["status"], "completed")

    def test_a_queue_item_is_editable_until_its_instance_starts(self):
        wf = workflow([{
            "id": "batch", "type": "foreach", "items": {"queue": True},
            "nodes": [task(inputs={"item": {"ref": "item"}})],
        }])
        self.start(wf)
        self.engine.queue_add(self.rid, "batch", [
            {"id": "a", "name": "one"}, {"id": "b", "name": "two"},
        ])
        self.engine.settle(self.rid)
        # 'a' is running; 'b' has no instance yet, so it is still the caller's
        table = self.engine.queue_table(self.rid, "batch")
        self.assertEqual(
            [(r["id"], r["editable"]) for r in table["items"]],
            [("a", False), ("b", True)],
        )
        self.engine.queue_update(self.rid, "batch", "b", {"name": "two revised"})
        with self.assertRaisesRegex(RuntimeError, "already started"):
            self.engine.queue_update(self.rid, "batch", "a", {"name": "nope"})

        # an item's status *is* its instance's status -- one vocabulary, no copy
        record = self.storage.find_run(self.rid)
        by_key = {i["key"]: i["status"] for i in record["subflow_state"]["instances"]}
        table = self.engine.queue_table(self.rid, "batch")
        self.assertEqual([r["status"] for r in table["items"]],
                         ["running", "pending"])
        for row in table["items"]:
            self.assertEqual(row["status"], by_key.get(row["id"], "pending"))

    def test_rejected_outputs_also_have_step_budget(self):
        wf = workflow([task(output_schema={"type": "integer"})], max_steps=2)
        self.start(wf)
        self.act("bad one")
        self.act("bad two")
        self.assertEqual(self.act(3)["status"], "failed")

    def test_malformed_refs_and_old_ref_syntax_fail_validation(self):
        for value in ({"ref": ""}, {"ref": "a", "fallback": 1}, {"var": "a"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_workflow(workflow([task(inputs={"x": value})]))

    def test_return_revision_with_check_and_human_review(self):
        wf = workflow([
            task("draft", check={"criteria": ["quality"], "max_attempts": 2},
                 review={"requirements": ["accept"]}),
            task("middle"), task("hold"),
        ])
        self.start(wf)
        self.act("v1")
        self.act({"verdict": "PASS", "reason": "tested"})
        self.act(command="review", verdict="APPROVE")
        self.act("middle")
        self.engine.revise(wf, self.rid, "draft", "adjust", mode="return")
        self.act("v2")
        self.act({"verdict": "PASS", "reason": "adjusted"})
        spec = self.act(command="review", verdict="APPROVE")
        self.assertEqual(spec["node"]["id"], "hold")
        self.assertIsNone(spec["revision"])

    def test_cascade_foreach_preserves_child_audit_and_invalidates_action(self):
        wf = workflow([
            task("plan"),
            {"id": "batch", "type": "foreach", "items": [1],
             "inputs": {"plan": {"ref": "plan"}},
             "nodes": [task(inputs={"plan": {"ref": "plan"}})],
             "exports": ["draft"]},
            task("hold"),
        ])
        self.start(wf)
        spec = self.act("plan 1")
        old_action = spec["action"]["id"]
        child_id = spec["action"]["run_id"]
        self.act("draft 1")
        old_events = self.storage.find_run(child_id)["events"]
        self.engine.revise(wf, self.rid, "plan", "new plan")
        spec = self.act("plan 2")
        self.assertEqual(spec["action"]["run_id"], child_id)
        self.assertNotEqual(spec["action"]["id"], old_action)
        self.assertEqual(spec["node"]["inputs"]["plan"], "plan 2")
        child = self.storage.find_run(child_id)
        self.assertEqual(child["events"][:len(old_events)], old_events)
        self.assertTrue(any(n["output"] == "draft 1" for n in child["node_runs"]))
        self.act("draft 2")
        self.assertEqual(self.act("done")["status"], "completed")

    def test_general_novel_fixture_preserves_all_reviews(self):
        repo = Path(__file__).resolve().parents[1]
        wf = json.loads((repo / "sim/novel-full/.aiwf/workflows/novel-writing.json").read_text(encoding="utf-8"))
        spec = self.start(wf, {"idea": "测试灵感"})
        reviews = []
        while spec["status"] == "running":
            action = spec["action"]
            if action["kind"] == "provide":
                spec = self.act(command="provide", values={
                    "genre": "测试", "tone": "测试", "target_chapters": 2,
                })
            elif action["kind"] == "review":
                reviews.append(action["node_id"])
                spec = self.act(command="review", verdict="APPROVE", feedback="TEST FIXTURE ONLY")
            elif action["kind"] == "check":
                spec = self.act({"verdict": "PASS", "reason": "TEST FIXTURE ONLY"})
            else:
                node = spec["node"]
                path = Path(node["output"]["file"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("测试夹具。", encoding="utf-8")
                output = {"$file": str(path)}
                if node["id"] == "outline":
                    manifest = path.parent / "chapters.json"
                    self.storage.write_json_file(str(manifest), [
                        {"number": 1, "title": "一"}, {"number": 2, "title": "二"},
                    ])
                    output["chapters"] = {"$file": str(manifest)}
                if node["id"] == "draft":
                    text = "测试正文。" * 550
                    path.write_text(text, encoding="utf-8")
                    output["word_count"] = sum("\u4e00" <= c <= "\u9fff" for c in text)
                spec = self.act(output)
        self.assertEqual(spec["status"], "completed")
        self.assertEqual(reviews, ["worldbuilding", "characters", "outline", "draft", "draft", "assemble"])

    def test_cli_three_chapter_fixture(self):
        """Protocol fixture only: prose/checks/reviews here are simulated."""
        repo = REPO
        wf = json.loads((repo / "sim/novel-3ch/.aiwf/workflows/novel-3ch.json").read_text(encoding="utf-8"))
        self.storage.save_workflow(wf)
        env = dict(os.environ, AIWF_HOME=self.storage.root, **CLI_ENV)
        calls = []
        def call(command, *args, expect=0):
            proc = subprocess.run([*CLI, command, *args, "--json"], env=env,
                                  text=True, encoding="utf-8",
                                  capture_output=True, check=False)
            self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
            calls.append(command)
            return json.loads(proc.stdout)
        spec = call("run", wf["id"], "--title", "三章试跑",
                    "-p", "theme=末世重生")
        root_id = spec["run_id"]
        first_revision = False
        first_path = None
        chapter_paths = []
        approvals = 0
        while spec["status"] == "running":
            action = spec["action"]
            args = (root_id, "--action", action["id"])
            if action["kind"] == "provide":
                spec = call("provide", *args, "-p", "premise=测试设定", "-p", "protagonist=测试人物")
            elif action["kind"] == "review":
                approvals += 1
                spec = call("review", *args, "--verdict", "APPROVE", "--feedback", "TEST FIXTURE ONLY")
            elif action["kind"] == "check":
                reject = action["node_id"] == "draft" and not first_revision
                first_revision |= reject
                output = {"verdict": "REVISE" if reject else "PASS",
                          "reason": "TEST FIXTURE: add ending" if reject else "TEST FIXTURE: accepted"}
                spec = call("complete", *args, "--output", json.dumps(output))
            else:
                self.assertEqual(action["kind"], "complete")
                node = spec["node"]
                path = Path(node["output"]["file"])
                path.parent.mkdir(parents=True, exist_ok=True)
                if node["id"] == "plan":
                    path.write_text("测试大纲，仅用于协议验证。", encoding="utf-8")
                    output = {"$file": str(path), "chapters": [
                        {"number": i, "title": f"测试第{i}章", "summary": "测试摘要",
                         "key_events": ["测试事件一", "测试事件二", "测试事件三"]}
                        for i in (1, 2, 3)
                    ]}
                    invalid = copy.deepcopy(output)
                    invalid["chapters"].pop()
                    rejected = call("complete", *args, "--output", json.dumps(invalid),
                                    expect=3)
                    self.assertEqual(rejected["action"]["node_id"], "plan")
                    args = (root_id, "--action", rejected["action"]["id"])
                elif node["id"] == "draft":
                    number = node["inputs"]["chapter"]["number"]
                    text = "测试夹具正文。" * 180
                    count = sum("\u4e00" <= c <= "\u9fff" for c in text)
                    path.write_text(text, encoding="utf-8")
                    output = {"$file": str(path), "title": f"第{number}章", "word_count": count}
                    if number == 1:
                        if first_path:
                            self.assertEqual(str(path), first_path)
                            self.assertIn("add ending", node["feedback"])
                        first_path = str(path)
                    else:
                        self.assertIsNone(node["previous_output"])
                        self.assertEqual(node["inputs"]["previous"]["draft"]["$file"], chapter_paths[-1])
                    if str(path) not in chapter_paths:
                        chapter_paths.append(str(path))
                else:
                    self.assertEqual(node["id"], "assemble")
                    manifest = self.storage.read_json_file(node["inputs"]["results"]["$file"])
                    self.assertEqual([i["status"] for i in manifest["items"]], ["success"] * 3)
                    path.write_text(
                        "\n".join(Path(p).read_text(encoding="utf-8") for p in chapter_paths),
                        encoding="utf-8",
                    )
                    output = {"$file": str(path), "total_word_count": 3240}
                spec = call("complete", *args, "--output", json.dumps(output))
        self.assertEqual(spec["status"], "completed")
        self.assertEqual(len(chapter_paths), 3)
        self.assertEqual(approvals, 2)
        self.assertEqual(len(calls), 16)
        self.assertNotIn("current", calls)
        root = self.storage.find_run(root_id)
        for _, _, record_path, _ in self.storage.iter_run_files():
            record = json.loads(Path(record_path).read_text(encoding="utf-8"))
            for nr in record["node_runs"]:
                self.assertLessEqual(len(json.dumps(nr["output"], ensure_ascii=False)), 2000)
        self.assertTrue((Path(self.storage.run_dir_for_record(root)) / "novel.md").is_file())

    def test_cli_root_action_roundtrip(self):
        self.storage.save_workflow(workflow([task()]))
        env = dict(os.environ, AIWF_HOME=self.storage.root, **CLI_ENV)
        def call(*args):
            proc = subprocess.run([*CLI, *args, "--json"], env=env,
                                  text=True, encoding="utf-8",
                                  capture_output=True, check=True)
            return json.loads(proc.stdout)
        spec = call("run", "usability", "--title", "round trip")
        stale = subprocess.run(
            [*CLI, "complete", spec["run_id"], "--action", "deadbeef",
             "--output", "late", "--json"],
            env=env, text=True, encoding="utf-8", capture_output=True,
        )
        self.assertEqual(stale.returncode, 4)
        self.assertEqual(json.loads(stale.stdout)["code"], "E_STALE_ACTION")
        # L1: omitting --action uses the current action (no id to copy)
        implicit = subprocess.run(
            [*CLI, "complete", spec["run_id"], "--output", "late", "--json"],
            env=env, text=True, encoding="utf-8", capture_output=True,
        )
        self.assertEqual(implicit.returncode, 0, implicit.stderr)
        self.assertEqual(implicit.stderr, "")
        self.assertEqual(json.loads(implicit.stdout)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
