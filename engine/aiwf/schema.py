"""Schema loading and validation. jsonschema is imported lazily so that
lightweight usage needs no heavy deps.
"""
import json
import os
import re
from typing import Any, Dict, Optional

from .errors import InstallError, SchemaError
from .paths import data_candidates


def _candidate_schema_dirs():
    """Where bundled schemas may live, best first.

    A source checkout keeps them at ``<repo>/schemas``; an installed wheel
    puts them under ``<prefix>/share/aiwf/schemas`` (see pyproject
    data-files); a vendored layout may keep them inside the package.
    """
    env_dir = os.getenv("AIWF_SCHEMAS_DIR")
    if env_dir:
        yield env_dir
    here = os.path.dirname(os.path.abspath(__file__))
    yield os.path.join(here, "schemas")
    repo_root = os.path.dirname(os.path.dirname(here))
    yield os.path.join(repo_root, "schemas")
    yield from data_candidates("schemas")


def _resolve_schema_dir() -> str:
    for candidate in _candidate_schema_dirs():
        if os.path.isfile(os.path.join(candidate, "workflow.schema.json")):
            return candidate
    # Keep the checkout path so a missing installation names a useful location.
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "schemas",
    )


SCHEMA_DIR = _resolve_schema_dir()


def load_schema(name: str) -> Dict[str, Any]:
    path = os.path.join(SCHEMA_DIR, f"{name}.schema.json")
    if not os.path.isfile(path):
        raise InstallError(
            f"schema file not found: {path}",
            hint="set AIWF_SCHEMAS_DIR to the directory holding workflow.schema.json, "
                 "or reinstall aiwf with its bundled data",
        )
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _jsonschema_validate(
    instance: Any,
    schema: Dict[str, Any],
    *,
    hint: Optional[str] = None,
    where: Optional[str] = None,
):
    try:
        import jsonschema
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "jsonschema is required for validation; install with `pip install jsonschema`"
        ) from e
    try:
        jsonschema.validate(instance=instance, schema=schema)
    except jsonschema.ValidationError as e:
        path = "/".join(str(part) for part in e.absolute_path)
        if e.validator == "required":
            # absolute_path points at the object; the useful location is the
            # missing key itself, which jsonschema only names in the message.
            match = re.search(r"'([^']+)' is a required property", e.message)
            if match:
                path = "/".join(part for part in (path, match.group(1)) if part)
        path = path or "(root)"
        message = f"{where}: {e.message}" if where else e.message
        raise SchemaError(message, at=path, hint=hint) from None
    except jsonschema.SchemaError as e:  # a broken bundled schema, not user input
        raise RuntimeError(f"invalid bundled schema: {e.message}") from e


def node_user_input_schema(node: Dict[str, Any]) -> Dict[str, Any]:
    """User questions; input_schema is a legacy node-level alias only."""
    if "user_input_schema" in node and "input_schema" in node:
        raise ValueError(
            f"node {node['id']!r}: use user_input_schema only; "
            "cannot also specify legacy input_schema"
        )
    return node.get("user_input_schema", node.get("input_schema")) or {}


def _check_view_template(
    template: Any, known_nodes: set, view_mod, where: str, allow_item: bool
) -> None:
    """A ``detail`` typo must fail at validation, not render as a dash later."""
    if not template:
        return
    for key in view_mod.placeholders(template):
        parts = key.split(".")
        root = parts[0]
        if root not in view_mod.NOTE_ROOTS:
            raise ValueError(
                f"{where}: unknown placeholder ${{{key}}}; "
                f"available roots: {list(view_mod.NOTE_ROOTS)}"
            )
        if root == "item" and not allow_item:
            raise ValueError(
                f"{where}: ${{{key}}} is only valid inside view.items.label"
            )
        if root == "node":
            if len(parts) < 2:
                raise ValueError(f"{where}: ${{{key}}} needs a node id")
            if parts[1] not in known_nodes:
                raise ValueError(
                    f"{where}: ${{{key}}} references unknown node {parts[1]!r}"
                )
        if root == "loop" and (
            len(parts) < 2 or parts[1] not in view_mod.LOOP_FIELDS
        ):
            raise ValueError(
                f"{where}: ${{{key}}} must be one of "
                + ", ".join(f"${{loop.{f}}}" for f in view_mod.LOOP_FIELDS)
            )
        if root == "run" and len(parts) > 1 and parts[1] not in view_mod.RUN_FIELDS:
            raise ValueError(
                f"{where}: ${{{key}}} must be one of "
                + ", ".join(f"${{run.{f}}}" for f in view_mod.RUN_FIELDS)
            )
        if root in ("phase", "status") and len(parts) > 1:
            raise ValueError(f"{where}: ${{{key}}} takes no field")


def _validate_view(workflow: Dict[str, Any], spec: Dict[str, Any]) -> None:
    """Semantic checks the JSON Schema cannot express.

    Two invariants: every stage points at real nodes, and every node that does
    work is covered by some stage. An uncovered node would make the view lie by
    omission, which is worse than having no view at all.

    Coverage is a promise only declared stages make. A view that carries just a
    ``dynamic_title`` is naming runs, not describing the flow, so it is exempt.
    """
    from . import view as view_mod

    known = {n["id"] for n in workflow["nodes"]}
    covered: set = set()
    for stage in spec.get("stages", []):
        unknown = [n for n in stage.get("nodes", []) if n not in known]
        if unknown:
            raise ValueError(
                f"view stage {stage.get('name')!r} references unknown node(s): "
                f"{unknown}"
            )
        covered.update(stage["nodes"])

    if spec.get("stages"):
        required = {
            n["id"] for n in workflow["nodes"]
            if n["type"] in ("task", "review", "subflow", "foreach")
        }
        missing = sorted(required - covered)
        if missing:
            raise ValueError(
                f"view must cover every task/review/subflow node; missing: {missing}"
            )

    for stage in spec.get("stages", []):
        _check_view_template(
            stage.get("detail"), known, view_mod,
            where=f"view stage {stage.get('name')!r} detail", allow_item=False,
        )
    _check_view_template(
        spec.get("dynamic_title"), known, view_mod,
        where="view.dynamic_title", allow_item=False,
    )
    _check_view_template(
        (spec.get("items") or {}).get("label"), known, view_mod,
        where="view.items.label", allow_item=True,
    )


def normalize_workflow(workflow: Dict[str, Any]) -> Dict[str, Any]:
    """Expand ``use: [name, ...]`` into ``inputs: {name: {"ref": name}}``.

    In place and idempotent. Explicit ``inputs`` wins on a duplicate name. Only
    a well-formed list of strings is expanded; anything else is left for the
    schema to reject.
    """
    for node in workflow.get("nodes") or []:
        use = node.get("use")
        if (node.get("type") in ("task", "branch")
                and isinstance(use, list) and use
                and all(isinstance(name, str) for name in use)):
            merged = {name: {"ref": name} for name in use}
            merged.update(node.get("inputs") or {})
            node["inputs"] = merged
            node.pop("use", None)
        if isinstance(node.get("nodes"), list):
            normalize_workflow({"nodes": node["nodes"]})
    return workflow


def validate_workflow(workflow: Dict[str, Any]) -> None:
    """Structural validation against workflow.schema.json + semantic checks."""
    _jsonschema_validate(
        workflow, load_schema("workflow"),
        hint="按 at 定位定义字段（如 nodes/0/output）；字段名与类型见 "
             "skills/aiwf/references/data-reference.md",
    )
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
        if "use" in node and node["type"] not in ("task", "branch"):
            raise ValueError(
                f"node {node['id']!r}: use is only supported on task/branch nodes"
            )
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

    view_spec = workflow.get("view")
    if view_spec is not None:
        _validate_view(workflow, view_spec)

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
        _jsonschema_validate(
            inputs, schema,
            hint="启动入参不符合 input_schema；用 -p key=value 或 --input-file 补齐",
        )


def validate_run_record(record: Dict[str, Any]) -> None:
    _jsonschema_validate(record, load_schema("run_record"))
