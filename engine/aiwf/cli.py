"""aiwf command line interface.

The CLI is the boundary between an AI agent / human and the engine state machine:

    aiwf init                            create .aiwf/{workflows,runs}
    aiwf validate PATH                   validate a workflow definition file
    aiwf workflows                       list saved workflow ids
    aiwf run WF_ID [-p k=v] ...          start a run, print the first spec
    aiwf current RUN_ID                  print the current spec (any phase)
    aiwf complete RUN_ID --output...     report task/branch output, advance
    aiwf provide RUN_ID [-p k=v] ...     answer a node's user-input questions;
                                         --node changes a completed node (revision)
    aiwf review RUN_ID --verdict V ...   human acceptance verdict
    aiwf revise RUN_ID --target ID ...   out-of-plan jump to a prior task
    aiwf fail RUN_ID --error "..."       report node failure
    aiwf resume WF_ID RUN_ID             reopen a failed run
    aiwf runs list|show|search ...       inspect audit records
    aiwf skills list|install ...         package agent skills

Every state-changing command prints the resulting spec so an agent can loop
without re-querying. --json emits machine-readable spec envelopes.
"""
import argparse
import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional

from .engine import WorkflowEngine
from .schema import validate_workflow
from .storage import Storage


# ---- output helpers ------------------------------------------------------
def print_spec(spec_envelope: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(spec_envelope, ensure_ascii=False, indent=2))
        return

    node = spec_envelope.get("node")
    print(f"run_id   : {spec_envelope['run_id']}")
    print(f"workflow : {spec_envelope['workflow_id']}")
    print(f"status   : {spec_envelope['status']}")
    print(f"phase    : {spec_envelope['phase']}")
    if spec_envelope.get("action"):
        action = spec_envelope["action"]
        print(f"action   : {action['kind']} --action {action['id']} (target {action['run_id']})")
    if spec_envelope.get("check_request"):
        print("quality check (agent):")
        print(json.dumps(spec_envelope["check_request"], ensure_ascii=False, indent=2))
        return

    rev = spec_envelope.get("revision")
    if rev:
        print(f"revision : #{rev['seq']} target={rev['target']} mode={rev['mode']}")
        print(f"  stale : {', '.join(rev['stale_nodes'])}")
        print(f"  reused: {', '.join(rev['reused_nodes'])}")
        print(f"  redone: {', '.join(rev['redone_nodes'])}")

    input_request = spec_envelope.get("input_request")
    if input_request is not None:
        print("--- awaiting user input: ask the user, then: aiwf provide ... ---")
        print(f"node     : {input_request['node_id']}")
        for field in input_request["fields"]:
            print(f"  * {field['title']} ({field['name']}: {field['type']})")
            if field["description"]:
                print(f"      {field['description']}")
            if field.get("enum"):
                print(f"      options: {', '.join(map(str, field['enum']))}")
        return

    review_request = spec_envelope.get("review_request")
    if review_request is not None:
        print("--- awaiting human review: collect verdict, then: aiwf review ... ---")
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
        return

    if node is None:
        print("node     : (none; run finished)")
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
        print("--- prompt (execute with your own model/tools, then: aiwf complete ...) ---")
        print(node["prompt"])
        print(f"output_key: {node['output_key']}")
        if node.get("output_schema"):
            print("output contract (output MUST satisfy):")
            print("  " + json.dumps(node["output_schema"], ensure_ascii=False))
    elif ntype == "branch":
        print(f"model    : {node.get('model_hint') or '(agent default)'}")
        if node.get("deterministic"):
            print("--- deterministic condition: engine evaluates it, then: aiwf complete <run_id> --empty ---")
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
        print("action   : no execution needed; run `aiwf complete <run_id> --empty`")


def _print_record_table(record: Dict[str, Any]) -> None:
    wf = record["workflow_id"]
    print(f"run_id     : {record['run_id']}")
    print(f"workflow   : {wf}")
    print(f"status     : {record['status']} / {record['phase']}")
    print(f"started_at : {record['started_at']}")
    if record.get("ended_at"):
        print(f"ended_at   : {record['ended_at']}")
        print(f"duration   : {record.get('duration_ms')} ms")
    if record.get("error"):
        print(f"error      : {record['error']}")
    print(f"record dir : {Storage().run_dir_for_record(record)}")
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
        raise argparse.ArgumentTypeError(f"parameter must be key=value, got: {raw}")
    key, value = raw.split("=", 1)
    try:
        value = json.loads(value)  # numbers/bools/null/JSON
    except json.JSONDecodeError:
        pass
    return {key.strip(): value}


def _read_data(args) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    if getattr(args, "data_file", None):
        with open(args.data_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise SystemExit("data file must contain a JSON object")
        values.update(loaded)
    for p in getattr(args, "param", None) or []:
        values.update(_parse_param(p))
    return values


def _collect_inputs(args) -> Dict[str, Any]:
    inputs: Dict[str, Any] = {}
    if getattr(args, "input_file", None):
        with open(args.input_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise SystemExit("--input-file must contain a JSON object")
        inputs.update(loaded)
    for p in getattr(args, "param", None) or []:
        inputs.update(_parse_param(p))
    return inputs


def _load_workflow_for_record(storage: Storage, record: Dict[str, Any]):
    return storage.load_workflow_for_record(record)


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
    print_spec(spec, args.json)
    return 1 if spec["status"] == "failed" else 0


def _act(engine, args, command, **kwargs):
    spec = engine.act(args.run_id, args.action, command, **kwargs)
    print_spec(spec, args.json)
    return 1 if spec["status"] == "failed" else 0


def cmd_init(args, storage: Storage) -> int:
    storage.init_dirs()
    print(f"initialized aiwf storage at {storage.root}")
    print(f"  definitions: {storage.workflows_dir}")
    print(f"  run records: {storage.runs_dir}")
    return 0


def cmd_validate(args, storage: Storage) -> int:
    if args.project:
        result = WorkflowEngine(storage).validate_project()
        print(f"valid project: {len(result['workflows'])} workflow(s)")
        for src, targets in result["edges"].items():
            print(f"  {src} -> {', '.join(targets)}")
        return 0
    if not args.path:
        raise SystemExit("validate needs a PATH, or use --project")
    with open(args.path, "r", encoding="utf-8") as f:
        workflow = json.load(f)
    validate_workflow(workflow)
    WorkflowEngine(storage).references_check(workflow)
    print(f"valid: {args.path} (workflow {workflow['id']!r}, {len(workflow['nodes'])} nodes)")
    return 0


def cmd_workflows(args, storage: Storage) -> int:
    ids = storage.list_workflows()
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
    record = engine.start(workflow, _collect_inputs(args))
    return _emit(engine, record, args)


def cmd_current(args, storage: Storage) -> int:
    record = storage.find_run(args.run_id)
    return _emit(WorkflowEngine(storage), record, args, advance=False)


def _read_output(args) -> Any:
    sources = [args.output is not None, bool(args.output_file), args.empty]
    if sum(sources) != 1:
        raise SystemExit("provide exactly one of: --output S | --output-file F | --empty")
    if args.empty:
        return None
    if args.output is not None:
        try:  # convenient for structured branch answers
            return json.loads(args.output)
        except json.JSONDecodeError:
            return args.output
    with open(args.output_file, "r", encoding="utf-8") as f:
        return json.load(f)  # output files are JSON (use a JSON string for plain text)


def _read_tokens(args) -> Optional[Dict[str, int]]:
    if args.tokens is None and not args.tokens_file:
        return None
    if args.tokens_file:
        with open(args.tokens_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(args.tokens)


def cmd_complete(args, storage: Storage) -> int:
    record = storage.find_run(args.run_id)
    workflow = _load_workflow_for_record(storage, record)
    engine = WorkflowEngine(storage)
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
    print_spec(engine.node_spec(workflow, record), args.json)
    return 0 if record["status"] != "failed" else 1


def cmd_provide(args, storage: Storage) -> int:
    values = _read_data(args)
    if not values:
        raise SystemExit("provide answers via -p key=value and/or --data-file")
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
    record = storage.find_run(args.run_id)
    workflow = _load_workflow_for_record(storage, record)
    engine = WorkflowEngine(storage)
    manual = args.nodes.split(",") if args.nodes else None
    record = engine.revise(
        workflow, args.run_id, args.target, args.reason,
        mode=args.mode, scope=args.scope, manual_nodes=manual,
    )
    return _emit(engine, record, args)


def cmd_fail(args, storage: Storage) -> int:
    record = storage.find_run(args.run_id)
    workflow = _load_workflow_for_record(storage, record)
    engine = WorkflowEngine(storage)
    if _new_protocol(engine, record):
        _act(engine, args, "fail", error=args.error)
        return 1
    record = engine.fail(workflow, args.run_id, args.error)
    workflow = _load_workflow_for_record(storage, record)
    print_spec(engine.node_spec(workflow, record), args.json)
    return 1


def cmd_resume(args, storage: Storage) -> int:
    record = storage.find_run(args.run_id)
    workflow = storage.load_workflow_for_record(record)
    if workflow["id"] != args.workflow_id:
        raise ValueError("workflow id does not match run")
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


def cmd_runs_list(args, storage: Storage) -> int:
    rows = []
    for wf, rid, path, parent_run in storage.iter_run_files(
        getattr(args, "workflow_id", None)
    ):
        if not getattr(args, "all", False) and parent_run is not None:
            continue
        with open(path, "r", encoding="utf-8") as f:
            r = json.load(f)
        if getattr(args, "status", None) and r["status"] != args.status:
            continue
        rows.append((r["started_at"], rid, wf, r["status"], r.get("duration_ms"),
                     len(r["node_runs"]), parent_run))
    if not rows:
        print("(no runs)")
        return 0
    for started, rid, wf, status, dur, n, parent_run in sorted(rows, reverse=True):
        tag = f" (of {parent_run})" if parent_run else ""
        print(f"{started}  {rid}  {wf:<20} {status:<9} {str(dur):>9} ms  {n} nodes{tag}")
    return 0


def cmd_runs_show(args, storage: Storage) -> int:
    record = storage.find_run(args.run_id)
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
    _print_record_table(record)
    return 0


def cmd_runs_events(args, storage: Storage) -> int:
    record = storage.find_run(args.run_id)
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
    hits = 0
    for wf, rid, path, _parent in storage.iter_run_files():
        with open(path, "r", encoding="utf-8") as f:
            r = json.load(f)
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
            print(f"{r['started_at']}  {rid}  {wf:<20} {r['status']:<9} "
                  f"{(r.get('error') or '').strip()[:80]}")
            hits += 1
    if hits == 0:
        print(f"(no runs matched: {args.query})")
    return 0


def _bundled_skills_dir() -> str:
    env_dir = os.getenv("AIWF_SKILLS_DIR")
    if env_dir:
        return env_dir
    # engine/aiwf/cli.py -> <repo>/skills
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, "skills")


def cmd_skills_list(args, storage: Storage) -> int:
    base = _bundled_skills_dir()
    names = sorted(
        d for d in os.listdir(base)
        if os.path.isfile(os.path.join(base, d, "SKILL.md"))
    ) if os.path.isdir(base) else []
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


# ---- argparse wiring -----------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aiwf", description="Schema-driven AI workflow engine")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create .aiwf storage dirs").set_defaults(func=cmd_init)

    pv = sub.add_parser("validate", help="validate a workflow definition JSON file, or the whole project")
    pv.add_argument("path", nargs="?")
    pv.add_argument("--project", action="store_true",
                    help="validate all definitions and the subflow call graph")
    pv.set_defaults(func=cmd_validate)

    sub.add_parser("workflows", help="list saved workflows").set_defaults(func=cmd_workflows)

    pr = sub.add_parser("run", help="start a workflow run; prints first spec")
    pr.add_argument("workflow_id")
    pr.add_argument("-p", "--param", action="append",
                    help="input key=value (value may be JSON); repeatable")
    pr.add_argument("--input-file", help="JSON object file with inputs")
    pr.add_argument("--json", action="store_true", help="machine-readable spec envelope")
    pr.set_defaults(func=cmd_run)

    pc = sub.add_parser("current", help="print the current spec of a run")
    pc.add_argument("run_id")
    pc.add_argument("--json", action="store_true")
    pc.set_defaults(func=cmd_current)

    pcomp = sub.add_parser("complete", help="report task/branch output and advance")
    pcomp.add_argument("run_id")
    pcomp.add_argument("--action", help="current action id (required for 1.1)")
    pcomp.add_argument("--output", help="output; parsed as JSON when possible")
    pcomp.add_argument("--output-file", help="JSON file containing the output")
    pcomp.add_argument("--empty", action="store_true", help="no output (start/end)")
    pcomp.add_argument("--model", help="model used by the agent (audit)")
    pcomp.add_argument("--tokens", help="token usage JSON (audit)")
    pcomp.add_argument("--tokens-file", help="JSON file with token usage")
    pcomp.add_argument("--json", action="store_true")
    pcomp.set_defaults(func=cmd_complete)

    pprov = sub.add_parser("provide", help="supply user input for a task node")
    pprov.add_argument("run_id")
    pprov.add_argument("--action", help="current action id (required for 1.1 input)")
    pprov.add_argument("--node", help="completed task node whose input is changed")
    pprov.add_argument("-p", "--param", action="append", help="key=value; repeatable")
    pprov.add_argument("--data-file", help="JSON object file with answers")
    pprov.add_argument("--reason", help="why the input changed (audit)")
    pprov.add_argument("--json", action="store_true")
    pprov.set_defaults(func=cmd_provide)

    prev2 = sub.add_parser("review", help="submit human review verdict")
    prev2.add_argument("run_id")
    prev2.add_argument("--action", help="current action id (required for 1.1)")
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
    prev3.add_argument("--json", action="store_true")
    prev3.set_defaults(func=cmd_revise)

    pf = sub.add_parser("fail", help="report a node failure")
    pf.add_argument("run_id")
    pf.add_argument("--action", help="current action id (required for 1.1)")
    pf.add_argument("--error", required=True)
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(func=cmd_fail)

    pres = sub.add_parser("resume", help="reopen a failed run; prints its spec")
    pres.add_argument("workflow_id")
    pres.add_argument("run_id")
    pres.add_argument("--json", action="store_true")
    pres.set_defaults(func=cmd_resume)

    prd = sub.add_parser("redo", help="redo one subflow/foreach instance")
    prd.add_argument("run_id")
    prd.add_argument("--node", required=True, help="subflow/foreach node id")
    prd.add_argument("--instance", required=True, help="instance key (index or id_field)")
    prd.add_argument("--reason", help="why this instance is redone (audit)")
    prd.add_argument("--json", action="store_true")
    prd.set_defaults(func=cmd_redo)

    pruns = sub.add_parser("runs", help="run record operations")
    rsub = pruns.add_subparsers(dest="runs_command", required=True)

    rl = rsub.add_parser("list", help="list runs (top-level only; --all includes children)")
    rl.add_argument("workflow_id", nargs="?")
    rl.add_argument("--status", choices=["running", "completed", "failed"])
    rl.add_argument("--all", action="store_true", help="include nested child runs")
    rl.set_defaults(func=cmd_runs_list)

    rs = rsub.add_parser("show", help="show one run record")
    rs.add_argument("run_id")
    rs.add_argument("--node", help="full detail of one node run")
    rs.add_argument("--json", action="store_true", help="full record as JSON")
    rs.set_defaults(func=cmd_runs_show)

    rev = rsub.add_parser("events", help="show append-only event stream")
    rev.add_argument("run_id")
    rev.set_defaults(func=cmd_runs_events)

    rse = rsub.add_parser("search", help="search run history")
    rse.add_argument("query")
    rse.set_defaults(func=cmd_runs_search)

    ps = sub.add_parser("skills", help="agent skill packaging")
    ssub = ps.add_subparsers(dest="skills_command", required=True)
    ssub.add_parser("list", help="list bundled skills").set_defaults(func=cmd_skills_list)
    si = ssub.add_parser("install", help="install skills into a project")
    si.add_argument("--target", help="skills directory (default: .trae/skills)")
    si.set_defaults(func=cmd_skills_install)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    storage = Storage()
    try:
        return args.func(args, storage)
    except (ValueError, RuntimeError, KeyError, OSError) as e:
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
