"""Schema loading and validation. jsonschema is imported lazily so that
lightweight usage needs no heavy deps.
"""
import json
import os
import re
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


def node_user_input_schema(node: Dict[str, Any]) -> Dict[str, Any]:
    """User questions; input_schema is a legacy node-level alias only."""
    if "user_input_schema" in node and "input_schema" in node:
        raise ValueError(
            f"node {node['id']!r}: use user_input_schema only; "
            "cannot also specify legacy input_schema"
        )
    return node.get("user_input_schema", node.get("input_schema")) or {}


def validate_workflow(workflow: Dict[str, Any]) -> None:
    """Structural validation against workflow.schema.json + semantic checks."""
    _jsonschema_validate(workflow, load_schema("workflow"))
    if (workflow["schema_version"] == "1.1"
            and "run" in (workflow.get("input_schema") or {}).get("properties", {})):
        raise ValueError("'run' is reserved for runtime metadata")

    def validate_data_refs(value, node_id):
        if isinstance(value, dict):
            if workflow["schema_version"] == "1.1" and "var" in value and set(value) <= {"var", "default"}:
                raise ValueError(f"node {node_id!r}: use typed ref instead of var in version 1.1")
            if "ref" in value:
                if workflow["schema_version"] != "1.1":
                    raise ValueError(f"node {node_id!r}: typed ref requires version 1.1")
                if (set(value) != {"ref"} or not isinstance(value["ref"], str)
                        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*", value["ref"])):
                    raise ValueError(f"node {node_id!r}: invalid typed ref {value!r}")
            for child in value.values():
                validate_data_refs(child, node_id)
        elif isinstance(value, list):
            for child in value:
                validate_data_refs(child, node_id)

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
        node_input = node_user_input_schema(node)
        if "user_input_schema" in node and node["type"] != "task":
            raise ValueError(f"node {node['id']!r}: user_input_schema is only supported on task nodes")
        for value in (node.get("inputs"), node.get("items"), node.get("when")):
            validate_data_refs(value, node["id"])
        if workflow["schema_version"] == "1.0":
            if "output" in node or "check" in node:
                raise ValueError("output/check require schema_version 1.1")
        elif node["type"] != "task" and ("output" in node or "check" in node):
            raise ValueError("output/check are only supported on task nodes")
        if workflow["schema_version"] == "1.1":
            if (node.get("output_key", node["id"]) == "run"
                    or node.get("outputs_key") == "run"
                    or "run" in node.get("inputs", {})
                    or "run" in node_input.get("properties", {})
                    or node.get("as") == "run" or node.get("index_as") == "run"):
                raise ValueError("'run' is reserved for runtime metadata")

        # routing nodes (branch / standalone review) must not use edges
        if node["type"] in ("branch", "review"):
            if n_out:
                raise ValueError(
                    f"{node['type']} node {node['id']!r} must not have outgoing edges; "
                    f"routing is defined by routes/default"
                )
            if node["type"] == "branch" and node.get("when") is not None:
                missing = {"TRUE", "FALSE"} - set(node.get("routes", {}))
                if missing:
                    raise ValueError(
                        f"branch node {node['id']!r} with when needs TRUE/FALSE "
                        f"routes, missing: {sorted(missing)}"
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

        # User-input schema is independent of the task's inputs mapping.
        if node_input:
            if node_input.get("type", "object") != "object":
                raise ValueError(
                    f"node {node['id']!r} user_input_schema must be a JSON Schema object"
                )

        # inline foreach body: reserved ids, internal edges
        body = node.get("nodes")
        if body is not None:
            if not body:
                raise ValueError(
                    f"node {node['id']!r} nodes must not be empty"
                )
            body_ids = {n["id"] for n in body}
            for n in body:
                if n["id"] in ("start", "end") or n["type"] in ("start", "end"):
                    raise ValueError(
                        f"inline body of node {node['id']!r} must not define "
                        f"start/end (they are wrapped automatically)"
                    )
            body_id_list = [n["id"] for n in body]
            dup_body = {i for i in body_id_list if body_id_list.count(i) > 1}
            if dup_body:
                raise ValueError(
                    f"duplicate inline node ids in {node['id']!r}: "
                    f"{sorted(dup_body)}"
                )
            for edge in node.get("edges", []):
                for end in ("from", "to"):
                    if edge[end] not in body_ids:
                        raise ValueError(
                            f"inline edge of node {node['id']!r} references "
                            f"unknown body node: {edge[end]}"
                        )
            # Validate body routes too, including the generated exit.
            body_workflow = {
                "schema_version": workflow["schema_version"],
                "id": "inline-validation", "name": "inline",
                "nodes": body + [{"id": "end", "type": "end"}],
            }
            if "edges" in node:
                body_workflow["edges"] = node["edges"]
            validate_workflow(body_workflow)

    # top-level exports: key -> JSON Schema fragment
    exports = workflow.get("exports")
    if exports is not None:
        if not isinstance(exports, dict):
            raise ValueError("workflow exports must be an object mapping key -> schema")
        for key, fragment in exports.items():
            if not isinstance(fragment, dict):
                raise ValueError(f"export {key!r} must be a JSON Schema object")


def validate_inputs(workflow: Dict[str, Any], inputs: Dict[str, Any]) -> None:
    """Validate run inputs against the workflow's input_schema (draft-07)."""
    schema = workflow.get("input_schema")
    if schema:
        _jsonschema_validate(inputs, schema)


def validate_run_record(record: Dict[str, Any]) -> None:
    _jsonschema_validate(record, load_schema("run_record"))
