#!/usr/bin/env python3
"""Interface benchmark for the two-command loop.

A *blind follower* drives each scenario with only `aiwf status --json` and
`aiwf submit ...`: it never chooses a verb, copies an action id, or writes a
payload object. All node work is a fixture; the point is to measure the
interface, not model quality.

Run from the repository root:

    python bench/run_bench.py

Exit code 0 means every scenario completed, with no deprecated commands, no
tracebacks and no non-zero `submit`.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "aiwf"]
ENV = dict(os.environ, PYTHONPATH=str(REPO / "engine"))


def sequential():
    return {
        "schema_version": "1.1", "id": "sequential", "name": "sequential",
        "input_schema": {"type": "object", "required": ["idea"],
                         "properties": {"idea": {"type": "string"}}},
        "nodes": [
            {"id": "draft", "type": "task", "inputs": {"idea": {"ref": "idea"}},
             "prompt": "写初稿", "output": {"file": "${run.dir}/draft.md"}},
            {"id": "polish", "type": "task", "inputs": {"draft": {"ref": "draft"}},
             "prompt": "润色", "output": {"file": "${run.dir}/final.md"}},
        ],
    }


def checked():
    return {
        "schema_version": "1.1", "id": "checked", "name": "checked",
        "input_schema": {"type": "object", "required": ["idea"],
                         "properties": {"idea": {"type": "string"}}},
        "nodes": [{
            "id": "draft", "type": "task", "inputs": {"idea": {"ref": "idea"}},
            "prompt": "写初稿", "output": {"file": "${run.dir}/draft.md"},
            "check": {"criteria": ["非空"], "max_attempts": 2},
            "review": {"requirements": ["可以定稿"]},
        }],
    }


def batch():
    return {
        "schema_version": "1.1", "id": "batch", "name": "batch",
        "nodes": [{
            "id": "work", "type": "foreach", "items": {"queue": True},
            "nodes": [{
                "id": "step", "type": "task", "inputs": {"item": {"ref": "item"}},
                "prompt": "处理 ${item.name}",
                "output": {"file": "${run.dir}/${item.id}.md"},
            }],
        }],
    }


def asked():
    return {
        "schema_version": "1.1", "id": "asked", "name": "asked",
        "input_schema": {"type": "object", "required": ["idea"],
                         "properties": {"idea": {"type": "string"}}},
        "nodes": [{
            "id": "draft", "type": "task",
            "user_input_schema": {
                "type": "object", "required": ["tone"],
                "properties": {"tone": {"type": "string",
                                        "enum": ["冷峻", "温暖"]}},
            },
            "inputs": {"idea": {"ref": "idea"}, "tone": {"ref": "tone"}},
            "prompt": "用 ${tone} 写", "output": {"file": "${run.dir}/draft.md"},
        }],
    }


SCENARIOS = {"sequential": sequential, "checked": checked,
             "batch": batch, "asked": asked}


def cli(cwd, *args):
    return subprocess.run([*CLI, *args], cwd=cwd, env=ENV,
                          text=True, encoding="utf-8", capture_output=True)


def write_artifact(cwd, path):
    target = Path(path) if Path(path).is_absolute() else Path(cwd) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("fixture", encoding="utf-8")


def drive(name, maker, *, remove_template=False):
    tmp = tempfile.TemporaryDirectory()
    try:
        cwd = tmp.name
        assert cli(cwd, "init").returncode == 0
        workflow = maker()
        (Path(cwd) / ".aiwf" / "workflows" / f"{name}.json").write_text(
            json.dumps(workflow, ensure_ascii=False), encoding="utf-8"
        )
        args = ["run", name, "--title", f"bench {name}", "--json"]
        if name != "batch":
            args += ["-p", "idea=fixture"]
        run = cli(cwd, *args)
        assert run.returncode == 0, run.stderr
        spec = json.loads(run.stdout)
        if remove_template and spec["node"].get("output"):
            write_artifact(cwd, spec["node"]["output"]["file"])
            record = Path(cwd) / ".aiwf" / "runs" / name / spec["run_id"]
            (record / "submit.json").unlink()

        commands, errors, tracebacks = 1, 0, 0
        for _ in range(40):
            status = cli(cwd, "status", "--json")
            commands += 1
            if status.returncode != 0:
                errors += 1
                break
            spec = json.loads(status.stdout)
            if spec["status"] != "running":
                break
            args = ["submit"]
            if spec.get("input_request"):
                args += ["-p", "tone=冷峻"]
            elif spec.get("review_request"):
                args += ["--approve", "--feedback", "fixture"]
            elif spec.get("check_request"):
                args += ["--verdict", "PASS", "--reason", "fixture"]
            elif spec.get("phase") == "awaiting_items":
                items = Path(cwd) / "items.json"
                items.write_text(json.dumps([
                    {"id": "A", "name": "a"}, {"id": "B", "name": "b"},
                ]), encoding="utf-8")
                args += ["--item-file", str(items), "--seal"]
            else:
                node = spec["node"]
                path = (node.get("output") or {}).get("file")
                if path:
                    write_artifact(cwd, path)
            done = cli(cwd, *args, "--json")
            commands += 1
            if done.returncode != 0:
                errors += 1
                break
            if "Traceback" in (done.stderr or ""):
                tracebacks += 1
                break
            spec = json.loads(done.stdout)
            if spec["status"] != "running":
                break
        return {
            "scenario": name, "completed": spec["status"] == "completed",
            "commands": commands, "errors": errors, "tracebacks": tracebacks,
        }
    finally:
        tmp.cleanup()


def main():
    results = [
        drive("sequential", sequential),
        drive("checked", checked),
        drive("batch", batch),
        drive("asked", asked),
        drive("sequential", sequential, remove_template=True),
    ]
    print(f"{'scenario':<12} {'done':<6} {'cmds':<5} {'errors':<7} traceback")
    for row in results:
        print(f"{row['scenario']:<12} {str(row['completed']):<6} "
              f"{row['commands']:<5} {row['errors']:<7} {row['tracebacks']}")
    ok = all(row["completed"] and row["errors"] == 0 and row["tracebacks"] == 0
             for row in results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
