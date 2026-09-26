"""Workflow state machine.

The engine never calls an LLM and never executes task logic. It is driven by
an external AI agent (and human reviewers) via this protocol:

    start()    validate, create a run record, enter the start node
    spec()     render the current node info:
                 executing      -> task/branch/start/end node spec
                 awaiting_input -> questions from a task node's user_input_schema
                 awaiting_review-> acceptance requirements for a human
    provide()  agent supplies user answers for a node's user_input_schema;
               changing an already-completed node's input auto-opens a revision
    complete() agent reports a task/branch output -> audit, context, routing
               (task output is checked against output_schema; a branch with
               `when` is evaluated deterministically by the engine)
    review()   human verdict APPROVE/REJECT on an acceptance gate
    revise()   out-of-plan jump back to a prior task node; the dependency graph
               (${var} refs plus explicit consumes, glob-aware) decides which
               downstream nodes are redone/reused
    fail()     agent reports a node failure -> run failed (propagates to parent)
    resume()   reopen a failed run / nudge a dynamic foreach
    redo()     reopen a single foreach instance (or one subflow call)

subflow node  invokes another workflow once; foreach node fans the same
workflow (external def or inline nodes) out over an items list. Every child
run is a normal run, nested physically inside the parent run folder.

Persistence is the JSON run record, rewritten after every transition.
"""
import fnmatch
import copy
import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from .schema import (
    _jsonschema_validate,
    node_user_input_schema,
    validate_inputs,
    validate_run_record,
    validate_workflow,
)
from .storage import Storage

SCHEMA_VERSION = "1.0"
SUMMARY_LIMIT = 160
# Hard cap on the serialized length of any single node output. Large artifacts
# (chapter text, scripts, ...) MUST be written to a file and reported as a file
# reference, so run records stay small and portable.
OUTPUT_LIMIT = 2000
# Marker key of a file reference object: {"$file": "path/relative/to/project"}.
FILE_REF_KEY = "$file"
# Maximum nesting depth of subflow chains.
MAX_SUBFLOW_DEPTH = 8
DEFAULT_MAX_INSTANCES = 1000
# Variable reference, optionally dotted: ${var}, ${item.number}, ${plan.chapter_count}.
TEMPLATE_RE = re.compile(
    r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*\}"
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def make_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + os.urandom(3).hex()


def is_file_ref(value: Any) -> bool:
    return isinstance(value, dict) and FILE_REF_KEY in value


def file_ref_path(value: Any) -> Optional[str]:
    return value[FILE_REF_KEY] if is_file_ref(value) else None


def file_ref_paths(value: Any) -> Set[str]:
    paths: Set[str] = set()
    if isinstance(value, dict):
        path = file_ref_path(value)
        if isinstance(path, str):
            paths.add(path)
        for child in value.values():
            paths.update(file_ref_paths(child))
    elif isinstance(value, list):
        for child in value:
            paths.update(file_ref_paths(child))
    return paths


def _stringify_context_value(value: Any) -> str:
    """Scalar -> text for template substitution. File refs render as their path
    (agents read the file themselves); other non-strings are JSON-encoded.
    """
    if isinstance(value, str):
        return value
    path = file_ref_path(value)
    if path is not None:
        return str(path)
    return json.dumps(value, ensure_ascii=False)


def validate_output_size(output: Any) -> None:
    """Reject oversized inline output so large content is externalized."""
    if output is None:
        return
    size = len(json.dumps(output, ensure_ascii=False))
    if size > OUTPUT_LIMIT:
        raise ValueError(
            f"output is {size} chars, exceeds the {OUTPUT_LIMIT}-char limit; "
            f"write the full content to a file and report a reference instead: "
            f'{{"{FILE_REF_KEY}": "path/to/file"}}'
        )


def resolve_dotted(context: Dict[str, Any], dotted: str) -> Any:
    """Resolve 'a.b.c' against context: dict keys, numeric list indices.

    The root key must exist; a missing root/intermediate segment is a hard
    error naming the unresolved path.
    """
    parts = dotted.split(".")
    root = parts[0]
    if root not in context:
        raise KeyError(
            f"template variable ${{{dotted}}} not found in workflow context"
        )
    cur = context[root]
    walked = root
    for seg in parts[1:]:
        walked += f".{seg}"
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            raise KeyError(
                f"template variable ${{{walked}}} not found in workflow context"
            )
    return cur


def render(value: Any, context: Dict[str, Any]) -> Any:
    """Substitute ${var}/${a.b} in strings; recurse into dict/list.

    Non-string context values are JSON-encoded (file refs render as paths);
    a missing variable is a hard error.
    """
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            return _stringify_context_value(resolve_dotted(context, m.group(1)))
        return TEMPLATE_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    return value


def resolve_data(value: Any, context: Dict[str, Any]) -> Any:
    """Typed references are data; templates remain text. Never dereference files."""
    if isinstance(value, dict):
        if set(value) == {"ref"}:
            return copy.deepcopy(resolve_dotted(context, value["ref"]))
        return {k: resolve_data(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_data(v, context) for v in value]
    return render(value, context)


def data_refs(value: Any) -> Set[str]:
    refs = template_refs(value)
    if isinstance(value, dict):
        if set(value) == {"ref"}:
            refs.add(value["ref"])
        else:
            for v in value.values():
                refs.update(data_refs(v))
    elif isinstance(value, list):
        for v in value:
            refs.update(data_refs(v))
    return refs


def template_refs(value: Any) -> Set[str]:
    """Collect every ${var} referenced inside strings of a JSON-like value."""
    refs: Set[str] = set()
    if isinstance(value, str):
        refs.update(TEMPLATE_RE.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            refs.update(template_refs(v))
    elif isinstance(value, list):
        for v in value:
            refs.update(template_refs(v))
    return refs


def make_summary(output: Any) -> str:
    if output is None:
        return ""
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SUMMARY_LIMIT]


# ---- deterministic condition language (branch `when`) --------------------
COMPARISON_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
}


def resolve_condition_value(value: Any, context: Dict[str, Any]) -> Any:
    """Literal, or {"var": "key"} read from context (optional default)."""
    if isinstance(value, dict) and set(value) == {"ref"}:
        return resolve_data(value, context)
    if isinstance(value, dict) and "var" in value:
        key = value["var"]
        try:
            return resolve_dotted(context, key)
        except KeyError:
            # default applies when the root key is absent; a broken inner path
            # is a definition error and must surface
            if "default" in value and key.split(".", 1)[0] not in context:
                return value["default"]
            raise
    return value


def eval_condition(cond: Dict[str, Any], context: Dict[str, Any]) -> bool:
    """Evaluate a one-operator condition tree against context.

    Operators: eq/ne/gt/gte/lt/lte (two operands), and/or (lists), not.
    This is plain deterministic evaluation, never an LLM call.
    """
    if not isinstance(cond, dict) or len(cond) != 1:
        raise ValueError("condition must be an object with exactly one operator")
    (op, arg), = cond.items()
    if op in COMPARISON_OPS:
        a, b = arg
        return bool(
            COMPARISON_OPS[op](
                resolve_condition_value(a, context),
                resolve_condition_value(b, context),
            )
        )
    if op == "and":
        return all(eval_condition(c, context) for c in arg)
    if op == "or":
        return any(eval_condition(c, context) for c in arg)
    if op == "not":
        return not eval_condition(arg, context)
    raise ValueError(f"unknown condition operator: {op!r}")


def condition_refs(cond: Any) -> Set[str]:
    """Collect every {"var": "..."} reference inside a condition tree."""
    refs: Set[str] = data_refs(cond)
    if isinstance(cond, dict):
        if "var" in cond and isinstance(cond["var"], str):
            refs.add(cond["var"])
        for value in cond.values():
            refs.update(condition_refs(value))
    elif isinstance(cond, list):
        for item in cond:
            refs.update(condition_refs(item))
    return refs


def normalize_answer(raw: Any) -> str:
    """First line, keep alnum/underscore, uppercase. 'PASS.' -> 'PASS'."""
    lines = str(raw).strip().splitlines()
    first = lines[0] if lines else ""
    return re.sub(r"[^A-Za-z0-9_]", "", first).upper()


def _elapsed_between(started_iso: Optional[str], ended: datetime) -> int:
    try:
        started = datetime.fromisoformat(started_iso)
        return int((ended - started).total_seconds() * 1000)
    except (TypeError, ValueError):
        return 0


class WorkflowEngine:
    def __init__(self, storage: Optional[Storage] = None):
        self.storage = storage or Storage()

    # ---- graph helpers -----------------------------------------------
    @staticmethod
    def build_adjacency(workflow: Dict[str, Any]) -> Dict[str, List[str]]:
        edges = workflow.get("edges")
        ids = [n["id"] for n in workflow["nodes"]]
        if edges is None:  # convenience: chain nodes in declared order
            return {ids[i]: [ids[i + 1]] for i in range(len(ids) - 1)}
        adj: Dict[str, List[str]] = {}
        for e in edges:
            adj.setdefault(e["from"], []).append(e["to"])
        return adj

    def successors(self, workflow: Dict[str, Any], node_id: str) -> List[str]:
        """Edge successors, plus route targets for branch nodes."""
        node = self._node(workflow, node_id)
        out = list(self.build_adjacency(workflow).get(node_id, []))
        if node["type"] in ("branch", "review"):
            out.extend(v for v in node.get("routes", {}).values() if isinstance(v, str))
            if node.get("default"):
                out.append(node["default"])
        return list(dict.fromkeys(out))

    @staticmethod
    def start_node_id(workflow: Dict[str, Any]) -> str:
        for n in workflow["nodes"]:
            if n["type"] == "start":
                return n["id"]
        return workflow["nodes"][0]["id"]

    @staticmethod
    def _node(workflow: Dict[str, Any], node_id: str) -> Dict[str, Any]:
        return next(n for n in workflow["nodes"] if n["id"] == node_id)

    @staticmethod
    def produced_keys(node: Dict[str, Any]) -> List[str]:
        if node["type"] == "task":
            return [node.get("output_key", node["id"])]
        if node["type"] == "subflow":
            return [node.get("output_key", node["id"])]
        if node["type"] == "foreach":
            return [node.get("outputs_key", node["id"])]
        return []

    def dependency_graph(
        self, workflow: Dict[str, Any]
    ) -> Tuple[
        Dict[str, Set[str]], Dict[str, Set[str]], Dict[str, Set[str]]
    ]:
        """Return (node -> referenced context keys, context key -> consuming
        nodes, glob pattern -> consuming nodes).

        Refs come from ${var} in prompts/requirements AND explicit consumes
        declarations; glob entries land in the third mapping and are matched
        against $file output paths at revision time.
        """
        refs: Dict[str, Set[str]] = {}
        consumers: Dict[str, Set[str]] = {}
        glob_consumers: Dict[str, Set[str]] = {}
        for node in workflow["nodes"]:
            nrefs = data_refs(node.get("inputs", {}))
            nrefs.update(data_refs(node.get("output", {})))
            nrefs.update(data_refs(node.get("check", {})))
            nrefs.update(condition_refs(node.get("when")))
            nrefs.update(data_refs(node.get("items")))
            if node["type"] in ("task", "branch"):
                nrefs.update(template_refs(node.get("prompt", "")))
            if node["type"] == "task":
                if node.get("review"):
                    nrefs.update(template_refs(node["review"].get("requirements")))
                for item in node.get("consumes", []):
                    if isinstance(item, str):
                        nrefs.add(item)
                    else:  # {"glob": "..."}
                        glob_consumers.setdefault(item["glob"], set()).add(node["id"])
            if node["type"] == "review":
                nrefs.update(template_refs(node.get("requirements")))
            if node["type"] in ("subflow", "foreach"):
                nrefs.update(template_refs(node.get("inputs", {})))
            if node["type"] == "foreach":
                items_src = node.get("items")
                if isinstance(items_src, dict) and "var" in items_src:
                    nrefs.add(items_src["var"])
            refs[node["id"]] = nrefs
            for key in nrefs:
                # a.b refs depend on the whole node output stored under root a
                consumers.setdefault(key.split(".", 1)[0], set()).add(node["id"])
        return refs, consumers, glob_consumers

    @staticmethod
    def references_check(workflow: Dict[str, Any]) -> None:
        """Static reference sanity: every ${var} root (and foreach items var)
        must resolve to a run input, a node output, or a node-collected input.

        A global pool (not ordering) is used deliberately: branches and loops
        make precise dominance analysis fragile; this catches typos and
        missing producers without false positives on legal forward references.
        """
        available: Set[str] = set(
            (workflow.get("input_schema") or {}).get("properties", {}).keys()
        )
        if workflow["schema_version"] == "1.1":
            available.add("run")
        for n in workflow["nodes"]:
            if n["type"] == "task":
                available.add(n.get("output_key", n["id"]))
                available.update(
                    node_user_input_schema(n).get("properties", {}).keys()
                )
            elif n["type"] == "subflow":
                available.add(n.get("output_key", n["id"]))
            elif n["type"] == "foreach":
                available.add(n.get("outputs_key", n["id"]))

        def check(node: Dict[str, Any], refs: Set[str]) -> None:
            injected = (node.get("as", "item"), node.get("index_as", "index"), "previous")
            for ref in refs:
                root = ref.split(".", 1)[0]
                if node["type"] == "foreach" and root in injected:
                    continue  # item/index are injected per instance
                if root not in available:
                    raise ValueError(
                        f"node {node['id']!r} references undefined context key "
                        f"${{{ref}}}; provide it via run inputs, an earlier "
                        f"node output, or this node's user_input_schema"
                    )

        for node in workflow["nodes"]:
            ntype = node["type"]
            refs = data_refs(node.get("inputs", {}))
            refs.update(data_refs(node.get("output", {})))
            refs.update(data_refs(node.get("check", {})))
            refs.update(data_refs(node.get("items")))
            if ntype in ("task", "branch"):
                refs.update(template_refs(node.get("prompt", "")))
            if ntype == "branch" and node.get("when"):
                refs.update(condition_refs(node["when"]))
            if ntype == "task":
                if node.get("review"):
                    refs.update(template_refs(node["review"].get("requirements")))
                for item in node.get("consumes", []):
                    if isinstance(item, str):
                        refs.add(item)
            elif ntype == "review":
                refs.update(template_refs(node.get("requirements")))
            if ntype in ("subflow", "foreach"):
                refs.update(template_refs(node.get("inputs", {})))
                items = node.get("items")
                if ntype == "foreach" and isinstance(items, dict) and "var" in items:
                    refs.add(items["var"])
            check(node, refs)

    # ---- project-wide validation: call graph + contracts -------------
    def validate_project(self) -> Dict[str, Any]:
        """Validate every stored definition and the subflow call graph:
        missing refs, cycles, and required-input coverage."""
        defs: Dict[str, Dict[str, Any]] = {
            i: self.storage.load_workflow(i)
            for i in self.storage.list_workflows()
        }
        graph: Dict[str, set] = {wid: set() for wid in defs}
        queue = list(defs.values())
        while queue:
            wf = queue.pop()
            validate_workflow(wf)
            self.references_check(wf)
            for node in wf["nodes"]:
                if node["type"] not in ("subflow", "foreach"):
                    continue
                if node.get("workflow"):
                    ref = node["workflow"]
                    if ref not in defs:
                        raise ValueError(
                            f"workflow {wf['id']!r} node {node['id']!r} calls "
                            f"missing workflow {ref!r}"
                        )
                    child = defs[ref]
                else:
                    child = self._synthesize_inline(wf, node)
                    if child["id"] not in defs:
                        defs[child["id"]] = child
                        graph[child["id"]] = set()
                        queue.append(child)
                graph[wf["id"]].add(child["id"])
                self._check_inputs_cover(wf, node, child)

        self._assert_acyclic(graph)
        return {
            "workflows": sorted(defs),
            "edges": {k: sorted(v) for k, v in graph.items() if v},
        }

    @staticmethod
    def _check_inputs_cover(
        parent_wf: Dict[str, Any],
        node: Dict[str, Any],
        child_wf: Dict[str, Any],
    ) -> None:
        provided = set((node.get("inputs") or {}).keys())
        if node["type"] == "foreach":
            provided.add(node.get("as", "item"))
            provided.add(node.get("index_as", "index"))
        required = set((child_wf.get("input_schema") or {}).get("required", []))
        missing = required - provided
        if missing:
            raise ValueError(
                f"node {parent_wf['id']!r}.{node['id']!r} does not map "
                f"required inputs {sorted(missing)} of {child_wf['id']!r}"
            )

    @staticmethod
    def _assert_acyclic(graph: Dict[str, set]) -> None:
        WHITE, GREY, BLACK = 0, 1, 2
        color = {k: WHITE for k in graph}

        def visit(node: str, trail: List[str]) -> None:
            color[node] = GREY
            for nxt in sorted(graph.get(node, ())):
                if color[nxt] == GREY:
                    cycle = trail[trail.index(nxt):] + [nxt]
                    raise ValueError(
                        f"subflow call cycle: {' -> '.join(cycle)}"
                    )
                if color[nxt] == WHITE:
                    visit(nxt, trail + [nxt])
            color[node] = BLACK

        for node in sorted(graph):
            if color[node] == WHITE:
                visit(node, [node])

    def stale_closure(
        self,
        workflow: Dict[str, Any],
        changed_keys: Set[str],
        context: Optional[Dict[str, Any]] = None,
    ) -> Set[str]:
        """Nodes whose output may change when changed_keys change (transitive).

        Glob consumers are matched against the current $file paths found in
        context, so batch consumers (e.g. "assemble all chapters") are marked
        stale when one of their input files changes.
        """
        _, consumers, glob_consumers = self.dependency_graph(workflow)
        context = context or {}
        affected: Set[str] = set()
        pending = set(changed_keys)

        def match_globs(key: str) -> None:
            paths = file_ref_paths(context.get(key))
            if not paths:
                return
            for pattern, nodes in glob_consumers.items():
                if not any(fnmatch.fnmatch(path, pattern) for path in paths):
                    continue
                for node_id in nodes:
                    if node_id in affected:
                        continue
                    affected.add(node_id)
                    node = self._node(workflow, node_id)
                    pending.update(self.produced_keys(node))

        while pending:
            key = pending.pop()
            match_globs(key)
            for node_id in consumers.get(key, ()):  # type: ignore[union-attr]
                if node_id in affected:
                    continue
                affected.add(node_id)
                node = self._node(workflow, node_id)
                pending.update(self.produced_keys(node))
        return affected

    def _forward_reachable(self, workflow: Dict[str, Any], origin: str) -> Set[str]:
        seen: Set[str] = set()
        stack = [origin]
        while stack:
            cur = stack.pop()
            for nxt in self.successors(workflow, cur):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    # ---- subflow / foreach machinery ---------------------------------
    @staticmethod
    def _make_instance(key: str, index: int) -> Dict[str, Any]:
        return {
            "key": key, "index": index, "status": "pending", "run_id": None,
            "started_at": None, "ended_at": None, "error": None, "exports": {},
        }

    def _instance_key(
        self, node: Dict[str, Any], index: int, item: Any
    ) -> str:
        id_field = node.get("id_field")
        if id_field and isinstance(item, dict) and id_field in item:
            key = str(item[id_field])
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                raise ValueError(f"invalid instance key {key!r}; use letters, digits, _ or -")
            return key
        return str(index)

    def _new_single_state(
        self, node: Dict[str, Any], child_wf: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {
            "node_id": node["id"], "status": "active", "kind": "single",
            "subflow_id": child_wf["id"], "inline": False,
            "items_mode": None, "items_source": None,
            "as": "item", "index_as": "index", "id_field": None,
            "cursor": 0, "total_seen": 1, "done": False,
            "returning": False, "on_fail": "abort",
            "output_key": node.get("output_key", node["id"]),
            "instances": [self._make_instance("0", 0)],
        }

    def _new_foreach_state(
        self, node: Dict[str, Any], child_wf: Dict[str, Any],
        items_mode: str, items: list, done: bool
    ) -> Dict[str, Any]:
        instances = [
            self._make_instance(self._instance_key(node, i, item), i)
            for i, item in enumerate(items)
        ]
        if len({i["key"] for i in instances}) != len(instances):
            raise ValueError("foreach instance keys must be unique")
        return {
            "node_id": node["id"], "status": "active", "kind": "foreach",
            "subflow_id": child_wf["id"], "inline": bool(node.get("nodes")),
            "items_mode": items_mode, "items_source": node["items"],
            "as": node.get("as", "item"),
            "index_as": node.get("index_as", "index"),
            "id_field": node.get("id_field"),
            "cursor": 0, "total_seen": len(items), "done": done,
            "returning": False, "on_fail": node.get("on_fail", "abort"),
            "output_key": node.get("outputs_key", node["id"]),
            "instances": instances,
        }

    def _resolve_items(
        self, node: Dict[str, Any], ctx: Dict[str, Any]
    ) -> Tuple[str, list, bool]:
        """Return (mode, items, done). Array source = static; an object
        {"done":bool,"items":[...]} = dynamic."""
        src = node["items"]
        if isinstance(src, list):
            value: Any = src
        elif isinstance(src, dict) and "ref" in src:
            value = resolve_data(src, ctx)
        elif isinstance(src, dict) and "var" in src:
            key = src["var"]
            value = resolve_dotted(ctx, key)
        elif isinstance(src, dict) and "$file" in src:
            value = render(src, ctx)
        else:
            raise ValueError("unsupported items source; use an array, {ref} or {$file}")
        if is_file_ref(value):
            value = self.storage.read_json_file(value["$file"])
        if isinstance(value, list):
            return "static", value, False
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            if not isinstance(value.get("done"), bool):
                raise ValueError("dynamic items require an explicit boolean done")
            return "dynamic", value["items"], value["done"]
        raise ValueError("items source must be an array or {\"done\":...,\"items\":[...]}")

    def _merge_items(
        self, state: Dict[str, Any], items: list, done: bool,
        node: Dict[str, Any]
    ) -> None:
        """Dynamic boundary: append pending instances for newly added items."""
        cap = node.get("max_instances", DEFAULT_MAX_INSTANCES)
        if len(items) > cap:
            raise ValueError(
                f"items count {len(items)} exceeds max_instances {cap}"
            )
        existing = state["instances"]
        if state.get("items_snapshot"):
            previous = self.storage.read_json_file(state["items_snapshot"])
            if items[:len(previous)] != previous:
                raise ValueError("dynamic items are append-only; existing items changed or removed")
            keys = [self._instance_key(node, i, item) for i, item in enumerate(items)]
            if len(set(keys)) != len(keys):
                raise ValueError("foreach instance keys must be unique")
            self.storage.write_json_file(state["items_snapshot"], items)
        if len(items) > len(existing):
            for i in range(len(existing), len(items)):
                existing.append(
                    self._make_instance(self._instance_key(node, i, items[i]), i)
                )
        state["total_seen"] = len(items)
        state["done"] = done

    @staticmethod
    def _find_instance(
        state: Dict[str, Any], key: str
    ) -> Dict[str, Any]:
        inst = next(
            (i for i in state["instances"] if i["key"] == str(key)), None
        )
        if inst is None:
            raise RuntimeError(
                f"instance {key!r} not found in node {state['node_id']!r}"
            )
        return inst

    @staticmethod
    def _active_instance(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        inst = next((i for i in state["instances"] if i["status"] == "running"), None)
        if inst:
            return inst
        return next((i for i in state["instances"] if i["status"] == "pending"), None)

    @staticmethod
    def _next_pending(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return next((i for i in state["instances"] if i["status"] == "pending"), None)

    # ---- definition loading / inline synthesis -----------------------
    def _load_or_synthesize_child(
        self,
        parent_workflow: Dict[str, Any],
        parent_record: Dict[str, Any],
        node: Dict[str, Any],
    ) -> Dict[str, Any]:
        if node.get("workflow"):
            child_wf = self.storage.load_workflow(node["workflow"])
            validate_workflow(child_wf)
            return child_wf
        child_wf = self._synthesize_inline(parent_workflow, node)
        self.storage.write_json_file(
            self.storage.inline_subflow_path(parent_record, node["id"]),
            child_wf,
        )
        return child_wf

    def _synthesize_inline(
        self, parent_workflow: Dict[str, Any], node: Dict[str, Any]
    ) -> Dict[str, Any]:
        body = node["nodes"]
        raw_exports = node.get("exports")
        if isinstance(raw_exports, list):
            exports_map = {k: {} for k in raw_exports}
        else:
            exports_map = dict(raw_exports or {})
        input_keys = list((node.get("inputs") or {}).keys())
        props = {k: {} for k in input_keys}
        # item/index are injected per instance even without an inputs mapping
        props[node.get("as", "item")] = {}
        props[node.get("index_as", "index")] = {}
        wf = {
            "schema_version": parent_workflow["schema_version"],
            "max_steps": parent_workflow.get("max_steps", 50),
            "id": f'{parent_workflow["id"]}.{node["id"]}',
            "name": f'inline:{node["id"]}',
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": input_keys,
            },
            "exports": exports_map,
            "nodes": (
                [{"id": "start", "type": "start"}]
                + [dict(n) for n in body]
                + [{"id": "end", "type": "end"}]
            ),
            "edges": self._synthesized_edges(node, body),
        }
        validate_workflow(wf)
        return wf

    @staticmethod
    def _synthesized_edges(
        node: Dict[str, Any], body: List[Dict[str, Any]]
    ) -> List[Dict[str, str]]:
        first_id, last_id = body[0]["id"], body[-1]["id"]
        raw = node.get("edges")
        if raw is not None:
            edges = [dict(e) for e in raw]
        else:
            edges = [
                {"from": body[i]["id"], "to": body[i + 1]["id"]}
                for i in range(len(body) - 1)
                if body[i]["type"] not in ("branch", "review")
            ]
        pairs = {(e["from"], e["to"]) for e in edges}
        if ("start", first_id) not in pairs:
            edges.append({"from": "start", "to": first_id})
        if (last_id, "end") not in pairs and body[-1]["type"] not in ("branch", "review"):
            edges.append({"from": last_id, "to": "end"})
        return edges

    def _child_wf_for_state(
        self,
        workflow: Dict[str, Any],
        record: Dict[str, Any],
        node: Dict[str, Any],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        if state["inline"]:
            return self.storage.read_json_file(
                self.storage.inline_subflow_path(record, node["id"])
            )
        child_wf = self.storage.load_workflow(state["subflow_id"])
        validate_workflow(child_wf)
        return child_wf

    def _item_at(
        self, node: Dict[str, Any], ctx: Dict[str, Any], index: int
    ) -> Any:
        _mode, items, _done = self._resolve_items(node, ctx)
        return items[index]

    # ---- chain guards -------------------------------------------------
    def _assert_chain_ok(
        self, parent_record: Dict[str, Any], child_wf_id: str
    ) -> None:
        cur = parent_record
        seen: Set[str] = set()
        depth = 0
        while True:
            if cur["workflow_id"] == child_wf_id:
                raise RuntimeError(
                    f"subflow cycle: {child_wf_id!r} already running in "
                    f"the parent chain"
                )
            parent = cur.get("parent")
            if not parent:
                return
            if parent["run_id"] in seen:
                raise RuntimeError("subflow parent chain is cyclic")
            seen.add(parent["run_id"])
            depth += 1
            if depth >= MAX_SUBFLOW_DEPTH:
                raise RuntimeError(
                    f"subflow nesting depth exceeds {MAX_SUBFLOW_DEPTH}"
                )
            cur = self.storage.find_run(parent["run_id"])

    # ---- instance start / completion ----------------------------------
    def _begin_instance(
        self,
        workflow: Dict[str, Any],
        record: Dict[str, Any],
        node: Dict[str, Any],
        state: Dict[str, Any],
        inst: Dict[str, Any],
    ) -> None:
        child_wf = self._child_wf_for_state(workflow, record, node, state)
        scope = dict(record["context"])
        if state["kind"] == "foreach":
            if state.get("items_snapshot"):
                item_value = self.storage.read_json_file(state["items_snapshot"])[inst["index"]]
            else:
                item_value = self._item_at(node, record["context"], inst["index"])
            scope[state["as"]] = item_value
            scope[state["index_as"]] = inst["index"]
            scope["previous"] = (
                copy.deepcopy(state["instances"][inst["index"] - 1]["exports"])
                if inst["index"] else None
            )
        resolved = (
            resolve_data(node.get("inputs", {}), scope)
            if workflow["schema_version"] == "1.1"
            else render(node.get("inputs", {}), scope)
        )
        if state["kind"] == "foreach":
            # the current item is part of the child contract context
            resolved[state["as"]] = item_value
            resolved[state["index_as"]] = inst["index"]
        validate_inputs(child_wf, resolved)
        self._assert_chain_ok(record, child_wf["id"])

        prior = None
        prior_path = os.path.join(
            self.storage.child_dir(record, node["id"], inst["key"]), "record.json"
        )
        if workflow["schema_version"] == "1.1" and os.path.isfile(prior_path):
            prior = self.storage.load_record(prior_path, cache=True)
        run_id = inst["run_id"] or (prior["run_id"] if prior else make_run_id())
        inst.update(
            run_id=run_id, status="running", started_at=now_iso(),
            error=None, exports={},
        )
        link = {
            "workflow_id": record["workflow_id"],
            "run_id": record["run_id"],
            "node_id": node["id"],
            "instance": inst["key"],
        }
        self._append_event(
            record, "subflow_started", node_id=node["id"],
            instance=inst["key"], child_run_id=run_id,
        )
        self._save(workflow, record)

        child_record = self._new_child_record(
            child_wf, resolved, run_id, link
        )
        if prior:
            child_record["started_at"] = prior["started_at"]
            child_record["node_runs"] = prior["node_runs"]
            child_record["events"] = prior["events"] + child_record["events"]
        self._initialize_protocol(child_wf, child_record)
        self._enter_node(child_wf, child_record)

    def _new_child_record(
        self,
        child_wf: Dict[str, Any],
        resolved: Dict[str, Any],
        run_id: str,
        link: Dict[str, Any],
    ) -> Dict[str, Any]:
        record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "workflow_id": child_wf["id"],
            "status": "running",
            "phase": "executing",
            "started_at": now_iso(),
            "ended_at": None,
            "duration_ms": None,
            "inputs": resolved,
            "context": dict(resolved),
            "next_node": self.start_node_id(child_wf),
            "node_started_at": None,
            "error": None,
            "last_feedback": None,
            "pending_review": None,
            "revision": None,
            "node_runs": [],
            "events": [],
            "parent": link,
            "subflow_state": None,
        }
        self._append_event(record, "run_started")
        return record

    def _collect_exports(
        self, workflow: Dict[str, Any], record: Dict[str, Any]
    ) -> Dict[str, Any]:
        spec = workflow.get("exports") or {}
        exports: Dict[str, Any] = {}
        for key, schema in spec.items():
            if key not in record["context"]:
                raise KeyError(f"export {key!r} missing from child context")
            value = record["context"][key]
            if schema:
                _jsonschema_validate(value, schema)
            exports[key] = value
        return exports

    def _propagate_child_success(
        self,
        child_wf: Dict[str, Any],
        child_record: Dict[str, Any],
        ended: datetime,
        exports: Dict[str, Any],
    ) -> Dict[str, Any]:
        self._complete(child_wf, child_record)
        link = child_record["parent"]
        parent_record = self.storage.find_run(link["run_id"])
        parent_wf = self.storage.load_workflow_for_record(parent_record)
        node = self._node(parent_wf, link["node_id"])
        state = parent_record["subflow_state"]
        inst = self._find_instance(state, link["instance"])
        inst.update(
            status="success", ended_at=child_record["ended_at"],
            error=None, exports=exports,
        )
        self._append_event(
            parent_record, "instance_completed", node_id=node["id"],
            instance=inst["key"], child_run_id=child_record["run_id"],
        )
        if state["kind"] == "single":
            return self._finish_subflow_node(
                parent_wf, parent_record, node, state
            )
        return self._advance_foreach(parent_wf, parent_record, node, state)

    def _propagate_child_failure(
        self,
        child_wf: Dict[str, Any],
        child_record: Dict[str, Any],
        message: str,
    ) -> Dict[str, Any]:
        self._fail(child_wf, child_record, message)
        link = child_record["parent"]
        parent_record = self.storage.find_run(link["run_id"])
        parent_wf = self.storage.load_workflow_for_record(parent_record)
        node = self._node(parent_wf, link["node_id"])
        state = parent_record["subflow_state"]
        inst = self._find_instance(state, link["instance"])
        err = re.sub(r"\s+", " ", message).strip()[:300]
        inst.update(
            status="failed", ended_at=child_record["ended_at"], error=err
        )
        self._append_event(
            parent_record, "instance_failed", node_id=node["id"],
            instance=inst["key"], child_run_id=child_record["run_id"],
            reason=err,
        )
        if state["kind"] == "single" or state.get("on_fail", "abort") == "abort":
            return self._fail(
                parent_wf, parent_record,
                f"subflow node {node['id']} instance {inst['key']} failed: {err}",
            )
        return self._advance_foreach(parent_wf, parent_record, node, state)

    def _advance_foreach(
        self,
        parent_wf: Dict[str, Any],
        parent_record: Dict[str, Any],
        node: Dict[str, Any],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        if state.get("returning"):
            return self._finish_foreach_node(
                parent_wf, parent_record, node, state
            )
        try:
            if state["items_mode"] == "dynamic":
                mode, items, done = self._resolve_items(node, parent_record["context"])
                if mode != "dynamic":
                    raise ValueError("dynamic items source changed mode")
                self._merge_items(state, items, done, node)
        except (ValueError, KeyError, OSError) as e:
            return self._fail(parent_wf, parent_record, f"foreach items invalid: {e}")
        nxt = self._next_pending(state)
        if nxt is not None:
            self._save(parent_wf, parent_record)
            self._begin_instance(parent_wf, parent_record, node, state, nxt)
            return parent_record
        if state["items_mode"] == "dynamic" and not state["done"]:
            parent_record["phase"] = "awaiting_items"
            self._save(parent_wf, parent_record)
            return parent_record
        return self._finish_foreach_node(parent_wf, parent_record, node, state)

    def _finish_foreach_node(
        self,
        parent_wf: Dict[str, Any],
        parent_record: Dict[str, Any],
        node: Dict[str, Any],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload = {
            "items": [
                {"key": i["key"], "status": i["status"], "exports": i["exports"]}
                for i in state["instances"]
            ]
        }
        path = self.storage.results_manifest_path(parent_record, node["id"])
        self.storage.write_json_file(path, payload)
        output = {"$file": path}
        parent_record["context"][node.get("outputs_key", node["id"])] = output
        return self._finish_subflow_node(
            parent_wf, parent_record, node, state, output
        )

    def _finish_subflow_node(
        self,
        parent_wf: Dict[str, Any],
        parent_record: Dict[str, Any],
        node: Dict[str, Any],
        state: Dict[str, Any],
        output: Any = None,
    ) -> Dict[str, Any]:
        if output is None:  # single call: exports land under output_key
            output = state["instances"][0]["exports"]
            parent_record["context"][
                node.get("output_key", node["id"])
            ] = output
        state["status"] = "completed"
        state["done"] = True
        parent_record["phase"] = "executing"
        parent_record["next_node"] = node["id"]
        ended = datetime.now()
        parent_record["node_runs"].append(
            self._node_run(
                node, parent_record, "success", ended, output, None, None, None
            )
        )
        rev = self._active_revision(parent_record)
        if rev and node["id"] in rev["stale_nodes"]:
            rev["redone_nodes"].append(node["id"])
            if (
                rev["mode"] != "return"
                and set(rev["redone_nodes"]) == set(rev["stale_nodes"])
            ):
                self._close_revision(parent_record)
        parent_record["subflow_state"] = state
        self._advance(parent_wf, parent_record, node, None)
        return parent_record

    def _activate_subflow_node(
        self,
        workflow: Dict[str, Any],
        record: Dict[str, Any],
        node: Dict[str, Any],
    ) -> None:
        state = record.get("subflow_state")
        if state and state.get("status") == "active" and state["node_id"] == node["id"]:
            self._reconcile_state(workflow, record, node, state)
            return
        self._start_subflow_node(workflow, record, node)

    def _start_subflow_node(
        self,
        workflow: Dict[str, Any],
        record: Dict[str, Any],
        node: Dict[str, Any],
    ) -> None:
        if node["type"] == "subflow":
            child_wf = self.storage.load_workflow(node["workflow"])
            validate_workflow(child_wf)
            state = self._new_single_state(node, child_wf)
        else:
            items_mode, items, done = self._resolve_items(
                node, record["context"]
            )
            cap = node.get("max_instances", DEFAULT_MAX_INSTANCES)
            if len(items) > cap:
                raise ValueError(
                    f"items count {len(items)} exceeds max_instances {cap}"
                )
            child_wf = self._load_or_synthesize_child(
                workflow, record, node
            )
            state = self._new_foreach_state(
                node, child_wf, items_mode, items, done
            )
            if workflow["schema_version"] == "1.1":
                state["items_snapshot"] = os.path.join(
                    self.storage.run_dir_for_record(record), f"foreach-{node['id']}-items.json"
                )
                self.storage.write_json_file(state["items_snapshot"], items)
            if not items and (items_mode == "static" or done):
                record["subflow_state"] = state
                self._finish_foreach_node(workflow, record, node, state)
                return
            if not items:  # dynamic source, not done yet
                record["subflow_state"] = state
                record["phase"] = "awaiting_items"
                self._save(workflow, record)
                return
        record["subflow_state"] = state
        self._save(workflow, record)
        self._begin_instance(
            workflow, record, node, state, state["instances"][0]
        )

    def _reconcile_state(
        self,
        workflow: Dict[str, Any],
        record: Dict[str, Any],
        node: Dict[str, Any],
        state: Dict[str, Any],
    ) -> bool:
        """Crash recovery at a parent node. Return True when the run moved."""
        inst = self._active_instance(state)
        if inst is None or not inst.get("run_id"):
            if inst is not None:
                self._begin_instance(workflow, record, node, state, inst)
                return True
            if state["kind"] == "single":
                self._begin_instance(
                    workflow, record, node, state, state["instances"][0]
                )
                return True
            # foreach with zero instances: evaluate the boundary; starting
            # a new instance counts as moved, settling on awaiting_items does not
            self._advance_foreach(workflow, record, node, state)
            fresh = self._active_instance(state)
            return bool(fresh and fresh.get("run_id"))
        try:
            child_record = self.storage.find_run(inst["run_id"])
        except FileNotFoundError:
            inst.update(status="pending", run_id=None)
            self._begin_instance(workflow, record, node, state, inst)
            return True
        child_wf = self.storage.load_workflow_for_record(child_record)
        if child_record["status"] == "completed":
            if inst["status"] != "success":
                exports = self._collect_exports(child_wf, child_record)
                self._propagate_child_success(
                    child_wf, child_record, datetime.now(), exports
                )
            elif state["kind"] == "single":
                self._finish_subflow_node(workflow, record, node, state)
            else:
                self._advance_foreach(workflow, record, node, state)
            return True
        if child_record["status"] == "failed":
            if inst["status"] != "failed":
                self._propagate_child_failure(
                    child_wf, child_record,
                    child_record.get("error") or "child run failed",
                )
            elif state["kind"] == "single" or state.get("on_fail") == "abort":
                self._fail(
                    workflow, record,
                    f"subflow node {node['id']} instance {inst['key']} failed: "
                    f"{inst['error']}",
                )
            else:
                self._advance_foreach(workflow, record, node, state)
            return True
        # child genuinely running: nothing changed
        if record["phase"] == "awaiting_items":
            record["phase"] = "executing"
            self._save(workflow, record)
        return False

    # ---- protocol: start / resume ------------------------------------
    def start(
        self,
        workflow: Dict[str, Any],
        inputs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        validate_workflow(workflow)
        inputs = inputs or {}
        validate_inputs(workflow, inputs)
        if workflow["schema_version"] == "1.1":
            if "run" in inputs:
                raise ValueError("'run' is reserved for runtime metadata")
            self.references_check(workflow)

        record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": make_run_id(),
            "workflow_id": workflow["id"],
            "status": "running",
            "phase": "executing",
            "started_at": now_iso(),
            "ended_at": None,
            "duration_ms": None,
            "inputs": inputs,
            "context": dict(inputs),
            "next_node": self.start_node_id(workflow),
            "node_started_at": None,
            "error": None,
            "last_feedback": None,
            "pending_review": None,
            "revision": None,
            "node_runs": [],
            "events": [],
            "parent": None,
            "subflow_state": None,
        }
        self._append_event(record, "run_started")
        self._initialize_protocol(workflow, record)
        self._enter_node(workflow, record)
        return record

    def _initialize_protocol(self, workflow, record):
        record["workflow_version"] = workflow["schema_version"]
        if workflow["schema_version"] == "1.1":
            record["context"]["run"] = {
                "id": record["run_id"],
                "dir": self.storage.run_dir_for_record(record),
            }
            self.storage.write_json_file(
                os.path.join(self.storage.run_dir_for_record(record), "workflow.json"),
                workflow,
            )

    def root_record(self, record):
        while record.get("parent"):
            record = self.storage.find_run(record["parent"]["run_id"])
        return record

    def _leaf_record(self, record):
        while record["status"] == "running":
            state = record.get("subflow_state")
            if not (state and state["status"] == "active"
                    and state["node_id"] == record["next_node"]):
                break
            inst = self._active_instance(state)
            if not inst or not inst.get("run_id"):
                break
            try:
                record = self.storage.find_run(inst["run_id"])
            except FileNotFoundError:
                break
        return record

    def action_spec(self, run_id):
        """Read-only root-to-leaf task packet; no scheduling or reconciliation."""
        root = self.root_record(self.storage.find_run(run_id))
        leaf = self._leaf_record(root)
        workflow = self.storage.load_workflow_for_record(leaf)
        spec = self.node_spec(workflow, leaf)
        spec["run_id"] = root["run_id"]
        spec["root_workflow_id"] = root["workflow_id"]
        spec["action"] = None
        if leaf["status"] == "running":
            kind = {
                "awaiting_input": "provide", "awaiting_review": "review",
                "awaiting_items": "resume", "awaiting_check": "check",
            }.get(leaf["phase"], "complete")
            node = self._node(workflow, leaf["next_node"])
            if node["type"] in ("start", "end", "subflow", "foreach") or node.get("when"):
                if leaf["phase"] == "executing":
                    kind = "resume"
            digest = hashlib.sha256(json.dumps(
                [leaf, workflow], sort_keys=True, ensure_ascii=False
            ).encode()).hexdigest()[:24]
            spec["action"] = {
                "id": digest, "run_id": leaf["run_id"],
                "node_id": leaf["next_node"], "kind": kind,
            }
        return spec

    def settle(self, run_id):
        """Explicit mutation boundary: recover children, audit mechanical steps."""
        root_id = self.root_record(self.storage.find_run(run_id))["run_id"]
        while True:
            root = self.storage.find_run(root_id)
            if root["status"] != "running":
                return root
            record = root
            moved = False
            while record["status"] == "running":
                workflow = self.storage.load_workflow_for_record(record)
                state = record.get("subflow_state")
                if not (state and state["status"] == "active"
                        and state["node_id"] == record["next_node"]
                        and record["phase"] != "awaiting_items"):
                    break
                node = self._node(workflow, state["node_id"])
                if self._reconcile_state(workflow, record, node, state):
                    moved = True
                    break
                inst = self._active_instance(state)
                if not inst or not inst.get("run_id"):
                    break
                record = self.storage.find_run(inst["run_id"])
            if moved:
                continue
            workflow = self.storage.load_workflow_for_record(record)
            if record["status"] != "running" or record["phase"] != "executing":
                return self.storage.find_run(root_id)
            node = self._node(workflow, record["next_node"])
            if node["type"] not in ("start", "end") and not (
                node["type"] == "branch" and node.get("when")
            ):
                return self.storage.find_run(root_id)
            self.complete(workflow, record["run_id"])

    def act(self, run_id, action_id, command, **kwargs):
        """Guard an agent/human response against the current leaf identity."""
        spec = self.action_spec(run_id)
        action = spec["action"]
        if not action or not action_id or action_id != action["id"]:
            raise RuntimeError("missing or stale action; fetch `current --json` again")
        expected = "complete" if action["kind"] == "check" else action["kind"]
        if command not in (expected, "fail"):
            raise RuntimeError(f"current action requires {expected}, not {command}")
        record = self.storage.find_run(action["run_id"])
        workflow = self.storage.load_workflow_for_record(record)
        getattr(self, command)(workflow, record["run_id"], **kwargs)
        self.settle(spec["run_id"])
        return self.action_spec(spec["run_id"])

    def resume(self, workflow: Dict[str, Any], run_id: str) -> Dict[str, Any]:
        validate_workflow(workflow)
        record = self.storage.load_run(workflow["id"], run_id)
        if record.get("workflow_version") == "1.1":
            workflow = self.storage.load_workflow_for_record(record)
        if record["status"] == "completed":
            raise RuntimeError(f"run {run_id} is already completed")
        state = record.get("subflow_state")
        node = (
            self._node(workflow, state["node_id"])
            if state and state.get("node_id") else None
        )
        if record["phase"] == "awaiting_items":
            if node is None:
                raise RuntimeError("invalid awaiting_items state")
            record["phase"] = "executing"
            record["error"] = None
            self._append_event(record, "run_resumed")
            self._save(workflow, record)
            return self._advance_foreach(workflow, record, node, state)
        if not record.get("next_node"):
            raise RuntimeError(f"run {run_id} has no next_node to resume from")
        record["status"] = "running"
        record["error"] = None
        record["ended_at"] = None
        record["duration_ms"] = None
        self._append_event(record, "run_resumed")
        # phase/pending state already describes the node; only reset its timer
        record["node_started_at"] = now_iso()
        # a failed instance of the active subflow node: reopen the child too
        if state and state.get("status") == "active":
            failed = next(
                (i for i in state["instances"] if i["status"] == "failed"), None
            )
            if failed and failed.get("run_id"):
                child_record = self.storage.find_run(failed["run_id"])
                child_wf = self.storage.load_workflow_for_record(child_record)
                self.resume(child_wf, child_record["run_id"])
                failed.update(
                    status="running", error=None, started_at=now_iso()
                )
        self._save(workflow, record)
        if state and state.get("status") == "active":
            self._reconcile_state(workflow, record, node, state)
        return record

    # ---- protocol: node spec -----------------------------------------
    def node_spec(self, workflow: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
        envelope: Dict[str, Any] = {
            "run_id": record["run_id"],
            "workflow_id": workflow["id"],
            "status": record["status"],
            "phase": record["phase"],
            "revision": self._revision_brief(record),
            "error": record.get("error"),
            "node": None,
            "input_request": None,
            "review_request": None,
        }
        current = record.get("next_node")
        if record["status"] != "running" or current is None:
            return envelope

        node = self._node(workflow, current)
        if record["phase"] == "awaiting_input":
            envelope["input_request"] = self._input_request(node, record["context"])
            return envelope

        if record["phase"] == "awaiting_review":
            envelope["review_request"] = {
                **record["pending_review"],
                "output": self._last_output(record, node["id"]),
            }
            envelope["node"] = self._basic_node(node, record, workflow, include_outputs=False)
            return envelope

        envelope["node"] = self._basic_node(node, record, workflow)
        if record["phase"] == "awaiting_check":
            envelope["check_request"] = record["pending_check"]
        return envelope

    def _basic_node(self, node: Dict[str, Any], record: Dict[str, Any],
                    workflow: Dict[str, Any], include_outputs: bool = True) -> Dict[str, Any]:
        ntype = node["type"]
        spec: Dict[str, Any] = {
            "id": node["id"],
            "type": ntype,
            "name": node.get("name", node["id"]),
        }
        if not include_outputs:
            return spec

        ctx = record["context"]
        if ntype == "task":
            spec.update(
                model_hint=node.get("model") or workflow.get("default_model"),
                prompt=render(node.get("prompt", ""), ctx),
                output_key=node.get("output_key", node["id"]),
                output_schema=node.get("output_schema"),
                previous_output=self._last_output(record, node["id"]),
                feedback=record.get("last_feedback"),
                under_revision=bool(self._active_revision(record)),
            )
            if workflow["schema_version"] == "1.1":
                spec["inputs"] = self._materials(node, ctx)
                spec["output"] = resolve_data(node.get("output"), ctx)
        elif ntype == "branch":
            spec.update(
                model_hint=node.get("model") or workflow.get("default_model"),
                prompt=render(node.get("prompt", ""), ctx),
                routes=node.get("routes", {}),
                default=node.get("default"),
            )
            if node.get("when"):
                spec.update(deterministic=True, when=node["when"])
            if workflow["schema_version"] == "1.1":
                spec["inputs"] = self._materials(node, ctx)
        elif ntype in ("subflow", "foreach"):
            state = record.get("subflow_state")
            spec["subflow_ref"] = node.get("workflow")
            spec["inline"] = bool(node.get("nodes"))
            try:  # foreach ${item} is not in the parent context -> raw
                spec["inputs"] = render(node.get("inputs", {}), ctx)
            except KeyError:
                spec["inputs"] = node.get("inputs", {})
            if ntype == "subflow":
                spec["output_key"] = node.get("output_key", node["id"])
            else:
                try:
                    f_mode, f_items, _f_done = self._resolve_items(node, ctx)
                except Exception:
                    f_mode, f_items = "unresolved", []
                spec.update(
                    items_mode=f_mode,
                    items=f_items if len(f_items) <= 100 else f_items[:100],
                    items_truncated=len(f_items) > 100,
                    as_=node.get("as", "item"),
                    index_as=node.get("index_as", "index"),
                    id_field=node.get("id_field"),
                    outputs_key=node.get("outputs_key", node["id"]),
                    on_fail=node.get("on_fail", "abort"),
                    max_instances=node.get(
                        "max_instances", DEFAULT_MAX_INSTANCES
                    ),
                )
            if state and state.get("node_id") == node["id"]:
                done_n = sum(
                    1 for i in state["instances"] if i["status"] == "success"
                )
                spec["subflow_state"] = {
                    "subflow_id": state["subflow_id"],
                    "status": state["status"],
                    "progress": f"{done_n}/{len(state['instances'])}",
                    "items_mode": state["items_mode"],
                }
                inst = self._active_instance(state)
                if (
                    inst and inst.get("run_id")
                    and state.get("status") == "active"
                ):
                    spec["child"] = {
                        "run_id": inst["run_id"],
                        "instance": inst["key"],
                        "index": inst["index"],
                    }
            if record["phase"] == "awaiting_items":
                spec["message"] = (
                    "dynamic items source has no pending items; append items "
                    "(or set done=true), then `aiwf resume`"
                )
        # start / end: id/type/name only
        return spec

    @staticmethod
    def _materials(node, ctx):
        materials = {}
        for name, value in node.get("inputs", {}).items():
            try:
                materials[name] = resolve_data(value, ctx)
            except KeyError as e:
                raise ValueError(
                    f"node {node['id']!r} input {name!r}: {e}"
                ) from e
        return materials

    def _input_request(self, node: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        schema = node_user_input_schema(node)
        missing = self._missing_node_inputs(node, ctx)
        props = schema.get("properties", {})
        fields = []
        for name in missing:
            meta = props.get(name, {})
            fields.append({
                "name": name,
                "type": meta.get("type", "string"),
                "title": meta.get("title", name),
                "description": meta.get("description", ""),
                "enum": meta.get("enum"),
            })
        return {"node_id": node["id"], "fields": fields, "schema": schema}

    def _revision_brief(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        rev = record.get("revision")
        if not rev or rev["status"] != "active":
            return None
        return {
            "seq": rev["seq"], "target": rev["target"], "reason": rev["reason"],
            "mode": rev["mode"], "stale_nodes": rev["stale_nodes"],
            "reused_nodes": rev["reused_nodes"], "redone_nodes": rev["redone_nodes"],
        }

    # ---- protocol: provide -------------------------------------------
    def provide(
        self,
        workflow: Dict[str, Any],
        run_id: str,
        values: Dict[str, Any],
        node_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        record = self._load_running(workflow, run_id)
        if not isinstance(values, dict) or not values:
            raise ValueError("provide requires a non-empty JSON object of answers")

        awaiting_current = (
            record["phase"] == "awaiting_input"
            and (node_id is None or node_id == record["next_node"])
        )
        if awaiting_current:
            target_id = record["next_node"]  # type: ignore[assignment]
            node = self._node(workflow, target_id)
            merged = dict(record["context"])
            merged.update(values)
            _jsonschema_validate(merged, node_user_input_schema(node))
            record["context"].update(values)
            missing = self._missing_node_inputs(node, record["context"])
            self._append_event(
                record, "input_provided", node_id=target_id, values=values,
                fields=sorted(missing),
            )
            if missing:  # more questions remain on the same node
                record["node_started_at"] = now_iso()
                self._save(workflow, record)
            else:
                self._enter_node(workflow, record)
            return record

        # changing input of a node that already finished -> revision
        if node_id is None:
            raise RuntimeError(
                "run is not awaiting input; name a completed task node with --node "
                "to change its input (opens a revision)"
            )
        node = self._node(workflow, node_id)
        if node["type"] != "task":
            raise ValueError(f"node {node_id!r} is not a task node")
        if not self._has_success(record, node_id):
            raise RuntimeError(f"task node {node_id!r} has not been executed yet")
        user_schema = node_user_input_schema(node)
        if user_schema:
            _jsonschema_validate(
                {**record["context"], **values}, user_schema
            )
        self._begin_revision(
            workflow, record,
            target=node, changed_keys=set(values.keys()),
            reason=reason or "node input changed",
            mode="cascade", scope="auto",
            requested_from=record["next_node"],  # type: ignore[arg-type]
            pre_values=values, event_type="input_changed",
        )
        return record

    # ---- protocol: complete / fail -----------------------------------
    def complete(
        self,
        workflow: Dict[str, Any],
        run_id: str,
        output: Any = None,
        model: Optional[str] = None,
        tokens: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        record = self._load_running(workflow, run_id)
        if record["phase"] == "awaiting_check":
            return self._check_result(workflow, record, output)
        if record["phase"] != "executing":
            raise RuntimeError(
                f"run is in phase {record['phase']!r}, not accepting task output"
            )
        if workflow["schema_version"] == "1.1" and len(record["node_runs"]) >= workflow.get("max_steps", 50):
            return self._fail(workflow, record, "max_steps exceeded before accepting output")
        validate_output_size(output)
        current = record["next_node"]  # type: ignore[assignment]
        node = self._node(workflow, current)
        ended = datetime.now()
        pending = record.get("pending_check")
        if pending and pending.get("exhausted"):
            raise RuntimeError("check attempts exhausted; revise explicitly or start a new run")

        # while a child run is live the parent node cannot be driven directly
        sub_state = record.get("subflow_state")
        if (
            node["type"] in ("subflow", "foreach")
            and sub_state and sub_state.get("status") == "active"
            and sub_state.get("node_id") == current
        ):
            inst = self._active_instance(sub_state)
            child_id = inst.get("run_id") if inst else None
            raise RuntimeError(
                f"node {current!r} has child run {child_id} in progress; "
                f"drive that child (aiwf current {child_id}) or finish it"
            )

        # a child run reaches end only if its exports contract is satisfied
        child_exports: Optional[Dict[str, Any]] = None
        if node["type"] == "end" and record.get("parent"):
            try:
                child_exports = self._collect_exports(workflow, record)
            except Exception as e:
                record["node_runs"].append(
                    self._node_run(node, record, "failed", ended, None,
                                   None, None, None)
                )
                return self._propagate_child_failure(
                    workflow, record,
                    f"exports contract unmet at end: {e}",
                )

        # output contract: a non-conforming report is rejected with feedback,
        # the node stays open and the run keeps running
        if node["type"] == "task" and node.get("output_schema"):
            try:
                _jsonschema_validate(output, node["output_schema"])
            except Exception as e:
                return self._reject_output(
                    workflow, record, node, ended, output, str(e)
                )
        if node["type"] == "task" and node.get("output"):
            expected = resolve_data(node["output"], record["context"])["file"]
            path = file_ref_path(output)
            if path != expected or not isinstance(path, str) or not os.path.isfile(path):
                return self._reject_output(
                    workflow, record, node, ended, output,
                    f"expected existing file {expected!r}, got {path!r}",
                )

        decision: Optional[Dict[str, str]] = None
        try:
            ntype = node["type"]
            if ntype == "task":
                record["context"][node.get("output_key", node["id"])] = output
            elif ntype == "branch":
                if node.get("when"):
                    answer = (
                        "TRUE"
                        if eval_condition(node["when"], record["context"])
                        else "FALSE"
                    )
                else:
                    answer = output
                routes = {normalize_answer(k): v for k, v in node.get("routes", {}).items()}
                target = routes.get(normalize_answer(answer))
                if not target:
                    target = node.get("default")
                if not target:
                    raise RuntimeError(
                        f"branch node {node['id']!r}: no route matched answer "
                        f"{str(answer).strip()!r} and no default configured"
                    )
                decision = {"answer": str(answer).strip(), "route": target}
            # start / end: nothing to store
        except Exception as e:
            record["node_runs"].append(
                self._node_run(node, record, "failed", ended, output,
                               model, tokens, decision)
            )
            return self._fail(workflow, record, f"node {current!r} failed: {e}")

        record["node_runs"].append(
            self._node_run(node, record, "success", ended, output,
                           model, tokens, decision)
        )

        rev = self._active_revision(record)
        if rev and node["id"] in rev["stale_nodes"]:
            rev["redone_nodes"].append(node["id"])
            if rev["mode"] != "return" and set(rev["redone_nodes"]) == set(rev["stale_nodes"]):
                self._close_revision(record)

        if node["type"] == "end":
            if record.get("parent"):
                return self._propagate_child_success(
                    workflow, record, ended, child_exports  # type: ignore[arg-type]
                )
            return self._complete(workflow, record)

        if workflow["schema_version"] == "1.0" and len(record["node_runs"]) >= workflow.get("max_steps", 50):
            return self._fail(
                workflow, record,
                f"max_steps ({workflow.get('max_steps', 50)}) exceeded; "
                f"possible infinite loop",
            )

        if node["type"] == "task" and node.get("check"):
            attempt = pending["attempt"] + 1 if pending else 1
            record["pending_check"] = {
                "node_id": node["id"], "attempt": attempt,
                "max_attempts": node["check"]["max_attempts"],
                "criteria": render(node["check"]["criteria"], record["context"]),
                "output": output, "started_at": now_iso(),
            }
            record["phase"] = "awaiting_check"
            self._append_event(record, "check_requested", node_id=node["id"],
                               attempt=attempt)
            self._save(workflow, record)
            return record

        return self._after_execution(workflow, record, node, decision)

    def _after_execution(self, workflow, record, node, decision=None):
        rev = self._active_revision(record)
        # embedded human acceptance gate
        if node["type"] == "task" and node.get("review"):
            rendered_req = render(node["review"]["requirements"], record["context"])
            routes = node["review"].get("routes", {})
            record["pending_review"] = {
                "kind": "embedded",
                "node_id": node["id"],
                "requirements": rendered_req,
                "routes": {"APPROVE": routes.get("APPROVE"),
                           "REJECT": routes.get("REJECT")},
                "attempt": self._review_attempt(record, node["id"]),
            }
            record["phase"] = "awaiting_review"
            self._append_event(
                record, "review_requested", node_id=node["id"],
                requirements=rendered_req,
            )
            self._save(workflow, record)
            return record

        # return-mode revision: target done -> jump straight back
        if rev and rev["mode"] == "return" and node["id"] == rev["target"]:
            self._close_revision(record)
            self._move_to(workflow, record, rev["return_to"])
            return record

        self._advance(workflow, record, node, decision)
        return record

    def _check_result(self, workflow, record, output):
        validate_output_size(output)
        _jsonschema_validate(output, {
            "type": "object", "required": ["verdict", "reason"],
            "additionalProperties": False,
            "properties": {
                "verdict": {"enum": ["PASS", "REVISE"]},
                "reason": {"type": "string", "minLength": 1},
            },
        })
        if not output["reason"].strip():
            raise ValueError("check reason must not be blank")
        pending = record["pending_check"]
        if pending.get("exhausted"):
            raise RuntimeError("check attempts exhausted; revise explicitly or start a new run")
        node = self._node(workflow, pending["node_id"])
        self._append_event(
            record, "check_decided", node_id=node["id"], attempt=pending["attempt"],
            verdict=output["verdict"], reason=output["reason"],
            started_at=pending["started_at"],
            duration_ms=_elapsed_between(pending["started_at"], datetime.now()),
            input={"criteria": pending["criteria"], "output": pending["output"]},
            output=output, summary=make_summary(output),
        )
        if output["verdict"] == "PASS":
            record["pending_check"] = None
            record["last_feedback"] = None
            return self._after_execution(workflow, record, node)
        record["last_feedback"] = output["reason"]
        if pending["attempt"] >= pending["max_attempts"]:
            pending["exhausted"] = True
            return self._fail(workflow, record,
                              f"check max_attempts ({pending['max_attempts']}) exhausted: "
                              f"{output['reason']}")
        self._enter_node(workflow, record)
        return record

    def _reject_output(
        self,
        workflow: Dict[str, Any],
        record: Dict[str, Any],
        node: Dict[str, Any],
        ended: datetime,
        output: Any,
        message: str,
    ) -> Dict[str, Any]:
        """Audit an output that failed output_schema, feed the reason back,
        and re-enter the same node in executing phase (run stays running)."""
        record["node_runs"].append(
            self._node_run(node, record, "rejected", ended, output,
                           None, None, None)
        )
        feedback = f"output_schema 校验未通过：{message}"
        feedback = re.sub(r"\s+", " ", feedback).strip()[:300]
        record["last_feedback"] = feedback
        record["node_started_at"] = now_iso()
        self._append_event(
            record, "output_rejected", node_id=node["id"], reason=feedback
        )
        self._save(workflow, record)
        return record

    def fail(self, workflow: Dict[str, Any], run_id: str, error: str) -> Dict[str, Any]:
        record = self._load_running(workflow, run_id)
        current = record["next_node"]
        node = self._node(workflow, current)
        ended = datetime.now()
        record["node_runs"].append(
            self._node_run(node, record, "failed", ended, None, None, None, None)
        )
        message = f"node {current!r} failed: {error}"
        if record.get("parent"):
            return self._propagate_child_failure(
                workflow, record, message
            )
        return self._fail(workflow, record, message)

    # ---- protocol: redo a single subflow instance --------------------
    def redo(
        self,
        workflow: Dict[str, Any],
        run_id: str,
        node_id: str,
        instance: str,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        record = self.storage.load_run(workflow["id"], run_id)
        node = self._safe_node(workflow, node_id)
        if node is None or node["type"] not in ("subflow", "foreach"):
            raise ValueError(f"node {node_id!r} is not subflow/foreach")
        state = record.get("subflow_state")
        if not state or state["node_id"] != node_id:
            raise RuntimeError(
                f"node {node_id!r} has no subflow runs in this run"
            )
        if state["status"] == "active":
            raise RuntimeError(
                f"node {node_id!r} is still active; finish/fail it first"
            )
        inst = self._find_instance(state, instance)
        if not inst.get("run_id"):
            raise RuntimeError(f"instance {instance!r} has no child run to redo")
        child_record = self.storage.find_run(inst["run_id"])
        child_wf = self.storage.load_workflow_for_record(child_record)

        # reopen the parent node
        record["status"] = "running"
        record["phase"] = "executing"
        record["error"] = None
        record["ended_at"] = None
        record["duration_ms"] = None
        record["next_node"] = node_id
        state["status"] = "active"
        state["returning"] = True
        state["done"] = False
        inst.update(
            status="running", error=None, started_at=now_iso(), exports={}
        )
        self._append_event(
            record, "instance_redone", node_id=node_id,
            instance=inst["key"], child_run_id=inst["run_id"],
            reason=reason,
        )
        self._save(workflow, record)

        # restart the child from its start node, same directory/run_id
        child_record["status"] = "running"
        child_record["error"] = None
        if child_wf["schema_version"] == "1.1":
            child_record["context"] = {
                **child_record["inputs"], "run": child_record["context"]["run"],
            }
            child_record["pending_check"] = None
            child_record["pending_review"] = None
            child_record["subflow_state"] = None
            child_record["ended_at"] = None
            child_record["duration_ms"] = None
        self._append_event(child_record, "run_resumed")
        self._move_to(child_wf, child_record, self.start_node_id(child_wf))
        return record

    # ---- protocol: human review verdict ------------------------------
    def review(
        self,
        workflow: Dict[str, Any],
        run_id: str,
        verdict: str,
        feedback: Optional[str] = None,
    ) -> Dict[str, Any]:
        record = self._load_running(workflow, run_id)
        if record["phase"] != "awaiting_review":
            raise RuntimeError("run is not awaiting a review verdict")
        pending = record["pending_review"]
        label = normalize_answer(verdict)
        if label not in pending["routes"]:
            raise RuntimeError(
                f"verdict {label!r} not allowed; choose from "
                f"{sorted(pending['routes'])}"
            )
        self._append_event(
            record, "review_decided", node_id=pending["node_id"],
            verdict=label, feedback=feedback,
        )

        # a standalone checkpoint inside a revision counts as redone on APPROVE
        # (embedded gates already counted their subject task at complete time)
        rev = self._active_revision(record)
        if (
            label == "APPROVE" and rev
            and pending["kind"] == "standalone"
            and pending["node_id"] in rev["stale_nodes"]
        ):
            rev["redone_nodes"].append(pending["node_id"])
            if rev["mode"] != "return" and set(rev["redone_nodes"]) == set(rev["stale_nodes"]):
                self._close_revision(record)

        routes = pending["routes"]
        subject_id = pending["node_id"]
        record["pending_review"] = None

        if label == "APPROVE":
            record["last_feedback"] = None
            target = routes["APPROVE"]
            if (rev and rev["mode"] == "return" and subject_id == rev["target"]
                    and target is None):
                self._close_revision(record)
                self._move_to(workflow, record, rev["return_to"])
                return record
            if target is None:
                # embedded gate: follow normal flow from the task node
                subject = self._node(workflow, subject_id)
                self._advance(workflow, record, subject, None)
            else:
                self._move_to(workflow, record, target)
            return record

        # REJECT
        record["last_feedback"] = feedback
        target = routes["REJECT"]
        if target is None:
            target = subject_id  # embedded default: redo the task itself
        self._move_to(workflow, record, target)
        return record

    # ---- protocol: out-of-plan revise --------------------------------
    def revise(
        self,
        workflow: Dict[str, Any],
        run_id: str,
        target: str,
        reason: str,
        mode: str = "cascade",
        scope: str = "auto",
        manual_nodes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        record = self._load_running(workflow, run_id)
        if self._active_revision(record):
            raise RuntimeError("a revision is already active; finish it first")
        node = self._node(workflow, target)
        if node["type"] != "task":
            raise ValueError(f"revise target {target!r} is not a task node")
        if not self._has_success(record, target):
            raise RuntimeError(f"task node {target!r} has not been executed yet")
        changed = set(self.produced_keys(node))
        return self._begin_revision(
            workflow, record, target=node, changed_keys=changed,
            reason=reason, mode=mode, scope=scope,
            requested_from=record["next_node"],  # type: ignore[arg-type]
            manual_nodes=manual_nodes,
        )

    def _begin_revision(
        self,
        workflow: Dict[str, Any],
        record: Dict[str, Any],
        target: Dict[str, Any],
        changed_keys: Set[str],
        reason: str,
        mode: str,
        scope: str,
        requested_from: str,
        manual_nodes: Optional[List[str]] = None,
        pre_values: Optional[Dict[str, Any]] = None,
        event_type: str = "revise_requested",
    ) -> Dict[str, Any]:
        if pre_values:
            record["context"].update(pre_values)

        if mode == "return":
            stale = {target["id"]}  # only the target redoes; everything else reused
        elif scope == "auto":
            stale = self.stale_closure(
                workflow, changed_keys, context=record["context"]
            )
        else:  # manual / all
            if scope == "all":
                stale = self._forward_reachable(workflow, target["id"])
            else:
                if not manual_nodes:
                    raise ValueError("scope=manual requires --nodes")
                id_set = {x["id"] for x in workflow["nodes"]}
                unknown = [n for n in manual_nodes if n not in id_set]
                if unknown:
                    raise ValueError(f"unknown manual nodes: {unknown}")
                stale = set(manual_nodes)
        stale.add(target["id"])

        executed = {nr["node_id"] for nr in record["node_runs"] if nr["status"] == "success"}
        forward = self._forward_reachable(workflow, target["id"])
        reused = {n for n in forward if n in executed and n not in stale}

        seq = (record["revision"]["seq"] + 1) if record.get("revision") else 1
        revision = {
            "seq": seq,
            "target": target["id"],
            "requested_from": requested_from,
            "reason": reason,
            "mode": mode,
            "scope": scope,
            "stale_nodes": sorted(stale),
            "reused_nodes": sorted(reused),
            "redone_nodes": [],
            "return_to": requested_from if mode == "return" else None,
            "status": "active",
            "started_at": now_iso(),
            "ended_at": None,
        }
        record["revision"] = revision
        event_kwargs: Dict[str, Any] = dict(
            target=target["id"], requested_from=requested_from,
            reason=reason, mode=mode, scope=scope,
            stale_nodes=revision["stale_nodes"],
            reused_nodes=revision["reused_nodes"],
        )
        if pre_values:
            event_kwargs["values"] = pre_values
        self._append_event(record, event_type, **event_kwargs)

        record["next_node"] = target["id"]
        if workflow["schema_version"] == "1.1":
            record["pending_check"] = None
            record["last_feedback"] = reason
            # Invalidated values must not survive a different branch on replay.
            for nid in stale:
                for key in self.produced_keys(self._node(workflow, nid)):
                    record["context"].pop(key, None)
        self._enter_node(workflow, record)
        return record

    # ---- internals: node entry / movement ----------------------------
    @staticmethod
    def _missing_node_inputs(node: Dict[str, Any], ctx: Dict[str, Any]) -> List[str]:
        schema = node_user_input_schema(node)
        return [k for k in schema.get("required", []) if k not in ctx]

    def _enter_node(self, workflow: Dict[str, Any], record: Dict[str, Any]) -> None:
        """Set phase after arriving at next_node; standalone review pauses here."""
        current = record["next_node"]
        node = self._node(workflow, current)
        record["node_started_at"] = now_iso()
        if workflow["schema_version"] == "1.1":
            record["activation_id"] = make_run_id()
        if workflow["schema_version"] == "1.1" and len(record["node_runs"]) >= workflow.get("max_steps", 50):
            self._fail(workflow, record, f"max_steps ({workflow.get('max_steps', 50)}) exceeded")
            return
        if (workflow["schema_version"] == "1.1"
                and node["type"] in ("task", "branch", "review")
                and not self._missing_node_inputs(node, record["context"])):
            try:
                self._materials(node, record["context"])
                render(node.get("prompt", ""), record["context"])
                render(node.get("requirements"), record["context"])
                resolve_data(node.get("output"), record["context"])
            except (KeyError, ValueError) as e:
                self._fail(workflow, record, f"node {current!r} inputs unresolved: {e}")
                return

        if node["type"] == "task":
            if self._missing_node_inputs(node, record["context"]):
                record["phase"] = "awaiting_input"
            else:
                record["phase"] = "executing"
        elif node["type"] == "review":
            requirements = render(node["requirements"], record["context"])
            record["phase"] = "awaiting_review"
            record["pending_review"] = {
                "kind": "standalone",
                "node_id": node["id"],
                "requirements": requirements,
                "routes": dict(node.get("routes", {})),
                "attempt": self._review_attempt(record, node["id"]),
            }
            self._append_event(
                record, "review_requested", node_id=node["id"],
                requirements=requirements,
            )
        elif node["type"] in ("subflow", "foreach"):
            self._save(workflow, record)
            try:
                self._activate_subflow_node(workflow, record, node)
            except (ValueError, KeyError, OSError) as e:
                self._fail(workflow, record, f"node {current!r} activation failed: {e}")
            return
        else:
            record["phase"] = "executing"
        self._save(workflow, record)

    def _advance(
        self,
        workflow: Dict[str, Any],
        record: Dict[str, Any],
        completed_node: Dict[str, Any],
        decision: Optional[Dict[str, str]],
    ) -> None:
        current = completed_node["id"]
        if completed_node["type"] == "branch":
            nxt = decision["route"]  # type: ignore[index]
        else:
            nexts = self.build_adjacency(workflow).get(current, [])
            if not nexts:
                if workflow["schema_version"] == "1.1":
                    if record.get("parent"):
                        try:
                            exports = self._collect_exports(workflow, record)
                        except Exception as e:
                            self._propagate_child_failure(workflow, record, f"exports contract unmet: {e}")
                        else:
                            self._propagate_child_success(workflow, record, datetime.now(), exports)
                    else:
                        self._complete(workflow, record)
                else:
                    self._fail(
                        workflow, record,
                        f"node {current!r} has no successor; expected an end node or edge",
                    )
                return
            nxt = nexts[0]
        self._move_to(workflow, record, nxt)

    def _move_to(self, workflow: Dict[str, Any], record: Dict[str, Any],
                 target_id: str) -> None:
        """Move toward target; during a cascade revision auto-skip reused nodes."""
        nxt = target_id
        if workflow["schema_version"] == "1.1":
            record["pending_check"] = None
        rev = self._active_revision(record)
        while rev and rev["mode"] == "cascade":
            if nxt in rev["stale_nodes"]:
                # Stale nodes always re-execute, even on a later loop
                # iteration: a chapter node revisited starts a new chapter
                # rather than reusing the old one.
                break
            prior = self._last_success_node_run(record, nxt)
            if prior is not None:
                if len(record["node_runs"]) >= workflow.get("max_steps", 50):
                    self._fail(workflow, record, "max_steps exceeded during reuse")
                    return
                record["node_runs"].append(self._skipped_run(record, prior))
                self._append_event(record, "node_reused", node_id=nxt)
                node = self._node(workflow, nxt)
                if node["type"] == "branch":
                    nxt = prior["decision"]["route"]
                    continue
                nexts = self.build_adjacency(workflow).get(nxt, [])
                if not nexts:
                    self._advance(workflow, record, node, None)
                    return
                nxt = nexts[0]
                continue
            break  # reached new territory: execute normally

        self._maybe_close_cascade(workflow, record, nxt)
        record["next_node"] = nxt
        self._enter_node(workflow, record)

    def _close_revision(self, record: Dict[str, Any]) -> None:
        rev = record["revision"]
        rev["status"] = "completed"
        rev["ended_at"] = now_iso()
        self._append_event(
            record, "revision_completed", target=rev["target"],
            redone_nodes=rev["redone_nodes"], seq=rev["seq"],
        )

    def _maybe_close_cascade(
        self, workflow: Dict[str, Any], record: Dict[str, Any], nxt: str
    ) -> None:
        """Close a cascade revision once the live path can no longer reach
        any un-redone stale node.

        stale_closure is a static over-approximation: it includes nodes
        behind branch routes the actual path never takes (e.g. a fix node
        reachable only when a check answers REVISE). Such nodes will never
        be redone, so the revision ends when execution leaves the stale
        region instead of waiting for the full static set.
        """
        rev = self._active_revision(record)
        if not rev or rev["mode"] != "cascade":
            return
        remaining = set(rev["stale_nodes"]) - set(rev["redone_nodes"])
        reachable = self._forward_reachable(workflow, nxt) | {nxt}
        if not (remaining & reachable):
            self._close_revision(record)

    # ---- internals: audit --------------------------------------------
    def _load_running(self, workflow: Dict[str, Any], run_id: str) -> Dict[str, Any]:
        record = self.storage.load_run(workflow["id"], run_id)
        if record["status"] != "running":
            raise RuntimeError(
                f"run {run_id} is {record['status']}, not running; "
                f"resume it before reporting"
            )
        if not record.get("next_node"):
            raise RuntimeError(f"run {run_id} has no current node")
        return record

    @staticmethod
    def _has_success(record: Dict[str, Any], node_id: str) -> bool:
        return any(
            nr["node_id"] == node_id and nr["status"] == "success"
            for nr in record["node_runs"]
        )

    @staticmethod
    def _last_success_node_run(record: Dict[str, Any], node_id: str):
        for nr in reversed(record["node_runs"]):
            if nr["node_id"] == node_id and nr["status"] == "success":
                return nr
        return None

    @staticmethod
    def _last_output(record: Dict[str, Any], node_id: str) -> Any:
        nr = WorkflowEngine._last_success_node_run(record, node_id)
        return nr["output"] if nr else None

    @staticmethod
    def _active_revision(record: Dict[str, Any]):
        rev = record.get("revision")
        return rev if rev and rev["status"] == "active" else None

    @staticmethod
    def _review_attempt(record: Dict[str, Any], node_id: str) -> int:
        return 1 + sum(
            1
            for e in record["events"]
            if e["type"] == "review_requested" and e.get("node_id") == node_id
        )

    def _node_run(
        self,
        node: Dict[str, Any],
        record: Dict[str, Any],
        status: str,
        ended: datetime,
        output: Any,
        model: Optional[str],
        tokens: Optional[Dict[str, int]],
        decision: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        rev = self._active_revision(record)
        return {
            "node_id": node["id"],
            "node_name": node.get("name", node["id"]),
            "type": node["type"],
            "status": status,
            "started_at": record.get("node_started_at") or now_iso(),
            "ended_at": ended.isoformat(timespec="seconds"),
            "duration_ms": _elapsed_between(record.get("node_started_at"), ended),
            "model": model,
            "tokens": tokens,
            "decision": decision,
            "feedback": record.get("last_feedback"),
            "revision_seq": rev["seq"] if rev else None,
            "input": self._audited_input_for(record, node),
            "summary": make_summary(output),
            "output": output,
        }

    def _skipped_run(self, record: Dict[str, Any], prior: Dict[str, Any]) -> Dict[str, Any]:
        rev = self._active_revision(record)
        ended = datetime.now()
        return {
            "node_id": prior["node_id"],
            "node_name": prior["node_name"],
            "type": prior["type"],
            "status": "skipped",
            "started_at": now_iso(),
            "ended_at": ended.isoformat(timespec="seconds"),
            "duration_ms": 0,
            "model": prior.get("model"),
            "tokens": None,
            "decision": prior.get("decision"),
            "feedback": None,
            "revision_seq": rev["seq"] if rev else None,
            "input": None,
            "summary": "reused previous output",
            "output": prior["output"],
        }

    def _audited_input_for(self, record: Dict[str, Any], node: Dict[str, Any]) -> Any:
        ntype = node["type"]
        if ntype == "task":
            result = {"prompt": render(node.get("prompt", ""), record["context"])}
            if node.get("inputs"):
                result["inputs"] = self._materials(node, record["context"])
            return result
        if ntype == "branch":
            return {"prompt": render(node.get("prompt", ""), record["context"])}
        if ntype in ("subflow", "foreach"):
            # raw mapping: per-instance rendering lives in each child's inputs
            return {
                "workflow": node.get("workflow"),
                "inline": bool(node.get("nodes")),
                "inputs": node.get("inputs", {}),
            }
        return None

    @staticmethod
    def _append_event(record: Dict[str, Any], etype: str, **kwargs) -> None:
        event = {"type": etype, "at": now_iso()}
        for k, v in kwargs.items():
            if v is not None:
                event[k] = v
        record["events"].append(event)

    def _save(self, workflow: Dict[str, Any], record: Dict[str, Any]) -> None:
        validate_run_record(record)
        context_md = self._render_context_md(workflow, record)
        self.storage.save_run(record)
        self.storage.save_context_doc(record, context_md)

    @staticmethod
    def _elapsed_total(record: Dict[str, Any], ended: datetime) -> Optional[int]:
        try:
            started = datetime.fromisoformat(record["started_at"])
            return int((ended - started).total_seconds() * 1000)
        except (KeyError, ValueError):
            return None

    def _complete(self, workflow: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
        ended = datetime.now()
        # safety net: a finished run must not carry an active revision
        if self._active_revision(record):
            self._close_revision(record)
        record.update(
            status="completed",
            phase="executing",
            ended_at=ended.isoformat(timespec="seconds"),
            duration_ms=self._elapsed_total(record, ended),
            next_node=None,
            node_started_at=None,
            pending_review=None,
        )
        self._save(workflow, record)
        return record

    def _fail(self, workflow: Dict[str, Any], record: Dict[str, Any],
              message: str) -> Dict[str, Any]:
        ended = datetime.now()
        record.update(
            status="failed",
            ended_at=ended.isoformat(timespec="seconds"),
            duration_ms=self._elapsed_total(record, ended),
            error=message,
        )
        self._append_event(record, "run_failed", reason=message)
        self._save(workflow, record)
        return record

    # ---- internals: context.md sharing hub ---------------------------
    def _safe_node(self, workflow: Dict[str, Any],
                   node_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if node_id is None:
            return None
        return next((n for n in workflow["nodes"] if n["id"] == node_id), None)

    @staticmethod
    def _inline(value: Any, limit: int = 120) -> str:
        path = file_ref_path(value)
        if path is not None:
            return f"文件 {path}"
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        text = re.sub(r"\s+", " ", text).strip()
        return text if len(text) <= limit else text[:limit] + "…"

    def _md_kv_bullets(self, obj: Dict[str, Any]) -> List[str]:
        if not obj:
            return ["- （无）"]
        return [f"- **{k}**: {self._inline(v)}" for k, v in obj.items()]

    def _collect_artifacts(
        self,
        record: Dict[str, Any],
        seen: Set[str],
        depth: int = 0,
        run_dir: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        """All (path, origin) $file artifacts of a record: direct/nested in
        node outputs, the foreach items source, and every child run's outputs.

        Child records are addressed by derived path (children/<node>/<inst>) and
        never looked up by scanning the run tree, so rendering stays linear in
        the number of instances instead of quadratic.
        """
        found: List[Tuple[str, str]] = []

        def scan(value: Any, origin: str) -> None:
            if is_file_ref(value):
                path = value[FILE_REF_KEY]
                if path not in seen:
                    seen.add(path)
                    found.append((path, origin))
            if isinstance(value, dict):
                for v in value.values():
                    scan(v, origin)
            elif isinstance(value, list):
                for v in value:
                    scan(v, origin)

        for nr in record.get("node_runs", []):
            scan(nr.get("output"), f"{nr['node_id']}（{nr.get('node_name', '')}）")

        state = record.get("subflow_state")
        if state:
            src = state.get("items_source")
            if isinstance(src, dict):
                origin = f"{state['node_id']} items 源"
                if "$file" in src:
                    scan({FILE_REF_KEY: src["$file"]}, origin)
                elif "var" in src or "ref" in src:
                    try:
                        scan(resolve_dotted(record["context"], src.get("ref", src.get("var"))), origin)
                    except KeyError:
                        pass
            if depth < MAX_SUBFLOW_DEPTH:
                base = run_dir or self.storage.run_dir_for_record(record)
                for inst in state["instances"]:
                    rid = inst.get("run_id")
                    if not rid:
                        continue
                    child_dir = os.path.join(
                        base, "children", state["node_id"], inst["key"]
                    )
                    child = self._read_record_at(child_dir)
                    if child is None:
                        continue
                    origin = f"子运行 {rid}（{state['node_id']}/{inst['key']}）"
                    for path, _origin in self._collect_artifacts(
                        child, seen, depth + 1, child_dir
                    ):
                        found.append((path, origin))
        return found

    def _read_record_at(self, run_dir: str) -> Optional[Dict[str, Any]]:
        """Read a run record by its directory; None when absent or unreadable."""
        path = os.path.join(run_dir, "record.json")
        if not os.path.isfile(path):
            return None
        try:
            return self.storage.load_record(path)
        except (OSError, ValueError):
            return None

    def _render_context_md(self, workflow: Dict[str, Any],
                           record: Dict[str, Any]) -> str:
        wf_name = workflow.get("name", record["workflow_id"])
        lines: List[str] = [f"# 运行上下文 · {wf_name}", ""]
        lines += [
            f"- run_id: `{record['run_id']}`",
            f"- 工作流: `{record['workflow_id']}`",
        ]

        status = f"**{record['status']}**"
        if record["status"] == "running":
            status += f" / phase `{record['phase']}`"
        lines.append(f"- 状态: {status}")

        cur = record.get("next_node")
        if cur is not None:
            cur_node = self._safe_node(workflow, cur)
            cur_name = cur_node.get("name", "") if cur_node else ""
            lines.append(f"- 当前节点: `{cur}` {cur_name}".rstrip())
        elif record["status"] == "completed":
            lines.append("- 当前节点: 无（已完成）")

        lines.append(f"- 开始时间: {record['started_at']}")
        if record.get("ended_at"):
            lines.append(f"- 结束时间: {record['ended_at']}")
        if record.get("duration_ms") is not None:
            lines.append(f"- 总耗时: {record['duration_ms']} ms")
        if record.get("error"):
            lines.append(f"- 错误: {record['error']}")
        lines.append("")

        lines.append("## 输入")
        lines += self._md_kv_bullets(record.get("inputs", {}))
        lines.append("")

        lines.append("## 共享上下文")
        lines += self._md_kv_bullets(record.get("context", {}))
        lines.append("")

        lines.append("## 产物索引")
        artifacts = self._collect_artifacts(record, set())
        lines += (
            [f"- `{path}` ← {origin}" for path, origin in artifacts]
            or ["- （暂无外置产物）"]
        )
        lines.append("")

        state = record.get("subflow_state")
        if state:
            lines.append("## 子工作流")
            tag = "内联" if state["inline"] else "外引"
            lines.append(
                f"- 节点 `{state['node_id']}` → `{state['subflow_id']}`"
                f"（{tag}，{state['status']}）"
            )
            if state["kind"] == "foreach":
                done_n = sum(
                    1 for i in state["instances"] if i["status"] == "success"
                )
                lines.append(
                    f"- 进度 {done_n}/{len(state['instances'])}"
                    f"，模式 {state['items_mode']}"
                    f"，on_fail {state['on_fail']}"
                )
            for inst in state["instances"]:
                row = (
                    f"  - [{inst['status']}] `{inst['key']}`"
                    f" run `{inst.get('run_id')}`"
                )
                if inst.get("error"):
                    row += f" — {inst['error']}"
                lines.append(row)
            lines.append("")

        return "\n".join(lines) + "\n"
