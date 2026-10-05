"""Semantic progress view: derive business stages from a run record.

Deliberately not part of the run state machine. A definition may declare, once,
how its nodes map onto the stages a human actually cares about::

    "view": {
      "stages": [
        {"name": "确定目标",   "nodes": ["prepare"]},
        {"name": "实施改动",   "nodes": ["implement"], "detail": "${node.implement.summary}"},
        {"name": "人工验收",   "nodes": ["implement"], "gate": true},
        {"name": "独立复检",   "nodes": ["audit"]},
        {"name": "收口与回写", "nodes": ["finalize"]}
      ],
      "items": {"label": "${item.name}"}
    }

Two properties matter:

* the view is **derived on every read and never stored**, so it cannot drift
  from the record the way a hand-maintained status field would;
* stages that have **not run yet** are still listed -- which is what answers
  "what is still ahead", the one question a plain node stream cannot answer.

``detail`` is a template over a **closed** placeholder set (:data:`NOTE_ROOTS`,
:data:`LOOP_FIELDS`). Arithmetic stays in the engine: a template may
concatenate ``${loop.left}`` but can never compute it. That is what keeps the
whole thing statically checkable without shipping an expression evaluator.
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

from .engine import action_digest

MISSING = "—"

STAGE_STATUSES = ("done", "active", "waiting", "failed", "pending")

#: Placeholder roots a definition may use. ``item`` is only legal inside
#: ``view.items.label`` -- a stage detail has no single item to point at.
NOTE_ROOTS = ("node", "loop", "item", "phase", "status", "run")

#: Fields the engine precomputes for a foreach stage. Deliberately scalars:
#: the template concatenates them, it does not do arithmetic on them.
LOOP_FIELDS = (
    "done", "total", "left", "current", "current_index",
    "current_label", "current_stage", "current_action",
)

#: Fields of a run record worth exposing as ``${run.<field>}``.
RUN_FIELDS = (
    "run_id", "workflow_id", "status", "phase",
    "started_at", "ended_at", "duration_ms",
)

PLACEHOLDER_RE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)*)\}"
)

_STATUS_MARK = {
    "done": "ok", "active": ">>", "waiting": "wait",
    "failed": "ERR", "pending": "",
}


def mark(status: str) -> str:
    """Fixed-width tag used by the text renderer."""
    return _STATUS_MARK.get(status, "?")


# ---- placeholders --------------------------------------------------------
def placeholders(template: str) -> List[str]:
    """Every ``${...}`` key in a template, in order of appearance."""
    return PLACEHOLDER_RE.findall(template or "")


def interpolate(template: str, ctx: Dict[str, Any]) -> str:
    """Substitute placeholders; an unresolvable one renders as :data:`MISSING`
    rather than raising -- a view must never break an inspection command."""
    return PLACEHOLDER_RE.sub(lambda m: _fmt(_resolve(m.group(1), ctx)), template)


def _resolve(key: str, ctx: Dict[str, Any]):
    root, _, rest = key.partition(".")
    if root == "node":
        node_id, _, path = rest.partition(".")
        base = ctx.get("node", {}).get(node_id)
        return _dotted(base, path) if path else base
    if root in ("loop", "item"):
        if not rest:
            return ctx.get(root)
        return _dotted(ctx.get(root), rest)
    return ctx.get(root)


def _dotted(value: Any, path: str):
    cur = value
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _fmt(value: Any) -> str:
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ---- record readers ------------------------------------------------------
def _node_context(record: Dict[str, Any]) -> Dict[str, Any]:
    """node_id -> latest successful output, with a ``summary`` always present.

    ``output["summary"]`` wins when the node reported one (that is the agent's
    own words); otherwise the engine's 160-char dump is kept as the fallback.
    """
    ctx: Dict[str, Any] = {}
    for nr in record.get("node_runs") or []:
        if nr.get("status") not in ("success", "skipped"):
            continue
        out = nr.get("output")
        value = dict(out) if isinstance(out, dict) else {"value": out}
        if not (isinstance(value.get("summary"), str) and value["summary"].strip()):
            value["summary"] = nr.get("summary") or ""
        ctx[nr["node_id"]] = value
    return ctx


def _item_at(state: Dict[str, Any], inst: Dict[str, Any]) -> Any:
    """The queue item behind one instance, or None when the snapshot is gone."""
    path = state.get("items_snapshot")
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return None
    idx = inst.get("index")
    if isinstance(idx, int) and 0 <= idx < len(items):
        return items[idx]
    return None


def _child_stage(
    record: Dict[str, Any], node_id: str, inst: Dict[str, Any],
    storage: Any,
) -> Optional[Dict[str, Any]]:
    """Where one foreach instance currently stands: its business stage name,
    the phase it is stuck in and, when it is a gate, what decides it."""
    if storage is None:
        return None
    try:
        path = os.path.join(
            storage.child_dir(record, node_id, inst["key"]), "record.json"
        )
        if not os.path.isfile(path):
            return None
        child = storage.load_record(path)
        child_wf = storage.load_workflow_for_record(child)
    except Exception:
        # A view is an inspection aid; a missing or unreadable child must not
        # turn `runs show` into a stack trace.
        return None
    return {
        "stage": _child_stage_name(child, child_wf),
        "phase": child.get("phase"),
        "status": child.get("status"),
        "action": _action_for(child, child_wf),
    }


def _child_stage_name(child: Dict[str, Any], child_wf: Any) -> Optional[str]:
    stages = ((child_wf or {}).get("view") or {}).get("stages") or []
    cur = child.get("next_node")
    if child.get("status") == "completed":
        if stages:
            return stages[-1]["name"]
        return _last_node_name(child)
    for stage in stages:
        if cur in (stage.get("nodes") or []):
            return stage["name"]
    return _node_name(child_wf, cur) or _last_node_name(child)


def _node_name(workflow: Any, node_id: Any) -> Optional[str]:
    for node in (workflow or {}).get("nodes") or []:
        if node["id"] == node_id:
            return node.get("name", node["id"])
    return None


def _last_node_name(child: Dict[str, Any]) -> Optional[str]:
    runs = child.get("node_runs") or []
    return runs[-1].get("node_name") if runs else None


def _action_for(
    record: Dict[str, Any], workflow: Any
) -> Optional[Dict[str, Any]]:
    """The gate a run is sitting on, carrying everything a command needs.

    The `id` is derived exactly the way the engine derives it, so the command
    the view prints is one the engine still accepts. A command missing it is
    rejected as stale -- worse than printing none at all.
    """
    kind = {
        "awaiting_review": "review",
        "awaiting_input": "provide",
        "awaiting_check": "check",
        "awaiting_items": "resume",
    }.get(str(record.get("phase")))
    if not kind or not record.get("run_id"):
        return None
    return {
        "kind": kind,
        "run_id": record["run_id"],
        "workflow_id": record.get("workflow_id"),
        "id": action_digest(record, workflow or {}),
    }


def _waiting_on_children(record: Dict[str, Any]) -> bool:
    """True when the run is blocked on its queue rather than on a gate of its
    own: a parent's phase just mirrors its active child, so a command built
    from the parent's id would be rejected. The child owns that action."""
    state = record.get("subflow_state") or {}
    return (
        state.get("kind") == "foreach"
        and state.get("status") == "active"
        and state.get("node_id") == record.get("next_node")
    )


def _current_instance(instances: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The item the queue is on: the running one, else the next pending one.

    Both the stage detail and the queue rows use this, so they can never
    disagree about which item is "current".
    """
    return (
        next((i for i in instances if i.get("status") == "running"), None)
        or next((i for i in instances if i.get("status") == "pending"), None)
    )


def _loop_context(
    state: Dict[str, Any], record: Dict[str, Any], spec: Dict[str, Any],
    storage: Any,
) -> Optional[Dict[str, Any]]:
    """Progress of the active foreach queue, or None for a single subflow call.

    Only the *current* instance gets its child record read: that is the one
    whose stage and gate a human needs, and it keeps rendering O(1) no matter
    how long the queue is.
    """
    if not state or state.get("kind") != "foreach":
        return None
    instances = state.get("instances") or []
    total = len(instances)
    done = sum(1 for i in instances if i.get("status") == "success")
    current = _current_instance(instances)
    label_tpl = ((spec.get("items") or {}).get("label")) or ""
    loop: Dict[str, Any] = {
        "node": state.get("node_id"),
        "done": done,
        "total": total,
        "left": total - done,
        "current": current.get("key") if current else None,
        "current_index": current.get("index") if current else None,
        "current_label": None,
        "current_stage": None,
        "current_action": None,
    }
    if current is not None and label_tpl:
        item = _item_at(state, current)
        if item is not None:
            loop["current_label"] = interpolate(label_tpl, {"item": item})
    if current is not None:
        child = _child_stage(record, state["node_id"], current, storage)
        if child:
            loop["current_stage"] = child.get("stage")
            loop["current_action"] = child.get("action")
    return loop


def _instance_rows(
    state: Dict[str, Any], spec: Dict[str, Any], record: Dict[str, Any],
    storage: Any,
) -> List[Dict[str, Any]]:
    """One row per queue item -- the "what has been done / what is left" list."""
    label_tpl = ((spec.get("items") or {}).get("label")) or ""
    current = _current_instance(state.get("instances") or [])
    current_key = current.get("key") if current else None
    rows = []
    for inst in state.get("instances") or []:
        label = None
        if label_tpl:
            item = _item_at(state, inst)
            if item is not None:
                label = interpolate(label_tpl, {"item": item})
        rows.append({
            "key": inst.get("key"),
            "index": inst.get("index"),
            "status": inst.get("status"),
            "label": label or inst.get("key"),
            "current": inst.get("key") == current_key,
        })
    return rows


# ---- stage derivation ----------------------------------------------------
def stage_status(
    stage: Dict[str, Any], record: Dict[str, Any], order: Dict[str, int]
) -> str:
    """Derive one stage's state from facts the record already holds.

    Order of judgement: a failure anywhere in the stage wins; then a gate the
    run is currently sitting on; then "every mapped node succeeded"; then
    wherever the run currently stands; then the definition's node order
    decides between in-flight and not-yet-reached.
    """
    target = set(stage["nodes"])
    runs = [
        nr for nr in (record.get("node_runs") or [])
        if nr.get("node_id") in target
    ]
    if any(nr.get("status") == "failed" for nr in runs):
        return "failed"

    cur = record.get("next_node")
    awaiting = str(record.get("phase") or "").startswith("awaiting")
    # A gate the run is sitting on is not done, even though the node feeding it
    # already produced its output: `assemble` has succeeded, but nobody has
    # ruled on it yet. Judging "every node succeeded" first would hide exactly
    # the state a human is being asked to act on.
    if record.get("status") == "running" and awaiting and cur in target:
        return "waiting"

    succeeded = {nr["node_id"] for nr in runs if nr.get("status") == "success"}
    if succeeded >= target:
        return "done"

    if record.get("status") == "running":
        if cur in target:
            return "active"
        if succeeded:
            return "active"
        pos, first = order.get(cur), order.get(stage["nodes"][0])
        if pos is not None and first is not None and pos > first:
            return "active"
    return "pending"


def _default_detail(
    stage: Dict[str, Any], loop: Optional[Dict[str, Any]],
    nodes_ctx: Dict[str, Any],
) -> str:
    """Rendering used when the definition declares no ``detail``."""
    if loop:
        return loop_line(loop)
    for node_id in reversed(stage["nodes"]):
        value = nodes_ctx.get(node_id)
        if isinstance(value, dict):
            text = value.get("summary")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def loop_line(loop: Dict[str, Any]) -> str:
    """The default business sentence for a foreach stage."""
    line = f"已完成 {loop['done']}/{loop['total']}"
    if not loop["left"]:
        return line + " · 全部完成"
    current = loop.get("current_label") or loop.get("current")
    if current:
        line += f" · 当前 {current}"
        if loop.get("current_stage"):
            line += f"（{loop['current_stage']}）"
    return line + f" · 还剩 {loop['left']}"


def derive(
    workflow: Optional[Dict[str, Any]], record: Dict[str, Any],
    storage: Any = None,
) -> Optional[Dict[str, Any]]:
    """The whole view, or None when the definition declares none."""
    spec = ((workflow or {}).get("view") or {})
    stages_spec = spec.get("stages") or []
    if not stages_spec:
        return None

    order = {n["id"]: i for i, n in enumerate(workflow.get("nodes") or [])}
    nodes_ctx = _node_context(record)
    state = record.get("subflow_state") or {}
    loop = _loop_context(state, record, spec, storage)

    stages = []
    for stage in stages_spec:
        owned = loop if loop and loop["node"] in stage.get("nodes", []) else None
        ctx = {
            "node": nodes_ctx,
            "loop": owned,
            "phase": record.get("phase"),
            "status": record.get("status"),
            "run": record,
        }
        status = stage_status(stage, record, order)
        action = None
        if owned and owned.get("current_action"):
            # A gate nested inside the queue is still this stage's business.
            status, action = "waiting", owned["current_action"]
        elif status == "waiting" and not _waiting_on_children(record):
            action = _action_for(record, workflow)
        detail = (
            interpolate(stage["detail"], ctx) if stage.get("detail")
            else _default_detail(stage, owned, nodes_ctx)
        )
        entry: Dict[str, Any] = {
            "name": stage["name"],
            "nodes": list(stage["nodes"]),
            "status": status,
            "detail": detail,
            "gate": bool(stage.get("gate")),
            "action": action,
        }
        if owned:
            # The queue is *part of* the stage it feeds. Rendering it as a
            # sibling block made one process read as two unrelated things.
            entry["queue"] = dict(
                owned, items=_instance_rows(state, spec, record, storage)
            )
        stages.append(entry)

    return {
        "run_id": record.get("run_id"),
        "workflow_id": record.get("workflow_id"),
        "status": record.get("status"),
        "phase": record.get("phase"),
        "stages": stages,
    }
