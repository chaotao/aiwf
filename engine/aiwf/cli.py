"""aiwf command line interface.

The CLI is the boundary between an AI agent / human and the engine state machine:

    aiwf init                            create .aiwf/{workflows,runs}
    aiwf validate PATH                   validate a workflow definition file
    aiwf workflows                       list saved workflow ids
    aiwf run WF_ID --title "..." [-p k=v] ...
                                         start a run; --title names this run
                                         (what the work is about right now),
                                         then print the first spec
    aiwf current RUN_ID                  print the current spec (any phase)
    aiwf complete RUN_ID --output...     report task/branch output, advance
    aiwf provide RUN_ID [-p k=v] ...     answer a node's user-input questions;
                                         --node changes a completed node (revision)
    aiwf review RUN_ID --verdict V ...   human acceptance verdict
    aiwf revise RUN_ID --target ID ...   out-of-plan jump to a prior task
    aiwf fail RUN_ID --error "..."       report node failure
    aiwf resume WF_ID RUN_ID             reopen a failed run
    aiwf runs list|show|search ...       inspect audit records
    aiwf items list|add|update|remove|move|seal
                                         the engine-owned foreach queue behind
                                         `items: {"queue": true}`; the engine is
                                         its only writer, started items are
                                         immutable, `add`/`seal` resume a run
                                         that was waiting for items
    aiwf skills list|install ...         package agent skills
    aiwf wf list|show|install|diff|...   bundled workflow template library

`aiwf runs show` renders a workflow's declared `view` -- the business stages a
human cares about -- above the raw node stream; `--view` prints only that.

Every state-changing command prints the resulting spec so an agent can loop
without re-querying. --json emits machine-readable spec envelopes.
"""
import argparse
import json
import os
import shutil
import sys
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import __version__, library as template_library
from . import view as view_mod
from .engine import WorkflowEngine
from .errors import (
    AiwfError,
    NotFoundError,
    SchemaError,
    StaleActionError,
    UsageError,
)
from .paths import data_candidates
from .schema import normalize_workflow, validate_workflow
from .storage import Storage


# ---- output helpers ------------------------------------------------------
#: Width of the leading title column in `runs list`: wide enough for a real
#: name, narrow enough that the columns behind it still fit one terminal line.
TITLE_COLUMN = 28


def _force_utf8() -> None:
    """Make the CLI's own output UTF-8 regardless of the console code page.

    An agent captures stdout through a pipe, where Python otherwise encodes
    with the locale codec (cp936 on a Chinese Windows). Every JSON envelope and
    every prompt would then arrive as mojibake.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _read_json(path: str) -> Any:
    """Read a JSON file written by any tool: tolerate the BOM Windows editors
    and PowerShell's `-Encoding utf8` add, and report syntax errors as usage."""
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise UsageError(
            f"invalid JSON in {path}: {e.msg} (line {e.lineno}, column {e.colno})",
            at=path,
            hint="the file must be a single JSON value; rewrite it with UTF-8 "
                 "and no comments",
        ) from None


def _error_payload(err: AiwfError) -> Dict[str, Any]:
    """One flat object: ``error`` stays a string for older consumers, while
    ``code``/``at``/``hint``/``next`` are additive."""
    payload: Dict[str, Any] = {
        "status": "error", "ok": False, "error": err.message,
    }
    payload.update({k: v for k, v in err.as_dict().items() if k != "message"})
    return payload


def _fail(err: AiwfError, as_json: bool) -> int:
    if as_json:
        print(json.dumps(_error_payload(err), ensure_ascii=False))
    else:
        print(f"error: {err.message}", file=sys.stderr)
        if err.at:
            print(f"  at   : {err.at}", file=sys.stderr)
        if err.hint:
            print(f"  hint : {err.hint}", file=sys.stderr)
        if err.next_command:
            print(f"  next : {err.next_command}", file=sys.stderr)
    return err.exit_code



def _display_width(text: str) -> int:
    """Columns ``text`` occupies. A CJK glyph is twice as wide as a latin one,
    so padding by ``len()`` would ragged-align every title written in Chinese."""
    return sum(
        2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text
    )


def _clip(text: str, width: int) -> str:
    """``text`` cut to ``width`` columns, with ``…`` when something was lost."""
    if _display_width(text) <= width:
        return text
    kept, used = "", 0
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + step > width - 1:
            break
        kept += char
        used += step
    return kept + "…"


def _cell(text: str, width: int) -> str:
    """One left-aligned table cell, truncated and padded to a fixed width."""
    text = _clip(text, width)
    return text + " " * max(0, width - _display_width(text))


def _posix(path: str) -> str:
    """Display a path with forward slashes: safe inside JSON and in every shell."""
    return str(path).replace("\\", "/")


# ---- submit template -----------------------------------------------------
SUBMIT_NAME = "submit.json"


def _submit_path(storage: Storage, run_id: str) -> str:
    record = storage.find_run(run_id)
    return os.path.join(storage.run_dir_for_record(record), SUBMIT_NAME)


def _default_for_type(field: Dict[str, Any]) -> Any:
    return {
        "integer": 0, "number": 0, "boolean": False,
        "array": [], "object": {},
    }.get(field.get("type", "string"), "")


def _submit_kind(spec: Dict[str, Any]) -> str:
    if spec.get("check_request"):
        return "check"
    if spec.get("review_request"):
        return "review"
    if spec.get("input_request"):
        return "provide"
    if spec.get("phase") == "awaiting_items":
        return "items"
    node = spec.get("node") or {}
    if node.get("type") in ("start", "end"):
        return "empty"
    return "complete"


def build_submit_template(spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The one file an agent edits: action fingerprint + a pre-filled payload."""
    action = spec.get("action")
    if not action:
        return None
    node = spec.get("node") or {}
    kind = _submit_kind(spec)

    if kind == "check":
        request = spec.get("check_request") or {}
        criteria = request.get("criteria") or []
        output: Any = {"verdict": "PASS|REVISE", "reason": ""}
        hint = ("逐条核对 criteria；verdict 填 PASS 或 REVISE，reason 写依据。"
                "\ncriteria: " + " / ".join(map(str, criteria)))
    elif kind == "review":
        request = spec.get("review_request") or {}
        requirements = request.get("requirements") or []
        if not isinstance(requirements, list):
            requirements = [requirements]
        output = {"verdict": "APPROVE|REJECT", "feedback": ""}
        hint = ("结论必须来自用户；verdict 填 APPROVE 或 REJECT。"
                "\nrequirements: " + " / ".join(map(str, requirements)))
    elif kind == "provide":
        request = spec.get("input_request") or {}
        fields = request.get("fields") or []
        output = {field["name"]: _default_for_type(field) for field in fields}
        hint = "按 input_request 填字段；enum 只能取列出的值。"
    elif kind == "items":
        output = []
        hint = "把条目对象放进 output；结束队列时把 seal 设为 true。"
    elif kind == "empty":
        output = None
        hint = "无需 payload；直接 aiwf submit。"
    else:  # complete
        declared = (node.get("output") or {}).get("file")
        if node.get("type") == "branch":
            labels = "|".join(node.get("routes") or {}) or "TRUE|FALSE"
            output = labels
            hint = f"output 改成其中一个 route：{labels}"
        elif declared:
            output = {"$file": _posix(declared)}
            hint = f"把文件写到 {_posix(declared)}；写完直接 aiwf submit。"
        elif node.get("output_schema"):
            output = {}
            hint = ("按 node.output_schema 填 output；数组/对象可直接改本文件，"
                    "或改用 --json-file。")
        else:
            output = {}
            hint = "把结果放进 output；短对象可直接改本文件。"

    template: Dict[str, Any] = {
        "action": action["id"],
        "action_run_id": action["run_id"],
        "run_id": spec["run_id"],
        "node": action["node_id"],
        "kind": kind,
        "hint": hint,
        "output": output,
    }
    if kind == "items":
        template["seal"] = False
    return template


def write_submit_template(storage: Storage, spec: Dict[str, Any]) -> Optional[str]:
    """Refresh ``submit.json`` after a transition; remove it at rest."""
    if not spec.get("run_id"):
        return None
    path = _submit_path(storage, spec["run_id"])
    if spec.get("status") != "running" or not spec.get("action"):
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    template = build_submit_template(spec)
    if template is None:
        return None
    storage.write_json_file(path, template)
    return path


def _next_for_spec(spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The command that continues this run, computed from the packet itself.

    This is the one piece an agent must get right every turn; deriving it here
    means the model never has to translate action.kind into a command, copy a
    24-hex id, or decide between `complete`/`provide`/`review`.
    """
    run_id = spec.get("run_id")
    if not run_id:
        return None
    status = spec.get("status")
    if status == "failed":
        return {"kind": "resume", "text": f"aiwf resume {run_id} --json"}
    if status == "completed":
        return {"kind": "done", "text": f"aiwf runs show {run_id}"}

    action = spec.get("action") or {}
    node = spec.get("node") or {}

    if spec.get("input_request"):
        fields = spec["input_request"].get("fields") or []
        sample = f" -p {fields[0]['name']}=..." if fields else " -p key=value"
        return {"kind": "submit", "text": f"aiwf submit{sample}"}
    if spec.get("review_request"):
        return {"kind": "submit",
                "choices": ["APPROVE", "REJECT"],
                "text": 'aiwf submit --verdict APPROVE|REJECT --feedback "..."'}
    if spec.get("check_request"):
        return {"kind": "submit",
                "choices": ["PASS", "REVISE"],
                "text": 'aiwf submit --verdict PASS|REVISE --reason "..."'}
    if spec.get("phase") == "awaiting_items":
        return {"kind": "submit", "text": "aiwf submit",
                "note": "编辑 submit.json 的 output/seal，或 "
                        "aiwf submit --item-file items.json"}
    if action.get("kind") == "resume":
        return {"kind": "resume", "text": f"aiwf resume {run_id} --json"}

    ntype = node.get("type")
    if ntype == "task":
        return {"kind": "submit", "text": "aiwf submit"}
    if ntype == "branch":
        routes = list(node.get("routes") or {})
        labels = "|".join(routes) or "TRUE|FALSE"
        return {"kind": "submit", "choices": routes or ["TRUE", "FALSE"],
                "text": f'aiwf submit --output "{labels}"'}
    if ntype in ("start", "end"):
        return {"kind": "submit", "text": "aiwf submit --empty"}
    return None


def _print_next(next_command: Optional[Dict[str, Any]]) -> None:
    if next_command:
        print(f"next     : {next_command['text']}")
        if next_command.get("note"):
            print(f"note     : {next_command['note']}")


def _print_rejection(spec: Dict[str, Any]) -> None:
    rejected = spec.get("rejected")
    if not rejected:
        return
    print("REJECTED: the reported output did not satisfy the contract")
    print(f"  node   : {rejected.get('node_id')}")
    if rejected.get("expected"):
        print(f"  want   : {_posix(rejected['expected'])}")
    got = rejected.get("got")
    print(f"  got    : {_posix(got) if got else '(no $file)'}")
    print(f"  reason : {rejected.get('reason')}")


def print_spec(spec_envelope: Dict[str, Any], as_json: bool) -> None:
    next_command = _next_for_spec(spec_envelope)
    if as_json:
        payload = dict(spec_envelope)
        if next_command is not None:
            payload["next"] = next_command
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    node = spec_envelope.get("node")
    print(f"run_id   : {spec_envelope['run_id']}")
    print(f"workflow : {spec_envelope['workflow_id']}")
    print(f"status   : {spec_envelope['status']}")
    print(f"phase    : {spec_envelope['phase']}")
    if spec_envelope.get("action"):
        action = spec_envelope["action"]
        print(f"action   : {action['kind']} --action {action['id']} (target {action['run_id']})")
    _print_rejection(spec_envelope)
    if spec_envelope.get("check_request"):
        print("quality check (agent):")
        print(json.dumps(spec_envelope["check_request"], ensure_ascii=False, indent=2))
        _print_next(next_command)
        return

    rev = spec_envelope.get("revision")
    if rev:
        print(f"revision : #{rev['seq']} target={rev['target']} mode={rev['mode']}")
        print(f"  stale : {', '.join(rev['stale_nodes'])}")
        print(f"  reused: {', '.join(rev['reused_nodes'])}")
        print(f"  redone: {', '.join(rev['redone_nodes'])}")

    input_request = spec_envelope.get("input_request")
    if input_request is not None:
        print("--- awaiting user input: ask the user, then submit the answers ---")
        print(f"node     : {input_request['node_id']}")
        for field in input_request["fields"]:
            print(f"  * {field['title']} ({field['name']}: {field['type']})")
            if field["description"]:
                print(f"      {field['description']}")
            if field.get("enum"):
                print(f"      options: {', '.join(map(str, field['enum']))}")
        _print_next(next_command)
        return

    review_request = spec_envelope.get("review_request")
    if review_request is not None:
        print("--- awaiting human review: collect the real verdict ---")
        node_id = review_request["node_id"]
        print(f"node     : {node_id}  (attempt #{review_request['attempt']}, "
              f"{review_request['kind']})")
        if review_request.get("output") is not None:
            print("artifact : " + json.dumps(review_request["output"], ensure_ascii=False))
        req = review_request["requirements"]
        print("requirements:")
        if isinstance(req, list):
            for item in req:
                print(f"  [ ] {item}")
        else:
            print(f"  {req}")
        print("routes:")
        for label, target in review_request["routes"].items():
            print(f"  {label} -> {target if target is not None else '(default)'}")
        _print_next(next_command)
        return

    if node is None:
        print("node     : (none; run finished)")
        _print_next(next_command)
        return

    print(f"node     : {node['id']} [{node['type']}] {node['name']}")
    ntype = node["type"]
    if ntype == "task":
        if node.get("inputs"):
            print("materials: " + json.dumps(node["inputs"], ensure_ascii=False))
        if node.get("output"):
            print("artifact : " + json.dumps(node["output"], ensure_ascii=False))
        print(f"model    : {node.get('model_hint') or '(agent default)'}")
        if node.get("feedback"):
            print("--- reviewer feedback for this redo ---")
            print(node["feedback"])
        if node.get("previous_output") is not None:
            print("previous output (edit on top of it):")
            print(str(node["previous_output"])[:400])
        print("--- prompt (execute with your own model/tools, then report) ---")
        print(node["prompt"])
        print(f"output_key: {node['output_key']}")
        if node.get("output_schema"):
            print("output contract (output MUST satisfy):")
            print("  " + json.dumps(node["output_schema"], ensure_ascii=False))
    elif ntype == "branch":
        print(f"model    : {node.get('model_hint') or '(agent default)'}")
        if node.get("deterministic"):
            print("--- deterministic condition: the engine evaluates it ---")
            print(json.dumps(node["when"], ensure_ascii=False))
        else:
            print("--- decision prompt: answer with one route label ---")
            print(node["prompt"])
        print("routes:")
        for label, target in node.get("routes", {}).items():
            print(f"  {label} -> {target}")
        if node.get("default"):
            print(f"  default -> {node['default']}")
    elif ntype in ("subflow", "foreach"):
        ref = node.get("subflow_ref")
        print(f"subflow  : {ref if ref else '(inline nodes)'}")
        if ntype == "foreach":
            print(f"items    : {node.get('items')}")
            print(f"bind     : {node.get('as_')} / {node.get('index_as')}"
                  f" -> outputs_key {node.get('outputs_key')}"
                  f" (on_fail {node.get('on_fail')})")
        else:
            print(f"output_key: {node.get('output_key')}")
        sstate = node.get("subflow_state")
        if sstate:
            print(f"state    : {sstate['status']} {sstate['progress']}"
                  f" ({sstate['subflow_id']})")
        child = node.get("child")
        if child:
            print(f"child    : run {child['run_id']} instance {child['instance']}"
                  f" — drive it with `aiwf current {child['run_id']}`")
        if node.get("message"):
            print(node["message"])
        if not child:
            print("action   : drive the child run; it is created/advanced automatically")
    else:  # start / end: agent just advances
        print("action   : no execution needed")
    _print_next(next_command)


def _print_record_table(record: Dict[str, Any], storage: Optional[Storage] = None) -> None:
    workflow, view_error = _resolve_definition(record, storage)
    wf = record["workflow_id"]
    print(f"run_id     : {record['run_id']}")
    print(f"title      : {view_mod.run_title(workflow, record)}")
    print(f"workflow   : {wf}")
    if record["status"] == "running":
        print(f"status     : running / {record['phase']}")
    else:
        print(f"status     : {record['status']}")
    print(f"started_at : {record['started_at']}")
    if record.get("ended_at"):
        print(f"ended_at   : {record['ended_at']}")
        print(f"duration   : {record.get('duration_ms')} ms")
    if record.get("error"):
        print(f"error      : {record['error']}")
    print(f"record dir : {Storage().run_dir_for_record(record)}")
    derived = view_mod.derive(workflow, record, storage) if workflow else None
    if derived:
        _print_view(derived)
    elif view_error:
        # Never fail silently: a view that is missing for a reason must say so.
        print(f"view       : (unavailable: {view_error})")
    if record["node_runs"]:
        print("nodes:")
        for nr in record["node_runs"]:
            if nr["status"] == "success":
                mark = "ok "
            elif nr["status"] == "skipped":
                mark = "reuse"
            elif nr["status"] == "rejected":
                mark = "rej"
            else:
                mark = "ERR"
            print(
                f"  [{mark}] {nr['node_id']:<16} {nr['duration_ms']:>7} ms  "
                f"{nr['node_name']}  -- {nr['summary']}"
            )
    _print_subflow_table(record)


# Instance status -> the stage vocabulary the view speaks.
_ROW_STATUS = {
    "success": "done", "running": "active",
    "failed": "failed", "pending": "pending",
}


def _resolve_definition(
    record: Dict[str, Any], storage: Optional[Storage]
) -> tuple:
    """``(workflow, error)``.

    ``(None, None)`` means there is nothing to resolve against (no storage).
    A non-empty ``error`` means the definition itself could not be resolved --
    a legacy record whose run predates ``workflow_version``, say -- which is a
    different answer from "no view" and must not be swallowed.

    Both a view and a run's title need this, so it is resolved once per call
    and handed to whichever of the two the caller asks for.
    """
    if storage is None:
        return None, None
    try:
        return storage.load_workflow_for_record(record), None
    except Exception as e:
        return None, str(e)


def _print_view(derived: Dict[str, Any]) -> None:
    """Stages in node order; a loop's items indent under the stage they feed,
    and a gate prints the exact command a human would run to clear it.

    A title-only view has no stages to print -- the title is already rendered
    above it, so an empty ``view:`` header would only be noise.
    """
    if not derived["stages"]:
        return
    print("view       :")
    for stage in derived["stages"]:
        tag = view_mod.mark(stage["status"]).ljust(4)
        suffix = f"  -- {stage['detail']}" if stage["detail"] else ""
        print(f"  [{tag}] {stage['name']}{suffix}")
        queue = stage.get("queue")
        if queue:
            for row in queue["items"]:
                rtag = view_mod.mark(
                    _ROW_STATUS.get(row["status"], "pending")
                ).ljust(4)
                note = ""
                if row["current"]:
                    note = "  <- 当前"
                    if queue.get("current_stage"):
                        note += f"（{queue['current_stage']}）"
                print(f"    [{rtag}] {row['key']}  {row['label']}{note}")
        if stage["status"] == "waiting" and stage.get("action"):
            print(f"    -> {_gate_command(stage['action'])}")


def _gate_command(action: Dict[str, Any]) -> str:
    """The command that clears a gate; the submit template carries the action."""
    kind, run_id = action["kind"], action["run_id"]
    if kind == "review":
        return f'aiwf submit {run_id} --verdict APPROVE|REJECT --feedback "..."'
    if kind == "check":
        return f'aiwf submit {run_id} --verdict PASS|REVISE --reason "..."'
    if kind == "provide":
        return f"aiwf submit {run_id} -p key=value"
    if kind == "resume":
        return f"aiwf resume {run_id}"
    return f"aiwf submit {run_id}"


def _print_subflow_table(record: Dict[str, Any]) -> None:
    state = record.get("subflow_state")
    if not state:
        return
    kind = state["kind"]
    print(f"subflow    : node {state['node_id']} -> {state['subflow_id']}"
          f" ({'inline' if state['inline'] else 'external'}, {state['status']})")
    if kind == "foreach":
        print("instances:")
        for inst in state["instances"]:
            row = (
                f"  [{inst['status']:<7}] {inst['key']:<12} "
                f"run {inst.get('run_id')}"
            )
            if inst.get("error"):
                row += f"  -- {inst['error'][:80]}"
            print(row)


def _parse_param(raw: str) -> Dict[str, Any]:
    if "=" not in raw:
        raise UsageError(
            f"parameter must be key=value, got: {raw!r}",
            hint='quote values with spaces: -p idea="末世里每次重生都失去一段记忆"',
        )
    key, value = raw.split("=", 1)
    try:
        value = json.loads(value)  # numbers/bools/null/JSON
    except json.JSONDecodeError:
        pass
    return {key.strip(): value}


def _read_data(args) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    if getattr(args, "data_file", None):
        loaded = _read_json(args.data_file)
        if not isinstance(loaded, dict):
            raise UsageError(
                "data file must contain a JSON object",
                at=args.data_file,
            )
        values.update(loaded)
    for p in getattr(args, "param", None) or []:
        values.update(_parse_param(p))
    return values


def _declared_inputs(workflow: Dict[str, Any]) -> set:
    """Keys the definition itself declares - what run_defaults may fill."""
    schema = workflow.get("input_schema") or {}
    return set(schema.get("properties") or {}) | set(schema.get("required") or [])


def _collect_inputs(
    args, workflow: Dict[str, Any], storage: Storage
) -> Dict[str, Any]:
    """Start-up inputs, later sources winning:

        project run_defaults  ->  --input-file  ->  -p/--param

    Defaults are filtered to keys the definition declares, so one project's
    settings never leak into unrelated workflows. A value the project does not
    configure simply stays missing, and the input_schema check reports it.
    """
    declared = _declared_inputs(workflow)
    inputs: Dict[str, Any] = {
        key: value
        for key, value in storage.run_defaults().items()
        if key in declared
    }
    if getattr(args, "input_file", None):
        loaded = _read_json(args.input_file)
        if not isinstance(loaded, dict):
            raise UsageError(
                "--input-file must contain a JSON object",
                at=args.input_file,
                hint='expected {"idea": "...", ...}',
            )
        inputs.update(loaded)
    for p in getattr(args, "param", None) or []:
        inputs.update(_parse_param(p))
    return inputs


def _load_workflow_for_record(storage: Storage, record: Dict[str, Any]):
    return storage.load_workflow_for_record(record)


# ---- run / action selectors ---------------------------------------------
def _resolve_run_id(raw: str, storage: Storage) -> str:
    """Exact run id, or a unique prefix of one."""
    try:
        return storage.find_run(raw)["run_id"]
    except FileNotFoundError:
        pass
    matches = [
        rid for _wf, rid, _path, parent in storage.iter_run_files()
        if parent is None and rid.startswith(raw)
    ]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise UsageError(
            f"run id {raw!r} is ambiguous",
            at="run",
            hint="matches: " + ", ".join(sorted(matches)[:8]),
        )
    raise NotFoundError(
        f"no run with id or prefix {raw!r}",
        hint="list runs with `aiwf runs list`",
    )


def _pick_run(
    storage: Storage,
    raw: Optional[str],
    *,
    need_running: bool,
    prefer_running: bool = True,
) -> str:
    """Resolve an omitted run id: the sole running run, else the latest.

    A weak caller usually has exactly one run in flight and should not have to
    copy a 22-character id. When several are running the ambiguity is reported
    with the candidates instead of guessing.

    ``prefer_running=False`` means "latest activity" (used by `runs show`),
    while the default means "the run I am working on" (used by `current` /
    `status` / `items`): a still-running run wins over a newer finished one.
    """
    if raw:
        return _resolve_run_id(raw, storage)
    running: List[tuple] = []
    latest: Optional[tuple] = None
    for _wf, rid, path, parent in storage.iter_run_files():
        if parent is not None:
            continue
        try:
            record = storage.load_record(path)
            mtime = os.path.getmtime(path)
        except (OSError, ValueError):
            continue
        started = record.get("started_at") or ""
        # started_at has one-second resolution; mtime breaks ties between two
        # runs created in the same second (the later record.json is newer).
        stamp = (started, mtime, rid)
        if latest is None or stamp > latest:
            latest = stamp
        if record.get("status") == "running":
            running.append((stamp, rid, record.get("title") or rid))
    running.sort(reverse=True)
    if need_running:
        if len(running) == 1:
            return running[0][1]
        if not running:
            raise UsageError(
                "no running run",
                hint="start one with `aiwf run ...`, or recover a failed run "
                     "with `aiwf resume <run_id>` (see `aiwf runs list`)",
            )
        raise UsageError(
            "multiple runs are running; pass a run_id",
            hint="; ".join(f"{rid} ({title})" for _s, rid, title in running),
        )
    if len(running) == 1:
        return running[0][1]
    if running and prefer_running:
        return running[0][1]
    if latest is not None:
        return latest[2]
    raise NotFoundError(
        "no runs found",
        hint="create storage with `aiwf init`, then start one with `aiwf run ...`",
    )


def _resolve_action_id(engine, run_id: str, raw: Optional[str]) -> Optional[str]:
    """Accept the full action id, a unique prefix, the current node id, or
    ``current``/``latest``/omitted.

    The digest still guards against reporting to a *different* node: anything
    that does not resolve is passed through unchanged and the engine rejects it
    as stale.
    """
    spec = engine.action_spec(run_id)
    action = spec.get("action")
    if not action:
        return raw
    if raw in (None, "", "current", "latest", action["node_id"]):
        return action["id"]
    if raw == action["id"]:
        return raw
    if len(raw) >= 4 and action["id"].startswith(raw):
        return action["id"]
    return raw


# ---- command handlers ----------------------------------------------------
def _new_protocol(engine, record):
    root = engine.root_record(record)
    return root.get("workflow_version") == "1.1"


def _emit(engine, record, args, advance=True):
    if _new_protocol(engine, record):
        if advance:
            engine.settle(record["run_id"])
        spec = engine.action_spec(record["run_id"])
    else:
        workflow = engine.storage.load_workflow_for_record(record)
        spec = engine.node_spec(workflow, record)
    _attach_rejection(engine, spec)
    if advance and not engine.last_rejection:
        write_submit_template(engine.storage, spec)
    print_spec(spec, args.json)
    if engine.last_rejection:
        return 3
    return 1 if spec["status"] == "failed" else 0


def _attach_rejection(engine, spec):
    """Surface a soft rejection (node kept open, feedback recorded) to the CLI.

    The engine deliberately does not raise on a bad artifact: the run stays
    usable and the feedback is part of the next packet. The exit code still has
    to say "nothing advanced", otherwise a caller treats the turn as success.
    """
    if getattr(engine, "last_rejection", None):
        spec["rejected"] = engine.last_rejection
    return spec


def _act(engine, args, command, **kwargs):
    raw = getattr(args, "action", None)
    action_id = _resolve_action_id(engine, args.run_id, raw)
    if raw in (None, "") and not getattr(args, "json", False):
        print(
            f"note: --action omitted; using the current action ({action_id})",
            file=sys.stderr,
        )
    spec = engine.act(args.run_id, action_id, command, **kwargs)
    _attach_rejection(engine, spec)
    if not engine.last_rejection:
        write_submit_template(engine.storage, spec)
    print_spec(spec, args.json)
    if engine.last_rejection:
        return 3
    return 1 if spec["status"] == "failed" else 0


def _write_project_launchers() -> List[str]:
    """Write ./bin/aiwf (and .cmd) pinned to this interpreter and engine.

    The skill tells an agent to run "aiwf" from the project directory, but the
    engine usually lives in another checkout; a relative ``engine/bin/aiwf``
    stops working the moment you ``cd`` into the project. A generated launcher
    removes that whole class of "command not found".
    """
    engine_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    python = sys.executable
    bin_dir = os.path.join(os.getcwd(), "bin")
    os.makedirs(bin_dir, exist_ok=True)
    written = []

    cmd_path = os.path.join(bin_dir, "aiwf.cmd")
    with open(cmd_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("@echo off\n")
        f.write("setlocal\n")
        f.write(f'set "PYTHONPATH={engine_dir};%PYTHONPATH%"\n')
        f.write(f'"{python}" -m aiwf %*\n')
    written.append(cmd_path)

    sh_path = os.path.join(bin_dir, "aiwf")
    with open(sh_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/bin/sh\n")
        f.write(f'PYTHONPATH="{engine_dir}:${{PYTHONPATH}}" '
                f'exec "{python}" -m aiwf "$@"\n')
    try:
        os.chmod(sh_path, 0o755)
    except OSError:
        pass
    written.append(sh_path)
    return written


def _scaffold_workflow(workflow_id: str, kind: str) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "schema_version": "1.1",
        "id": workflow_id,
        "name": workflow_id,
    }
    if kind == "checked":
        base["input_schema"] = {
            "type": "object", "required": ["idea"],
            "properties": {"idea": {"type": "string"}},
        }
        base["nodes"] = [{
            "id": "draft",
            "type": "task",
            "inputs": {"idea": {"ref": "idea"}},
            "prompt": "按 idea 产出第一版。要求写清楚：目标读者、核心内容、验收标准。",
            "output": {"file": "${run.dir}/draft.md"},
            "check": {
                "criteria": ["产物覆盖 idea 的核心要求", "结构与表达可直接交付"],
                "max_attempts": 3,
            },
            "review": {"requirements": ["是否达到可交付标准"]},
        }]
    elif kind == "batch":
        base["nodes"] = [{
            "id": "work",
            "type": "foreach",
            "items": {"queue": True},
            "nodes": [{
                "id": "step",
                "type": "task",
                "inputs": {"item": {"ref": "item"}},
                "prompt": "处理 item：${item.name}。只改必要内容，完成后写产物。",
                "output": {"file": "${run.dir}/${item.id}.md"},
                "review": {"requirements": ["该项可以验收"]},
            }],
        }]
    else:  # sequential
        base["input_schema"] = {
            "type": "object", "required": ["idea"],
            "properties": {"idea": {"type": "string"}},
        }
        base["nodes"] = [
            {
                "id": "draft",
                "type": "task",
                "inputs": {"idea": {"ref": "idea"}},
                "prompt": "按 idea 产出第一版；把目标、范围和验收标准写清楚。",
                "output": {"file": "${run.dir}/draft.md"},
            },
            {
                "id": "polish",
                "type": "task",
                "inputs": {"draft": {"ref": "draft"}},
                "prompt": "读取 draft 文件，按目标润色；保留事实与结构。",
                "output": {"file": "${run.dir}/final.md"},
                "review": {"requirements": ["是否达到可交付标准"]},
            },
        ]
    return base


def cmd_new(args, storage: Storage) -> int:
    workflow = _scaffold_workflow(args.workflow_id, args.kind)
    validate_workflow(workflow)  # also enforces the id pattern
    path = storage.workflow_path(workflow["id"])
    if os.path.exists(path) and not getattr(args, "force", False):
        raise UsageError(
            f"workflow file already exists: {path}",
            hint="choose another id, or pass --force to overwrite",
        )
    storage.save_workflow(workflow)
    print(f"created: {path}  (kind={args.kind})")
    if args.kind == "batch":
        print("next: aiwf validate --project; aiwf run "
              f"{workflow['id']} --title \"...\" --json")
    else:
        print("next: aiwf validate --project; aiwf run "
              f"{workflow['id']} --title \"...\" -p idea=\"...\" --json")
    return 0


def cmd_init(args, storage: Storage) -> int:
    storage.init_dirs()
    print(f"initialized aiwf storage at {storage.root}")
    print(f"  definitions: {storage.workflows_dir}")
    print(f"  run records: {storage.runs_dir}")
    for path in _write_project_launchers():
        print(f"  launcher   : {path}")
    print("next: aiwf wf list   (install a template)  or  aiwf validate --project")
    return 0


def cmd_doctor(args, storage: Storage) -> int:
    """One read-only check of everything the CLI needs to work in this shell."""
    from . import schema as schema_mod

    rows: List[Dict[str, Any]] = []

    def add(name, status, detail):
        rows.append({"check": name, "status": status, "detail": detail})

    add("aiwf", "ok", f"{__version__} at {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
    add("python", "ok", f"{sys.version.split()[0]} ({sys.executable})")
    try:
        import jsonschema
        add("jsonschema", "ok", getattr(jsonschema, "__version__", "installed"))
    except ImportError:
        add("jsonschema", "fail", "missing; pip install jsonschema")
    add("platform", "ok", f"{sys.platform}  cwd={os.getcwd()}")
    add("stdout", "ok", f"encoding={getattr(sys.stdout, 'encoding', '?')}  "
                        f"PYTHONUTF8={os.getenv('PYTHONUTF8', '(unset)')}")
    add("storage", "ok" if os.path.isdir(storage.root) else "warn",
        f"{storage.root}  workflows={len(storage.list_workflows())}  "
        f"runs={sum(1 for _ in storage.iter_run_files())}")
    missing_templates = []
    for _wf, rid, path, parent in storage.iter_run_files():
        if parent is not None:
            continue
        try:
            candidate = storage.load_record(path)
        except (OSError, ValueError):
            continue
        if candidate.get("status") != "running":
            continue
        template = os.path.join(
            storage.run_dir_for_record(candidate), SUBMIT_NAME
        )
        if not os.path.isfile(template):
            missing_templates.append(rid)
    add("submit-files", "warn" if missing_templates else "ok",
        (f"{len(missing_templates)} running run(s) without submit.json; "
         f"run `aiwf status --ensure`" if missing_templates else "all running runs"))
    schemas_ok = all(
        os.path.isfile(os.path.join(schema_mod.SCHEMA_DIR, f"{name}.schema.json"))
        for name in ("workflow", "run_record")
    )
    add("schemas", "ok" if schemas_ok else "fail", schema_mod.SCHEMA_DIR)
    try:
        library_dir = template_library.library_dir(getattr(args, "library", None))
        count = len(template_library.load_library(getattr(args, "library", None)))
        add("library", "ok", f"{library_dir}  templates={count}")
    except Exception as e:
        add("library", "warn", f"{e}  (aiwf wf ... unavailable; set AIWF_LIBRARY_DIR)")
    skills_dir = _bundled_skills_dir()
    add("skills", "ok" if os.path.isdir(skills_dir) else "warn",
        skills_dir if os.path.isdir(skills_dir) else f"{skills_dir} (missing)")
    installed = os.path.join(".trae", "skills", "aiwf", "SKILL.md")
    add("installed-skill", "ok" if os.path.isfile(installed) else "warn",
        os.path.abspath(installed))

    failed = [row for row in rows if row["status"] == "fail"]
    if getattr(args, "json", False):
        print(json.dumps({"ok": not failed, "checks": rows},
                         ensure_ascii=False, indent=2))
    else:
        print(f"{'check':<16} {'status':<6} detail")
        for row in rows:
            print(f"{row['check']:<16} {row['status']:<6} {row['detail']}")
        if failed:
            print("\nhint: fix the failed rows above; AIWF_SCHEMAS_DIR / "
                  "AIWF_LIBRARY_DIR override the bundled paths")
    return 2 if failed else 0


def _report_run_defaults(storage: Storage) -> None:
    """Advisory only, never a failure: which required inputs the project's
    run_defaults does not fill, and which configured keys no definition
    declares (usually a typo). Both are normal while a project is still being
    set up, so they are printed as notes rather than raised."""
    defaults = storage.run_defaults()
    declared_any: set = set()
    needed: List[str] = []
    for wid in storage.list_workflows():
        workflow = storage.load_workflow(wid)
        declared_any |= _declared_inputs(workflow)
        required = (workflow.get("input_schema") or {}).get("required") or []
        needed += [
            f"{wid}.{key}" for key in sorted(required) if key not in defaults
        ]
    if needed:
        print("  required inputs not covered by run_defaults "
              f"(pass them with -p at run time): {', '.join(needed)}")
    unused = sorted(set(defaults) - declared_any)
    if unused:
        print("  run_defaults keys no definition declares: "
              f"{', '.join(unused)}")


def cmd_validate(args, storage: Storage) -> int:
    if args.project:
        ids = storage.list_workflows()
        if not ids:
            raise UsageError(
                f"no workflow definitions found under {storage.workflows_dir}",
                at=str(storage.workflows_dir),
                hint="run `aiwf init` first, or install a template with "
                     "`aiwf wf install <id>`",
            )
        result = WorkflowEngine(storage).validate_project()
        print(f"valid project: {len(result['workflows'])} workflow(s)")
        for src, targets in result["edges"].items():
            print(f"  {src} -> {', '.join(targets)}")
        _report_run_defaults(storage)
        return 0
    if not args.path:
        raise UsageError(
            "validate needs a PATH, or use --project",
            hint="e.g. aiwf validate .aiwf/workflows/my-flow.json",
        )
    workflow = _read_json(args.path)
    try:
        validate_workflow(workflow)
    except SchemaError as e:
        raise SchemaError(e.message, at=f"{args.path}:{e.at}", hint=e.hint) from None
    workflow = normalize_workflow(workflow)
    WorkflowEngine(storage).references_check(workflow)
    print(f"valid: {args.path} (workflow {workflow['id']!r}, {len(workflow['nodes'])} nodes)")
    return 0


def cmd_workflows(args, storage: Storage) -> int:
    ids = storage.list_workflows()
    if getattr(args, "json", False):
        print(json.dumps(
            [{"id": wid, "name": storage.load_workflow(wid).get("name", "")}
             for wid in ids],
            ensure_ascii=False, indent=2,
        ))
        return 0
    if not ids:
        print("(no workflows; create one with the aiwf skill)")
        return 0
    for wid in ids:
        wf = storage.load_workflow(wid)
        print(f"{wid:<28} {wf.get('name', '')}")
    return 0


def cmd_run(args, storage: Storage) -> int:
    workflow = storage.load_workflow(args.workflow_id)
    engine = WorkflowEngine(storage)
    record = engine.start(
        workflow, _collect_inputs(args, workflow, storage), title=args.title
    )
    return _emit(engine, record, args)


def cmd_current(args, storage: Storage) -> int:
    args.run_id = _pick_run(storage, args.run_id, need_running=False)
    record = storage.find_run(args.run_id)
    return _emit(WorkflowEngine(storage), record, args, advance=False)


def _print_status(
    spec: Dict[str, Any],
    submit_file: Optional[str] = None,
    submit_missing: bool = False,
) -> None:
    print(f"run   {spec['run_id']}  {spec.get('title', '')}  {spec['status']}")
    node = spec.get("node")
    input_request = spec.get("input_request")
    review_request = spec.get("review_request")
    check_request = spec.get("check_request")

    if input_request:
        print(f"now   [{input_request['node_id']}] 等待用户回答")
        fields = input_request.get("fields") or []
        for field in fields:
            extra = ""
            if field.get("enum"):
                extra = "  可选: " + " | ".join(map(str, field["enum"]))
            print(f"  - {field['name']} ({field.get('type', 'string')})"
                  f" {field.get('title', '')}{extra}")
            if field.get("description"):
                print(f"      {field['description']}")
    elif review_request:
        print(f"now   [{review_request['node_id']}] 人工验收"
              f"（第 {review_request.get('attempt', 1)} 次）")
        requirements = review_request.get("requirements")
        if isinstance(requirements, list):
            for item in requirements:
                print(f"  [ ] {item}")
        elif requirements:
            print(f"  {requirements}")
        if review_request.get("output") is not None:
            print("artifact: " + json.dumps(review_request["output"], ensure_ascii=False))
    elif node:
        print(f"now   [{node['id']}] {node.get('name', node['id'])}")
    else:
        print("now   (none; run finished)")

    if node:
        if node.get("inputs"):
            print("materials:")
            for name, value in node["inputs"].items():
                print(f"  {name}: {json.dumps(value, ensure_ascii=False)}")
        if node.get("output"):
            print(f"artifact: {_posix((node['output'] or {}).get('file', ''))}")
        check = node.get("check")
        if check and not check_request:
            print(f"check : 最多 {check.get('max_attempts')} 次")
            for criterion in check.get("criteria") or []:
                print(f"  [ ] {criterion}")
        review = node.get("review")
        if review and not review_request:
            print("review: 用户验收")
            requirements = review.get("requirements")
            if isinstance(requirements, list):
                for item in requirements:
                    print(f"  [ ] {item}")
            elif requirements:
                print(f"  {requirements}")
        if node.get("feedback"):
            print(f"feedback: {str(node['feedback'])[:300]}")
        if node.get("previous_output") is not None:
            print("previous: " + json.dumps(
                node["previous_output"], ensure_ascii=False)[:300])
        if node.get("prompt"):
            print("prompt:")
            for line in str(node["prompt"]).splitlines():
                print(f"  {line}")
        if node.get("routes"):
            print("routes: " + ", ".join(node["routes"]))
        if node.get("items"):
            print("items: " + json.dumps(node["items"], ensure_ascii=False))
        if node.get("child"):
            print(f"child: run {node['child']['run_id']} "
                  f"instance {node['child']['instance']}")
        if node.get("message"):
            print(f"note  : {node['message']}")
        if node.get("output_schema"):
            print("contract: " + json.dumps(node["output_schema"], ensure_ascii=False))

    if check_request:
        print(f"check : 第 {check_request.get('attempt')}/"
              f"{check_request.get('max_attempts')} 次")
        for criterion in check_request.get("criteria") or []:
            print(f"  [ ] {criterion}")
        if check_request.get("output") is not None:
            print("artifact: " + json.dumps(check_request["output"], ensure_ascii=False))

    if spec.get("error"):
        print(f"error : {str(spec['error'])[:300]}")
    if submit_file:
        print(f"file  : {_posix(submit_file)}")
    elif submit_missing:
        print("file  : (missing; run `aiwf status --ensure` or `aiwf submit --print`)")
    next_command = _next_for_spec(spec)
    if next_command:
        print(f"next  : {next_command['text']}")
        if next_command.get("note"):
            print(f"note  : {next_command['note']}")


def cmd_status(args, storage: Storage) -> int:
    """One read-only answer to "where is this run, and what do I type next?"."""
    args.run_id = _pick_run(storage, args.run_id, need_running=False)
    engine = WorkflowEngine(storage)
    record = storage.find_run(args.run_id)
    if _new_protocol(engine, record):
        spec = engine.action_spec(record["run_id"])
    else:
        spec = engine.node_spec(_load_workflow_for_record(storage, record), record)
    workflow, _error = _resolve_definition(record, storage)
    spec["title"] = view_mod.run_title(workflow, record)
    if getattr(args, "ensure", False) and spec.get("action"):
        write_submit_template(storage, spec)
    submit_file = _submit_path(storage, spec["run_id"])
    if not os.path.isfile(submit_file):
        submit_file = None
    submit_missing = bool(spec.get("action") and not submit_file)

    if getattr(args, "json", False):
        payload = dict(spec)
        payload["submit_file"] = _posix(submit_file) if submit_file else None
        payload["submit_missing"] = submit_missing
        next_command = _next_for_spec(spec)
        if next_command is not None:
            payload["next"] = next_command
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    _print_status(spec, submit_file, submit_missing)
    return 0


def _archive_submission(storage: Storage, spec: Dict[str, Any],
                        kind: str, payload: Any, seal: bool = False) -> str:
    record = storage.find_run(spec["run_id"])
    directory = os.path.join(storage.run_dir_for_record(record), "submissions")
    os.makedirs(directory, exist_ok=True)
    existing = [
        int(name.split("-", 1)[0])
        for name in os.listdir(directory)
        if name[:4].isdigit() and name.endswith(".json")
    ]
    sequence = (max(existing) if existing else 0) + 1
    action = spec.get("action") or {}
    node = action.get("node_id") or "unknown"
    path = os.path.join(directory, f"{sequence:04d}-{node}.json")
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "run_id": spec["run_id"],
        "action": action.get("id"),
        "node": node,
        "kind": kind,
        "output": payload,
    }
    if seal:
        entry["seal"] = True
    storage.write_json_file(path, entry)
    return path


def _has_submit_flags(args) -> bool:
    return any((
        getattr(args, "file", None) is not None,
        getattr(args, "verdict", None) is not None,
        getattr(args, "reason", None) is not None,
        getattr(args, "feedback", None) is not None,
        getattr(args, "approve", False),
        getattr(args, "reject", False),
        bool(getattr(args, "set", None)),
        bool(getattr(args, "output_file", None)),
        bool(getattr(args, "empty", False)),
        getattr(args, "output", None) is not None,
        bool(getattr(args, "item", None)),
        bool(getattr(args, "item_file", None)),
        bool(getattr(args, "seal", False)),
        getattr(args, "fail_reason", None) is not None,
        bool(getattr(args, "param", None)),
        bool(getattr(args, "data_file", None)),
    ))


def _validate_submit_flags(kind: str, args) -> None:
    """Reject a payload flag that does not belong to this step's kind."""
    if kind != "items" and (getattr(args, "item", None)
                            or getattr(args, "item_file", None)):
        raise UsageError("--item / --item-file only apply to a queue step")
    if kind != "provide" and (getattr(args, "param", None)
                              or getattr(args, "data_file", None)):
        raise UsageError("-p / --data-file only apply to a provide step")
    if kind != "review" and (getattr(args, "approve", False)
                             or getattr(args, "reject", False)
                             or getattr(args, "feedback", None) is not None):
        raise UsageError("--approve / --reject / --feedback only apply to a review step")
    if kind != "check" and getattr(args, "reason", None) is not None:
        raise UsageError("--reason only applies to a check step")
    if kind in ("check", "review", "provide") and any((
        getattr(args, "file", None) is not None,
        bool(getattr(args, "set", None)),
        bool(getattr(args, "output_file", None)),
        bool(getattr(args, "empty", False)),
        getattr(args, "output", None) is not None,
    )):
        raise UsageError(f"a {kind} step uses its own payload, not "
                         "--file/--set/--json-file/--empty/--output")
    if kind not in ("check", "review") and getattr(args, "verdict", None) is not None:
        raise UsageError("--verdict only applies to check or review")


def _template_ready(template: Dict[str, Any], spec: Dict[str, Any]) -> bool:
    """Whether a freshly created template can be submitted without edits.

    Only a single-file task (file already written) or an empty mechanical step
    qualifies. check/review/provide always need a human/agent decision, and a
    structured output needs a payload.
    """
    kind = template.get("kind")
    if kind == "empty":
        return True
    if kind == "complete":
        output = template.get("output")
        if isinstance(output, dict) and output.get("$file"):
            return os.path.isfile(output["$file"])
    return False


def _submit_items(args) -> List[Any]:
    items: List[Any] = []
    for raw in getattr(args, "item", None) or []:
        parsed = _json_arg(raw)
        items.extend(parsed if isinstance(parsed, list) else [parsed])
    for path in getattr(args, "item_file", None) or []:
        parsed = _read_json(path)
        items.extend(parsed if isinstance(parsed, list) else [parsed])
    return items


def cmd_submit(args, storage: Storage) -> int:
    """The single write verb: report the current step's result.

    Payload precedence: explicit flags > ``submit.json``'s ``output``. The
    template also carries the action id, which is what restores the staleness
    guard without asking a caller to copy it.
    """
    args.run_id = _pick_run(
        storage, args.run_id,
        need_running=not getattr(args, "print_template", False),
    )
    engine = WorkflowEngine(storage)
    spec = engine.action_spec(args.run_id)
    if not spec.get("action"):
        status = spec.get("status")
        if status == "failed":
            raise UsageError(
                f"run {args.run_id} is failed; there is no step to submit",
                hint=f"recover it with `aiwf resume {args.run_id}`",
                next_command=f"aiwf resume {args.run_id}",
            )
        raise UsageError(
            f"run {args.run_id} is {status}; nothing to submit",
            hint=f"inspect it with `aiwf runs show {args.run_id}`",
        )

    template_path = _submit_path(storage, spec["run_id"])
    if getattr(args, "print_template", False):
        path = write_submit_template(storage, spec)
        if not path:
            raise UsageError("no action to write a template for")
        if args.json:
            print(json.dumps({"submit_file": path,
                              "template": build_submit_template(spec)},
                             ensure_ascii=False, indent=2))
        else:
            print(_posix(path))
            print(json.dumps(build_submit_template(spec),
                             ensure_ascii=False, indent=2))
        return 0

    template = _read_json(template_path) if os.path.isfile(template_path) else None
    if template is not None:
        if not isinstance(template, dict) or not template.get("action"):
            raise UsageError(
                f"invalid submit template: {template_path}",
                hint="rebuild it with `aiwf submit --print`",
            )
        if template["action"] != spec["action"]["id"]:
            raise StaleActionError(
                "submit.json is stale: the run has moved to another action",
                at=template_path,
                hint="run `aiwf status` to see the current step, then "
                     "`aiwf submit --print` to refresh the template",
                next_command="aiwf status",
            )
    elif not _has_submit_flags(args):
        write_submit_template(storage, spec)
        template = build_submit_template(spec)
        if not _template_ready(template, spec):
            raise UsageError(
                f"submit template created at {template_path}",
                at=template_path,
                hint="fill `output` and run `aiwf submit`, or pass a payload flag "
                     "(e.g. --file PATH / --verdict PASS --reason \"...\")",
                next_command="aiwf submit",
            )

    kind = (template or {}).get("kind") or _submit_kind(spec)
    expected_kind = _submit_kind(spec)
    if template is not None and template.get("kind") not in (None, expected_kind):
        raise UsageError(
            "submit.json kind does not match the current step",
            at=template_path,
            hint="refresh it with `aiwf submit --print`",
        )
    action_id = spec["action"]["id"]
    if getattr(args, "approve", False) and getattr(args, "reject", False):
        raise UsageError("choose --approve or --reject, not both")
    if getattr(args, "fail_reason", None) is not None:
        conflicting = any((
            getattr(args, "file", None), getattr(args, "verdict", None),
            getattr(args, "reason", None), getattr(args, "feedback", None),
            getattr(args, "approve", False), getattr(args, "reject", False),
            getattr(args, "set", None), getattr(args, "output_file", None),
            getattr(args, "empty", False), getattr(args, "output", None),
            getattr(args, "item", None), getattr(args, "item_file", None),
            getattr(args, "param", None), getattr(args, "data_file", None),
        ))
        if conflicting:
            raise UsageError("--fail takes no other payload")
    if getattr(args, "fail_reason", None) is None:
        _validate_submit_flags(kind, args)

    if getattr(args, "fail_reason", None) is not None:
        kind = "fail"
        payload: Any = args.fail_reason
        seal = False
    elif kind == "review":
        verdict = "APPROVE" if getattr(args, "approve", False) else (
            "REJECT" if getattr(args, "reject", False) else getattr(args, "verdict", None)
        )
        if verdict is None and template is not None and not _has_submit_flags(args):
            body = template.get("output") or {}
            verdict = body.get("verdict")
            feedback = body.get("feedback") or ""
        else:
            feedback = getattr(args, "feedback", None) or ""
        if verdict not in ("APPROVE", "REJECT"):
            raise UsageError(
                "review verdict must be APPROVE or REJECT",
                hint='e.g. aiwf submit --verdict APPROVE --feedback "用户认可"',
            )
        payload = {"verdict": verdict, "feedback": feedback}
    elif kind == "check":
        if getattr(args, "approve", False) or getattr(args, "reject", False):
            raise UsageError("a check step uses --verdict PASS|REVISE, not --approve")
        if _has_submit_flags(args):
            payload = _read_output(args)
        else:
            payload = (template or {}).get("output") or {}
        verdict = str(payload.get("verdict", ""))
        reason = str(payload.get("reason", "")).strip()
        if verdict not in ("PASS", "REVISE") or not reason:
            raise UsageError(
                "check needs a verdict and a reason",
                hint='edit submit.json or run aiwf submit --verdict PASS '
                     '--reason "实测依据"',
            )
        payload = {"verdict": verdict, "reason": reason}
    elif kind == "provide":
        if _has_submit_flags(args):
            payload = _read_data(args)
        else:
            payload = (template or {}).get("output") or {}
        if not isinstance(payload, dict) or not payload:
            raise UsageError(
                "provide needs at least one answer",
                hint="use -p key=value or edit submit.json's output",
            )
        required = ((spec.get("input_request") or {}).get("schema") or {}).get(
            "required") or []
        missing = [
            key for key in required
            if payload.get(key) in (None, "")
        ]
        if missing:
            raise UsageError(
                "submit.json still has unanswered fields: " + ", ".join(missing),
                hint="fill them in submit.json, or pass -p key=value",
            )
    elif kind == "items":
        if _has_submit_flags(args):
            payload = _submit_items(args)
            seal = bool(getattr(args, "seal", False))
        else:
            payload = (template or {}).get("output") or []
            seal = bool((template or {}).get("seal", False))
        if not isinstance(payload, list) or (not payload and not seal):
            raise UsageError(
                "items submit needs an item array, or seal=true to close",
                hint="put item objects in submit.json's output, or "
                     "set seal=true to finish the queue",
            )
    elif kind == "empty":
        if _has_submit_flags(args) and not getattr(args, "empty", False):
            raise UsageError("an empty step accepts no payload",
                             hint="just run aiwf submit --empty")
        payload = None
        seal = False
    else:  # complete
        if getattr(args, "verdict", None) is not None:
            raise UsageError(
                "a task step does not take --verdict",
                hint="use --file / --json-file / --set, or edit submit.json",
            )
        if _has_submit_flags(args):
            payload = _read_output(args)
        else:
            payload = (template or {}).get("output")
        seal = False
        if isinstance(payload, str) and "|" in payload and payload == (
            (template or {}).get("output")
        ):
            raise UsageError(
                "submit.json still has a placeholder value: " + payload,
                hint="replace it with one of the route labels, then rerun",
            )

    if kind == "items":
        root = engine.root_record(storage.find_run(args.run_id))
        leaf = engine._leaf_record(root)
        node_id = (spec.get("node") or {}).get("id") or spec["action"]["node_id"]
        if payload:
            engine.queue_add(leaf["run_id"], node_id, payload)
        if seal:
            engine.queue_seal(leaf["run_id"], node_id)
        engine.settle(root["run_id"])
        spec2 = engine.action_spec(root["run_id"])
    else:
        command = {
            "check": "complete", "complete": "complete", "empty": "complete",
            "provide": "provide", "review": "review", "fail": "fail",
        }[kind]
        kwargs: Dict[str, Any] = {}
        if kind in ("check", "complete", "empty"):
            kwargs["output"] = payload
        elif kind == "provide":
            kwargs["values"] = payload
        elif kind == "review":
            kwargs["verdict"] = payload["verdict"]
            kwargs["feedback"] = payload["feedback"]
        elif kind == "fail":
            kwargs["error"] = payload
        spec2 = engine.act(args.run_id, action_id, command, **kwargs)

    _attach_rejection(engine, spec2)
    if engine.last_rejection:
        print_spec(spec2, args.json)
        return 3
    _archive_submission(storage, spec, kind, payload,
                        seal=locals().get("seal", False))
    write_submit_template(storage, spec2)
    print_spec(spec2, args.json)
    return 1 if spec2.get("status") == "failed" else 0


def _read_output(args) -> Any:
    """Build the reported result from exactly one payload flag.

    The common cases have first-class flags so an agent never has to write JSON
    (and never fights shell quoting): --file for one artifact, --verdict for a
    check, --set for a small object. --output remains for advanced callers.
    """
    pairs = list(getattr(args, "set", None) or [])
    if getattr(args, "reason", None) and getattr(args, "verdict", None) is None:
        raise UsageError(
            "--reason only applies together with --verdict",
            hint='e.g. --verdict PASS --reason "关键事件完整"',
        )
    sources = {
        "--output": args.output is not None,
        "--output-file/--json-file": bool(getattr(args, "output_file", None)),
        "--empty": bool(args.empty),
        "--file": getattr(args, "file", None) is not None,
        "--verdict": getattr(args, "verdict", None) is not None,
        "--set": bool(pairs),
    }
    chosen = [name for name, used in sources.items() if used]
    if len(chosen) != 1:
        raise UsageError(
            "provide exactly one payload: " + ", ".join(sources),
            hint=("got: " + ", ".join(chosen)) if chosen else
                 "e.g. --file path/to/artifact.md, or --verdict PASS --reason \"...\"",
        )
    if args.empty:
        return None
    if getattr(args, "file", None) is not None:
        path = args.file
        if not path.strip():
            raise UsageError("--file needs a path")
        return {"$file": path}
    if getattr(args, "verdict", None) is not None:
        reason = (getattr(args, "reason", None) or "").strip()
        if not reason:
            raise UsageError(
                "--verdict needs --reason",
                hint='e.g. --verdict PASS --reason "关键事件完整，章尾有钩子"',
            )
        return {"verdict": args.verdict, "reason": reason}
    if pairs:
        result: Dict[str, Any] = {}
        for raw in pairs:
            result.update(_parse_param(raw))
        return result
    if args.output is not None:
        text = args.output
        try:  # convenient for structured branch answers
            return json.loads(text)
        except json.JSONDecodeError:
            stripped = text.lstrip()
            if stripped.startswith(("{", "[")):
                raise UsageError(
                    "the value looks like JSON but does not parse",
                    at="--output",
                    hint="a shell (PowerShell) may have stripped the double quotes; "
                         "use --json-file result.json, or --set key=value",
                ) from None
            return text
    return _read_json(args.output_file)


def _read_tokens(args) -> Optional[Dict[str, int]]:
    if args.tokens is None and not args.tokens_file:
        return None
    if args.tokens_file:
        return _read_json(args.tokens_file)
    try:
        return json.loads(args.tokens)
    except json.JSONDecodeError as e:
        raise UsageError(f"--tokens is not valid JSON: {e.msg}", at="--tokens") from None


def cmd_complete(args, storage: Storage) -> int:
    args.run_id = _pick_run(storage, args.run_id, need_running=True)
    record = storage.find_run(args.run_id)
    engine = WorkflowEngine(storage)
    # The root record's phase stays "executing" while a child is the leaf, so
    # ask the protocol for the phase of the action actually being answered.
    if _new_protocol(engine, record):
        phase = engine.action_spec(record["run_id"]).get("phase")
    else:
        phase = record.get("phase")
    if getattr(args, "verdict", None) is not None and phase != "awaiting_check":
        raise UsageError(
            f"--verdict does not apply in phase {phase!r}",
            at=f"run {args.run_id}",
            hint="a task result uses --file / --json-file / --set; "
                 "only a check action takes --verdict",
        )
    if getattr(args, "file", None) is not None and phase == "awaiting_check":
        raise UsageError(
            "a check action expects a verdict, not a file",
            at=f"run {args.run_id}",
            hint='use --verdict PASS|REVISE --reason "..."',
        )
    workflow = _load_workflow_for_record(storage, record)
    if _new_protocol(engine, record):
        return _act(engine, args, "complete", output=_read_output(args),
                    model=args.model, tokens=_read_tokens(args))
    record = engine.complete(
        workflow, args.run_id,
        output=_read_output(args),
        model=args.model,
        tokens=_read_tokens(args),
    )
    # completing a child end propagates into the (possibly parent) record
    workflow = _load_workflow_for_record(storage, record)
    spec = engine.node_spec(workflow, record)
    if engine.last_rejection:
        spec["rejected"] = engine.last_rejection
    else:
        write_submit_template(storage, spec)
    print_spec(spec, args.json)
    if engine.last_rejection:
        return 3
    return 0 if record["status"] != "failed" else 1


def cmd_provide(args, storage: Storage) -> int:
    args.run_id = _pick_run(
        storage, args.run_id, need_running=not bool(getattr(args, "node", None))
    )
    values = _read_data(args)
    if not values:
        raise UsageError(
            "provide answers via -p key=value and/or --data-file",
            hint="e.g. aiwf provide <run> --action <id> -p tone=冷峻",
        )
    record = storage.find_run(args.run_id)
    workflow = _load_workflow_for_record(storage, record)
    engine = WorkflowEngine(storage)
    if _new_protocol(engine, record) and not args.node:
        return _act(engine, args, "provide", values=values)
    record = engine.provide(
        workflow, args.run_id, values,
        node_id=args.node, reason=args.reason,
    )
    return _emit(engine, record, args)


def cmd_review(args, storage: Storage) -> int:
    args.run_id = _pick_run(storage, args.run_id, need_running=True)
    record = storage.find_run(args.run_id)
    workflow = _load_workflow_for_record(storage, record)
    engine = WorkflowEngine(storage)
    if _new_protocol(engine, record):
        return _act(engine, args, "review", verdict=args.verdict, feedback=args.feedback)
    record = engine.review(
        workflow, args.run_id, args.verdict,
        feedback=args.feedback,
    )
    return _emit(engine, record, args)


def cmd_revise(args, storage: Storage) -> int:
    args.run_id = _pick_run(storage, args.run_id, need_running=False)
    record = storage.find_run(args.run_id)
    workflow = _load_workflow_for_record(storage, record)
    engine = WorkflowEngine(storage)
    if getattr(args, "instance", None):
        if args.mode != "cascade" or args.scope != "auto" or args.nodes:
            raise UsageError(
                "--instance is a single-instance redo; drop --mode/--scope/--nodes",
                hint="e.g. aiwf revise <run> --target work --instance TASK-002 --reason '...'",
            )
        record = engine.redo(
            workflow, args.run_id, args.target, args.instance,
            reason=args.reason,
        )
        return _emit(engine, record, args)
    manual = args.nodes.split(",") if args.nodes else None
    record = engine.revise(
        workflow, args.run_id, args.target, args.reason,
        mode=args.mode, scope=args.scope, manual_nodes=manual,
    )
    return _emit(engine, record, args)


def cmd_fail(args, storage: Storage) -> int:
    args.run_id = _pick_run(storage, args.run_id, need_running=True)
    record = storage.find_run(args.run_id)
    workflow = _load_workflow_for_record(storage, record)
    engine = WorkflowEngine(storage)
    if _new_protocol(engine, record):
        _act(engine, args, "fail", error=args.error)
        return 1
    record = engine.fail(workflow, args.run_id, args.error)
    workflow = _load_workflow_for_record(storage, record)
    spec = engine.node_spec(workflow, record)
    write_submit_template(storage, spec)
    print_spec(spec, args.json)
    return 1


def cmd_resume(args, storage: Storage) -> int:
    targets = args.targets
    if len(targets) == 1:
        run_id = _resolve_run_id(targets[0], storage)
        record = storage.find_run(run_id)
        workflow = storage.load_workflow_for_record(record)
    elif len(targets) == 2:
        workflow_id, raw = targets
        run_id = _resolve_run_id(raw, storage)
        record = storage.find_run(run_id)
        workflow = storage.load_workflow_for_record(record)
        if workflow["id"] != workflow_id:
            raise UsageError(
                f"workflow {workflow_id!r} does not own run {run_id!r}",
                at=f"run {run_id}",
                hint=f"its workflow is {workflow['id']!r}; `aiwf resume {run_id}` infers it",
            )
    else:
        raise UsageError(
            "resume needs RUN_ID, or WORKFLOW_ID RUN_ID",
            hint="e.g. aiwf resume 20261025-...-abcdef",
        )
    engine = WorkflowEngine(storage)
    if _new_protocol(engine, record) and record["status"] == "running":
        leaf = engine._leaf_record(record)
        if leaf["phase"] == "awaiting_items":
            record = leaf
            workflow = storage.load_workflow_for_record(record)
    record = engine.resume(workflow, record["run_id"])
    return _emit(engine, record, args)


def cmd_redo(args, storage: Storage) -> int:
    record = storage.find_run(args.run_id)
    workflow = _load_workflow_for_record(storage, record)
    engine = WorkflowEngine(storage)
    record = engine.redo(
        workflow, args.run_id, args.node, args.instance,
        reason=args.reason,
    )
    workflow = _load_workflow_for_record(storage, record)
    return _emit(engine, record, args)


#: Instance status -> row mark for `aiwf items list`. The status vocabulary is
#: the engine's own instance one, so it matches `view --json` exactly.
_ITEM_MARKS = {"pending": "  ", "running": ">>", "success": "ok", "failed": "!!"}


def _items_text(table: Dict[str, Any]) -> str:
    seal = "sealed" if table["done"] else "open"
    lines = [f"queue {table['node']}  ({table['total']} items, {seal})"]
    for row in table["items"]:
        mark = _ITEM_MARKS.get(row["status"], "??")
        lines.append(f"  [{mark}] {row['id']}  {row['name']}  ({row['status']})")
    if not table["items"]:
        lines.append("  (empty)")
    return "\n".join(lines)


def _json_arg(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise UsageError(
            f"expected a JSON argument, got {raw!r}: {e.msg}",
            hint="quote the whole value in the calling shell; prefer --json-file "
                 "for anything longer than one line",
        ) from None


def _only_queue_node(engine, leaf: Dict[str, Any]) -> str:
    """The only queue in this run's parent chain, or a typed ambiguity error."""
    found: Dict[str, Any] = {}
    cur, seen = leaf, set()
    while True:
        found.update(cur.get("queues") or {})
        parent = cur.get("parent")
        if not parent or parent["run_id"] in seen:
            break
        seen.add(parent["run_id"])
        cur = engine.storage.find_run(parent["run_id"])
    if len(found) == 1:
        return next(iter(found))
    if not found:
        raise UsageError(
            "this run has no engine-owned queue",
            hint='a foreach node must declare items: {"queue": true}',
        )
    raise UsageError(
        "this run has multiple queues; pass --node",
        hint="queues: " + ", ".join(sorted(found)),
    )


def cmd_items(args, storage: Storage) -> int:
    """The engine-owned foreach queue -- the engine is its only writer.

    ``list`` is read-only; every other verb mutates the queue and then reports
    where the run stands, because an edit may be exactly what the run was
    waiting for (``add``/``seal`` on an ``awaiting_items`` run resume it).
    """
    args.run_id = _pick_run(
        storage, args.run_id, need_running=args.items_command != "list"
    )
    engine = WorkflowEngine(storage)
    root = engine.root_record(storage.find_run(args.run_id))
    leaf = engine._leaf_record(root)
    node = args.node or _only_queue_node(engine, leaf)
    verb = args.items_command

    if verb == "list":
        table = engine.queue_table(leaf["run_id"], node)
    elif verb == "add":
        items: List[Any] = []
        for raw in args.item or []:
            parsed = _json_arg(raw)
            items.extend(parsed if isinstance(parsed, list) else [parsed])
        for path in getattr(args, "item_file", None) or []:
            parsed = _read_json(path)
            items.extend(parsed if isinstance(parsed, list) else [parsed])
        if not items:
            raise UsageError(
                "items add needs --item '<json>' or --item-file FILE",
                hint='write [{"id":"T1","name":"first"}] to items.json, then '
                     'pass --item-file items.json',
            )
        table = engine.queue_add(leaf["run_id"], node, items)
    elif verb == "update":
        table = engine.queue_update(
            leaf["run_id"], node, args.id, _json_arg(args.set)
        )
    elif verb == "remove":
        table = engine.queue_remove(leaf["run_id"], node, args.id)
    elif verb == "move":
        table = engine.queue_move(leaf["run_id"], node, args.id, args.to)
    else:  # seal
        table = engine.queue_seal(leaf["run_id"], node)

    spec = None
    if verb != "list":
        if _new_protocol(engine, root):
            engine.settle(root["run_id"])
            spec = engine.action_spec(root["run_id"])
        else:
            leaf = engine._leaf_record(storage.find_run(root["run_id"]))
            spec = engine.node_spec(storage.load_workflow_for_record(leaf), leaf)
        write_submit_template(storage, spec)
    if args.json:
        print(json.dumps({"queue": table, "spec": spec}, ensure_ascii=False, indent=2))
    else:
        print(_items_text(table))
        if spec is not None:
            print()
            print_spec(spec, False)
    return 1 if spec and spec["status"] == "failed" else 0


def cmd_runs_list(args, storage: Storage) -> int:
    rows = []
    for wf, rid, path, parent_run in storage.iter_run_files(
        getattr(args, "workflow_id", None)
    ):
        if not getattr(args, "all", False) and parent_run is not None:
            continue
        r = storage.load_record(path)
        if getattr(args, "status", None) and r["status"] != args.status:
            continue
        workflow, _err = _resolve_definition(r, storage)
        rows.append((r["started_at"], rid, wf, r["status"], r.get("duration_ms"),
                     len(r["node_runs"]), parent_run,
                     view_mod.run_title(workflow, r)))
    ordered = sorted(rows, reverse=True)
    limit = getattr(args, "limit", 20)
    if limit and limit > 0:
        ordered = ordered[:limit]
    if getattr(args, "json", False):
        print(json.dumps(
            [{"started_at": started, "run_id": rid, "workflow_id": wf,
              "title": title, "status": status, "duration_ms": dur,
              "node_count": node_count, "parent_run": parent_run}
             for started, rid, wf, status, dur, node_count, parent_run, title
             in ordered],
            ensure_ascii=False, indent=2,
        ))
        return 0
    if not ordered:
        print("(no runs)")
        return 0
    # The title leads: it is the only column that answers "which run is this?".
    for started, rid, wf, status, dur, n, parent_run, title in ordered:
        tag = f" (of {parent_run})" if parent_run else ""
        duration = f"{dur} ms" if dur is not None else "-"
        print(f"{_cell(title, TITLE_COLUMN)}  {started}  {rid}  "
              f"{wf:<20} {status:<9} {duration:>9}  {n} nodes{tag}")
    return 0


def cmd_runs_show(args, storage: Storage) -> int:
    args.run_id = _pick_run(
        storage, args.run_id, need_running=False, prefer_running=False
    )
    record = storage.find_run(args.run_id)
    if getattr(args, "view", False):
        workflow, error = _resolve_definition(record, storage)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        derived = view_mod.derive(workflow, record, storage) if workflow else None
        if derived is None:
            print(f"run {args.run_id}: workflow declares no view", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(derived, ensure_ascii=False, indent=2))
        else:
            print(f"title      : {derived['title']}")
            _print_view(derived)
        return 0
    if args.json:
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0
    if args.node:
        for nr in record["node_runs"]:
            if nr["node_id"] == args.node:
                print(json.dumps(nr, ensure_ascii=False, indent=2))
                return 0
        print(f"node {args.node!r} not found in run {args.run_id}", file=sys.stderr)
        return 1
    _print_record_table(record, storage)
    return 0


def cmd_runs_events(args, storage: Storage) -> int:
    record = storage.find_run(args.run_id)
    if getattr(args, "json", False):
        print(json.dumps(record["events"], ensure_ascii=False, indent=2))
        return 0
    for e in record["events"]:
        line = f"{e['at']}  {e['type']}"
        if e.get("node_id"):
            line += f"  ({e['node_id']})"
        if e.get("verdict"):
            line += f"  {e['verdict']}"
        if e.get("reason"):
            line += f"  -- {e['reason'][:80]}"
        print(line)
    return 0


def cmd_runs_search(args, storage: Storage) -> int:
    query = args.query.lower()
    rows = []
    for wf, rid, path, _parent in storage.iter_run_files():
        r = storage.load_record(path)
        haystack = json.dumps(
            {
                "workflow_id": r["workflow_id"],
                "status": r["status"],
                "error": r.get("error"),
                "inputs": r["inputs"],
                "events": r["events"],
                "nodes": [
                    {"node_id": n["node_id"], "name": n["node_name"], "summary": n["summary"]}
                    for n in r["node_runs"]
                ],
            },
            ensure_ascii=False,
        ).lower()
        if query in haystack:
            rows.append({
                "started_at": r["started_at"],
                "run_id": rid,
                "workflow_id": wf,
                "status": r["status"],
                "error": r.get("error"),
            })
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        print(f"{row['started_at']}  {row['run_id']}  {row['workflow_id']:<20} "
              f"{row['status']:<9} {(row['error'] or '').strip()[:80]}")
    if not rows:
        print(f"(no runs matched: {args.query})")
    return 0


def _bundled_skills_dir() -> str:
    env_dir = os.getenv("AIWF_SKILLS_DIR")
    if env_dir:
        return env_dir
    # engine/aiwf/cli.py -> <repo>/skills
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    checkout = os.path.join(repo_root, "skills")
    if os.path.isdir(checkout):
        return checkout
    for candidate in data_candidates("skills"):
        if os.path.isdir(candidate):
            return candidate
    return checkout


def cmd_skills_list(args, storage: Storage) -> int:
    base = _bundled_skills_dir()
    names = sorted(
        d for d in os.listdir(base)
        if os.path.isfile(os.path.join(base, d, "SKILL.md"))
    ) if os.path.isdir(base) else []
    if getattr(args, "json", False):
        print(json.dumps(names, ensure_ascii=False, indent=2))
        return 0
    for name in names:
        print(name)
    return 0


def cmd_skills_install(args, storage: Storage) -> int:
    base = _bundled_skills_dir()
    target = args.target or os.path.join(".trae", "skills")
    os.makedirs(target, exist_ok=True)
    installed = []
    for name in sorted(os.listdir(base)):
        src = os.path.join(base, name)
        if not os.path.isfile(os.path.join(src, "SKILL.md")):
            continue
        dst = os.path.join(target, name)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        installed.append(dst)
    for dst in installed:
        print(f"installed: {dst}")
    print(f"\n{len(installed)} skill(s) installed. Ensure the aiwf CLI is reachable "
          f"(add engine/bin to PATH, pip install, or use PYTHONPATH=engine python3 -m aiwf).")
    return 0


# ---- template library ----------------------------------------------------
def cmd_wf_list(args, storage: Storage) -> int:
    entries = template_library.load_library(args.library)
    base = template_library.library_dir(args.library)
    if not entries:
        print(f"(no templates in {base})")
        return 0
    installed = set(storage.list_workflows())
    print(f"library: {base}")
    print(f"{'ID':<24} {'MATURITY':<13} {'LOCAL':<6} SUMMARY")
    for entry_id in sorted(entries):
        meta = entries[entry_id]["meta"] or {}
        summary = (meta.get("summary")
                   or entries[entry_id]["definition"].get("description", ""))
        print(f"{entry_id:<24} {str(meta.get('maturity') or '-'):<13} "
              f"{'yes' if entry_id in installed else '-':<6} {summary[:70]}")
    return 0


def cmd_wf_show(args, storage: Storage) -> int:
    entries = template_library.load_library(args.library)
    entry = entries.get(args.template_id)
    if entry is None:
        raise ValueError(
            f"template {args.template_id!r} not found in the library at "
            f"{template_library.library_dir(args.library)}"
        )
    meta = entry["meta"] or {}
    definition = entry["definition"]
    state = next(
        (r for r in template_library.status(storage, args.library)
         if r["id"] == args.template_id),
        None,
    )
    deps = [d for d in template_library.dependency_closure(
        args.template_id, entries) if d != args.template_id]

    print(f"id        : {args.template_id}")
    print(f"name      : {definition.get('name', '')}")
    print(f"maturity  : {meta.get('maturity', 'unspecified')}")
    if meta.get("summary"):
        print(f"summary   : {meta['summary']}")
    print(f"source    : {entry['path']}")
    print(f"schema    : {definition.get('schema_version')}")
    if state is None:
        print("local     : not installed")
    else:
        print(f"local     : {state['state']} ({state['path']})")
        print(f"upstream  : {state['upstream']}")
    print(f"deps      : {', '.join(deps) if deps else '(none)'}")

    inputs = definition.get("input_schema") or {}
    required = inputs.get("required") or []
    optional = [k for k in (inputs.get("properties") or {}) if k not in required]
    print(f"inputs    : required={', '.join(required) if required else '(none)'}"
          f"  optional={', '.join(optional) if optional else '(none)'}")
    for label, key in (("scenarios", "scenarios"), ("not_for", "not_for")):
        for value in meta.get(key) or []:
            print(f"{label:<10}: {value}")
    return 0


def cmd_wf_install(args, storage: Storage) -> int:
    installed = template_library.install(
        storage,
        args.template_id,
        as_id=getattr(args, "as_id", None),
        force=args.force,
        with_deps=not args.no_deps,
        library=args.library,
    )
    for item in installed:
        tag = "dependency" if item["dependency"] else "template"
        how = "updated" if item["updated"] else "installed"
        print(f"{how:<9} [{tag}] {item['id']} -> {item['path']}")
    print(f"\nledger   : {template_library.ledger_path(storage)}")
    print("next     : aiwf validate --project")
    return 0


def cmd_wf_diff(args, storage: Storage) -> int:
    rendered, _local, _upstream = template_library.diff(
        storage, args.template_id, args.library
    )
    if not rendered:
        print(f"{args.template_id}: identical to the library version")
        return 0
    print(rendered, end="" if rendered.endswith("\n") else "\n")
    return 0


def cmd_wf_status(args, storage: Storage) -> int:
    rows = template_library.status(storage, args.library)
    if not rows:
        print("(no templates installed from the library)")
        return 0
    print(f"{'ID':<24} {'LOCAL':<9} {'UPSTREAM':<10} {'MATURITY':<13} INSTALLED_AT")
    for row in rows:
        print(f"{row['id']:<24} {row['state']:<9} {row['upstream']:<10} "
              f"{str(row['maturity'] or '-'):<13} {row['installed_at'] or '-'}")
    print("\nLOCAL=edited 表示本地已改动，重装需 --force；"
          "UPSTREAM=updated 表示库里有新版本")
    return 0


def cmd_wf_index(args, storage: Storage) -> int:
    path = template_library.index_path(args.library)
    storage._atomic_write_text(path, template_library.render_index(args.library))
    print(f"wrote {path}")
    return 0


# ---- argparse wiring -----------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aiwf", description="Schema-driven AI workflow engine",
        epilog="Run `aiwf` with no arguments for the current step; "
               "`aiwf --help-all` lists advanced commands.",
    )
    p.add_argument("--version", action="version", version=f"aiwf {__version__}")
    p.add_argument("--help-all", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=False)

    sub.add_parser(
        "init", help=argparse.SUPPRESS,
        description="create .aiwf storage dirs and ./bin launchers",
    ).set_defaults(func=cmd_init)

    pd = sub.add_parser("doctor", help="check the environment the CLI needs")
    pd.add_argument("--library", help="template library dir to inspect")
    pd.add_argument("--json", action="store_true")
    pd.set_defaults(func=cmd_doctor)

    pv = sub.add_parser("validate", help="validate a workflow definition JSON file, or the whole project")
    pv.add_argument("path", nargs="?")
    pv.add_argument("--project", action="store_true",
                    help="validate all definitions and the subflow call graph")
    pv.set_defaults(func=cmd_validate)

    pw = sub.add_parser("workflows", help=argparse.SUPPRESS,
                        description="list saved workflows")
    pw.add_argument("--json", action="store_true")
    pw.set_defaults(func=cmd_workflows)

    pn = sub.add_parser(
        "new", help="scaffold a runnable workflow definition",
        description="Write .aiwf/workflows/<id>.json from a minimal template, "
                    "then validate and run it.",
    )
    pn.add_argument("workflow_id")
    pn.add_argument("--kind", choices=["sequential", "checked", "batch"],
                    default="sequential",
                    help="sequential: two tasks; checked: one task + check/review; "
                         "batch: queue foreach (default sequential)")
    pn.add_argument("--force", action="store_true",
                    help="overwrite an existing definition file")
    pn.set_defaults(func=cmd_new)

    pr = sub.add_parser("run", help="start a workflow run; prints first spec")
    pr.add_argument("workflow_id")
    pr.add_argument("--title", required=True,
                    help="human-facing identity of this run, e.g. what the "
                         "work is about right now; shown by `aiwf runs list`")
    pr.add_argument("-p", "--param", action="append",
                    help="input key=value (value may be JSON); repeatable")
    pr.add_argument("--input-file", help="JSON object file with inputs")
    pr.add_argument("--json", action="store_true", help="machine-readable spec envelope")
    pr.set_defaults(func=cmd_run)

    pc = sub.add_parser("current", help=argparse.SUPPRESS,
                        description="print the current spec of a run (JSON)")
    pc.add_argument("run_id", nargs="?",
                    help="run id (prefix ok); default: the sole running run, else the latest")
    pc.add_argument("--json", action="store_true")
    pc.set_defaults(func=cmd_current)

    ps = sub.add_parser(
        "status", help="one-line answer: current node and the next command",
        description="Read-only. Prints the run title/status, the current node, "
                    "and the command that continues it.",
    )
    ps.add_argument("run_id", nargs="?")
    ps.add_argument("--ensure", action="store_true",
                    help="create submit.json if a running run lacks one")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_status)

    psub = sub.add_parser(
        "submit", help="report the current step's result (the one write verb)",
        description="Reads submit.json (action + pre-filled output) and "
                    "dispatches by the current phase: task output, check "
                    "verdict, human review, user input, or queue items. "
                    "Explicit payload flags override the template.",
    )
    psub.add_argument("run_id", nargs="?")
    psub.add_argument("--file", metavar="PATH",
                      help="artifact path; becomes {\"$file\": PATH}")
    psub.add_argument("--verdict",
                      help="PASS|REVISE for check, APPROVE|REJECT for review")
    psub.add_argument("--reason", help="check reason")
    psub.add_argument("--feedback", help="review feedback")
    psub.add_argument("--approve", action="store_true", help="review: APPROVE")
    psub.add_argument("--reject", action="store_true", help="review: REJECT")
    psub.add_argument("--set", action="append", metavar="KEY=VALUE",
                      help="one scalar output field (repeatable)")
    psub.add_argument("--output", help="raw JSON/string output (advanced)")
    psub.add_argument("--output-file", "--json-file", dest="output_file",
                      metavar="FILE", help="JSON file with the whole output")
    psub.add_argument("--empty", action="store_true",
                      help="no payload (start/end steps)")
    psub.add_argument("--fail", dest="fail_reason", metavar="REASON",
                      help="report a failure instead of a result")
    psub.add_argument("--item", action="append",
                      help="one queue item as JSON (repeatable)")
    psub.add_argument("--item-file", action="append", metavar="FILE",
                      help="JSON file with one item or an array")
    psub.add_argument("--seal", action="store_true",
                      help="close the queue after adding items")
    psub.add_argument("-p", "--param", action="append",
                      help="user-input answer key=value (provide phase)")
    psub.add_argument("--data-file", help="JSON file with user answers")
    psub.add_argument("--print", dest="print_template", action="store_true",
                      help="rebuild submit.json from the current state")
    psub.add_argument("--json", action="store_true")
    psub.set_defaults(func=cmd_submit)

    pcomp = sub.add_parser(
        "complete", help=argparse.SUPPRESS,
        description="Report the current action's result. Pick exactly one "
                    "payload flag; the task packet's `next` line shows which.",
    )
    pcomp.add_argument("run_id", nargs="?",
                       help="run id (prefix ok); default: the sole running run")
    pcomp.add_argument("--action",
                       help="action id (full, unique prefix, node id, or 'current'; "
                            "default: the current action)")
    pcomp.add_argument("--file", metavar="PATH",
                       help='report {"$file": PATH} for the artifact you wrote')
    pcomp.add_argument("--verdict", choices=["PASS", "REVISE"],
                       help="check verdict; requires --reason")
    pcomp.add_argument("--reason", help="check reason (why PASS/REVISE)")
    pcomp.add_argument("--set", action="append", metavar="KEY=VALUE",
                       help="one scalar result field (repeatable; value parsed "
                            "as JSON); use --json-file for arrays/objects")
    pcomp.add_argument("--output", help="output; parsed as JSON when possible")
    pcomp.add_argument("--output-file", "--json-file", dest="output_file",
                       metavar="FILE",
                       help="JSON file containing the whole output (not the artifact)")
    pcomp.add_argument("--empty", action="store_true", help="no output (start/end)")
    pcomp.add_argument("--model", help="model used by the agent (audit)")
    pcomp.add_argument("--tokens", help="token usage JSON (audit)")
    pcomp.add_argument("--tokens-file", help="JSON file with token usage")
    pcomp.add_argument("--json", action="store_true")
    pcomp.set_defaults(func=cmd_complete)

    pprov = sub.add_parser("provide", help=argparse.SUPPRESS,
                           description="supply user input for a task node")
    pprov.add_argument("run_id", nargs="?")
    pprov.add_argument("--action", help="action id (default: the current action)")
    pprov.add_argument("--node", help="completed task node whose input is changed")
    pprov.add_argument("-p", "--param", action="append", help="key=value; repeatable")
    pprov.add_argument("--data-file", help="JSON object file with answers")
    pprov.add_argument("--reason", help="why the input changed (audit)")
    pprov.add_argument("--json", action="store_true")
    pprov.set_defaults(func=cmd_provide)

    prev2 = sub.add_parser("review", help=argparse.SUPPRESS,
                           description="submit human review verdict")
    prev2.add_argument("run_id", nargs="?")
    prev2.add_argument("--action", help="action id (default: the current action)")
    prev2.add_argument("--verdict", required=True, choices=["APPROVE", "REJECT"],
                       help="accept or reject")
    prev2.add_argument("--feedback", help="review comments for the agent")
    prev2.add_argument("--json", action="store_true")
    prev2.set_defaults(func=cmd_review)

    prev3 = sub.add_parser("revise", help="jump back to a prior task node")
    prev3.add_argument("run_id")
    prev3.add_argument("--target", required=True, help="task node to redo")
    prev3.add_argument("--reason", required=True)
    prev3.add_argument("--mode", choices=["cascade", "return"], default="cascade")
    prev3.add_argument("--scope", choices=["auto", "all", "manual"], default="auto")
    prev3.add_argument("--nodes", help="scope=manual: comma-separated node ids")
    prev3.add_argument("--instance",
                       help="redo one subflow/foreach instance (the node's instance key)")
    prev3.add_argument("--json", action="store_true")
    prev3.set_defaults(func=cmd_revise)

    pf = sub.add_parser("fail", help=argparse.SUPPRESS,
                        description="report a node failure")
    pf.add_argument("run_id", nargs="?")
    pf.add_argument("--action", help="action id (default: the current action)")
    pf.add_argument("--error", required=True)
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(func=cmd_fail)

    pres = sub.add_parser(
        "resume", help="reopen a failed run; prints its spec",
        description="RUN_ID alone infers the workflow; the legacy "
                    "WORKFLOW_ID RUN_ID form is still accepted.",
    )
    pres.add_argument("targets", nargs="+", metavar="RUN_ID",
                      help="RUN_ID, or WORKFLOW_ID RUN_ID")
    pres.add_argument("--json", action="store_true")
    pres.set_defaults(func=cmd_resume)

    prd = sub.add_parser("redo", help=argparse.SUPPRESS,
                         description="redo one subflow/foreach instance")
    prd.add_argument("run_id")
    prd.add_argument("--node", required=True, help="subflow/foreach node id")
    prd.add_argument("--instance", required=True, help="instance key (index or id_field)")
    prd.add_argument("--reason", help="why this instance is redone (audit)")
    prd.add_argument("--json", action="store_true")
    prd.set_defaults(func=cmd_redo)

    pit = sub.add_parser(
        "items",
        help=argparse.SUPPRESS,
        description="The queue is run state, not a file: items are validated "
                    "against the fixed schema {id, name, ...extensions} and "
                    "only the engine writes them. Started items are immutable; "
                    "seal closes the queue.",
    )
    isub = pit.add_subparsers(dest="items_command", required=True)

    def _items_common(parser):
        parser.add_argument("run_id", nargs="?",
                            help="run id (default: the sole running run, else latest)")
        parser.add_argument("--node",
                            help="foreach node id (default: the only queue in the run)")
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=cmd_items)
        return parser

    _items_common(isub.add_parser(
        "list", help="print the queue and each item's execution status"
    ))
    pi_add = _items_common(isub.add_parser(
        "add", help="append items (never a started one)"
    ))
    pi_add.add_argument("--item", action="append",
                        help="one item as JSON (repeatable)")
    pi_add.add_argument("--item-file", action="append", metavar="FILE",
                        help="JSON file holding one item or an array of items")
    pi_up = _items_common(isub.add_parser(
        "update", help="edit an item that has not started yet"
    ))
    pi_up.add_argument("--id", required=True, help="item id (the instance key)")
    pi_up.add_argument("--set", required=True, help="patch as a JSON object")
    pi_rm = _items_common(isub.add_parser(
        "remove", help="drop an item that has not started yet"
    ))
    pi_rm.add_argument("--id", required=True, help="item id (the instance key)")
    pi_mv = _items_common(isub.add_parser(
        "move", help="reorder an item that has not started yet"
    ))
    pi_mv.add_argument("--id", required=True, help="item id (the instance key)")
    pi_mv.add_argument("--to", required=True, type=int,
                       help="destination index (0-based); must be at or after the "
                            "first pending slot")
    _items_common(isub.add_parser(
        "seal", help="close the queue: no more additions, stop waiting"
    ))

    pruns = sub.add_parser("runs", help="run record operations")
    rsub = pruns.add_subparsers(dest="runs_command", required=True)

    rl = rsub.add_parser("list", help="list runs (top-level only; --all includes children)")
    rl.add_argument("workflow_id", nargs="?")
    rl.add_argument("--status", choices=["running", "completed", "failed"])
    rl.add_argument("--all", action="store_true", help="include nested child runs")
    rl.add_argument("--limit", type=int, default=20,
                    help="max rows, newest first (0 = all; default 20)")
    rl.add_argument("--json", action="store_true")
    rl.set_defaults(func=cmd_runs_list)

    rs = rsub.add_parser(
        "show", help="show one run record (its declared business view first)"
    )
    rs.add_argument("run_id", nargs="?",
                    help="run id (prefix ok); default: the latest run")
    rs.add_argument("--node", help="full detail of one node run")
    rs.add_argument("--view", action="store_true",
                    help="only the workflow's declared view; with --json, as JSON")
    rs.add_argument("--json", action="store_true", help="full record as JSON")
    rs.set_defaults(func=cmd_runs_show)

    rev = rsub.add_parser("events", help="show append-only event stream")
    rev.add_argument("run_id")
    rev.add_argument("--json", action="store_true")
    rev.set_defaults(func=cmd_runs_events)

    rse = rsub.add_parser("search", help="search run history")
    rse.add_argument("query")
    rse.add_argument("--json", action="store_true")
    rse.set_defaults(func=cmd_runs_search)

    ps = sub.add_parser("skills", help=argparse.SUPPRESS,
                        description="agent skill packaging")
    ssub = ps.add_subparsers(dest="skills_command", required=True)
    sl = ssub.add_parser("list", help="list bundled skills")
    sl.add_argument("--json", action="store_true")
    sl.set_defaults(func=cmd_skills_list)
    si = ssub.add_parser("install", help="install skills into a project")
    si.add_argument("--target", help="skills directory (default: .trae/skills)")
    si.set_defaults(func=cmd_skills_install)

    wf_parent = argparse.ArgumentParser(add_help=False)
    wf_parent.add_argument(
        "--library",
        help="template library dir (default: $AIWF_LIBRARY_DIR or "
             "<repo>/library/workflows)",
    )

    pwf = sub.add_parser("wf", help="bundled workflow template library")
    wsub = pwf.add_subparsers(dest="wf_command", required=True)

    wl = wsub.add_parser("list", parents=[wf_parent],
                         help="list templates available in the library")
    wl.set_defaults(func=cmd_wf_list)

    ws = wsub.add_parser("show", parents=[wf_parent],
                         help="show one template's metadata and local state")
    ws.add_argument("template_id")
    ws.set_defaults(func=cmd_wf_show)

    wi = wsub.add_parser("install", parents=[wf_parent],
                         help="copy a template and its dependencies into this project")
    wi.add_argument("template_id")
    wi.add_argument("--as", dest="as_id", metavar="NEW_ID",
                    help="install under a different workflow id")
    wi.add_argument("--force", action="store_true",
                    help="overwrite a locally edited copy")
    wi.add_argument("--no-deps", action="store_true",
                    help="install only this template, without its dependencies")
    wi.set_defaults(func=cmd_wf_install)

    wd = wsub.add_parser("diff", parents=[wf_parent],
                         help="diff the local copy against the library version")
    wd.add_argument("template_id")
    wd.set_defaults(func=cmd_wf_diff)

    wt = wsub.add_parser("status", parents=[wf_parent],
                         help="installed templates vs the library")
    wt.set_defaults(func=cmd_wf_status)

    wx = wsub.add_parser("index", parents=[wf_parent],
                         help=argparse.SUPPRESS,
                         description="regenerate library/INDEX.md from metadata")
    wx.set_defaults(func=cmd_wf_index)

    # argparse prints `==SUPPRESS==` for a subparser help set to SUPPRESS, so
    # drop the hidden entries from the listing and replace the usage metavar.
    hidden = {"init", "workflows", "current", "complete", "provide",
              "review", "fail", "redo", "items", "skills"}
    sub._choices_actions = [
        action for action in sub._choices_actions if action.dest not in hidden
    ]
    sub.metavar = "COMMAND"
    wsub._choices_actions = [
        action for action in wsub._choices_actions if action.dest != "index"
    ]
    wsub.metavar = "COMMAND"
    return p


def _full_help() -> str:
    return """aiwf — schema-driven workflow engine

  aiwf                      show the current step (same as `aiwf status`)
  aiwf status [RUN]         task packet: node, materials, artifact, next
  aiwf submit [RUN]         report the current step's result (one write verb)
  aiwf new ID --kind K      scaffold a runnable definition
  aiwf run WF --title ...   start a run
  aiwf runs ...             audit runs
  aiwf resume RUN           recover a failed run
  aiwf revise RUN ...       jump back / redo
  aiwf validate PATH        validate a definition (or --project)
  aiwf doctor               environment check
  aiwf wf ...               template library

advanced (hidden from --help, still supported):
  current  complete  provide  review  fail  redo  items

maintenance:
  init  workflows  skills  wf index
"""


def _normalize_argv(argv: Optional[List[str]]) -> List[str]:
    """Accept --json anywhere: agents habitually put global flags first, but
    argparse only knows the per-command copy. Move it to the end."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--json" in args:
        args = [a for a in args if a != "--json"] + ["--json"]
    return args


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8()
    raw = _normalize_argv(argv)
    as_json = "--json" in raw
    args = build_parser().parse_args(raw)
    if getattr(args, "help_all", False):
        print(_full_help())
        return 0
    storage = Storage()
    if not getattr(args, "command", None):
        try:
            return cmd_status(
                argparse.Namespace(run_id=None, json=as_json), storage
            )
        except AiwfError as e:
            return _fail(e, as_json)

    _deprecated = {
        "complete": "aiwf submit",
        "provide": "aiwf submit",
        "review": "aiwf submit",
        "fail": 'aiwf submit --fail "..."',
    }
    if args.command in _deprecated and not as_json:
        print(f"note: `aiwf {args.command}` is deprecated; "
              f"use `{_deprecated[args.command]}`", file=sys.stderr)

    try:
        return args.func(args, storage)
    except AiwfError as e:
        return _fail(e, as_json or getattr(args, "json", False))
    except FileNotFoundError as e:
        return _fail(
            NotFoundError(
                str(e),
                hint="list what exists: aiwf runs list / aiwf workflows; "
                     "create storage with aiwf init",
            ),
            as_json or getattr(args, "json", False),
        )
    except (ValueError, RuntimeError, KeyError, OSError) as e:
        return _fail(AiwfError(str(e)), as_json or getattr(args, "json", False))
    except Exception as e:  # last resort: an agent must never receive a traceback
        if os.getenv("AIWF_DEBUG"):
            raise
        return _fail(
            AiwfError(
                f"internal error: {type(e).__name__}: {e}",
                hint="set AIWF_DEBUG=1 to see the traceback; report this if it persists",
            ),
            as_json or getattr(args, "json", False),
        )


if __name__ == "__main__":
    sys.exit(main())
