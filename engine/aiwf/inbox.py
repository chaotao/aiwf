"""Project inbox: park a thought or problem without blocking the current work.

Deliberately **not** part of the run state machine. An inbox lives at
``<AIWF_HOME>/inbox/``, next to ``runs/``, and outlives every run: most items
are jotted down while an agent is just editing code, with no run in flight. So
nothing here imports ``engine``/``storage`` run APIs, the CLI has no command
that feeds inbox items into a run, and the run record is never touched.

Layout::

    <root>/inbox/
      inbox.md           # open items only
      history.md         # handed off / dropped; created on first archive
      notes/INB-NNN.md   # optional, only when one item needs more than a page

The Markdown files are the source of truth; this module is a thin layer that
does the four things prose alone cannot: allocate ids, refuse an item without a
``为何停放`` reason, archive in one step (status + history + ledger), and verify
the whole thing (``aiwf inbox check``).

Two conventions worth stating up front, because they are what keep the ledger
scannable:

* an item holds **one cohesive topic** - a new thought first tries to merge
  into an existing item (``--merge-into``, recorded as ``吸收``), and only opens
  a new id when it cannot;
* an item is **parked for a reason** - ``为何停放`` is required and enumerated.
  Missing it means the item probably belongs to the current node's work, which
  is not parkable: fix it through ``check``/``fail``/``revise`` instead.

Ids (``INB-NNN``) are shared by ledger and history, increase monotonically and
are never reused, so ``拆自`` / ``吸收`` / ``相关`` keep pointing at something.
"""
import copy
import os
import re
import unicodedata
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

LEDGER_NAME = "inbox.md"
HISTORY_NAME = "history.md"
NOTES_DIR = "notes"

OPEN = "未处理"
ARCHIVED = ("已交接", "丢弃")
_LEGACY_ARCHIVED = "已消化"   # g-inbox wording; accepted and normalised
STATUSES = (OPEN,) + ARCHIVED

#: ``为何停放`` values. An open item must carry one of these (several may be
#: joined with ``/``): the forced answer to "why can this wait?". A problem in
#: the *current node's own output* is not parkable and must go through
#: ``check``/``fail``/``revise`` instead.
PARK_REASONS = ("非本节点", "非本流程后续节点", "用户明确不挡", "范围收缩")

TYPES = ("想法", "体验", "问题", "技术债")

#: An item holds one cohesive topic; longer material belongs in
#: ``notes/INB-NNN.md`` so the ledger stays scannable.
SUMMARY_LIMIT = 500
_INDEX_SUMMARY_LIMIT = 80

#: Field order in a rendered item, and the label used to write it.
_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("status", "状态"),
    ("type", "类型"),
    ("date", "日期"),
    ("source", "来源"),
    ("summary", "摘要"),
    ("reason", "为何停放"),
    ("split_from", "拆自"),
    ("absorbed", "吸收"),
    ("related", "相关"),
    ("note", "备注"),
    ("target", "去向"),
)

ID_PATTERN = re.compile(r"^INB-(\d{3,})$")
_ENTRY_HEAD = re.compile(r"^###\s+(INB-\d+)\s*$")
_INDEX_ROW = re.compile(r"^-\s+(INB-\d+)\s*[：:]\s*(.*)$")
_FIELD_LINE = re.compile(r"^-\s*([^：:]+)[：:]\s*(.*)$")

_LEDGER_HEADER = (
    "# Inbox\n"
    "\n"
    "未进入当前流程的问题与想法。不是任务列表，不跟踪是否做完。\n"
    "一个条目只装一类内聚事项；新想法先看能否并入已有条目。\n"
    "\n"
    "交接后移入 [history.md](history.md)。\n"
)
_HISTORY_HEADER = (
    "# Inbox 历史\n"
    "\n"
    "已交接或丢弃的条目。不是任务列表，不跟踪是否做完。归档只追加，不重排。\n"
)


# ---- paths ---------------------------------------------------------------
def ledger_path(root: str) -> str:
    return os.path.join(root, LEDGER_NAME)


def history_path(root: str) -> str:
    return os.path.join(root, HISTORY_NAME)


def note_path(root: str, entry_id: str) -> str:
    return os.path.join(root, NOTES_DIR, f"{entry_id}.md")


# ---- small helpers -------------------------------------------------------
def _read(path: str) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _write(path: str, content: str) -> None:
    """Write via a temp file + rename, so a crash mid-write can never leave an
    item half-archived (still in the ledger *and* already in history)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    os.replace(tmp, path)


def _validate_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError(
            "为何停放 is required: say why this item can wait "
            f"({' / '.join(PARK_REASONS)}). If it is a defect of the current "
            "node's own output it is not parkable - use check/fail/revise."
        )
    parts = [p.strip() for p in reason.split("/") if p.strip()]
    if not parts or any(p not in PARK_REASONS for p in parts):
        raise ValueError(
            f"为何停放 must be one of {', '.join(PARK_REASONS)} "
            f"(several may be joined with '/'); got {reason!r}"
        )
    return "/".join(parts)


def _summary_text(text: str) -> str:
    """First non-empty line, for the ledger index row."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _index_line(entry: Dict[str, Any]) -> str:
    summary = _summary_text(entry.get("summary", ""))
    if len(summary) > _INDEX_SUMMARY_LIMIT:
        summary = summary[: _INDEX_SUMMARY_LIMIT - 1] + "…"
    return f"- {entry['id']}：{summary}"


def display_width(text: str) -> int:
    """Terminal cells for ``text``; CJK/wide chars take two, so a table with
    Chinese labels still lines up in a plain terminal."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad_display(text: str, width: int) -> str:
    """Left-justify to ``width`` cells, always leaving one trailing space so an
    over-long value cannot run into the next column."""
    return text + " " * max(1, width - display_width(text))


# ---- parsing -------------------------------------------------------------
def _section(text: str, heading: str) -> Optional[str]:
    """Body of ``## heading``, up to the next ``##`` heading (None if absent)."""
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$", text, re.MULTILINE)
    if not match:
        return None
    rest = text[match.end():]
    nxt = re.search(r"^##\s+", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _parse_index(body: str) -> List[Dict[str, str]]:
    rows = []
    for line in body.splitlines():
        match = _INDEX_ROW.match(line.strip())
        if match:
            rows.append({"id": match.group(1), "summary": match.group(2).strip()})
    return rows


def _parse_entries(body: str, archived: bool) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    last_key: Optional[str] = None     # continuation lines append to this field
    for raw in body.splitlines():
        head = _ENTRY_HEAD.match(raw.strip())
        if head:
            current = {
                "id": head.group(1),
                "status": "已交接" if archived else OPEN,
            }
            entries.append(current)
            last_key = None
            continue
        if current is None:
            continue
        line = raw.strip()
        if line.startswith("###"):
            break
        if line.startswith("-"):
            match = _FIELD_LINE.match(line)
            last_key = None
            if not match:
                continue
            label, value = match.group(1).strip(), match.group(2).strip()
            for key, name in _FIELDS:
                if label == name:
                    current[key] = value
                    last_key = key
                    break
        elif line and last_key:
            # Wrapped continuation of the previous field: keep the line break
            # so a multi-line 摘要/备注 round-trips exactly.
            current[last_key] = f"{current[last_key]}\n{line}"
    return entries


def _parse_document(
    text: Optional[str], kind: str
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Return (entries, index rows) for a ledger or history document."""
    if not text or not text.strip():
        return [], []
    if kind == "history":
        return _parse_entries(_section(text, "条目") or "", True), []
    entries = _parse_entries(_section(text, "条目") or "", False)
    rows = _parse_index(_section(text, "未处理") or "")
    return entries, rows


# ---- rendering -----------------------------------------------------------
def _render_entries(entries: List[Dict[str, Any]]) -> str:
    blocks = []
    for entry in entries:
        lines = [f"### {entry['id']}"]
        for key, name in _FIELDS:
            value = str(entry.get(key) or "").strip()
            if value:
                lines.append(f"- {name}：{value}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n" if blocks else ""


def render_ledger(entries: List[Dict[str, Any]]) -> str:
    rows = "\n".join(_index_line(e) for e in entries) or "（无）"
    body = _render_entries(entries)
    text = f"{_LEDGER_HEADER}\n## 未处理\n\n{rows}\n\n## 条目\n"
    return f"{text}\n{body}" if body else text


def render_history(entries: List[Dict[str, Any]]) -> str:
    body = _render_entries(entries)
    text = f"{_HISTORY_HEADER}\n## 条目\n"
    return f"{text}\n{body}" if body else text


# ---- load / save ---------------------------------------------------------
def load(root: str) -> Dict[str, Any]:
    """Read both documents. A missing file is an empty document, not an error."""
    entries, rows = _parse_document(_read(ledger_path(root)), "ledger")
    archived, _ = _parse_document(_read(history_path(root)), "history")
    return {"open": entries, "archived": archived, "index": rows}


def save(root: str, data: Dict[str, Any]) -> None:
    """Rewrite the ledger; history is written once it has content."""
    _write(ledger_path(root), render_ledger(data["open"]))
    if data["archived"] or os.path.isfile(history_path(root)):
        _write(history_path(root), render_history(data["archived"]))


def next_id(data: Dict[str, Any]) -> str:
    """One past the highest id in ledger *and* history, so ids never repeat."""
    highest = 0
    for entry in list(data["open"]) + list(data["archived"]):
        match = ID_PATTERN.match(entry["id"])
        if match:
            highest = max(highest, int(match.group(1)))
    return f"INB-{highest + 1:03d}"


def find(root: str, entry_id: str) -> Optional[Dict[str, Any]]:
    entry_id = entry_id.strip().upper()
    data = load(root)
    for entry in list(data["open"]) + list(data["archived"]):
        if entry["id"] == entry_id:
            return entry
    return None


# ---- operations ----------------------------------------------------------
def add(
    root: str,
    summary: str,
    *,
    reason: str,
    type_: str = "想法",
    source: Optional[str] = None,
    merge_into: Optional[str] = None,
    split_from: Optional[str] = None,
    related: Optional[str] = None,
    note: Optional[str] = None,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """Park one item and return the resulting entry.

    With ``merge_into`` no new id is allocated: the text is appended to that
    item's summary and ``吸收``. A new thought that fits an existing item must
    not fragment the ledger, so folding it in is the default move; open a new
    id only when the topic genuinely differs.
    """
    summary = (summary or "").strip()
    if not summary:
        raise ValueError("summary is required (the item's one-line subject)")
    if len(summary) > SUMMARY_LIMIT:
        raise ValueError(
            f"summary is {len(summary)} chars; keep an item under "
            f"{SUMMARY_LIMIT} and put the detail in {NOTES_DIR}/INB-NNN.md"
        )
    if type_ not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}; got {type_!r}")
    reason = _validate_reason(reason)
    stamp = today or date.today().isoformat()

    data = load(root)
    if merge_into:
        target_id = merge_into.strip().upper()
        target = next((e for e in data["open"] if e["id"] == target_id), None)
        if target is None:
            raise ValueError(
                f"{target_id} is not an open item; --merge-into needs an "
                f"existing open item (see `aiwf inbox list`)"
            )
        absorbed = f"并入：{stamp}"
        target["absorbed"] = (
            f"{target['absorbed']}；{absorbed}" if target.get("absorbed") else absorbed
        )
        target["summary"] = f"{target['summary']}；{summary}"
        if note and note.strip():
            target["note"] = (
                f"{target['note']}\n{note.strip()}" if target.get("note") else note.strip()
            )
        save(root, data)
        return target

    body = summary
    if note and note.strip():
        body = f"{summary}\n{note.strip()}"
    entry: Dict[str, Any] = {
        "id": next_id(data),
        "status": OPEN,
        "type": type_,
        "date": stamp,
        "summary": body,
        "reason": reason,
    }
    if source:
        entry["source"] = source
    if split_from:
        entry["split_from"] = split_from.strip().upper()
    if related:
        entry["related"] = related.strip().upper()
    data["open"].append(entry)
    save(root, data)
    return entry


def resolve(
    root: str,
    entry_id: str,
    *,
    status: str = "已交接",
    target: Optional[str] = None,
    split_into: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Archive one item: status + history + removal from the ledger, one step.

    ``split_into`` only records *where* the parts went (``去向：拆分为 INB-007``);
    it never writes the new items. Decomposing content is a judgement call, so
    the caller creates them separately with ``add --split-from``.
    """
    entry_id = entry_id.strip().upper()
    if status == _LEGACY_ARCHIVED:
        status = "已交接"
    if status not in ARCHIVED:
        raise ValueError(f"status must be one of {', '.join(ARCHIVED)}; got {status!r}")

    data = load(root)
    entry = next((e for e in data["open"] if e["id"] == entry_id), None)
    if entry is None:
        if any(e["id"] == entry_id for e in data["archived"]):
            raise ValueError(f"{entry_id} is already archived")
        raise ValueError(f"no open item {entry_id}; see `aiwf inbox list`")

    went_to = (target or "").strip()
    if split_into:
        parts = "、".join(p.strip().upper() for p in split_into if p.strip())
        if not parts:
            raise ValueError("--split-into needs at least one id, e.g. INB-007,INB-008")
        went_to = f"拆分为 {parts}" + (f"（{went_to}）" if went_to else "")
    if not went_to:
        raise ValueError(
            'destination is required: say where this item went '
            '(--to "纳入 next-round" / --split-into INB-007,INB-008)'
        )
    if went_to.startswith("已完成"):
        raise ValueError("去向 records a handoff, not completion; do not write 已完成")

    data["open"] = [e for e in data["open"] if e["id"] != entry_id]
    entry = copy.deepcopy(entry)
    entry["status"] = status
    entry["target"] = went_to
    entry.pop("reason", None)   # a reason to park is meaningless once resolved
    data["archived"].append(entry)
    save(root, data)
    return entry


# ---- verification --------------------------------------------------------
def check(root: str) -> List[str]:
    """Read-only format check. Returns a list of problems (empty is healthy)."""
    problems: List[str] = []
    ledger_present = os.path.isfile(ledger_path(root))
    history_present = os.path.isfile(history_path(root))
    data = load(root)

    for entry in data["open"]:
        if entry.get("status") != OPEN:
            problems.append(f"{entry['id']}: ledger item status must be {OPEN}")
        for key, name in (("summary", "摘要"), ("reason", "为何停放"),
                          ("date", "日期"), ("type", "类型")):
            if not entry.get(key):
                problems.append(f"{entry['id']}: missing {name}")
    for entry in data["archived"]:
        if entry.get("status") not in ARCHIVED:
            problems.append(f"{entry['id']}: history item status must be 已交接/丢弃")
        if not entry.get("target"):
            problems.append(f"{entry['id']}: history item missing 去向")

    seen = set()
    for entry in list(data["open"]) + list(data["archived"]):
        if entry["id"] in seen:
            problems.append(f"{entry['id']}: duplicate id")
        seen.add(entry["id"])

    if ledger_present:
        open_ids = [e["id"] for e in data["open"]]
        index_ids = [row["id"] for row in data["index"]]
        if index_ids != open_ids:
            missing = [i for i in open_ids if i not in index_ids]
            extra = [i for i in index_ids if i not in open_ids]
            detail = []
            if missing:
                detail.append(f"missing from index: {', '.join(missing)}")
            if extra:
                detail.append(f"in index but not an item: {', '.join(extra)}")
            if not detail:
                detail.append(f"order differs: index {index_ids} vs items {open_ids}")
            problems.append(f"{LEDGER_NAME}: index out of sync - " + "; ".join(detail))
    elif history_present:
        problems.append(f"{LEDGER_NAME}: missing while {HISTORY_NAME} exists")
    return problems
