"""Workflow state machine.

The engine never calls an LLM and never executes task logic. It is driven by
an external AI agent (and human reviewers) via this protocol:

    start()    validate, create a run record, enter the start node
    spec()     render the current node info:
                 executing      -> task/branch/start/end node spec
                 awaiting_input -> questions from a task node's input_schema
                 awaiting_review-> acceptance requirements for a human
    provide()  agent supplies user answers for a node's input_schema;
               changing an already-completed node's input auto-opens a revision
    complete() agent reports a task/branch output -> audit, context, routing
    review()   human verdict APPROVE/REJECT on an acceptance gate
    revise()   out-of-plan jump back to a prior task node; the dependency graph
               closure decides which downstream nodes are redone/reused
    fail()     agent reports a node failure -> run failed
    resume()   reopen a failed run

Persistence is the JSON run record, rewritten after every transition.
"""
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from .schema import (
    _jsonschema_validate,
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
TEMPLATE_RE = re.compile(r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def make_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + os.urandom(3).hex()


def is_file_ref(value: Any) -> bool:
    return isinstance(value, dict) and FILE_REF_KEY in value


def file_ref_path(value: Any) -> Optional[str]:
    return value[FILE_REF_KEY] if is_file_ref(value) else None


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


def render(value: Any, context: Dict[str, Any]) -> Any:
    """Substitute ${var} in strings; recurse into dict/list.

    Non-string context values are JSON-encoded (file refs render as paths);
    a missing key is a hard error.
    """
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            key = m.group(1)
            if key not in context:
                raise KeyError(f"template variable ${{{key}}} not found in workflow context")
            return _stringify_context_value(context[key])
        return TEMPLATE_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    return value


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
        return []

    def dependency_graph(
        self, workflow: Dict[str, Any]
    ) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
        """Return (node -> referenced context keys, context key -> consuming nodes)."""
        refs: Dict[str, Set[str]] = {}
        consumers: Dict[str, Set[str]] = {}
        for node in workflow["nodes"]:
            nrefs: Set[str] = set()
            if node["type"] in ("task", "branch"):
                nrefs.update(template_refs(node.get("prompt", "")))
            if node["type"] == "task" and node.get("review"):
                nrefs.update(template_refs(node["review"].get("requirements")))
            if node["type"] == "review":
                nrefs.update(template_refs(node.get("requirements")))
            refs[node["id"]] = nrefs
            for key in nrefs:
                consumers.setdefault(key, set()).add(node["id"])
        return refs, consumers

    def stale_closure(self, workflow: Dict[str, Any], changed_keys: Set[str]) -> Set[str]:
        """Nodes whose output may change when changed_keys change (transitive)."""
        _, consumers = self.dependency_graph(workflow)
        affected: Set[str] = set()
        pending = set(changed_keys)
        while pending:
            key = pending.pop()
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

    # ---- protocol: start / resume ------------------------------------
    def start(
        self,
        workflow: Dict[str, Any],
        inputs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        validate_workflow(workflow)
        inputs = inputs or {}
        validate_inputs(workflow, inputs)

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
        }
        self._append_event(record, "run_started")
        self._enter_node(workflow, record)
        return record

    def resume(self, workflow: Dict[str, Any], run_id: str) -> Dict[str, Any]:
        validate_workflow(workflow)
        record = self.storage.load_run(workflow["id"], run_id)
        if record["status"] == "completed":
            raise RuntimeError(f"run {run_id} is already completed")
        if not record.get("next_node"):
            raise RuntimeError(f"run {run_id} has no next_node to resume from")
        record["status"] = "running"
        record["error"] = None
        self._append_event(record, "run_resumed")
        # phase/pending state already describes the node; only reset its timer
        record["node_started_at"] = now_iso()
        self._save(workflow, record)
        return record

    # ---- protocol: node spec -----------------------------------------
    def node_spec(self, workflow: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
        envelope: Dict[str, Any] = {
            "run_id": record["run_id"],
            "workflow_id": workflow["id"],
            "status": record["status"],
            "phase": record["phase"],
            "revision": self._revision_brief(record),
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
            envelope["review_request"] = record["pending_review"]
            envelope["node"] = self._basic_node(node, record, workflow, include_outputs=False)
            return envelope

        envelope["node"] = self._basic_node(node, record, workflow)
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
                previous_output=self._last_output(record, node["id"]),
                feedback=record.get("last_feedback"),
                under_revision=bool(self._active_revision(record)),
            )
        elif ntype == "branch":
            spec.update(
                model_hint=node.get("model") or workflow.get("default_model"),
                prompt=render(node.get("prompt", ""), ctx),
                routes=node.get("routes", {}),
                default=node.get("default"),
            )
        # start / end: id/type/name only
        return spec

    def _input_request(self, node: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        schema = node["input_schema"]
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
            _jsonschema_validate(merged, node["input_schema"])
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
        _jsonschema_validate({**record["context"], **values}, node["input_schema"])
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
        if record["phase"] != "executing":
            raise RuntimeError(
                f"run is in phase {record['phase']!r}, not accepting task output"
            )
        validate_output_size(output)
        current = record["next_node"]  # type: ignore[assignment]
        node = self._node(workflow, current)
        ended = datetime.now()

        decision: Optional[Dict[str, str]] = None
        try:
            ntype = node["type"]
            if ntype == "task":
                record["context"][node.get("output_key", node["id"])] = output
            elif ntype == "branch":
                routes = {normalize_answer(k): v for k, v in node.get("routes", {}).items()}
                target = routes.get(normalize_answer(output))
                if not target:
                    target = node.get("default")
                if not target:
                    raise RuntimeError(
                        f"branch node {node['id']!r}: no route matched answer "
                        f"{str(output).strip()!r} and no default configured"
                    )
                decision = {"answer": str(output).strip(), "route": target}
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
            return self._complete(workflow, record)

        if len(record["node_runs"]) >= workflow.get("max_steps", 50):
            return self._fail(
                workflow, record,
                f"max_steps ({workflow.get('max_steps', 50)}) exceeded; "
                f"possible infinite loop",
            )

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

    def fail(self, workflow: Dict[str, Any], run_id: str, error: str) -> Dict[str, Any]:
        record = self._load_running(workflow, run_id)
        current = record["next_node"]
        node = self._node(workflow, current)
        ended = datetime.now()
        record["node_runs"].append(
            self._node_run(node, record, "failed", ended, None, None, None, None)
        )
        return self._fail(workflow, record, f"node {current!r} failed: {error}")

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
            stale = self.stale_closure(workflow, changed_keys)
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
        self._enter_node(workflow, record)
        return record

    # ---- internals: node entry / movement ----------------------------
    @staticmethod
    def _missing_node_inputs(node: Dict[str, Any], ctx: Dict[str, Any]) -> List[str]:
        schema = node.get("input_schema") or {}
        return [k for k in schema.get("required", []) if k not in ctx]

    def _enter_node(self, workflow: Dict[str, Any], record: Dict[str, Any]) -> None:
        """Set phase after arriving at next_node; standalone review pauses here."""
        current = record["next_node"]
        node = self._node(workflow, current)
        record["node_started_at"] = now_iso()

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
        rev = self._active_revision(record)
        while rev and rev["mode"] == "cascade":
            if nxt in rev["stale_nodes"] and nxt not in rev["redone_nodes"]:
                break  # must redo this one
            prior = self._last_success_node_run(record, nxt)
            if prior is not None:
                record["node_runs"].append(self._skipped_run(record, prior))
                self._append_event(record, "node_reused", node_id=nxt)
                node = self._node(workflow, nxt)
                if node["type"] == "branch":
                    nxt = prior["decision"]["route"]
                    continue
                nexts = self.build_adjacency(workflow).get(nxt, [])
                if not nexts:
                    self._fail(workflow, record,
                               f"reused node {nxt!r} has no successor")
                    return
                nxt = nexts[0]
                continue
            break  # reached new territory: execute normally

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
            return {"prompt": render(node.get("prompt", ""), record["context"])}
        if ntype == "branch":
            return {"prompt": render(node.get("prompt", ""), record["context"])}
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
        self.storage.save_context_doc(
            record["workflow_id"], record["run_id"], context_md
        )

    @staticmethod
    def _elapsed_total(record: Dict[str, Any], ended: datetime) -> Optional[int]:
        try:
            started = datetime.fromisoformat(record["started_at"])
            return int((ended - started).total_seconds() * 1000)
        except (KeyError, ValueError):
            return None

    def _complete(self, workflow: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
        ended = datetime.now()
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
        seen: Set[str] = set()
        artifacts = []
        for nr in record.get("node_runs", []):
            path = file_ref_path(nr.get("output"))
            if path and path not in seen:
                seen.add(path)
                artifacts.append(
                    f"- `{path}` ← {nr['node_id']}（{nr.get('node_name', '')}）"
                )
        lines += artifacts if artifacts else ["- （暂无外置产物）"]

        return "\n".join(lines) + "\n"
