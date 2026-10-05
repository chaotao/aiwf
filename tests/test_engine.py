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


def branch_switch_workflow():
    """a -> qc; qc PASS -> b (REVISE -> fix) -> c -> end; fix -> qc."""
    return {
        "schema_version": "1.0",
        "id": "branch-switch-wf",
        "name": "分支切换",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "a", "type": "task", "prompt": "a step"},
            {"id": "qc", "type": "branch",
             "prompt": "check ${a}",
             "routes": {"PASS": "b", "REVISE": "fix"}, "default": "b"},
            {"id": "fix", "type": "task", "prompt": "fix ${a}"},
            {"id": "b", "type": "task", "prompt": "use ${a}"},
            {"id": "c", "type": "task", "prompt": "brand new step"},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"from": "start", "to": "a"},
            {"from": "a", "to": "qc"},
            {"from": "b", "to": "c"},
            {"from": "c", "to": "end"},
            {"from": "fix", "to": "qc"},
        ],
    }


def loop_revision_workflow():
    """a -> gate: CONTINUE -> a, DONE -> tail -> end.

    fix references ${a} so it is stale by closure, but no edge routes to it:
    after gate=CONTINUE the path loops back to a (a new iteration).
    """
    return {
        "schema_version": "1.0",
        "id": "loop-rev-wf",
        "name": "循环修订",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "a", "type": "task", "prompt": "a step"},
            {"id": "gate", "type": "branch",
             "prompt": "check ${a}",
             "routes": {"CONTINUE": "a", "DONE": "tail"}, "default": "tail"},
            {"id": "fix", "type": "task", "prompt": "fix ${a}"},
            {"id": "tail", "type": "task", "prompt": "tail step"},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"from": "start", "to": "a"},
            {"from": "a", "to": "gate"},
            {"from": "tail", "to": "end"},
        ],
    }


def batch_consumers_workflow():
    """writer produces a $file under novel/chapters/; assembler consumes the
    whole batch solely via a glob (its prompt has no ${var} refs)."""
    return {
        "schema_version": "1.0",
        "id": "batch-wf",
        "name": "批量消费",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "writer", "type": "task", "prompt": "写出章节文件"},
            {"id": "assembler", "type": "task",
             "consumes": [{"glob": "novel/chapters/*.md"}],
             "prompt": "汇总全部章节"},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"from": "start", "to": "writer"},
            {"from": "writer", "to": "assembler"},
            {"from": "assembler", "to": "end"},
        ],
    }


def when_branch_workflow(routes=None):
    """gate deterministically compares input count >= 2."""
    return {
        "schema_version": "1.0",
        "id": "when-wf",
        "name": "确定性分支",
        "input_schema": {
            "type": "object",
            "required": ["count"],
            "properties": {"count": {"type": "integer"}},
        },
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "gate", "type": "branch",
             "when": {"gte": [{"var": "count"}, 2]},
             "routes": routes or {"TRUE": "big", "FALSE": "small"}},
            {"id": "big", "type": "task", "prompt": "大分支"},
            {"id": "small", "type": "task", "prompt": "小分支"},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"from": "start", "to": "gate"},
            {"from": "big", "to": "end"},
            {"from": "small", "to": "end"},
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

    def test_cascade_revision_closes_when_path_leaves_stale(self):
        wf = branch_switch_workflow()
        e = self.engine
        record = e.start(wf)
        record = e.complete(wf, record["run_id"])                    # start
        record = e.complete(wf, record["run_id"], output="a v1")     # a
        record = e.complete(wf, record["run_id"], output="PASS")     # qc -> b
        self.assertEqual(record["next_node"], "b")

        record = e.revise(wf, record["run_id"], "a", "调整 a")
        # fix is stale only by dependency closure; reachable solely via qc=REVISE
        self.assertEqual(
            sorted(record["revision"]["stale_nodes"]),
            ["a", "b", "fix", "qc"],
        )

        record = e.complete(wf, record["run_id"], output="a v2")     # a -> qc
        record = e.complete(wf, record["run_id"], output="PASS")     # qc -> b
        record = e.complete(wf, record["run_id"], output="b v2")     # b -> c

        # entering the brand-new node c the unreachable fix is forgiven:
        # the revision is over and c is a normal node
        self.assertEqual(record["next_node"], "c")
        self.assertEqual(record["revision"]["status"], "completed")
        self.assertFalse(e.node_spec(wf, record)["node"]["under_revision"])

        record = e.complete(wf, record["run_id"], output="c out")
        record = e.complete(wf, record["run_id"])                    # end
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["revision"]["status"], "completed")
        self.assertIsNone(
            next(n for n in record["node_runs"] if n["node_id"] == "c")["revision_seq"]
        )

    def test_cascade_loop_revisits_stale_node_instead_of_skipping(self):
        wf = loop_revision_workflow()
        e = self.engine
        record = e.start(wf)
        record = e.complete(wf, record["run_id"])                    # start
        record = e.complete(wf, record["run_id"], output="a v1")     # a
        record = e.complete(wf, record["run_id"], output="CONTINUE")  # gate -> a

        record = e.revise(wf, record["run_id"], "a", "调整 a")
        self.assertEqual(
            sorted(record["revision"]["stale_nodes"]),
            ["a", "fix", "gate"],
        )

        record = e.complete(wf, record["run_id"], output="a v2")     # a redo
        record = e.complete(wf, record["run_id"], output="CONTINUE")  # gate -> a

        # back at a on a new loop iteration: executed again, not skipped;
        # the unreachable fix is forgiven so the revision is over
        self.assertEqual(record["next_node"], "a")
        self.assertEqual(record["revision"]["status"], "completed")
        a_runs = [n for n in record["node_runs"] if n["node_id"] == "a"]
        self.assertEqual(len(a_runs), 2)
        self.assertTrue(all(n["status"] == "success" for n in a_runs))

        record = e.complete(wf, record["run_id"], output="a v3")     # a -> gate
        record = e.complete(wf, record["run_id"], output="DONE")     # gate -> tail
        record = e.complete(wf, record["run_id"], output="tail out")
        record = e.complete(wf, record["run_id"])                    # end
        self.assertEqual(record["status"], "completed")

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
            os.path.isfile(os.path.join(
                self.storage.runs_dir, "test-wf", record["run_id"], "record.json"
            ))
        )

        record = self.engine.complete(wf, record["run_id"])  # start
        record = self.engine.complete(
            wf, record["run_id"], output={"$file": "novel/chapter-1.md"}
        )  # draft

        path = os.path.join(
            self.storage.runs_dir, "test-wf", record["run_id"], "context.md"
        )
        self.assertTrue(os.path.isfile(path))
        with open(path, "r", encoding="utf-8") as f:
            md = f.read()
        self.assertIn("# 运行上下文", md)
        self.assertIn("## 产物索引", md)
        self.assertIn("novel/chapter-1.md", md)
        self.assertIn("draft", md)

    def test_artifact_index_renders_templated_items_source(self):
        """A foreach items source declared as {"$file": "<template>"} is
        rendered when the queue is resolved; the artifact index must render it
        the same way. Otherwise the index shows a raw ${...} and the entry
        misses dedup against the very same path reported by a node."""
        import json as _json
        wf = {
            "schema_version": "1.1", "id": "itemsrc-wf", "name": "队列源模板",
            "input_schema": {"type": "object", "properties": {"topic": {}}},
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "batch", "type": "foreach",
                 "items": {"$file": "${run.dir}/items.json"},
                 "inputs": {"topic": "${item.outline}"},
                 "nodes": [
                     {"id": "write", "type": "task", "output_key": "summary",
                      "prompt": "写：${topic}"}
                 ],
                 "exports": ["summary"],
                 "outputs_key": "results"},
                {"id": "end", "type": "end"},
            ],
            "edges": [
                {"from": "start", "to": "batch"},
                {"from": "batch", "to": "end"},
            ],
        }
        record = self.engine.start(wf, {"topic": "T"})
        source = os.path.join(
            self.storage.run_dir_for_record(record), "items.json"
        )
        with open(source, "w", encoding="utf-8") as f:
            _json.dump([{"outline": "甲"}, {"outline": "乙"}], f)
        record = self.engine.complete(wf, record["run_id"])  # start -> batch

        md_path = os.path.join(
            self.storage.run_dir_for_record(record), "context.md"
        )
        with open(md_path, "r", encoding="utf-8") as f:
            index = f.read().split("## 产物索引", 1)[1]
        self.assertIn("items.json", index)
        self.assertIn(source.replace(os.sep, "/"), index.replace(os.sep, "/"))
        self.assertNotIn("${run.dir}", index)

    # ---- consumes explicit dependencies ------------------------------
    def test_consumes_string_adds_dependency_beyond_prompt(self):
        wf = {
            "schema_version": "1.0", "id": "cons-wf", "name": "显式依赖",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "src", "type": "task", "prompt": "源节点"},
                {"id": "down", "type": "task",
                 "consumes": ["src"], "prompt": "下游（prompt 无引用）"},
                {"id": "end", "type": "end"},
            ],
            "edges": [
                {"from": "start", "to": "src"},
                {"from": "src", "to": "down"},
                {"from": "down", "to": "end"},
            ],
        }
        e = self.engine
        record = e.start(wf)
        record = drive_to_node(e, wf, record, "end")
        record = e.revise(wf, record["run_id"], "src", "源改动")
        self.assertEqual(record["revision"]["stale_nodes"], ["down", "src"])

    def test_consumes_glob_marks_batch_consumer_stale_and_reruns(self):
        wf = batch_consumers_workflow()
        e = self.engine
        record = e.start(wf)
        record = e.complete(wf, record["run_id"])                       # start
        record = e.complete(
            wf, record["run_id"], output={"$file": "novel/chapters/chapter-3.md"}
        )                                                                # writer
        record = e.complete(
            wf, record["run_id"], output={"$file": "novel/full.md"}
        )                                                                # assembler
        self.assertEqual(record["next_node"], "end")

        record = e.revise(wf, record["run_id"], "writer", "章节改动")
        self.assertIn("assembler", record["revision"]["stale_nodes"])

        # writer redone -> assembler must really execute, not be skipped
        record = e.complete(
            wf, record["run_id"], output={"$file": "novel/chapters/chapter-3.md"}
        )
        self.assertEqual(record["next_node"], "assembler")
        assembler_runs = [
            n for n in record["node_runs"] if n["node_id"] == "assembler"
        ]
        self.assertEqual(len(assembler_runs), 1)  # original run; now current

        record = e.complete(
            wf, record["run_id"], output={"$file": "novel/full-v2.md"}
        )
        assembler_runs = [
            n for n in record["node_runs"] if n["node_id"] == "assembler"
        ]
        self.assertEqual([n["status"] for n in assembler_runs],
                         ["success", "success"])
        self.assertFalse(
            any(n["node_id"] == "assembler" and n["status"] == "skipped"
                for n in record["node_runs"])
        )

    def test_consumes_glob_does_not_match_unrelated_file(self):
        wf = batch_consumers_workflow()
        e = self.engine
        record = e.start(wf)
        record = drive_to_node(e, wf, record, "end",
                               outputs={"writer": {"$file": "notes/scratch.md"},
                                        "assembler": {"$file": "novel/full.md"}})
        record = e.revise(wf, record["run_id"], "writer", "无关文件改动")
        self.assertNotIn("assembler", record["revision"]["stale_nodes"])

    # ---- output_schema -----------------------------------------------
    def test_output_schema_rejects_bad_output_then_accepts(self):
        wf = {
            "schema_version": "1.0", "id": "oschema-wf", "name": "产物契约",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "t", "type": "task", "prompt": "产出结构化结果",
                 "output_schema": {
                     "type": "object",
                     "required": ["status"],
                     "properties": {"status": {"type": "string"}},
                 }},
                {"id": "end", "type": "end"},
            ],
        }
        e = self.engine
        record = e.start(wf)
        record = e.complete(wf, record["run_id"])  # start -> t

        record = e.complete(wf, record["run_id"], output="not-an-object")
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["next_node"], "t")
        self.assertEqual(record["phase"], "executing")
        self.assertEqual(record["node_runs"][-1]["status"], "rejected")
        self.assertNotIn("t", record["context"])
        self.assertIn("output_schema", record["last_feedback"])

        spec = e.node_spec(wf, record)["node"]
        self.assertEqual(spec["feedback"], record["last_feedback"])
        self.assertEqual(
            spec["output_schema"]["required"], ["status"]
        )

        record = e.complete(
            wf, record["run_id"], output={"status": "ok"}
        )  # t -> end
        self.assertEqual(record["context"]["t"], {"status": "ok"})
        record = e.complete(wf, record["run_id"])
        self.assertEqual(record["status"], "completed")
        self.assertIn(
            "output_rejected",
            [ev["type"] for ev in record["events"]],
        )

    # ---- deterministic when branch -----------------------------------
    def test_when_branch_true_route(self):
        wf = when_branch_workflow()
        e = self.engine
        record = e.start(wf, {"count": 3})
        record = e.complete(wf, record["run_id"])  # start -> gate

        spec = e.node_spec(wf, record)["node"]
        self.assertTrue(spec["deterministic"])
        self.assertEqual(spec["when"], {"gte": [{"var": "count"}, 2]})

        record = e.complete(wf, record["run_id"])  # empty: engine evaluates
        self.assertEqual(record["next_node"], "big")
        self.assertEqual(
            record["node_runs"][-1]["decision"],
            {"answer": "TRUE", "route": "big"},
        )

    def test_when_branch_false_route(self):
        wf = when_branch_workflow()
        e = self.engine
        record = e.start(wf, {"count": 1})
        record = drive_to_node(e, wf, record, "gate")
        record = e.complete(wf, record["run_id"])
        self.assertEqual(record["next_node"], "small")
        self.assertEqual(
            record["node_runs"][-1]["decision"]["answer"], "FALSE"
        )

    def test_when_branch_needs_true_false_routes(self):
        wf = when_branch_workflow(routes={"TRUE": "big"})
        with self.assertRaises(ValueError):
            self.engine.start(wf, {"count": 1})

    def test_unknown_when_operator_rejected_at_start(self):
        wf = when_branch_workflow()
        wf["nodes"][1]["when"] = {"frobnicate": [1, 2]}
        with self.assertRaises(Exception):
            self.engine.start(wf, {"count": 1})

    # ---- subflow / foreach -------------------------------------------
    def _save_child(self):
        self.storage.save_workflow(chapter_child_workflow())

    def test_subflow_exports_returned_under_output_key(self):
        self._save_child()
        e, parent = self.engine, subflow_parent_workflow()
        self.storage.save_workflow(parent)
        record = e.start(parent)
        record = e.complete(parent, record["run_id"])  # start -> call
        state = record["subflow_state"]
        inst = state["instances"][0]
        self.assertEqual(inst["status"], "running")
        child_dir = self.storage.child_dir(record, "call", "0")
        self.assertTrue(
            os.path.isfile(os.path.join(child_dir, "record.json"))
        )
        record = _run_child_success(
            e, self.storage.find_run(inst["run_id"]), "章节摘要"
        )
        self.assertEqual(record["workflow_id"], "book-wf")
        self.assertEqual(
            record["context"]["chapter"], {"summary": "章节摘要"}
        )
        record = e.complete(parent, record["run_id"])  # end
        self.assertEqual(record["status"], "completed")

    def test_parent_blocks_complete_while_child_running(self):
        self._save_child()
        e, parent = self.engine, subflow_parent_workflow()
        record = e.start(parent)
        record = e.complete(parent, record["run_id"])
        with self.assertRaises(RuntimeError):
            e.complete(parent, record["run_id"])

    def test_foreach_external_static_manifest(self):
        self._save_child()
        e, parent = self.engine, foreach_parent_workflow()
        self.storage.save_workflow(parent)
        record = e.start(parent)
        record = e.complete(parent, record["run_id"])  # first instance
        state = record["subflow_state"]
        self.assertEqual(len(state["instances"]), 2)
        record = _run_child_success(
            e, self.storage.find_run(state["instances"][0]["run_id"]), "S1"
        )
        second = next(
            i for i in record["subflow_state"]["instances"] if i["key"] == "1"
        )
        self.assertEqual(second["status"], "running")
        record = _run_child_success(
            e, self.storage.find_run(second["run_id"]), "S2"
        )
        self.assertEqual(
            record["subflow_state"]["status"], "completed"
        )
        manifest = self.storage.read_json_file(
            record["context"]["results"]["$file"]
        )
        self.assertEqual(
            [i["status"] for i in manifest["items"]],
            ["success", "success"],
        )
        self.assertEqual(
            manifest["items"][0]["exports"], {"summary": "S1"}
        )

    def test_foreach_inline_body_materialized(self):
        e, parent = self.engine, foreach_inline_parent_workflow()
        self.storage.save_workflow(parent)
        record = e.start(parent)
        record = e.complete(parent, record["run_id"])
        state = record["subflow_state"]
        self.assertTrue(state["inline"])
        synth = self.storage.read_json_file(
            self.storage.inline_subflow_path(record, "batch")
        )
        self.assertEqual(synth["id"], "inline-book-wf.batch")
        record = _run_child_success(
            e, self.storage.find_run(state["instances"][0]["run_id"]), "A"
        )
        inst2 = next(
            i for i in record["subflow_state"]["instances"]
            if i["key"] == "1"
        )
        record = _run_child_success(
            e, self.storage.find_run(inst2["run_id"]), "B"
        )
        manifest = self.storage.read_json_file(
            record["context"]["results"]["$file"]
        )
        self.assertEqual(
            [i["exports"] for i in manifest["items"]],
            [{"summary": "A"}, {"summary": "B"}],
        )

    def test_foreach_queue_is_engine_owned_and_sealed_explicitly(self):
        """A queue node has no items file at all: `queue_add` seeds it (even
        before the run reaches the node), `queue_seal` is what ends the wait,
        and `id` -- not `id_field` -- keys the instances."""
        self._save_child()
        parent = foreach_parent_workflow()
        parent["nodes"][1]["items"] = {"queue": True}
        e = self.engine
        self.storage.save_workflow(parent)
        record = e.start(parent)

        # seeding a queue for a node the run has not reached yet
        table = e.queue_add(
            record["run_id"], "chapters",
            [{"id": "A", "name": "第一章", "outline": "第一章"}],
        )
        self.assertEqual([row["id"] for row in table["items"]], ["A"])
        self.assertFalse(table["done"])

        record = e.complete(parent, record["run_id"])
        self.assertEqual(record["phase"], "executing")
        self.assertEqual(record["subflow_state"]["items_mode"], "queue")
        inst = record["subflow_state"]["instances"][0]
        self.assertEqual(inst["key"], "A")
        record = _run_child_success(
            e, self.storage.find_run(inst["run_id"]), "D1"
        )
        # nothing pending and the queue is not sealed: a wait, not an end
        self.assertEqual(record["phase"], "awaiting_items")

        # appending is what moves it on again
        e.queue_add(
            record["run_id"], "chapters",
            [{"id": "B", "name": "第二章", "outline": "第二章"}],
        )
        record = self.storage.find_run(record["run_id"])
        inst = record["subflow_state"]["instances"][1]
        self.assertEqual(inst["key"], "B")
        record = _run_child_success(
            e, self.storage.find_run(inst["run_id"]), "D2"
        )
        self.assertEqual(record["phase"], "awaiting_items")

        e.queue_seal(record["run_id"], "chapters")
        record = self.storage.find_run(record["run_id"])
        manifest = self.storage.read_json_file(
            record["context"]["results"]["$file"]
        )
        self.assertEqual(
            [i["status"] for i in manifest["items"]],
            ["success", "success"],
        )
        # sealed and drained: the node is done and the run has moved past it
        self.assertEqual(record["subflow_state"]["status"], "completed")
        self.assertEqual(record["next_node"], "end")

    def test_queue_items_are_validated_and_mutable_only_before_they_start(self):
        self._save_child()
        parent = foreach_parent_workflow()
        parent["nodes"][1]["items"] = {"queue": True}
        e = self.engine
        self.storage.save_workflow(parent)
        record = e.start(parent)
        rid = record["run_id"]

        with self.assertRaisesRegex(ValueError, "needs non-empty string"):
            e.queue_add(rid, "chapters", [{"name": "no id"}])
        with self.assertRaisesRegex(ValueError, "must match"):
            e.queue_add(rid, "chapters", [{"id": "a b", "name": "spaced"}])
        e.queue_add(rid, "chapters", [
            {"id": "A", "name": "一", "outline": "第一章"},
            {"id": "B", "name": "二", "outline": "第二章"},
        ])
        with self.assertRaisesRegex(ValueError, "already in queue"):
            e.queue_add(rid, "chapters", [{"id": "A", "name": "dup"}])

        # a pending item is still editable, and its fields are extensible
        e.queue_update(rid, "chapters", "B", {"name": "二改", "note": "extra"})
        e.queue_remove(rid, "chapters", "B")
        table = e.queue_table(rid, "chapters")
        self.assertEqual([row["id"] for row in table["items"]], ["A"])

        e.queue_remove(rid, "chapters", "A")
        e.queue_seal(rid, "chapters")
        record = e.complete(parent, rid)
        # empty but sealed is an end, not a wait -- and the run moves on
        self.assertEqual(record["next_node"], "end")
        self.assertEqual(record["subflow_state"]["instances"], [])

    def test_started_queue_items_are_immutable_and_a_seal_is_final(self):
        self._save_child()
        parent = foreach_parent_workflow()
        parent["nodes"][1]["items"] = {"queue": True}
        e = self.engine
        self.storage.save_workflow(parent)
        record = e.start(parent)
        rid = record["run_id"]
        e.queue_add(rid, "chapters", [{"id": "A", "name": "一", "outline": "第一章"}])
        record = e.complete(parent, rid)
        inst = record["subflow_state"]["instances"][0]
        record = _run_child_success(
            e, self.storage.find_run(inst["run_id"]), "D1"
        )
        rid = record["run_id"]

        with self.assertRaisesRegex(RuntimeError, "already started"):
            e.queue_update(rid, "chapters", "A", {"name": "改不动"})
        with self.assertRaisesRegex(RuntimeError, "already started"):
            e.queue_remove(rid, "chapters", "A")
        with self.assertRaisesRegex(ValueError, "keys the instance"):
            e.queue_update(rid, "chapters", "A", {"id": "Z"})

        e.queue_seal(rid, "chapters")
        with self.assertRaisesRegex(RuntimeError, "is sealed"):
            e.queue_add(rid, "chapters", [{"id": "B", "name": "二"}])

    def test_pending_queue_items_can_be_reordered_but_never_ahead_of_ran_ones(self):
        """Reordering is the fourth mutation and stops exactly where the others
        do: a pending item may move anywhere in the pending suffix -- including
        into the next-to-run slot -- but never ahead of a started item, whose
        audit identity is already lived."""
        self._save_child()
        parent = foreach_parent_workflow()
        parent["nodes"][1]["items"] = {"queue": True}
        e = self.engine
        self.storage.save_workflow(parent)
        record = e.start(parent)
        rid = record["run_id"]

        e.queue_add(rid, "chapters", [
            {"id": "A", "name": "一", "outline": "第一章"},
            {"id": "B", "name": "二", "outline": "第二章"},
            {"id": "C", "name": "三", "outline": "第三章"},
        ])
        with self.assertRaisesRegex(ValueError, "no item"):
            e.queue_move(rid, "chapters", "Z", 0)
        # C to the head of the pending run: it becomes the next to start
        table = e.queue_move(rid, "chapters", "C", 0)
        self.assertEqual([r["id"] for r in table["items"]], ["C", "A", "B"])

        record = e.complete(parent, rid)
        inst = record["subflow_state"]["instances"][0]
        self.assertEqual(inst["key"], "C")   # the move decided who runs first

        # A and B are still pending while C runs, so they may still be reordered
        table = e.queue_move(rid, "chapters", "A", 2)
        self.assertEqual([r["id"] for r in table["items"]], ["C", "B", "A"])
        # ... but nothing may jump ahead of the started C
        with self.assertRaisesRegex(ValueError, "pending items live in"):
            e.queue_move(rid, "chapters", "A", 0)
        with self.assertRaisesRegex(RuntimeError, "already started"):
            e.queue_move(rid, "chapters", "C", 0)

        # the new order is what actually runs: B is next, not A
        record = _run_child_success(
            e, self.storage.find_run(inst["run_id"]), "D-C"
        )
        self.assertEqual(record["subflow_state"]["instances"][1]["key"], "B")

    def test_subflow_abort_fails_parent_and_resume_retries(self):
        self._save_child()
        e, parent = self.engine, subflow_parent_workflow()
        self.storage.save_workflow(parent)
        record = e.start(parent)
        record = e.complete(parent, record["run_id"])
        child = self.storage.find_run(
            record["subflow_state"]["instances"][0]["run_id"]
        )
        cwf = self.storage.load_workflow("chapter-wf")
        crid = child["run_id"]
        e.complete(cwf, crid)  # start -> write
        record = e.fail(cwf, crid, "模型爆炸")
        self.assertEqual(record["workflow_id"], "book-wf")
        self.assertEqual(record["status"], "failed")

        record = e.resume(parent, record["run_id"])
        inst = record["subflow_state"]["instances"][0]
        self.assertEqual(inst["status"], "running")
        child_record = self.storage.find_run(inst["run_id"])
        self.assertEqual(child_record["status"], "running")
        cwf = self.storage.load_workflow("chapter-wf")
        # child resumed at its write node: complete it -> end
        record = e.complete(
            cwf, inst["run_id"], output="恢复后摘要"
        )
        record = e.complete(cwf, inst["run_id"])  # end -> parent
        record = e.complete(parent, record["run_id"])
        self.assertEqual(record["status"], "completed")

    def test_foreach_on_fail_continue_skips_failed_item(self):
        self._save_child()
        parent = foreach_parent_workflow()
        parent["nodes"][1]["on_fail"] = "continue"
        self.storage.save_workflow(parent)
        e = self.engine
        record = e.start(parent)
        record = e.complete(parent, record["run_id"])
        first = record["subflow_state"]["instances"][0]
        child = self.storage.find_run(first["run_id"])
        cwf = self.storage.load_workflow("chapter-wf")
        crid = child["run_id"]
        e.complete(cwf, crid)
        record = e.fail(cwf, crid, "坏数据")
        # second instance started despite the first failure
        second = next(
            i for i in record["subflow_state"]["instances"]
            if i["key"] == "1"
        )
        self.assertEqual(second["status"], "running")
        record = _run_child_success(
            e, self.storage.find_run(second["run_id"]), "S2"
        )
        manifest = self.storage.read_json_file(
            record["context"]["results"]["$file"]
        )
        self.assertEqual(
            [i["status"] for i in manifest["items"]],
            ["failed", "success"],
        )

    def test_redo_single_foreach_instance(self):
        self._save_child()
        e, parent = self.engine, foreach_parent_workflow()
        self.storage.save_workflow(parent)
        record = e.start(parent)
        record = e.complete(parent, record["run_id"])
        first = record["subflow_state"]["instances"][0]
        record = _run_child_success(
            e, self.storage.find_run(first["run_id"]), "OLD1"
        )
        second = next(
            i for i in record["subflow_state"]["instances"]
            if i["key"] == "1"
        )
        record = _run_child_success(
            e, self.storage.find_run(second["run_id"]), "S2"
        )
        record = e.complete(parent, record["run_id"])  # end
        self.assertEqual(record["status"], "completed")

        record = e.redo(
            parent, record["run_id"], node_id="chapters",
            instance="0", reason="第一章重写",
        )
        self.assertEqual(record["next_node"], "chapters")
        child = self.storage.find_run(
            record["subflow_state"]["instances"][0]["run_id"]
        )
        record = _run_child_success(e, child, "NEW1")
        manifest = self.storage.read_json_file(
            record["context"]["results"]["$file"]
        )
        self.assertEqual(
            manifest["items"][0]["exports"], {"summary": "NEW1"}
        )
        self.assertEqual(
            manifest["items"][1]["exports"], {"summary": "S2"}
        )

    def test_validate_project_rejects_missing_subflow(self):
        parent = subflow_parent_workflow()
        self.storage.save_workflow(parent)
        with self.assertRaises(ValueError):
            self.engine.validate_project()

    def test_validate_project_rejects_call_cycle(self):
        self._save_child()
        self.storage.save_workflow(subflow_parent_workflow())
        cyc = {
            "schema_version": "1.0", "id": "loop-wf", "name": "自环",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "call", "type": "subflow", "workflow": "loop-wf",
                 "inputs": {}, "output_key": "r"},
                {"id": "end", "type": "end"},
            ],
            "edges": [
                {"from": "start", "to": "call"},
                {"from": "call", "to": "end"},
            ],
        }
        self.storage.save_workflow(cyc)
        with self.assertRaises(ValueError):
            self.engine.validate_project()

    def test_validate_project_rejects_unmapped_required(self):
        self._save_child()
        parent = subflow_parent_workflow()
        parent["nodes"][1]["inputs"] = {}
        self.storage.save_workflow(parent)
        with self.assertRaises(ValueError):
            self.engine.validate_project()

    def test_subflow_depth_guard(self):
        for i in range(10):
            self.storage.save_workflow(chain_wf(i))
        e = self.engine
        wf0 = self.storage.load_workflow("c0")
        record = e.start(wf0)
        record = e.complete(wf0, record["run_id"])  # c1 created
        for depth in range(1, 8):
            inst = record["subflow_state"]["instances"][0]
            crec = self.storage.find_run(inst["run_id"])
            cwf = self.storage.load_workflow_for_record(crec)
            record = e.complete(cwf, crec["run_id"])
        # completing start of c8 would create c9 -> depth limit
        inst = record["subflow_state"]["instances"][0]
        crec = self.storage.find_run(inst["run_id"])
        cwf = self.storage.load_workflow_for_record(crec)
        with self.assertRaises(RuntimeError):
            e.complete(cwf, crec["run_id"])

    def test_upstream_revision_redoes_foreach_wholly(self):
        self._save_child()
        e, parent = self.engine, foreach_feeder_workflow()
        self.storage.save_workflow(parent)
        record = e.start(parent, {"topic": "T"})
        record = e.complete(parent, record["run_id"])  # start -> prep
        record = e.complete(
            parent, record["run_id"],
            output=[{"outline": "a"}, {"outline": "b"}],
        )  # prep -> chapters, first instance
        first_id = record["subflow_state"]["instances"][0]["run_id"]
        record = _run_child_success(
            e, self.storage.find_run(first_id), "S1"
        )
        second = next(
            i for i in record["subflow_state"]["instances"]
            if i["key"] == "1"
        )
        record = _run_child_success(
            e, self.storage.find_run(second["run_id"]), "S2"
        )
        # parent is parked at the end node, still running

        # change the feeder task -> foreach node becomes stale
        record = e.provide(
            parent, record["run_id"],
            values={"topic": "新主题"},
            node_id="prep",
        )
        self.assertIn(
            "chapters", record["revision"]["stale_nodes"]
        )
        # redo prep with a fresh list -> foreach node re-entered wholesale
        record = e.complete(
            parent, record["run_id"],
            output=[{"outline": "x"}, {"outline": "y"}],
        )
        state = record["subflow_state"]
        self.assertEqual(
            [i["status"] for i in state["instances"]],
            ["running", "pending"],
        )
        self.assertNotEqual(
            state["instances"][0]["run_id"], first_id
        )

    # ---- record layout at rest ----------------------------------------
    def test_journal_is_removed_not_truncated_once_subsumed(self):
        """A resting run keeps only `record.json`: the snapshot deletes the
        delta log it subsumes instead of truncating it to a zero-byte file.
        Readers already treat a missing log as empty (`_iter_journal`)."""
        wf = linear_workflow()
        record = self.engine.start(wf, {"topic": "T"})
        record = drive_to_completion(self.engine, wf, record)
        self.assertEqual(record["status"], "completed")
        run_dir = self.storage.run_dir_for_record(record)
        self.assertTrue(os.path.isfile(os.path.join(run_dir, "record.json")))
        self.assertFalse(
            os.path.exists(os.path.join(run_dir, "record.log.jsonl"))
        )

    def test_inline_subflow_instance_has_no_duplicate_snapshot(self):
        """An inline subflow's definition is the parent's
        children/<node>/subflow.json -- itself synthesized from the parent's
        snapshot. Instances must not each copy it; the resolver falls back to
        that file through the parent link. Uses a 1.1 parent: only 1.1 runs
        snapshot their definition at all."""
        parent = batch_11_parent_workflow(inline=True)
        self.storage.save_workflow(parent)
        record = self.engine.start(parent)
        record = self.engine.complete(parent, record["run_id"])
        state = record["subflow_state"]
        self.assertTrue(state["inline"])
        child = self.storage.find_run(state["instances"][0]["run_id"])
        child_dir = self.storage.run_dir_for_record(child)
        self.assertFalse(
            os.path.isfile(os.path.join(child_dir, "workflow.json"))
        )
        resolved = self.storage.load_workflow_for_record(child)
        self.assertEqual(resolved["id"], "batch-11-wf.batch")

    def test_external_subflow_instance_keeps_its_snapshot(self):
        """An external subflow definition lives outside the run and may
        change, so its instances still carry their own snapshot."""
        self.storage.save_workflow(entry_11_child_workflow())
        parent = batch_11_parent_workflow(inline=False)
        self.storage.save_workflow(parent)
        record = self.engine.start(parent)
        record = self.engine.complete(parent, record["run_id"])
        child = self.storage.find_run(
            record["subflow_state"]["instances"][0]["run_id"]
        )
        child_dir = self.storage.run_dir_for_record(child)
        self.assertTrue(
            os.path.isfile(os.path.join(child_dir, "workflow.json"))
        )


# ---- subflow / foreach module-level fixtures ------------------------------
def chapter_child_workflow():
    return {
        "schema_version": "1.0",
        "id": "chapter-wf",
        "name": "章节子流程",
        "input_schema": {
            "type": "object",
            "required": ["outline"],
            "properties": {"outline": {"type": "string"}},
        },
        "exports": {"summary": {"type": "string"}},
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "write", "type": "task", "output_key": "summary",
             "prompt": "扩写：${outline}"},
            {"id": "end", "type": "end"},
        ],
    }


def subflow_parent_workflow():
    return {
        "schema_version": "1.0",
        "id": "book-wf",
        "name": "分卷",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "call", "type": "subflow",
             "workflow": "chapter-wf",
             "inputs": {"outline": "主线：革命"},
             "output_key": "chapter"},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"from": "start", "to": "call"},
            {"from": "call", "to": "end"},
        ],
    }


def foreach_parent_workflow():
    wf = subflow_parent_workflow()
    wf["nodes"][1] = {
        "id": "chapters", "type": "foreach",
        "workflow": "chapter-wf",
        "items": [{"outline": "第一章"}, {"outline": "第二章"}],
        "inputs": {"outline": "${item.outline}"},
        "outputs_key": "results",
    }
    wf["edges"] = [
        {"from": "start", "to": "chapters"},
        {"from": "chapters", "to": "end"},
    ]
    return wf


def foreach_inline_parent_workflow():
    return {
        "schema_version": "1.0",
        "id": "inline-book-wf",
        "name": "内联循环",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "batch", "type": "foreach",
             "items": ["甲", "乙"],
             "inputs": {"topic": "${item}"},
             "nodes": [
                 {"id": "write", "type": "task", "output_key": "summary",
                  "prompt": "写：${topic}"}
             ],
             "exports": ["summary"],
             "outputs_key": "results"},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"from": "start", "to": "batch"},
            {"from": "batch", "to": "end"},
        ],
    }


def entry_11_child_workflow():
    """A 1.1 external subflow target. 1.1 is what makes a run snapshot its
    definition at all, so this is the fixture that exercises the snapshot
    rules."""
    return {
        "schema_version": "1.1",
        "id": "entry-11",
        "name": "1.1 子流程",
        "input_schema": {"type": "object", "properties": {"topic": {}}},
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "write", "type": "task", "output_key": "summary",
             "prompt": "写：${topic}"},
            {"id": "end", "type": "end"},
        ],
    }


def batch_11_parent_workflow(inline: bool):
    """A 1.1 foreach parent. `inline=True` synthesizes the child body into
    children/batch/subflow.json; `False` points at entry_11_child_workflow()."""
    node = {
        "id": "batch", "type": "foreach",
        "items": [{"topic": "甲"}, {"topic": "乙"}],
        "inputs": {"topic": "${item.topic}"},
        "outputs_key": "results",
    }
    if inline:
        node["nodes"] = [
            {"id": "write", "type": "task", "output_key": "summary",
             "prompt": "写：${topic}"}
        ]
        node["exports"] = ["summary"]
    else:
        node["workflow"] = "entry-11"
    return {
        "schema_version": "1.1",
        "id": "batch-11-wf",
        "name": "1.1 循环",
        "input_schema": {"type": "object", "properties": {}},
        "nodes": [
            {"id": "start", "type": "start"},
            node,
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"from": "start", "to": "batch"},
            {"from": "batch", "to": "end"},
        ],
    }


def chain_wf(i: int):
    """c0 -> c1 -> ... -> c9 (c9 is plain)."""
    nodes = [{"id": "start", "type": "start"}]
    edges = []
    if i < 9:
        nodes.append({
            "id": "call", "type": "subflow",
            "workflow": f"c{i + 1}",
            "inputs": {}, "output_key": "r",
        })
        edges += [
            {"from": "start", "to": "call"},
            {"from": "call", "to": "end"},
        ]
    else:
        edges.append({"from": "start", "to": "end"})
    nodes.append({"id": "end", "type": "end"})
    return {
        "schema_version": "1.0",
        "id": f"c{i}",
        "name": f"chain {i}",
        "nodes": nodes,
        "edges": edges,
    }


def foreach_feeder_workflow():
    """start -> prep(task producing the items list) -> chapters(foreach) -> end."""
    wf = foreach_parent_workflow()
    wf["nodes"][1]["items"] = {"var": "prep"}
    wf["nodes"] = [
        wf["nodes"][0],
        {"id": "prep", "type": "task", "prompt": "规划：${topic}"},
        wf["nodes"][1],
        wf["nodes"][2],
    ]
    wf["edges"] = [
        {"from": "start", "to": "prep"},
        {"from": "prep", "to": "chapters"},
        {"from": "chapters", "to": "end"},
    ]
    wf["input_schema"] = {
        "type": "object",
        "required": ["topic"],
        "properties": {"topic": {"type": "string"}},
    }
    return wf


def _run_child_success(engine, child_record, summary_text="摘要"):
    """Drive a chapter-wf child (start -> write -> end); completing the
    child end propagates into the parent, which is returned."""
    child_wf = engine.storage.load_workflow_for_record(child_record)
    crid = child_record["run_id"]
    child_record = engine.complete(child_wf, crid)  # start -> write
    child_record = engine.complete(
        child_wf, crid, output=summary_text
    )  # write -> end
    return engine.complete(child_wf, crid)  # end -> parent


if __name__ == "__main__":
    unittest.main()
