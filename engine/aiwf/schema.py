"""Schema loading and validation. jsonschema is imported lazily so that
lightweight usage needs no heavy deps.
"""
import json
import os
from typing import Any, Dict

def _default_schema_dir() -> str:
    # engine/aiwf/schema.py -> <repo>/schemas
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, "schemas")


SCHEMA_DIR = os.getenv("AIWF_SCHEMAS_DIR", _default_schema_dir())


def load_schema(name: str) -> Dict[str, Any]:
    path = os.path.join(SCHEMA_DIR, f"{name}.schema.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _jsonschema_validate(instance: Any, schema: Dict[str, Any]):
    try:
        import jsonschema
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "jsonschema is required for validation; install with `pip install jsonschema`"
        ) from e
    jsonschema.validate(instance=instance, schema=schema)


def validate_workflow(workflow: Dict[str, Any]) -> None:
    """Structural validation against workflow.schema.json + semantic checks."""
    _jsonschema_validate(workflow, load_schema("workflow"))

    node_ids = [n["id"] for n in workflow["nodes"]]
    dup = {i for i in node_ids if node_ids.count(i) > 1}
    if dup:
        raise ValueError(f"duplicate node ids: {sorted(dup)}")

    id_set = set(node_ids)
    starts = [n["id"] for n in workflow["nodes"] if n["type"] == "start"]
    if len(starts) > 1:
        raise ValueError(f"at most one start node allowed, found: {starts}")

    # edge endpoints must exist
    out_counts: Dict[str, int] = {}
    for edge in workflow.get("edges", []):
        for end in ("from", "to"):
            if edge[end] not in id_set:
                raise ValueError(
                    f"edge {edge['from']} -> {edge['to']} references unknown node: {edge[end]}"
                )
        out_counts[edge["from"]] = out_counts.get(edge["from"], 0) + 1

    for node in workflow["nodes"]:
        n_out = out_counts.get(node["id"], 0)

        # routing nodes (branch / standalone review) must not use edges
        if node["type"] in ("branch", "review"):
            if n_out:
                raise ValueError(
                    f"{node['type']} node {node['id']!r} must not have outgoing edges; "
                    f"routing is defined by routes/default"
                )
            targets = list(node.get("routes", {}).values())
            if node["type"] == "branch" and "default" in node:
                targets.append(node["default"])
            if node["type"] == "review" and not node.get("routes", {}).get("APPROVE"):
                raise ValueError(
                    f"review node {node['id']!r} must explicitly route APPROVE"
                )
        else:
            if n_out > 1:
                raise ValueError(
                    f"node {node['id']!r} has {n_out} outgoing edges; "
                    f"conditional routing requires a branch node"
                )
            # embedded review routes
            review = node.get("review")
            targets = list((review or {}).get("routes", {}).values())

        for t in targets:
            if t is not None and t not in id_set:
                raise ValueError(
                    f"node {node['id']!r} routes to unknown node: {t}"
                )

        # branch needs routes and/or default
        if node["type"] == "branch":
            if not node.get("routes") and "default" not in node:
                raise ValueError(f"branch node {node['id']!r} needs routes and/or default")

        # node-level input schema sanity
        node_input = node.get("input_schema")
        if node_input is not None:
            if node_input.get("type", "object") != "object":
                raise ValueError(
                    f"node {node['id']!r} input_schema must be a JSON Schema object"
                )


def validate_inputs(workflow: Dict[str, Any], inputs: Dict[str, Any]) -> None:
    """Validate run inputs against the workflow's input_schema (draft-07)."""
    schema = workflow.get("input_schema")
    if schema:
        _jsonschema_validate(inputs, schema)


def validate_run_record(record: Dict[str, Any]) -> None:
    _jsonschema_validate(record, load_schema("run_record"))
