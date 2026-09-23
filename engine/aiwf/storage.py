"""JSON-file storage layer. Layout (per project):

    <root>/
      workflows/<workflow_id>.json              # workflow definitions
      runs/<workflow_id>/<run_id>/record.json   # audit record, one folder per run
      runs/<workflow_id>/<run_id>/context.md    # auto-generated sharing hub

No database. Everything is plain JSON (plus the Markdown context view).
"""
import json
import os
from typing import Any, Dict, List, Optional


class Storage:
    def __init__(self, root: Optional[str] = None):
        self.root = root or os.getenv("AIWF_HOME", ".aiwf")
        self.workflows_dir = os.path.join(self.root, "workflows")
        self.runs_dir = os.path.join(self.root, "runs")

    # ---- directories -------------------------------------------------
    def init_dirs(self) -> "Storage":
        os.makedirs(self.workflows_dir, exist_ok=True)
        os.makedirs(self.runs_dir, exist_ok=True)
        return self

    # ---- workflow definitions ---------------------------------------
    def workflow_path(self, workflow_id: str) -> str:
        return os.path.join(self.workflows_dir, f"{workflow_id}.json")

    def workflow_exists(self, workflow_id: str) -> bool:
        return os.path.exists(self.workflow_path(workflow_id))

    def load_workflow(self, workflow_id: str) -> Dict[str, Any]:
        path = self.workflow_path(workflow_id)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"workflow {workflow_id!r} not found at {path}; "
                f"run `aiwf init` and create it first"
            )
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_workflow(self, workflow: Dict[str, Any]) -> str:
        os.makedirs(self.workflows_dir, exist_ok=True)
        path = self.workflow_path(workflow["id"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(workflow, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return path

    def list_workflows(self) -> List[str]:
        if not os.path.isdir(self.workflows_dir):
            return []
        return sorted(
            fn[:-5]
            for fn in os.listdir(self.workflows_dir)
            if fn.endswith(".json")
        )

    # ---- run records -------------------------------------------------
    def run_dir(self, workflow_id: str, run_id: str) -> str:
        return os.path.join(self.runs_dir, workflow_id, run_id)

    def run_path(self, workflow_id: str, run_id: str) -> str:
        return os.path.join(self.run_dir(workflow_id, run_id), "record.json")

    def context_doc_path(self, workflow_id: str, run_id: str) -> str:
        return os.path.join(self.run_dir(workflow_id, run_id), "context.md")

    def save_run(self, record: Dict[str, Any]) -> str:
        d = self.run_dir(record["workflow_id"], record["run_id"])
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "record.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return path

    def save_context_doc(
        self, workflow_id: str, run_id: str, content: str
    ) -> str:
        d = self.run_dir(workflow_id, run_id)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "context.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def load_run(self, workflow_id: str, run_id: str) -> Dict[str, Any]:
        path = self.run_path(workflow_id, run_id)
        if not os.path.exists(path):
            raise FileNotFoundError(f"run record not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def iter_run_files(self, workflow_id: Optional[str] = None):
        """Yield (workflow_id, run_id, record_path) for all stored runs."""
        if not os.path.isdir(self.runs_dir):
            return
        wf_dirs = (
            [workflow_id]
            if workflow_id
            else sorted(
                d
                for d in os.listdir(self.runs_dir)
                if os.path.isdir(os.path.join(self.runs_dir, d))
            )
        )
        for wf in wf_dirs:
            wd = os.path.join(self.runs_dir, wf)
            if not os.path.isdir(wd):
                continue
            for run_id in sorted(os.listdir(wd)):
                rp = os.path.join(wd, run_id, "record.json")
                if os.path.isfile(rp):
                    yield wf, run_id, rp

    def find_run(self, run_id: str) -> Dict[str, Any]:
        """Load a run record by run_id across every workflow."""
        for wf, rid, path in self.iter_run_files():
            if rid == run_id:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        raise FileNotFoundError(f"no run with id {run_id!r}")
