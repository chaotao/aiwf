"""Project plan: a long-running task list that outlives any single run.

Deliberately **not** part of the run state machine. A plan lives at
``<AIWF_HOME>/plans/<plan-id>/``, next to ``runs/`` and ``inbox/``::

    <root>/plans/<plan-id>/
      index.md            # goals, scope, focus, task list, progress
      context.md          # optional: stable context shared by all tasks
      tasks/TASK-NNN.md   # optional: one contract per task

Why a separate layer, when runs already exist: a run records *execution* of one
workflow. A plan records *intent* over days or weeks and spans many runs -- one
task may take several runs (a revision starts a new one), one plan many
workflows. Runs answer "what happened"; a plan answers "what am I doing, and how
far along", which no single run can.

The Markdown index is the source of truth; this module is a thin layer that does
what prose alone cannot:

* **enforce the status vocabulary.** The drift that motivated this module came
  from two prose skills writing the same ``状态：`` field with no enum, so a
  value outside the set (``已验收``) appeared in the data;
* **check the dependency graph.** Every task names its deps in prose; nothing
  verified that they exist or are acyclic;
* **derive execution status from run records**, so ``进行中`` / ``待验收`` /
  ``已完成`` never need hand-writing -- they are facts the engine already stores;
* **keep goal, scope and focus machine-readable**, so the whole plan can be
  summarised in one glance instead of by reading every task file;
* **edit it a line at a time.** ``new`` / ``task add`` / ``focus`` / ``sync``
  rewrite only the line they must and leave every other byte -- prose, notes,
  必读文件 lists -- exactly as written. The inbox can re-render a whole file
  because the engine owns its every line; a plan is written by hand, so a
  writer here is a surgeon, not a printer.

Plan ids are the directory name (kebab-case). Task ids are ``TASK-NNN`` and are
unique **within a plan** -- the same numbers appear in every plan, so a run links
back through the *pair* ``(plan, task)``, carried in ``inputs.plan`` and
``inputs.task``.
"""
import os
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

INDEX_NAME = "index.md"
TASKS_DIR = "tasks"

#: Task status vocabulary. The authoritative copy is g-do's state machine;
#: it is mirrored here because verification has to happen somewhere a prose
#: skill cannot be trusted to enforce.
TASK_STATUSES = (
    "待开始", "进行中", "待验收", "已完成", "验收未通过", "阻塞", "已拆分", "已取消",
)
#: The subset g-plan may write: an executor owns the rest.
PLANNING_STATUSES = ("待开始", "已拆分", "已取消")
#: The subset derivable from run records; the rest are planning decisions.
EXECUTION_STATUSES = ("进行中", "待验收", "已完成", "验收未通过", "阻塞")

#: Plan-level status. The authoritative copy is g-plan's plan-format.md, which
#: lists 规划中 / 执行中 / 待整体验收 / 已完成 / 已暂停. ``已取消`` is not in
#: that list but is used in real data and is a real need (an abandoned plan);
#: it is accepted here and listed as a vocabulary gap rather than a defect.
PLAN_STATUSES = ("规划中", "执行中", "待整体验收", "已完成", "已暂停", "已取消")

#: 进度 labels that name task ids, mapped to the statuses they stand for. The
#: 已验收 row is handled separately: it is an ``N / M`` count, and M is not
#: assertable -- plans in one repo count active tasks (10/10 of 11) or all tasks
#: (10/12), so only the numerator is compared.
PROGRESS_LIST_LABELS = {
    "进行中": ("进行中",),
    "待验收": ("待验收",),
    "阻塞": ("阻塞",),
    "已取消或拆分": ("已取消", "已拆分"),
}

_FIELD_LINE = re.compile(r"^- ([^：:]+)[：:]\s*(.*)$")
#: A task row is ``- [TASK-NNN](link)｜name｜状态：X｜依赖：Y``. Real data varies
#: in two ways, both tolerated: the link is optional (a migrated task may be
#: ``- TASK-008｜name｜…``), and further ``｜`` segments may follow the deps
#: ("｜并入计划 ..." / "｜详情：..."), so the dependency is one segment and
#: anything after it is kept as a note.
_TASK_LINE = re.compile(
    r"^- (?:\[(TASK-\d+)\]\(([^)]+)\)|(TASK-\d+))\s*[｜|]\s*(.+?)\s*[｜|]\s*"
    r"状态：([^｜|]+?)\s*[｜|]\s*依赖：([^｜|]+?)\s*(?:[｜|]\s*(.*?))?\s*$"
)
_RANGE = re.compile(r"^TASK-(\d+)\s*[～~]\s*(?:TASK-)?(\d+)$")
_SINGLE = re.compile(r"^TASK-\d+$")
_NO_DEPS = ("无", "none", "-", "—", "")
#: A plan id is a directory name; keep it boring so links and paths stay valid.
PLAN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


# ---- paths ---------------------------------------------------------------
def index_path(root: str, plan_id: str) -> str:
    return os.path.join(root, plan_id, INDEX_NAME)


def tasks_dir(root: str, plan_id: str) -> str:
    return os.path.join(root, plan_id, TASKS_DIR)


def task_path(root: str, plan_id: str, task_id: str) -> str:
    return os.path.join(tasks_dir(root, plan_id), f"{task_id}.md")


def iter_plan_ids(root: str) -> List[str]:
    """Plan ids, sorted. A directory without an index.md is not a plan."""
    if not os.path.isdir(root):
        return []
    ids = []
    for name in sorted(os.listdir(root)):
        if os.path.isfile(index_path(root, name)):
            ids.append(name)
    return ids


# ---- small helpers -------------------------------------------------------
def _read(path: str) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _section_span(text: str, heading: str) -> Optional[Tuple[int, int]]:
    """``(start, end)`` offsets of the body of ``## heading``; end is the next
    ``##`` heading or the end of the file. Offsets rather than text, because a
    writer splices one changed line back into the original bytes -- everything
    the writer did not touch must survive byte for byte."""
    # ``[ \t]`` rather than ``\s``: a heading must not swallow the blank line
    # under it, or the span starts *past* that line and a writer splices into
    # the wrong place.
    match = re.search(rf"^##[ \t]+{re.escape(heading)}[ \t]*$", text, re.MULTILINE)
    if not match:
        return None
    start = match.end()
    nxt = re.search(r"^##\s+", text[start:], re.MULTILINE)
    end = start + nxt.start() if nxt else len(text)
    return start, end


def _section(text: str, heading: str) -> Optional[str]:
    """Body of ``## heading``, up to the next ``##`` heading (None if absent)."""
    span = _section_span(text, heading)
    return None if span is None else text[span[0]:span[1]]


def _parse_fields(body: Optional[str]) -> Dict[str, str]:
    """``- 名称：值`` lines into a dict. An indented ``- `` line under a field
    with no value (or a following plain indented line) continues that field on a
    new line, so the list-shaped 必读文件 block round-trips as text."""
    fields: Dict[str, str] = {}
    key: Optional[str] = None
    for raw in (body or "").splitlines():
        if not raw.strip():
            continue
        stripped = raw.strip()
        match = _FIELD_LINE.match(stripped)
        if match:
            key = match.group(1).strip()
            fields[key] = match.group(2).strip()
            continue
        if key and raw.startswith(" "):
            item = stripped[1:].strip() if stripped.startswith("-") else stripped
            fields[key] = f"{fields[key]}\n{item}".strip("\n") if fields[key] else item
    return fields


def parse_deps(text: str) -> List[str]:
    """``TASK-001、TASK-003`` -> ids. Also expands range shorthand seen in real
    data (``TASK-003～007``). An unrecognised token is kept as-is so ``check``
    can report it rather than silently dropping a dependency."""
    text = (text or "").strip()
    if text in _NO_DEPS:
        return []
    out: List[str] = []
    for part in re.split(r"[、,，;；]", text):
        part = part.strip()
        if not part:
            continue
        span = _RANGE.match(part)
        if span:
            start, end = int(span.group(1)), int(span.group(2))
            if start <= end and end - start < 500:
                out.extend("TASK-%03d" % n for n in range(start, end + 1))
                continue
        out.append(part)
    return out


# ---- parsing -------------------------------------------------------------
def parse_index(text: str, plan_id: Optional[str] = None) -> Dict[str, Any]:
    """Parse an index.md into {id, name, info, focus, tasks, progress}."""
    name = ""
    head = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    if head:
        name = head.group(1).strip()

    tasks: List[Dict[str, Any]] = []
    for line in (_section(text, "任务") or "").splitlines():
        match = _TASK_LINE.match(line.strip())
        if not match:
            continue
        deps_text = match.group(6).strip()
        tasks.append({
            "id": match.group(1) or match.group(3),
            "link": (match.group(2) or "").strip(),
            "name": match.group(4).strip(),
            "status": match.group(5).strip(),
            "deps_text": deps_text,
            "deps": parse_deps(deps_text),
            "note": (match.group(7) or "").strip(),
        })

    return {
        "id": plan_id,
        "name": name,
        "info": _parse_fields(_section(text, "计划信息")),
        "focus": _parse_fields(_section(text, "当前焦点")),
        "tasks": tasks,
        "progress": _parse_fields(_section(text, "进度")),
    }


def load(root: str, plan_id: str) -> Dict[str, Any]:
    """Read one plan. A missing index.md is an error, not an empty plan."""
    path = index_path(root, plan_id)
    text = _read(path)
    if text is None:
        known = ", ".join(iter_plan_ids(root)) or "(none)"
        raise ValueError(
            f"no plan {plan_id!r} at {path}; known plans: {known}"
        )
    plan = parse_index(text, plan_id=plan_id)
    plan["path"] = path
    #: Raw text, so a writer splices one line instead of re-rendering the file.
    plan["text"] = text
    return plan


def load_all(root: str) -> List[Dict[str, Any]]:
    return [load(root, plan_id) for plan_id in iter_plan_ids(root)]


# ---- derived facts -------------------------------------------------------
def counts(plan: Dict[str, Any]) -> Dict[str, int]:
    """Task tallies by status, plus ``total``. ``已验收`` counts 已完成."""
    tally: Dict[str, int] = {"total": len(plan["tasks"])}
    for task in plan["tasks"]:
        tally[task["status"]] = tally.get(task["status"], 0) + 1
    return tally


def focus_task_id(plan: Dict[str, Any]) -> Optional[str]:
    """The task named in 当前焦点, or None when it is empty / 无."""
    raw = (plan.get("focus", {}).get("当前任务") or "").strip()
    if not raw or raw in _NO_DEPS:
        return None
    match = re.search(r"TASK-\d+", raw)
    return match.group(0) if match else None


def outstanding(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Tasks still to do: everything not 已完成 / 已取消 / 已拆分, in order."""
    done = ("已完成", "已取消", "已拆分")
    return [t for t in plan["tasks"] if t["status"] not in done]


def derive_from_runs(storage: Any, plan_id: str) -> Dict[str, Dict[str, Any]]:
    """``task_id -> {status, run_id, started_at}`` derived from run records.

    A run links to a task through ``inputs.plan`` / ``inputs.task``; the newest
    run for a task wins. Only execution states are derived -- planning states
    (待开始 / 已拆分 / 已取消) have no run to read and stay as written.
    """
    latest: Dict[str, Dict[str, Any]] = {}
    for _wf, rid, path, parent in storage.iter_run_files():
        if parent is not None:
            continue
        try:
            record = storage.load_record(path)
        except (OSError, ValueError):
            continue
        inputs = record.get("inputs") or {}
        if inputs.get("plan") != plan_id:
            continue
        task_id = inputs.get("task")
        if not task_id:
            continue
        started = record.get("started_at") or ""
        prev = latest.get(task_id)
        if prev is None or started >= prev.get("started_at", ""):
            latest[task_id] = {
                "status": _derive_task_status(record),
                "run_id": rid,
                "started_at": started,
            }
    return latest


def _derive_task_status(record: Dict[str, Any]) -> str:
    """Map one run record to a task status.

    Conservative on purpose: a run that failed or was abandoned still means the
    task is 进行中, not 已完成 -- only a completed run closes a task. A rejected
    review surfaces separately as 验收未通过 when the last verdict was REJECT.
    """
    status = record.get("status")
    if status == "completed":
        return "已完成"
    for event in reversed(record.get("events") or []):
        if event.get("type") == "review_decided":
            if event.get("verdict") == "REJECT":
                return "验收未通过"
            break
    if status == "running":
        return "待验收" if record.get("phase") == "awaiting_review" else "进行中"
    return "进行中"


# ---- verification --------------------------------------------------------
def check(root: str, plan_id: Optional[str] = None) -> List[str]:
    """Read-only check. Returns a list of problems (empty is healthy).

    An empty plans directory is healthy, not an error: like a missing inbox
    ledger it simply means nothing has been planned yet.
    """
    problems: List[str] = []
    for pid in ([plan_id] if plan_id else iter_plan_ids(root)):
        try:
            plan = load(root, pid)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        problems.extend(_check_plan(plan))
    return problems


def _check_plan(plan: Dict[str, Any]) -> List[str]:
    pid = plan["id"]
    out: List[str] = []
    where = lambda msg: f"{pid}: {msg}"  # noqa: E731 - terse by design

    status = (plan["info"].get("状态") or "").strip()
    if not status:
        out.append(where("计划信息 missing 状态"))
    elif status not in PLAN_STATUSES:
        out.append(where(
            f"plan status {status!r} is outside the vocabulary "
            f"({' / '.join(PLAN_STATUSES)})"
        ))

    for field in ("目标", "范围"):
        if not (plan["info"].get(field) or "").strip():
            out.append(where(f"计划信息 missing {field} (required to be a plan)"))

    if not plan["tasks"]:
        out.append(where("no tasks listed"))

    known = {t["id"] for t in plan["tasks"]}
    for task in plan["tasks"]:
        if task["status"] not in TASK_STATUSES:
            out.append(where(
                f"{task['id']} status {task['status']!r} is outside the vocabulary "
                f"({' / '.join(TASK_STATUSES)})"
            ))
        for dep in task["deps"]:
            if dep == task["id"]:
                out.append(where(f"{task['id']} depends on itself"))
            elif dep not in known:
                out.append(where(f"{task['id']} depends on unknown {dep}"))

    out.extend(where(msg) for msg in _check_acyclic(plan["tasks"]))

    focus = focus_task_id(plan)
    if focus and focus not in known:
        out.append(where(f"当前焦点 names {focus}, which is not a task"))

    out.extend(where(msg) for msg in _check_progress(plan))
    return out


def _check_acyclic(tasks: List[Dict[str, Any]]) -> List[str]:
    """Report dependency cycles. Iterative DFS; a cycle is reported once."""
    graph = {t["id"]: [d for d in t["deps"] if d != t["id"]] for t in tasks}
    problems: List[str] = []
    state: Dict[str, int] = {}          # 0 unvisited, 1 on-stack, 2 done
    reported = set()

    for root_id in graph:
        if state.get(root_id):
            continue
        stack: List[Tuple[str, int]] = [(root_id, 0)]
        path: List[str] = []
        while stack:
            node, index = stack.pop()
            if index == 0:
                state[node] = 1
                path.append(node)
            if index < len(graph.get(node, [])):
                stack.append((node, index + 1))
                nxt = graph[node][index]
                if state.get(nxt) == 1:
                    cycle = path[path.index(nxt):] + [nxt]
                    key = tuple(sorted(set(cycle)))
                    if key not in reported:
                        reported.add(key)
                        problems.append(f"dependency cycle: {' -> '.join(cycle)}")
                elif not state.get(nxt):
                    stack.append((nxt, 0))
            else:
                state[node] = 2
                if path and path[-1] == node:
                    path.pop()
    return problems


def _check_progress(plan: Dict[str, Any]) -> List[str]:
    """The 进度 block restates facts the task list already holds; report where it
    has drifted rather than trusting a hand-maintained tally."""
    out: List[str] = []
    progress = plan.get("progress") or {}
    if not progress:
        return out

    head = re.match(r"^(\d+)", (progress.get("已验收") or "").strip())
    done = counts(plan).get("已完成", 0)
    if head and int(head.group(1)) != done:
        out.append(f"进度 已验收 says {head.group(1)}, task list has {done} 已完成")

    known = {t["id"] for t in plan["tasks"]}
    for label, statuses in PROGRESS_LIST_LABELS.items():
        if label not in progress:
            continue
        # Compare ids within this plan only: the row may also point at tasks of
        # another plan (real data: "TASK-002→demo01 TASK-009").
        listed = {i for i in re.findall(r"TASK-\d+", progress[label] or "") if i in known}
        want = {t["id"] for t in plan["tasks"] if t["status"] in statuses}
        if listed != want:
            out.append(
                f"进度 {label} lists {', '.join(sorted(listed)) or 'none'}, "
                f"task list has {', '.join(sorted(want)) or 'none'}"
            )
    return out


# ---- writing -------------------------------------------------------------
# The inbox re-renders its whole ledger, because the engine owns every line of
# it. A plan is the opposite: a human writes it, so a writer here is a surgeon,
# not a printer. Every function below finds the one line at fault and splices a
# replacement into the original bytes -- prose, notes, 必读文件 lists and blank
# lines all survive untouched. That is also why these live in the engine: a
# prose skill doing search-and-replace has no enum to check the new value
# against and no dependency graph to check it with.

#: Statuses a sync may overwrite. 已取消 / 已拆分 are planning decisions a run
#: cannot contradict -- an old run must never resurrect a dropped task.
_SYNCABLE = ("待开始",) + EXECUTION_STATUSES

#: 进度 labels written back as a list of task ids, and the statuses behind them.
PROGRESS_LIST_LABELS_WRITE: Dict[str, Tuple[str, ...]] = dict(PROGRESS_LIST_LABELS)
PROGRESS_LIST_LABELS_WRITE["验收未通过"] = ("验收未通过",)


def _write(path: str, content: str) -> None:
    """Temp file + rename, like the inbox: a crash mid-write cannot leave an
    index.md half-edited -- and this is the one file a human also edits."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    os.replace(tmp, path)


def save(root: str, plan_id: str, text: str) -> str:
    """Write an index.md verbatim; callers hand in text that came from ``load``
    with one splice applied."""
    path = index_path(root, plan_id)
    _write(path, text)
    return path


def _render_task_line(
    task_id: str,
    name: str,
    status: str,
    deps: List[str],
    *,
    link: bool = True,
    note: Optional[str] = None,
) -> str:
    label = f"[{task_id}](tasks/{task_id}.md)" if link else task_id
    deps_text = "、".join(deps) if deps else "无"
    line = f"- {label}｜{name}｜状态：{status}｜依赖：{deps_text}"
    if note:
        line += f"｜{note}"
    return line


def _insert_task_line(text: str, line: str) -> str:
    """Append a row after the last one the 任务 section already has; with no
    rows yet it lands where the first row would go, keeping one blank line
    between the list and the heading below it."""
    span = _section_span(text, "任务")
    if span is None:
        raise ValueError("index.md has no '## 任务' section to add a task to")
    head, body, tail = text[:span[0]], text[span[0]:span[1]], text[span[1]:]
    lines = body.splitlines(keepends=True)
    rows = [i for i, l in enumerate(body.splitlines()) if _TASK_LINE.match(l.strip())]
    if rows:
        at = rows[-1] + 1
    else:
        # body opens with the heading's own newline; step over it and the blank
        # line under it, not into them.
        at = 1 if lines and not lines[0].strip() else 0
        while at < len(lines) and not lines[at].strip():
            at += 1
    lines.insert(at, line + "\n")
    blank_after = at + 1 < len(lines) and not lines[at + 1].strip()
    if not tail.startswith("\n") and not blank_after:
        lines.insert(at + 1, "\n")          # keep the list off the next ##
    return head + "".join(lines) + tail


def _task_line_pattern(task_id: str):
    """Matches one task row, capturing only its status field: the row's name,
    deps and trailing note are the author's and must come back out unchanged."""
    token = re.escape(task_id)
    return re.compile(
        rf"^(-(?:\s+)(?:\[{token}\]\([^)\n]*\)|{token})"
        rf"(?:\s*[｜|]\s*[^｜|\n]+?)\s*[｜|]\s*状态[：:])"
        rf"([^｜|\n]+?)"
        rf"(\s*(?:[｜|][^\n]*)?)$",
        re.MULTILINE,
    )


def _rewrite_task_status(text: str, task_id: str, status: str) -> Tuple[str, bool]:
    new_text, count = _task_line_pattern(task_id).subn(
        lambda match: match.group(1) + status + match.group(3), text, count=1
    )
    return new_text, bool(count)


def _field_value(body: str, label: str) -> Optional[str]:
    match = re.search(rf"^-\s*{re.escape(label)}\s*[：:]\s*(.*)$", body, re.MULTILINE)
    return match.group(1).strip() if match else None


def _rewrite_focus(
    text: str, task_id: Optional[str], status: Optional[str]
) -> Tuple[str, List[Dict[str, Any]]]:
    """Rewrite the 当前任务 / 当前状态 lines of 当前焦点, nothing else."""
    span = _section_span(text, "当前焦点")
    if span is None:
        raise ValueError("index.md has no '## 当前焦点' section")
    body = text[span[0]:span[1]]
    changes: List[Dict[str, Any]] = []

    def rewrite(label: str, value: str) -> None:
        nonlocal body
        pattern = re.compile(rf"^(-\s*{re.escape(label)}\s*[：:])(.*)$", re.MULTILINE)
        match = pattern.search(body)
        if not match:
            return
        old = match.group(2).strip()
        if old == value:
            return
        body = body[:match.start()] + match.group(1) + value + body[match.end():]
        changes.append({"field": label, "old": old, "new": value})

    rewrite("当前任务", task_id or "无")
    if status:
        rewrite("当前状态", status)
    return text[:span[0]] + body + text[span[1]:], changes


def _rewrite_focus_status(
    text: str, task_id: str, status: str
) -> Tuple[str, Optional[str]]:
    """Follow the focus status, but only when it is *nothing but* a status word.

    ``当前状态：待开始`` is the same fact as the task row and may drift with it.
    ``待验收（代码完成，274 测试全绿）`` is the author explaining something; a
    bare swap would leave that explanation attached to the wrong status, so it
    stays put. Returns ``(text, old_value_or_None)``.
    """
    span = _section_span(text, "当前焦点")
    if span is None:
        return text, None
    body = text[span[0]:span[1]]
    if (_field_value(body, "当前任务") or "").strip() != task_id:
        return text, None
    current = _field_value(body, "当前状态")
    if current is None or current == status or current not in TASK_STATUSES:
        return text, None
    pattern = re.compile(r"^(-\s*当前状态\s*[：:])(.*)$", re.MULTILINE)
    body = pattern.sub(lambda match: match.group(1) + status, body, count=1)
    return text[:span[0]] + body + text[span[1]:], current


def _sync_progress(
    text: str, plan: Dict[str, Any], *, renumber: bool
) -> Tuple[str, List[Dict[str, Any]]]:
    """Restate the 进度 block from the task list. Only labels already present
    are touched: a plan that never had a 验收未通过 row does not grow one."""
    span = _section_span(text, "进度")
    if span is None:
        return text, []
    body = text[span[0]:span[1]]
    changes: List[Dict[str, Any]] = []

    by_status: Dict[str, List[str]] = {}
    for task in plan["tasks"]:
        by_status.setdefault(task["status"], []).append(task["id"])
    done = len(by_status.get("已完成", []))
    total = len(plan["tasks"])

    def ids_for(statuses: Tuple[str, ...]) -> str:
        found = sorted(i for st in statuses for i in by_status.get(st, []))
        return "、".join(found) if found else "无"

    def rewrite(label: str, value: str) -> None:
        nonlocal body
        pattern = re.compile(rf"^(-\s*{re.escape(label)}\s*[：:])(.*)$", re.MULTILINE)
        match = pattern.search(body)
        if not match:
            return
        old = match.group(2).strip()
        if old == value:
            return
        body = body[:match.start()] + match.group(1) + value + body[match.end():]
        changes.append({"field": f"进度 {label}", "old": old, "new": value})

    old_done = _field_value(body, "已验收")
    if old_done is not None:
        head = re.match(r"^(\d+)(?:\s*/\s*(\d+))?", old_done)
        # Anything after "N / M" is the author's -- a parenthetical naming the
        # accepted tasks, say -- and stays exactly as written.
        tail = old_done[head.end():]
        # The denominator is a convention each plan picked for itself (10/10 of
        # 11, or 10/12); only --renumber forces it to the task count.
        if head.group(2) is not None or renumber:
            denom = str(total) if renumber else head.group(2)
            value = f"{done} / {denom}"
        else:
            value = str(done)
        rewrite("已验收", value + tail)

    known = {t["id"] for t in plan["tasks"]}
    for label, statuses in PROGRESS_LIST_LABELS_WRITE.items():
        old = _field_value(body, label)
        if old is None:
            continue
        # Rewrite only when the ids are actually wrong. These rows carry the
        # author's own notes ("TASK-002→demo01 TASK-009", "TASK-004（并入 x）"),
        # and a cosmetic re-listing would discard them for nothing.
        listed = {i for i in re.findall(r"TASK-\d+", old) if i in known}
        want = {t["id"] for t in plan["tasks"] if t["status"] in statuses}
        if listed != want:
            rewrite(label, ids_for(statuses))
    return text[:span[0]] + body + text[span[1]:], changes


def render_index(
    plan_id: str,
    *,
    name: str,
    goal: str,
    scope: str,
    status: str = "规划中",
    game: Optional[str] = None,
    today: Optional[str] = None,
) -> str:
    """A fresh index.md, shaped like the hand-written ones it is modelled on:
    same headings, same field order, so the first human edit looks native."""
    stamp = today or date.today().isoformat()
    lines = [f"# {name}", "", "## 计划信息"]
    if game:
        lines.append(f"- 游戏：`{game}`")
    lines += [
        f"- 计划 ID：{plan_id}",
        f"- 状态：{status}",
        f"- 目标：{goal}",
        f"- 范围：{scope}",
        f"- 创建日期：{stamp}",
        f"- 最近更新：{stamp}",
        "",
        "## 当前焦点",
        "- 当前任务：无",
        f"- 当前状态：{status}",
        "- 下一步：无",
        "- 阻塞：无",
        "- 必读文件：无",
        "",
        "## 任务",
        "",
        "## 进度",
        # No denominator: each plan picks that convention for itself (10/10 of
        # 11, or 10/12), and a baked-in 0 would go stale the moment a task is
        # added. Write one by hand when the plan wants one; --renumber sets it
        # to the task count.
        "- 已验收：0",
        "- 进行中：无",
        "- 待验收：无",
        "- 阻塞：无",
        "- 已取消或拆分：无",
        "",
    ]
    return "\n".join(lines)


def new_plan(
    root: str,
    plan_id: str,
    *,
    goal: str,
    scope: str,
    name: Optional[str] = None,
    status: str = "规划中",
    game: Optional[str] = None,
    today: Optional[str] = None,
) -> str:
    """Create an empty plan. Refuses to touch an existing path: a plan is
    project memory, and overwriting one silently is unrecoverable."""
    if not PLAN_ID_PATTERN.match(plan_id or ""):
        raise ValueError(
            f"plan id must match {PLAN_ID_PATTERN.pattern} "
            f"(kebab-case, no spaces); got {plan_id!r}"
        )
    if status not in PLAN_STATUSES:
        raise ValueError(
            f"plan status must be one of {' / '.join(PLAN_STATUSES)}; got {status!r}"
        )
    path = index_path(root, plan_id)
    if os.path.exists(path):
        raise ValueError(f"plan {plan_id!r} already exists at {path}")
    _write(path, render_index(
        plan_id, name=(name or plan_id), goal=goal, scope=scope,
        status=status, game=game, today=today,
    ))
    return path


def add_task(
    root: str,
    plan_id: str,
    task_id: str,
    *,
    name: str,
    status: str = "待开始",
    deps: Optional[List[str]] = None,
    link: bool = True,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one row to 任务.

    The row is checked against the plan it joins *before* it is written: the id
    must be free, every dependency must exist, the status must be in the
    vocabulary. ``check`` would catch the same faults later, but a broken edge
    written now is a broken edge a human has to hunt down later.
    """
    task_id = (task_id or "").strip().upper()
    if not _SINGLE.match(task_id):
        raise ValueError(f"task id must look like TASK-001; got {task_id!r}")
    name = (name or "").strip()
    if not name:
        raise ValueError("a task needs a name (the ｜名称｜ column)")
    if status not in TASK_STATUSES:
        raise ValueError(
            f"task status must be one of {' / '.join(TASK_STATUSES)}; got {status!r}"
        )
    deps = [d.strip().upper() for d in (deps or []) if d.strip()]

    plan = load(root, plan_id)
    known = {t["id"] for t in plan["tasks"]}
    if task_id in known:
        raise ValueError(f"{task_id} is already in plan {plan_id!r}")
    for dep in deps:
        if dep == task_id:
            raise ValueError(f"{task_id} cannot depend on itself")
        # A brand-new id cannot close a cycle: nothing can depend on a row that
        # does not exist yet, so only the edges into the existing graph matter.
        if dep not in known:
            raise ValueError(
                f"unknown dependency {dep}; {plan_id!r} has "
                f"{', '.join(sorted(known)) or 'no tasks yet'}"
            )
    line = _render_task_line(task_id, name, status, deps, link=link, note=note)
    save(root, plan_id, _insert_task_line(plan["text"], line))
    return {"plan": plan_id, "task": task_id, "line": line}


def set_focus(
    root: str, plan_id: str, task_id: Optional[str], *, status: Optional[str] = None
) -> Dict[str, Any]:
    """Point the plan at a task -- or at nothing, with ``none``. The focus
    status follows that task's own status unless ``--status`` overrides it."""
    plan = load(root, plan_id)
    target = (task_id or "").strip()
    if target in ("", "none", "无"):
        target = None
    if target is None:
        if status is not None and status not in PLAN_STATUSES + TASK_STATUSES:
            raise ValueError(f"unknown status {status!r}")
    else:
        known = {t["id"]: t for t in plan["tasks"]}
        if target not in known:
            raise ValueError(
                f"no task {target} in plan {plan_id!r}; known: "
                f"{', '.join(sorted(known)) or '(none)'}"
            )
        if status is None:
            status = known[target]["status"]
        elif status not in TASK_STATUSES:
            raise ValueError(
                f"task status must be one of {' / '.join(TASK_STATUSES)}; "
                f"got {status!r}"
            )
    text, changes = _rewrite_focus(plan["text"], target, status)
    if changes:
        save(root, plan_id, text)
    return {"plan": plan_id, "focus": target, "status": status, "changes": changes}


def sync(
    plan: Dict[str, Any], derived: Dict[str, Dict[str, Any]], *, renumber: bool = False
) -> Tuple[str, List[Dict[str, Any]]]:
    """Fold run facts back into the index text; returns ``(text, changes)``.

    Only what a run actually owns is written -- a task's 状态, the 当前焦点
    status when that is nothing but a status word, and the 进度 block derived
    from the two. Planning states are left alone (a run cannot un-cancel a
    task), and so is any focus carrying a note: "待验收（手动实机验收）" is the
    author explaining something, and a bare status swap would make it a lie.
    """
    text = plan["text"]
    changes: List[Dict[str, Any]] = []

    for task in plan["tasks"]:
        info = derived.get(task["id"])
        if not info or task["status"] not in _SYNCABLE:
            continue
        new_status = info["status"]
        if new_status == task["status"] or new_status not in EXECUTION_STATUSES:
            continue
        text, written = _rewrite_task_status(text, task["id"], new_status)
        if not written:
            continue
        changes.append({
            "field": f"{task['id']} 状态",
            "old": task["status"],
            "new": new_status,
            "run_id": info.get("run_id"),
        })
        task["status"] = new_status

    # The focus is checked separately from the loop: a task synced in an earlier
    # run leaves the focus stale, and that drift would never be repaired if the
    # check only ran when the row itself changed.
    focused = focus_task_id(plan)
    if focused:
        settled = next((t["status"] for t in plan["tasks"] if t["id"] == focused), None)
        if settled:
            text, focus_old = _rewrite_focus_status(text, focused, settled)
            if focus_old is not None:
                changes.append({
                    "field": "当前焦点 当前状态",
                    "old": focus_old,
                    "new": settled,
                })

    text, progress_changes = _sync_progress(text, plan, renumber=renumber)
    return text, changes + progress_changes
