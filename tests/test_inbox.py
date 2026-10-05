"""Inbox: parking a thought/problem without touching run state.

Two properties matter more than the rest and are pinned hardest here:

* an item cannot be parked **without a reason** (that is the whole point of the
  mechanic - it forces the answer to "why can this wait?"), and
* archiving is **one step** - status, history and ledger move together, so the
  ledger can never keep a resolved item.

The inbox is deliberately independent of the run state machine, so nothing in
this suite touches ``runs/``.

Every file is read and written as UTF-8, matching the engine. Do not drop that
argument: on a CP936 host the default locale codec would mangle these files and
the suite would fail for the wrong reason.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from aiwf import Storage, inbox


ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "aiwf"]
# The CLI emits UTF-8 and takes Chinese argv (--why 用户明确不挡, --type 问题).
# Both directions must be pinned: JSON mode escapes nothing (ensure_ascii=False)
# but a plain `print` of a Chinese label would fail on a CP936 child, and argv
# crosses the locale codec too. Pinning the child's encoding keeps this suite
# deterministic on any host locale - see the warning in test_wf_library.py.
BASE_ENV = dict(
    os.environ,
    PYTHONPATH=str(ROOT / "engine"),
    PYTHONUTF8="1",
    PYTHONIOENCODING="utf-8",
)


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / ".aiwf"
        self.storage = Storage(str(self.home)).init_dirs()
        self.root = self.storage.inbox_dir

    # ---- helpers ---------------------------------------------------------
    def cli(self, *args):
        env = dict(BASE_ENV, AIWF_HOME=str(self.home))
        return subprocess.run(
            [*CLI, *args], cwd=self.tmp.name, env=env,
            encoding="utf-8", errors="replace", capture_output=True,
        )

    def ledger(self):
        return (self.home / "inbox" / "inbox.md").read_text(encoding="utf-8")

    def history(self):
        return (self.home / "inbox" / "history.md").read_text(encoding="utf-8")

    def add(self, summary, **kwargs):
        kwargs.setdefault("reason", "用户明确不挡")
        return inbox.add(self.root, summary, **kwargs)

    # ---- recording -------------------------------------------------------
    def test_add_writes_ledger_and_index(self):
        entry = self.add("四种砖是否需要材料玩法差异", type_="想法", source="对话")
        self.assertEqual(entry["id"], "INB-001")
        self.assertEqual(entry["status"], inbox.OPEN)
        self.assertEqual(entry["reason"], "用户明确不挡")
        self.assertEqual(entry["date"], date.today().isoformat())
        text = self.ledger()
        self.assertIn("### INB-001", text)
        self.assertIn("- INB-001：四种砖是否需要材料玩法差异", text)
        self.assertIn("- 来源：对话", text)
        # the ledger is the only file an open item needs
        self.assertFalse((self.home / "inbox" / "history.md").exists())

    def test_ids_increase_and_survive_archiving(self):
        self.add("第一条")
        self.add("第二条")
        inbox.resolve(self.root, "INB-001", target="交给 design")
        third = self.add("第三条")
        self.assertEqual(third["id"], "INB-003")

    def test_reason_is_required(self):
        with self.assertRaises(ValueError) as caught:
            inbox.add(self.root, "想记一下", reason="")
        self.assertIn("为何停放", str(caught.exception))
        # nothing may be written when the reason is missing
        self.assertFalse((self.home / "inbox" / "inbox.md").exists())

    def test_reason_must_be_one_of_the_four_values(self):
        with self.assertRaises(ValueError):
            inbox.add(self.root, "想记一下", reason="反正没用")
        self.assertFalse((self.home / "inbox" / "inbox.md").exists())

    def test_reason_may_combine_values(self):
        entry = self.add("两条例由", reason="非本节点 / 用户明确不挡")
        self.assertEqual(entry["reason"], "非本节点/用户明确不挡")

    def test_type_must_be_known(self):
        with self.assertRaises(ValueError):
            self.add("想记一下", type_="随便")

    def test_summary_is_capped(self):
        with self.assertRaises(ValueError) as caught:
            self.add("字" * (inbox.SUMMARY_LIMIT + 1))
        self.assertIn("notes/INB-NNN.md", str(caught.exception))

    def test_note_line_round_trips(self):
        entry = self.add("主题", note="第二行补充")
        self.assertIn("\n", entry["summary"])
        reloaded = inbox.find(self.root, "INB-001")
        self.assertEqual(reloaded["summary"], "主题\n第二行补充")
        # the index row stays a single scannable line
        self.assertIn("- INB-001：主题\n", self.ledger())

    # ---- merging (one cohesive item per topic) ---------------------------
    def test_merge_into_absorbs_without_new_id(self):
        first = self.add("四种砖是否需要材料玩法差异")
        merged = self.add("补充：材质影响音效", merge_into=first["id"])
        self.assertEqual(merged["id"], "INB-001")
        self.assertEqual(len(inbox.load(self.root)["open"]), 1)
        self.assertIn("补充：材质影响音效", merged["summary"])
        self.assertTrue(merged["absorbed"].startswith("并入："))
        self.assertEqual(inbox.next_id(inbox.load(self.root)), "INB-002")

    def test_merge_into_missing_item_is_refused(self):
        self.add("第一条")
        with self.assertRaises(ValueError) as caught:
            self.add("补充", merge_into="INB-042")
        self.assertIn("INB-042", str(caught.exception))

    # ---- archiving -------------------------------------------------------
    def test_resolve_moves_item_to_history_in_one_step(self):
        self.add("待交接")
        archived = inbox.resolve(self.root, "INB-001", target="纳入 next-round")
        self.assertEqual(archived["status"], "已交接")
        self.assertEqual(archived["target"], "纳入 next-round")
        self.assertNotIn("reason", archived)      # a park reason dies with it
        data = inbox.load(self.root)
        self.assertEqual(data["open"], [])
        self.assertEqual([e["id"] for e in data["archived"]], ["INB-001"])
        ledger = self.ledger()
        self.assertNotIn("### INB-001", ledger)
        self.assertIn("（无）", ledger)
        self.assertIn("### INB-001", self.history())
        self.assertIn("- 去向：纳入 next-round", self.history())

    def test_resolve_requires_a_destination(self):
        self.add("待交接")
        with self.assertRaises(ValueError):
            inbox.resolve(self.root, "INB-001")
        self.assertIn("### INB-001", self.ledger())   # still open

    def test_resolve_refuses_completion_wording(self):
        self.add("待交接")
        with self.assertRaises(ValueError):
            inbox.resolve(self.root, "INB-001", target="已完成")

    def test_resolve_records_split_targets_without_writing_them(self):
        self.add("混了两个方向")
        archived = inbox.resolve(
            self.root, "INB-001", target="留给下一轮", split_into=["INB-007", "INB-008"]
        )
        self.assertIn("拆分为 INB-007、INB-008", archived["target"])
        self.assertEqual(inbox.load(self.root)["open"], [])
        # the split targets are only *recorded*; they are not reserved, so the
        # next real item takes the next free number, not INB-007
        self.assertEqual(inbox.next_id(inbox.load(self.root)), "INB-002")

    def test_resolve_unknown_and_already_archived(self):
        with self.assertRaises(ValueError):
            inbox.resolve(self.root, "INB-001", target="x")
        self.add("待交接")
        inbox.resolve(self.root, "INB-001", target="x")
        with self.assertRaises(ValueError) as caught:
            inbox.resolve(self.root, "INB-001", target="y")
        self.assertIn("already archived", str(caught.exception))

    def test_split_from_and_related_are_recorded(self):
        entry = self.add("拆出来的一条", split_from="INB-001", related="INB-002")
        self.assertEqual(entry["split_from"], "INB-001")
        self.assertEqual(entry["related"], "INB-002")

    # ---- verification ----------------------------------------------------
    def test_check_is_clean_for_normal_use(self):
        self.add("第一条")
        self.add("第二条")
        inbox.resolve(self.root, "INB-001", target="交给 design")
        self.assertEqual(inbox.check(self.root), [])

    def test_check_is_clean_before_any_use(self):
        self.assertEqual(inbox.check(self.root), [])

    def test_check_catches_index_drift(self):
        self.add("第一条")
        path = self.home / "inbox" / "inbox.md"
        path.write_text(
            self.ledger().replace("- INB-001：第一条\n", ""), encoding="utf-8"
        )
        problems = inbox.check(self.root)
        self.assertTrue(any("index out of sync" in p for p in problems), problems)

    def test_check_catches_missing_reason(self):
        self.add("第一条")
        path = self.home / "inbox" / "inbox.md"
        path.write_text(
            self.ledger().replace("- 为何停放：用户明确不挡\n", ""), encoding="utf-8"
        )
        problems = inbox.check(self.root)
        self.assertTrue(any("为何停放" in p for p in problems), problems)

    def test_check_catches_duplicate_ids(self):
        self.add("第一条")
        entry = self.ledger().split("### INB-001", 1)[1]
        path = self.home / "inbox" / "inbox.md"
        path.write_text(self.ledger() + "\n### INB-001" + entry, encoding="utf-8")
        problems = inbox.check(self.root)
        self.assertTrue(any("duplicate id" in p for p in problems), problems)

    def test_check_catches_history_without_ledger(self):
        self.add("第一条")
        inbox.resolve(self.root, "INB-001", target="x")
        (self.home / "inbox" / "inbox.md").unlink()
        problems = inbox.check(self.root)
        self.assertTrue(any(inbox.LEDGER_NAME in p for p in problems), problems)

    # ---- CLI surface -----------------------------------------------------
    def test_cli_add_list_resolve_roundtrip(self):
        added = self.cli("inbox", "add", "来自流程外的想法",
                         "--why", "非本节点", "--type", "技术债", "--json")
        self.assertEqual(added.returncode, 0, added.stderr)
        payload = json.loads(added.stdout)
        self.assertEqual(payload["id"], "INB-001")
        self.assertEqual(payload["reason"], "非本节点")

        listed = self.cli("inbox", "list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(len(json.loads(listed.stdout)["open"]), 1)

        resolved = self.cli("inbox", "resolve", "INB-001", "--to", "交给 design", "--json")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(json.loads(resolved.stdout)["target"], "交给 design")

        checked = self.cli("inbox", "check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("1 archived", checked.stdout)

    def test_cli_rejects_item_without_reason(self):
        result = self.cli("inbox", "add", "想记一下")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.home / "inbox" / "inbox.md").exists())

    def test_cli_check_reports_problems_with_rc_2(self):
        self.add("第一条")
        path = self.home / "inbox" / "inbox.md"
        path.write_text(
            self.ledger().replace("- 为何停放：用户明确不挡\n", ""), encoding="utf-8"
        )
        result = self.cli("inbox", "check")
        self.assertEqual(result.returncode, 2)
        self.assertIn("为何停放", result.stdout)

    def test_cli_run_node_source_is_recorded(self):
        result = self.cli("inbox", "add", "流程中冒出来的事", "--why", "非本节点",
                          "--run", "20261004-120000-ab12cd", "--node", "draft", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["source"], "20261004-120000-ab12cd / draft"
        )


if __name__ == "__main__":
    unittest.main()
