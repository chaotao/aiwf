"""JSON-file storage layer. Layout (per project):

    <root>/
      workflows/<workflow_id>.json                  # workflow definitions
      runs/<workflow_id>/<run_id>/record.json       # top-level run record
      runs/<workflow_id>/<run_id>/context.md        # auto-generated sharing hub

Subflow / foreach children are nested inside their parent run folder:

      runs/<parent_wf>/<parent_run_id>/
        children/<node_id>/subflow.json             # inline foreach synthesized def
        children/<node_id>/<instance>/{record.json,context.md}
        foreach-<node_id>-results.json              # foreach collection manifest

No database. Everything is plain JSON (plus the Markdown context view).
"""
import json
import os
from typing import Any, Dict, List, Optional, Tuple

MAX_CHAIN_DEPTH = 8


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

    # ---- run directories (parent-chain aware) ------------------------
    def run_dir_for_record(
        self, record: Dict[str, Any], _depth: int = 0
    ) -> str:
        """Directory holding this record's record.json: the normal
        runs/<wf>/<rid> for top-level runs, or a children/... folder for
        subflow instances (recursive through the parent chain)."""
        parent = record.get("parent")
        if not parent:
            return os.path.join(
                self.runs_dir, record["workflow_id"], record["run_id"]
            )
        if _depth >= MAX_CHAIN_DEPTH:
            raise RuntimeError("subflow parent chain exceeds depth limit")
        parent_record = self.find_run(parent["run_id"])
        base = self.run_dir_for_record(parent_record, _depth + 1)
        return os.path.join(
            base, "children", parent["node_id"], parent["instance"]
        )

    def child_dir(
        self, parent_record: Dict[str, Any], node_id: str, instance: str
    ) -> str:
        return os.path.join(
            self.run_dir_for_record(parent_record),
            "children", node_id, instance,
        )

    def inline_subflow_path(
        self, parent_record: Dict[str, Any], node_id: str
    ) -> str:
        return os.path.join(
            self.run_dir_for_record(parent_record),
            "children", node_id, "subflow.json",
        )

    def results_manifest_path(
        self, parent_record: Dict[str, Any], node_id: str
    ) -> str:
        return os.path.join(
            self.run_dir_for_record(parent_record),
            f"foreach-{node_id}-results.json",
        )

    # ---- run records -------------------------------------------------
    def save_run(self, record: Dict[str, Any]) -> str:
        d = self.run_dir_for_record(record)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "record.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return path

    def save_context_doc(self, record: Dict[str, Any], content: str) -> str:
        d = self.run_dir_for_record(record)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "context.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def load_run(self, workflow_id: str, run_id: str) -> Dict[str, Any]:
        path = os.path.join(
            self.runs_dir, workflow_id, run_id, "record.json"
        )
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        # nested child: locate by recursive scan
        record = self.find_run(run_id)
        if record["workflow_id"] != workflow_id:
            raise FileNotFoundError(
                f"run {run_id!r} belongs to workflow "
                f"{record['workflow_id']!r}, not {workflow_id!r}"
            )
        return record

    def iter_run_files(
        self, workflow_id: Optional[str] = None
    ):
        """Recursively yield (workflow_id, run_id, record_path,
        parent_run_id_or_None) for every stored run."""
        if not os.path.isdir(self.runs_dir):
            return
        for dirpath, _dirs, files in os.walk(self.runs_dir):
            if "record.json" not in files:
                continue
            path = os.path.join(dirpath, "record.json")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    record = json.load(f)
            except (OSError, ValueError):
                continue
            wf = record.get("workflow_id")
            rid = record.get("run_id")
            if not wf or not rid:
                continue
            if workflow_id and wf != workflow_id:
                continue
            parent = record.get("parent")
            yield wf, rid, path, parent["run_id"] if parent else None

    def find_run(self, run_id: str) -> Dict[str, Any]:
        """Load a run record by run_id across every workflow."""
        for wf, rid, path, _parent in self.iter_run_files():
            if rid == run_id:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        raise FileNotFoundError(f"no run with id {run_id!r}")

    # ---- workflow definition resolution for a record -----------------
    def load_workflow_for_record(
        self, record: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Resolve the definition a record runs against. External defs come
        from workflows/; a synthesized inline def lives next to the parent
        node (children/<node>/subflow.json)."""
        workflow_id = record["workflow_id"]
        if self.workflow_exists(workflow_id):
            return self.load_workflow(workflow_id)
        parent = record.get("parent")
        if parent:
            path = os.path.join(
                self.run_dir_for_record(record),
                "..", "subflow.json",
            )
            path = os.path.normpath(path)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        raise FileNotFoundError(
            f"workflow definition for run {record['run_id']!r} not found"
        )

    # ---- JSON helper for engine-managed protocol files ---------------
    def read_json_file(self, path: str) -> Any:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def write_json_file(self, path: str, value: Any) -> str:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return path
