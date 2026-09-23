"""Unit tests for the aiwf state machine.
Run: PYTHONPATH=engine python3 -m unittest discover -s tests

Tests simulate an external AI agent (and human reviewer) driving the run:
start -> spec -> provide/complete/review/revise -> ... until finished.
No network, no API key.
"""
import os
import shutil
import tempfile
import unittest

from aiwf import Storage, WorkflowEngine
from aiwf.engine import OUTPUT_LIMIT


# ---- workflow fixtures ---------------------------------------------------
def linear_workflow():
    return {
        "schema_version": "1.0",
        "id": "test-wf",
        "name": "测试工作流",
        "input_schema": {
            "type": "object",
            "required": ["topic"],
            "properties": {"topic": {"type": "string"}},
        },
        "nodes": [
            {"id": "start", "type": "start", "name": "开始"},
            {"id": "draft", "type": "task", "name": "初稿",
             "prompt": "以《${topic}》写初稿"},
            {"id": "end", "type": "end", "name": "结束"},
        ],
    }


def diamond_workflow():
    """p1 -> p2,p3 both consume p1; p4 consumes p2.
    Edges: start p1 p2 p3 p4 end."""
    return {
        "schema_version": "1.0",
        "id": "dia-wf",
        "name": "依赖图工作流",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "p1", "type": "task", "prompt": "seed:${seed}"},
            {"id": "p2", "type": "task", "prompt": "from p1:${p1}"},
            {"id": "p3", "type": "task", "prompt": "from p1:${p1} only"},
            {"id": "p4", "type": "task", "prompt": "from p2:${p2}"},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"from": "start", "to": "p1"},
            {"from": "p1", "to": "p2"},
            {"from": "p2", "to": "p3"},
            {"from": "p3", "to": "p4"},
            {"from": "p4", "to": "end"},
        ],
    }


def embedded_review_workflow():
    return {
        "schema_version": "1.0",
        "id": "review-wf",
        "name": "内嵌验收",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "draft", "type": "task", "prompt": "写初稿：${topic}",
             "review": {"requirements": ["检查项一", "检查项二：${topic}"]}},
            {"id": "end", "type": "end"},
        ],
    }


def standalone_review_workflow():
    return {
        "schema_version": "1.0",
        "id": "checkpoint-wf",
        "name": "独立验收点",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "draft", "type": "task", "prompt": "产出：${topic}"},
            {"id": "checkpoint", "type": "review",
             "requirements": "请验收 draft：${draft}",
             "routes": {"APPROVE": "end", "REJECT": "draft"}},
            {"id": "end", "type": "end"},
        ],
    }


def node_input_workflow():
    return {
        "schema_version": "1.0",
        "id": "nodeinput-wf",
        "name": "节点用户输入",
        "input_schema": {
            "type": "object",
            "required": ["topic"],
            "properties": {"topic": {"type": "string"}},
        },
        "nodes": [
            {"id": "start", "type": "start"},
            {
                "id": "worldview", "type": "task",
                "input_schema": {
                    "type": "object",
                    "required": ["inspiration"],
                    "properties": {
                        "inspiration": {
                            "type": "string", "title": "世界观灵感",
                            "description": "一两句核心意象"
                        }
                    },
                },
                "prompt": "基于灵感 ${inspiration} 与主题 ${topic} 给出设定",
            },
            {"id": "end", "type": "end"},
        ],
    }


# ---- small agent helpers -------------------------------------------------
def drive_to_completion(engine, wf, record):
    while True:
        spec = engine.node_spec(wf, record)
        if spec["status"] != "running":
            return record
        node = spec["node"]
        record = engine.complete(wf, record["run_id"], output=f"{node['id']} output")


def drive_to_node(engine, wf, record, target_id, outputs=None):
    """Advance (completing nodes) until next_node == target_id."""
    outputs = outputs or {}
    while record["next_node"] != target_id:
        nid = record["next_node"]
        record = engine.complete(wf, record["run_id"], output=outputs.get(nid, f"{nid} v1"))
    return record


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = Storage(root=f"{self.tmp}/.aiwf").init_dirs()
        self.engine = WorkflowEngine(self.storage)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- basics -------------------------------------------------------
    def test_agent_driven_linear_run_is_audited(self):
        wf = linear_workflow()
        record = self.engine.start(wf, {"topic": "赛博侦探"})

        spec = self.engine.node_spec(wf, record)
        self.assertEqual(spec["phase"], "executing")
        self.assertEqual(spec["node"]["type"], "start")

        record = self.engine.complete(wf, record["run_id"])  # start

        spec = self.engine.node_spec(wf, record)
        self.assertEqual(spec["node"]["type"], "task")
        self.assertEqual(spec["node"]["prompt"], "以《赛博侦探》写初稿")
        self.assertIsNone(spec["node"]["previous_output"])

        record = self.engine.complete(
            wf, record["run_id"], output="这是初稿正文",
            model="agent-model",
            tokens={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )
        record = self.engine.complete(wf, record["run_id"])  # end

        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["context"]["draft"], "这是初稿正文")

        draft_run = next(n for n in record["node_runs"] if n["node_id"] == "draft")
        self.assertEqual(draft_run["status"], "success")
        self.assertEqual(draft_run["model"], "agent-model")
        self.assertEqual(draft_run["tokens"]["total_tokens"], 15)
        self.assertEqual(draft_run["input"]["prompt"], "以《赛博侦探》写初稿")
        self.assertEqual(draft_run["summary"], "这是初稿正文")

        on_disk = self.storage.load_run("test-wf", record["run_id"])
        self.assertEqual(on_disk["status"], "completed")

    def test_missing_startup_input_fails_validation(self):
        wf = linear_workflow()
        with self.assertRaises(Exception):
            self.engine.start(wf, {})

    # ---- branch -------------------------------------------------------
    def test_branch_auto_loop_then_pass(self):
        wf = linear_workflow()
        wf["nodes"] = [
            wf["nodes"][0],
            wf["nodes"][1],
            {"id": "qc", "type": "branch",
             "prompt": "只回答 PASS 或 REVISE：${draft}",
             "routes": {"PASS": "end", "REVISE": "fix"}, "default": "end"},
            {"id": "fix", "type": "task",
             "prompt": "按意见修改：${draft}", "output_key": "draft"},
            wf["nodes"][2],
        ]
        wf["edges"] = [
            {"from": "start", "to": "draft"},
            {"from": "draft", "to": "qc"},
            {"from": "fix", "to": "qc"},
        ]

        record = self.engine.start(wf, {"topic": "X"})
        e = self.engine
        record = e.complete(wf, record["run_id"])                    # start
        record = e.complete(wf, record["run_id"], output="初稿")     # draft

        spec = e.node_spec(wf, record)["node"]
        self.assertEqual(spec["routes"]["REVISE"], "fix")
        record = e.complete(wf, record["run_id"], output="revise")   # branch
        self.assertEqual(record["node_runs"][-1]["decision"],
                         {"answer": "revise", "route": "fix"})

        record = e.complete(wf, record["run_id"], output="修订稿")   # fix
        record = e.complete(wf, record["run_id"], output="pass.")    # branch again
        self.assertEqual(record["node_runs"][-1]["decision"],
                         {"answer": "pass.", "route": "end"})
        record = e.complete(wf, record["run_id"])                    # end

        self.assertEqual(record["status"], "completed")
        ids = [n["node_id"] for n in record["node_runs"]]
        self.assertEqual(ids, ["start", "draft", "qc", "fix", "qc", "end"])
        self.assertEqual(record["context"]["draft"], "修订稿")

    # ---- node input ---------------------------------------------------
    def test_node_input_pause_and_provide(self):
        wf = node_input_workflow()
        record = self.engine.start(wf, {"topic": "小说"})

        # arriving at worldview pauses for user input
        record = self.engine.complete(wf, record["run_id"])  # start
        spec = self.engine.node_spec(wf, record)
        self.assertEqual(record["phase"], "awaiting_input")
        self.assertIsNone(spec["node"])
        req = spec["input_request"]
        self.assertEqual(req["node_id"], "worldview")
        self.assertEqual(req["fields"][0]["title"], "世界观灵感")

        record = self.engine.provide(
            wf, record["run_id"], {"inspiration": "赛博雨夜"}
        )
        self.assertEqual(record["phase"], "executing")
        spec = self.engine.node_spec(wf, record)["node"]
        self.assertEqual(spec["prompt"], "基于灵感 赛博雨夜 与主题 小说 给出设定")

        record = self.engine.complete(wf, record["run_id"], output="设定 v1")
        record = self.engine.complete(wf, record["run_id"])  # end
        self.assertEqual(record["status"], "completed")

        types = [e["type"] for e in record["events"]]
        self.assertIn("input_provided", types)

    # ---- embedded review ---------------------------------------------
    def test_embedded_review_reject_then_approve(self):
        wf = embedded_review_workflow()
        record = self.engine.start(wf, {"topic": "T"})
        record = self.engine.complete(wf, record["run_id"])  # start

        # task output reported -> review gate, no advance
        record = self.engine.complete(wf, record["run_id"], output="初稿 v1")
        self.assertEqual(record["phase"], "awaiting_review")
        pending = record["pending_review"]
        self.assertEqual(pending["kind"], "embedded")
        self.assertEqual(pending["requirements"][1], "检查项二：T")
        self.assertEqual(pending["routes"], {"APPROVE": None, "REJECT": None})

        # reject with feedback -> back to the same task
        record = self.engine.review(
            wf, record["run_id"], "REJECT", feedback="冲突不够强"
        )
        self.assertEqual(record["next_node"], "draft")
        self.assertEqual(record["phase"], "executing")
        spec = self.engine.node_spec(wf, record)["node"]
        self.assertEqual(spec["feedback"], "冲突不够强")
        self.assertEqual(spec["previous_output"], "初稿 v1")

        record = self.engine.complete(wf, record["run_id"], output="初稿 v2")
        self.assertEqual(record["phase"], "awaiting_review")

        # approve -> advance along edges
        record = self.engine.review(wf, record["run_id"], "APPROVE")
        self.assertEqual(record["next_node"], "end")
        record = self.engine.complete(wf, record["run_id"])
        self.assertEqual(record["status"], "completed")

        redo = [n for n in record["node_runs"] if n["node_id"] == "draft"]
        self.assertEqual([n["output"] for n in redo], ["初稿 v1", "初稿 v2"])
        self.assertEqual(redo[1]["feedback"], "冲突不够强")

    # ---- standalone review -------------------------------------------
    def test_standalone_review_checkpoint(self):
        wf = standalone_review_workflow()
        record = self.engine.start(wf, {"topic": "T"})
        record = drive_to_node(self.engine, wf, record, "draft")
        record = self.engine.complete(wf, record["run_id"], output="draft v1")

        # arriving at the checkpoint pauses immediately
        self.assertEqual(record["next_node"], "checkpoint")
        self.assertEqual(record["phase"], "awaiting_review")
        self.assertIn("draft v1", record["pending_review"]["requirements"])

        record = self.engine.review(wf, record["run_id"], "REJECT")
        self.assertEqual(record["next_node"], "draft")
        record = self.engine.complete(wf, record["run_id"], output="draft v2")

        self.assertEqual(record["phase"], "awaiting_review")
        record = self.engine.review(wf, record["run_id"], "APPROVE")
        self.assertEqual(record["next_node"], "end")
        record = self.engine.complete(wf, record["run_id"])
        self.assertEqual(record["status"], "completed")

    # ---- revise cascade ----------------------------------------------
    def test_revise_cascade_redo_stale_reuse_others(self):
        wf = diamond_workflow()
        record = self.engine.start(wf, {"seed": "S"})
        # start, p1, p2, p3 executed; stop while p4 is current
        record = drive_to_node(self.engine, wf, record, "p4")

        record = self.engine.revise(
            wf, record["run_id"], target="p2", reason="p2 需要调整"
        )
        rev = record["revision"]
        self.assertEqual(rev["status"], "active")
        self.assertEqual(rev["stale_nodes"], ["p2", "p4"])
        self.assertEqual(rev["reused_nodes"], ["p3"])
        self.assertEqual(record["next_node"], "p2")

        # redo p2
        spec = self.engine.node_spec(wf, record)["node"]
        self.assertTrue(spec["under_revision"])
        self.assertEqual(spec["previous_output"], "p2 v1")
        record = self.engine.complete(wf, record["run_id"], output="p2 v2")

        # p3 auto-skipped (reused); p4 becomes current and is redone
        self.assertEqual(record["next_node"], "p4")
        skipped = [n for n in record["node_runs"] if n["status"] == "skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["node_id"], "p3")
        self.assertEqual(skipped[0]["output"], "p3 v1")

        record = self.engine.complete(wf, record["run_id"], output="p4 v2")
        self.assertEqual(record["revision"]["status"], "completed")
        record = self.engine.complete(wf, record["run_id"])  # end

        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["context"]["p2"], "p2 v2")
        self.assertEqual(record["context"]["p3"], "p3 v1")  # reused, not redone
        etypes = [e["type"] for e in record["events"]]
        self.assertEqual(etypes.count("node_reused"), 1)
        self.assertIn("revision_completed", etypes)

    def test_revise_closure_widens_for_p1(self):
        wf = diamond_workflow()
        record = self.engine.start(wf, {"seed": "S"})
        record = drive_to_node(self.engine, wf, record, "p4")
        record = self.engine.revise(wf, record["run_id"], "p1", "改根节点")
        self.assertEqual(
            record["revision"]["stale_nodes"], ["p1", "p2", "p3", "p4"]
        )

    def test_revise_return_mode_jumps_back(self):
        wf = diamond_workflow()
        record = self.engine.start(wf, {"seed": "S"})
        record = drive_to_node(self.engine, wf, record, "p3")
        record = self.engine.revise(
            wf, record["run_id"], "p1", "微调 p1", mode="return"
        )
        self.assertEqual(record["next_node"], "p1")
        record = self.engine.complete(wf, record["run_id"], output="p1 v2")

        # target done -> directly back to requested_from (p3); p2 untouched
        self.assertEqual(record["revision"]["status"], "completed")
        self.assertEqual(record["next_node"], "p3")
        self.assertEqual(record["context"]["p2"], "p2 v1")
        self.assertEqual(record["context"]["p1"], "p1 v2")

    # ---- provide on completed node -> revision -----------------------
    def test_change_completed_node_input_opens_revision(self):
        wf = node_input_workflow()
        record = self.engine.start(wf, {"topic": "T"})
        record = drive_to_node(self.engine, wf, record, "worldview")
        record = self.engine.provide(
            wf, record["run_id"], {"inspiration": "玄幻"}
        )
        record = self.engine.complete(wf, record["run_id"], output="设定 v1")
        # now current node is end; change worldview's input
        record = self.engine.provide(
            wf, record["run_id"], {"inspiration": "未来世界"},
            node_id="worldview", reason="换方向",
        )
        self.assertEqual(record["next_node"], "worldview")
        self.assertEqual(record["revision"]["stale_nodes"], ["worldview"])
        spec = self.engine.node_spec(wf, record)["node"]
        self.assertEqual(spec["prompt"], "基于灵感 未来世界 与主题 T 给出设定")

    # ---- fail / resume -----------------------------------------------
    def test_revision_including_checkpoint_closes_on_approve(self):
        wf = standalone_review_workflow()
        record = self.engine.start(wf, {"topic": "T"})
        record = drive_to_node(self.engine, wf, record, "draft")
        record = self.engine.complete(wf, record["run_id"], output="draft v1")

        # standing at the checkpoint gate, revise the upstream draft
        self.assertEqual(record["next_node"], "checkpoint")
        record = self.engine.revise(wf, record["run_id"], "draft", "draft 要改")
        self.assertEqual(
            record["revision"]["stale_nodes"], ["checkpoint", "draft"]
        )

        record = self.engine.complete(wf, record["run_id"], output="draft v2")
        self.assertEqual(record["next_node"], "checkpoint")
        # approving the stale checkpoint closes the revision
        record = self.engine.review(wf, record["run_id"], "APPROVE")
        self.assertEqual(record["revision"]["status"], "completed")
        self.assertEqual(record["next_node"], "end")

    def test_fail_then_resume(self):
        wf = linear_workflow()
        record = self.engine.start(wf, {"topic": "X"})
        record = self.engine.complete(wf, record["run_id"])  # start

        record = self.engine.fail(wf, record["run_id"], "rate limit hit")
        self.assertEqual(record["status"], "failed")
        self.assertIn("rate limit", record["error"])
        self.assertEqual(record["next_node"], "draft")

        with self.assertRaises(RuntimeError):
            self.engine.complete(wf, record["run_id"], output="late")

        record = self.engine.resume(wf, record["run_id"])
        record = self.engine.complete(wf, record["run_id"], output="恢复后的初稿")
        record = self.engine.complete(wf, record["run_id"])  # end

        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["context"]["draft"], "恢复后的初稿")

    def test_resume_completed_run_rejected(self):
        wf = linear_workflow()
        record = self.engine.start(wf, {"topic": "X"})
        record = drive_to_completion(self.engine, wf, record)
        with self.assertRaises(RuntimeError):
            self.engine.resume(wf, record["run_id"])

    # ---- output size, file refs, context doc --------------------------
    def test_oversized_output_rejected_keeps_run_running(self):
        wf = linear_workflow()
        record = self.engine.start(wf, {"topic": "X"})
        record = self.engine.complete(wf, record["run_id"])  # start -> draft

        with self.assertRaises(ValueError):
            self.engine.complete(
                wf, record["run_id"], output="字" * (OUTPUT_LIMIT + 1)
            )

        # bad output was never persisted; run is still running at draft
        on_disk = self.storage.load_run("test-wf", record["run_id"])
        self.assertEqual(on_disk["status"], "running")
        self.assertEqual(on_disk["next_node"], "draft")
        self.assertNotIn("draft", on_disk["context"])

    def test_file_ref_renders_as_path_in_prompt(self):
        wf = linear_workflow()
        wf["nodes"] = [
            wf["nodes"][0],
            wf["nodes"][1],
            {"id": "reader", "type": "task",
             "prompt": "读取文件：${draft}"},
            wf["nodes"][2],
        ]
        wf["edges"] = [
            {"from": "start", "to": "draft"},
            {"from": "draft", "to": "reader"},
            {"from": "reader", "to": "end"},
        ]

        record = self.engine.start(wf, {"topic": "T"})
        record = self.engine.complete(wf, record["run_id"])  # start
        record = self.engine.complete(
            wf, record["run_id"], output={"$file": "novel/x.md"}
        )  # draft -> reader

        spec = self.engine.node_spec(wf, record)["node"]
        self.assertEqual(spec["prompt"], "读取文件：novel/x.md")

    def test_context_doc_written_with_artifact_index(self):
        wf = linear_workflow()
        record = self.engine.start(wf, {"topic": "T"})
        self.assertTrue(
            os.path.isfile(self.storage.run_path("test-wf", record["run_id"]))
        )

        record = self.engine.complete(wf, record["run_id"])  # start
        record = self.engine.complete(
            wf, record["run_id"], output={"$file": "novel/chapter-1.md"}
        )  # draft

        path = self.storage.context_doc_path("test-wf", record["run_id"])
        self.assertTrue(os.path.isfile(path))
        with open(path, "r", encoding="utf-8") as f:
            md = f.read()
        self.assertIn("# 运行上下文", md)
        self.assertIn("## 产物索引", md)
        self.assertIn("novel/chapter-1.md", md)
        self.assertIn("draft", md)


if __name__ == "__main__":
    unittest.main()
