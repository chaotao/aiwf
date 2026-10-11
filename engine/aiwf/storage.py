"""JSON-file storage layer. Layout (per project):

    <root>/
      config.json                                   # optional project settings
      workflows/<workflow_id>.json                  # workflow definitions
      runs/<workflow_id>/<run_id>/record.json       # top-level run record
      runs/<workflow_id>/<run_id>/context.md        # auto-generated sharing hub
      runs/<workflow_id>/<run_id>/record.log.jsonl  # delta journal (absent at rest)

Subflow / foreach children are nested inside their parent run folder:

      runs/<parent_wf>/<parent_run_id>/
        children/<node_id>/subflow.json             # inline foreach synthesized def
        children/<node_id>/<instance>/{record.json,context.md,record.log.jsonl}
        foreach-<node_id>-results.json              # foreach collection manifest

No database. Everything is plain JSON (plus the Markdown context view).

Run records use a snapshot + append-only journal scheme: `save_run` writes one
small delta line per call and only rewrites the full `record.json` snapshot
every SNAPSHOT_EVERY saves (or as soon as the run reaches a resting status);
the delta log is deleted once a snapshot subsumes it, so it is absent at rest.
`record.json` therefore stays the complete state at rest; while a run is in
flight it may trail the journal by up to SNAPSHOT_EVERY saves. Always read
records through `load_record`/`load_run`/`find_run`, which replay the journal.
"""
import copy
import json
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

MAX_CHAIN_DEPTH = 8

CONFIG_NAME = "config.json"
JOURNAL_NAME = "record.log.jsonl"
# A snapshot is the one remaining O(record) write, so it is amortised over many
# saves: total snapshot cost drops from O(N^2) to O(N^2 / SNAPSHOT_EVERY) while
# an in-flight record.json trails by at most SNAPSHOT_EVERY transitions (a run
# that stops is always snapshotted, so it is exact at rest).
SNAPSHOT_EVERY = 64
JOURNAL_MAX_BYTES = 1 << 18     # snapshot early when the log grows past this
JOURNAL_CACHE_SIZE = 8          # records kept in memory for delta computation

# Fields whose growth gets a dedicated delta op instead of a value comparison.
_DIFF_SPECIAL = ("context", "node_runs", "events", "subflow_state")
# Append-only histories. Entries are never mutated after being appended, so a
# retained copy may share them and identity detects a wholesale rewrite.
_HISTORY = ("node_runs", "events")


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _same(a: Any, b: Any) -> bool:
    return a is b or a == b


def make_delta(
    prev: Dict[str, Any], cur: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Ops turning the already-persisted ``prev`` into ``cur``, or None when
    nothing changed. Every op is sized to what actually changed and is safe to
    replay on top of an equal-or-newer state, so crash recovery and snapshot
    races cannot duplicate or corrupt data."""
    ops: Dict[str, Any] = {}

    changed = {}
    for key, value in cur.items():
        if key in _DIFF_SPECIAL:
            continue
        if key not in prev or not _same(prev[key], value):
            changed[key] = value
    for key in prev:
        if key not in cur:
            changed[key] = None
    if changed:
        ops["set"] = changed

    pctx = prev.get("context") or {}
    cctx = cur.get("context") or {}
    set_ctx = {
        k: v for k, v in cctx.items() if k not in pctx or not _same(pctx[k], v)
    }
    if set_ctx:
        ops["context_set"] = set_ctx
    if any(k not in cctx for k in pctx):
        ops["context_del"] = [k for k in pctx if k not in cctx]

    for name in _HISTORY:
        before = prev.get(name) or []
        after = cur.get(name) or []
        if len(after) < len(before) or any(
            before[i] is not after[i] for i in range(len(before))
        ):
            ops[name] = after              # rewritten history: send it whole
        elif len(after) > len(before):
            ops[name + "_add"] = {"upto": len(after), "items": after[len(before):]}

    pstate = prev.get("subflow_state")
    cstate = cur.get("subflow_state")
    if cstate is None:
        if pstate is not None:
            ops["subflow"] = {"full": None}
    else:
        subflow = _subflow_delta(pstate, cstate)
        if subflow:
            ops["subflow"] = subflow
    return ops or None


def _subflow_delta(prev: Any, cur: Dict[str, Any]) -> Dict[str, Any]:
    """Delta for subflow_state, whose ``instances`` list only ever grows while
    the instances already in it are updated in place."""
    if not isinstance(prev, dict) or prev.get("node_id") != cur.get("node_id"):
        return {"full": cur}               # first save for this node
    out: Dict[str, Any] = {}
    patch = {
        k: v
        for k, v in cur.items()
        if k != "instances" and (k not in prev or not _same(prev[k], v))
    }
    if patch:
        out["patch"] = patch

    before = prev.get("instances") or []
    after = cur.get("instances") or []
    if len(after) < len(before):
        return {"full": cur}
    updates = {}
    for i, old in enumerate(before):
        new = after[i]
        if new != old:
            updates[str(i)] = {
                k: v for k, v in new.items() if k not in old or not _same(old[k], v)
            }
    if updates:
        out["inst_set"] = updates
    if len(after) > len(before):
        out["inst_add"] = {"upto": len(after), "items": after[len(before):]}
    return out


def apply_delta(
    record: Dict[str, Any], ops: Dict[str, Any], deep: bool = False
) -> None:
    """Apply journal ops to a record. ``deep`` copies the touched payloads so
    they stop being shared with the live record; that is what keeps an
    in-process copy usable as the diff base for the next save."""
    dupe = copy.deepcopy if deep else (lambda value: value)
    for key, value in (ops.get("set") or {}).items():
        record[key] = dupe(value)

    context = record.setdefault("context", {})
    for key, value in (ops.get("context_set") or {}).items():
        context[key] = dupe(value)
    for key in ops.get("context_del") or []:
        context.pop(key, None)

    for name in _HISTORY:
        if name in ops:
            record[name] = list(ops[name])
    for name in _HISTORY:
        # entries are shared on purpose, never copied: they are immutable once
        # appended, and the retained diff base detects a rewrite by identity.
        add = ops.get(name + "_add")
        if add and len(record.setdefault(name, [])) < add["upto"]:
            record[name].extend(add["items"])

    subflow = ops.get("subflow")
    if subflow:
        _apply_subflow(record, subflow, dupe)


def _apply_subflow(record: Dict[str, Any], op: Dict[str, Any], dupe) -> None:
    if "full" in op:
        record["subflow_state"] = dupe(op["full"])
        return
    state = record.get("subflow_state")
    if not isinstance(state, dict):
        return
    for key, value in (op.get("patch") or {}).items():
        state[key] = dupe(value)
    instances = state.setdefault("instances", [])
    add = op.get("inst_add")
    if add and len(instances) < add["upto"]:
        instances.extend(dupe(add["items"]))
    for index, fields in (op.get("inst_set") or {}).items():
        i = int(index)
        if 0 <= i < len(instances):
            for key, value in fields.items():
                instances[i][key] = dupe(value)


def _iter_journal(path: str) -> Iterator[Dict[str, Any]]:
    """Yield logged ops in order, skipping a torn final line left by a crash
    in the middle of an append."""
    try:
        handle = open(path, "r", encoding="utf-8")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                ops = json.loads(line)
            except ValueError:
                continue
            if isinstance(ops, dict):
                yield ops


def _clone_for_prev(record: Dict[str, Any]) -> Dict[str, Any]:
    """Copy shaped for delta computation: history entries stay shared (they are
    never mutated after being appended), anything mutated in place is copied."""
    clone = {}
    for key, value in record.items():
        if key in _HISTORY:
            clone[key] = list(value or [])
        elif key == "subflow_state":
            clone[key] = _clone_subflow(value)
        else:
            clone[key] = copy.deepcopy(value)
    return clone


def _clone_subflow(state: Any) -> Any:
    if not isinstance(state, dict):
        return copy.deepcopy(state)
    clone = {k: copy.deepcopy(v) for k, v in state.items() if k != "instances"}
    clone["instances"] = [
        copy.deepcopy(inst) for inst in state.get("instances") or []
    ]
    return clone


class Storage:
    def __init__(self, root: Optional[str] = None):
        self.root = root or os.getenv("AIWF_HOME", ".aiwf")
        self.workflows_dir = os.path.join(self.root, "workflows")
        self.runs_dir = os.path.join(self.root, "runs")
        # run_dir -> {"prev": last persisted state, "since"/"bytes": journal size
        # since the last snapshot}. Rebuilt from disk on the next load, so a
        # miss only costs one extra full snapshot.
        self._journal: Dict[str, Dict[str, Any]] = {}

    # ---- directories -------------------------------------------------
    def init_dirs(self) -> "Storage":
        os.makedirs(self.workflows_dir, exist_ok=True)
        os.makedirs(self.runs_dir, exist_ok=True)
        return self

    # ---- project settings ------------------------------------------------
    @property
    def config_path(self) -> str:
        return os.path.join(self.root, CONFIG_NAME)

    def load_config(self) -> Dict[str, Any]:
        """Project settings from ``<root>/config.json``; ``{}`` when absent."""
        text = self._read_text(self.config_path)
        if not text:
            return {}
        try:
            config = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"invalid {CONFIG_NAME}: {exc}") from exc
        if not isinstance(config, dict):
            raise ValueError(f"{CONFIG_NAME} must contain a JSON object")
        return config

    def run_defaults(self) -> Dict[str, Any]:
        """``run_defaults`` from the project config: per-project values for
        workflow inputs, so values a template requires (project dir, knowledge
        base path, test command) live with the project instead of being
        retyped on every ``aiwf run -p``."""
        defaults = self.load_config().get("run_defaults") or {}
        if not isinstance(defaults, dict):
            raise ValueError(f"{CONFIG_NAME} run_defaults must be a JSON object")
        return defaults

    @staticmethod
    def _atomic_write_text(path: str, content: str) -> None:
        """Write via a temp file + rename so a crash mid-write can never
        truncate an existing record/context file."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)

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
        self._atomic_write_text(path, _dump_json(workflow))
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
    def journal_path(self, run_dir: str) -> str:
        return os.path.join(run_dir, JOURNAL_NAME)

    def save_run(self, record: Dict[str, Any]) -> str:
        """Persist a run record. Appends only what changed since the last save
        and rewrites the whole snapshot every SNAPSHOT_EVERY saves (or as soon
        as the run stops running), so one transition costs one small append
        instead of a full rewrite of an ever-growing record."""
        run_dir = self.run_dir_for_record(record)
        os.makedirs(run_dir, exist_ok=True)
        path = os.path.join(run_dir, "record.json")
        state = self._journal.get(run_dir)
        if state is None:
            return self._snapshot_run(run_dir, record)
        ops = make_delta(state["prev"], record)
        if ops is None:
            return path
        self._append_journal(run_dir, ops)
        state["since"] += 1
        state["bytes"] += len(json.dumps(ops, ensure_ascii=False)) + 1
        apply_delta(state["prev"], ops, deep=True)
        if (
            record.get("status") != "running"
            or state["since"] >= SNAPSHOT_EVERY
            or state["bytes"] >= JOURNAL_MAX_BYTES
        ):
            self._snapshot_run(run_dir, record)
        return path

    def _snapshot_run(self, run_dir: str, record: Dict[str, Any]) -> str:
        """Write the full record, then drop the log it subsumes. Order matters:
        a crash in between leaves log entries that replay to the same state,
        whereas truncating first would lose them. The log is *removed* rather
        than emptied, so a resting run leaves no zero-byte file behind."""
        path = os.path.join(run_dir, "record.json")
        self._atomic_write_text(path, _dump_json(record))
        self._discard_journal(run_dir)
        self._remember(run_dir, record)
        return path

    @staticmethod
    def _discard_journal(run_dir: str) -> None:
        """Delete the delta log a snapshot subsumes. A missing log is already
        treated as empty by ``_iter_journal`` and ``_repair_journal_tail``, so
        a run that resumes simply appends a fresh one."""
        try:
            os.remove(os.path.join(run_dir, JOURNAL_NAME))
        except OSError:
            pass

    def _remember(self, run_dir: str, record: Dict[str, Any]) -> None:
        self._journal.pop(run_dir, None)
        self._journal[run_dir] = {
            "prev": _clone_for_prev(record), "since": 0, "bytes": 0,
        }
        while len(self._journal) > JOURNAL_CACHE_SIZE:
            self._journal.pop(next(iter(self._journal)))

    def _append_journal(self, run_dir: str, ops: Dict[str, Any]) -> None:
        path = self.journal_path(run_dir)
        self._repair_journal_tail(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ops, ensure_ascii=False) + "\n")

    @staticmethod
    def _repair_journal_tail(path: str) -> None:
        """Drop a torn final line from a crash mid-append, so the next entry
        cannot be glued onto it into one unparseable line."""
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size == 0:
                    return
                f.seek(size - 1)
                if f.read(1) == b"\n":
                    return
        except OSError:
            return
        with open(path, "rb") as f:
            data = f.read()
        cut = data.rfind(b"\n")
        with open(path, "wb") as f:
            f.write(data[: cut + 1] if cut >= 0 else b"")

    def load_record(self, path: str, cache: bool = False) -> Dict[str, Any]:
        """Read a run record: its snapshot plus every delta logged since.
        ``cache`` keeps a copy for delta computation and is only worth it when
        this record is about to be saved."""
        with open(path, "r", encoding="utf-8") as f:
            record = json.load(f)
        run_dir = os.path.dirname(path)
        for ops in _iter_journal(self.journal_path(run_dir)):
            apply_delta(record, ops)
        if cache:
            self._remember(run_dir, record)
        return record

    def save_context_doc(self, record: Dict[str, Any], content: str) -> str:
        d = self.run_dir_for_record(record)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "context.md")
        if self._read_text(path) == content:
            return path
        self._atomic_write_text(path, content)
        return path

    @staticmethod
    def _read_text(path: str) -> Optional[str]:
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def load_run(self, workflow_id: str, run_id: str) -> Dict[str, Any]:
        path = os.path.join(
            self.runs_dir, workflow_id, run_id, "record.json"
        )
        if os.path.exists(path):
            return self.load_record(path, cache=True)
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
        parent_run_id_or_None) for every stored run. Only identity metadata is
        read, none of which the journal ever touches, so the snapshot alone is
        enough and no replay is needed here."""
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
        for _wf, rid, path, _parent in self.iter_run_files():
            if rid == run_id:
                return self.load_record(path, cache=True)
        raise FileNotFoundError(f"no run with id {run_id!r}")

    # ---- workflow definition resolution for a record -----------------
    def load_workflow_for_record(
        self, record: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Resolve the definition a record runs against. External defs come
        from workflows/; a synthesized inline def lives next to the parent
        node (children/<node>/subflow.json)."""
        workflow_id = record["workflow_id"]
        snapshot = os.path.join(self.run_dir_for_record(record), "workflow.json")
        if os.path.exists(snapshot):
            return self.read_json_file(snapshot)
        if self.workflow_exists(workflow_id):
            workflow = self.load_workflow(workflow_id)
            if workflow["schema_version"] != record.get("workflow_version", "1.0"):
                raise ValueError(
                    "workflow version changed for an existing run; restore its "
                    "original definition or start a new run"
                )
            return workflow
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
        self._atomic_write_text(path, _dump_json(value))
        return path
