"""Template library: copy-based install, dependency closure, provenance.

The library ships with the repo and is resolved like bundled skills
(``AIWF_LIBRARY_DIR`` then ``<repo>/library/workflows``). Install copies a
template into the project, so these tests pin the two behaviours that choice
makes critical: the dependency closure must come along, and a copy the project
has edited must never be overwritten without ``--force``.

Every file is read and written as UTF-8, matching the engine (``storage.py``
opens all definitions, records and ledgers with ``encoding="utf-8"``). Do not
drop that argument: on a CP936 host the default locale codec would decode these
UTF-8 files as mojibake and this suite would fail for the wrong reason.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aiwf import Storage, library
from aiwf.schema import validate_workflow


ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "library" / "workflows"
META = ROOT / "library" / "meta"
INDEX = ROOT / "library" / "INDEX.md"
CLI = [sys.executable, "-m", "aiwf"]
BASE_ENV = dict(
    os.environ,
    PYTHONPATH=str(ROOT / "engine"),
    AIWF_LIBRARY_DIR=str(LIBRARY),
)
META_KEYS = {"summary", "maturity", "scenarios", "not_for", "deps"}


class TemplateLibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.home = self.project / ".aiwf"
        self.storage = Storage(str(self.home)).init_dirs()

    # ---- helpers ---------------------------------------------------------
    def cli(self, *args, library_dir=None):
        env = dict(BASE_ENV, AIWF_HOME=str(self.home))
        if library_dir is not None:
            env["AIWF_LIBRARY_DIR"] = str(library_dir)
        return subprocess.run(
            [*CLI, *args], cwd=self.project, env=env,
            text=True, encoding="utf-8", capture_output=True,
        )

    def local(self, workflow_id):
        return self.home / "workflows" / f"{workflow_id}.json"

    def copy_library(self):
        """A throwaway library so a test can mutate upstream safely."""
        dest = self.base / "lib" / "workflows"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(LIBRARY, dest)
        shutil.copytree(META, dest.parent / "meta")
        return dest

    # ---- library contents ------------------------------------------------
    def test_definitions_and_metadata_are_consistent(self):
        entries = library.load_library()
        self.assertTrue(entries, "library is empty")
        for entry_id, entry in entries.items():
            with self.subTest(entry=entry_id):
                validate_workflow(entry["definition"])
                self.assertTrue(
                    entry_id.startswith(library.PREFIX),
                    f"{entry_id} breaks the {library.PREFIX!r} naming convention",
                )
                meta = entry["meta"]
                self.assertTrue(meta, f"{entry_id} has no meta/{entry_id}.json")
                self.assertLessEqual(META_KEYS, set(meta))
                # Documented deps must match what the definition actually calls,
                # otherwise `wf show` would advertise a dependency that is not
                # installed - or hide one that is.
                documented = set(meta["deps"])
                actual = set(library.workflow_refs(entry["definition"]))
                self.assertEqual(documented, actual)

    # ---- install ---------------------------------------------------------
    def test_install_pulls_dependency_closure_and_project_validates(self):
        result = self.cli("wf", "install", "std.review-loop")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.local("std.review-loop").is_file())
        self.assertTrue(self.local("std.review-check").is_file())
        self.assertIn("dependency", result.stdout)

        ledger = json.loads(
            (self.home / "installed.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(ledger), {"std.review-loop", "std.review-check"}
        )
        self.assertEqual(
            ledger["std.review-check"]["source_id"], "std.review-check"
        )

        validated = self.cli("validate", "--project")
        self.assertEqual(
            validated.returncode, 0, validated.stdout + validated.stderr
        )

    def test_no_deps_installs_only_the_template(self):
        result = self.cli("wf", "install", "std.review-loop", "--no-deps")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.local("std.review-loop").is_file())
        self.assertFalse(self.local("std.review-check").exists())
        # Without the dependency the project cannot even validate - the reason
        # install pulls the closure by default.
        self.assertNotEqual(self.cli("validate", "--project").returncode, 0)

    def test_local_edit_is_never_overwritten_until_forced(self):
        self.assertEqual(self.cli("wf", "install", "std.review-loop").returncode, 0)
        path = self.local("std.review-loop")
        edited = json.loads(path.read_text(encoding="utf-8"))
        edited["nodes"][1]["check"]["max_attempts"] = 5
        path.write_text(
            json.dumps(edited, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        status = self.cli("wf", "status")
        self.assertIn("edited", status.stdout)

        refused = self.cli("wf", "install", "std.review-loop")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("refusing to overwrite", refused.stderr)
        self.assertIn("--force", refused.stderr)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))
            ["nodes"][1]["check"]["max_attempts"],
            5,
        )

        forced = self.cli("wf", "install", "std.review-loop", "--force")
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))
            ["nodes"][1]["check"]["max_attempts"],
            2,
        )

    def test_project_owned_file_is_not_clobbered(self):
        self.local("std.review-check").write_text(
            json.dumps({
                "schema_version": "1.1", "id": "std.review-check",
                "name": "project's own file", "nodes": [
                    {"id": "start", "type": "start"}, {"id": "end", "type": "end"},
                ],
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result = self.cli("wf", "install", "std.review-loop")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not installed by aiwf", result.stderr)
        self.assertIn(
            "project's own file",
            self.local("std.review-check").read_text(encoding="utf-8"),
        )

    def test_install_as_rewrites_the_definition_id(self):
        result = self.cli("wf", "install", "std.review-check", "--as", "my-check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        installed = json.loads(
            self.local("my-check").read_text(encoding="utf-8")
        )
        self.assertEqual(installed["id"], "my-check")
        self.assertFalse(self.local("std.review-check").exists())
        self.assertIn(
            "my-check",
            json.loads(
                (self.home / "installed.json").read_text(encoding="utf-8")
            ),
        )

    def test_missing_dependency_is_reported_not_skipped(self):
        upstream = self.copy_library()
        (upstream / "ghost-caller.json").write_text(
            json.dumps({
                "schema_version": "1.1", "id": "ghost-caller", "name": "ghost caller",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "call", "type": "subflow", "workflow": "std.ghost"},
                    {"id": "end", "type": "end"},
                ],
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result = self.cli("wf", "install", "ghost-caller", library_dir=upstream)
        self.assertEqual(result.returncode, 2)
        self.assertIn("std.ghost", result.stderr)
        self.assertIn(str(upstream), result.stderr)

    def test_unknown_template_reports_the_searched_library(self):
        result = self.cli("wf", "install", "std.nope")
        self.assertEqual(result.returncode, 2)
        self.assertIn("std.nope", result.stderr)
        self.assertIn(str(LIBRARY), result.stderr)

    # ---- provenance ------------------------------------------------------
    def test_status_and_diff_track_local_and_upstream_changes(self):
        upstream = self.copy_library()
        self.assertEqual(
            self.cli("wf", "install", "std.review-loop",
                     library_dir=upstream).returncode, 0
        )

        clean = self.cli("wf", "status", library_dir=upstream)
        self.assertIn("clean", clean.stdout)
        self.assertIn("current", clean.stdout)
        self.assertIn("identical", self.cli(
            "wf", "diff", "std.review-loop", library_dir=upstream).stdout)

        target = upstream / "std.review-loop.json"
        moved = json.loads(target.read_text(encoding="utf-8"))
        moved["nodes"][1]["check"]["max_attempts"] = 7
        target.write_text(
            json.dumps(moved, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        updated = self.cli("wf", "status", library_dir=upstream)
        self.assertIn("updated", updated.stdout)

        diff = self.cli("wf", "diff", "std.review-loop", library_dir=upstream)
        self.assertEqual(diff.returncode, 0)
        self.assertIn("max_attempts", diff.stdout)

    # ---- catalogue -------------------------------------------------------
    def test_generated_index_matches_the_committed_catalogue(self):
        entries = library.load_library()
        rendered = library.render_index()
        self.assertEqual(
            INDEX.read_text(encoding="utf-8"), rendered,
            "library/INDEX.md is stale; run `aiwf wf index`",
        )
        for entry_id in entries:
            self.assertIn(f"`{entry_id}`", rendered)


if __name__ == "__main__":
    unittest.main()
