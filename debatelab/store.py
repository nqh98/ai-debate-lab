"""File-backed debate storage: transcript.jsonl is the source of truth,
state.json is the derived checkpoint, summary.md the human-readable view.
"""

import contextlib
import fcntl
import json
import os
import re
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def slugify(title: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:max_len].rstrip("-")
    return slug or "debate"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    `with_name`, not `with_suffix`: with_suffix('.json.tmp') only works when
    the target already ends in .json and would turn summary.md into
    summary.json.tmp. Same directory means replace() is a same-filesystem
    rename, which is what makes it atomic for readers.

    No fsync: the goal is that the polling viewer never sees a torn file, not
    that writes survive power loss.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


class LockError(Exception):
    """Another process holds this debate's run lock."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _read_lock(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _is_stale(holder: dict) -> bool:
    """Only a live-PID check on THIS host can prove staleness.

    Anything unknowable — a foreign host, a missing or half-written lock —
    is treated as held. Erring toward a spurious refusal (resolvable with
    --force) beats erring toward two concurrent runs shredding a transcript.
    Inherits the usual PID-reuse race: a recycled PID reads as live.
    """
    if holder.get("host") != socket.gethostname():
        return False
    pid = holder.get("pid")
    if not isinstance(pid, int):
        return False
    return not _pid_alive(pid)


@contextlib.contextmanager
def _lock_transition(debate_path: Path):
    """Serialize lock acquisition, takeover, and release for one debate."""
    fd = os.open(debate_path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class DebateStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, debate_id: str) -> Path:
        candidate = Path(debate_id)
        if (
            not debate_id
            or debate_id in (".", "..")
            or candidate.is_absolute()
            or len(candidate.parts) != 1
            or "/" in debate_id
            or "\\" in debate_id
        ):
            raise ValueError(
                f"invalid debate id {debate_id!r}: expected a single directory name"
            )
        target = self.root / debate_id
        if target.is_symlink():
            raise ValueError(
                f"invalid debate id {debate_id!r}: symlinks are not allowed"
            )
        if target.resolve(strict=False).parent != self.root.resolve(strict=False):
            raise ValueError(f"invalid debate id {debate_id!r}: outside debate root")
        return target

    def create(self, title, problem, context_texts=()) -> str:
        base = f"{datetime.now().strftime('%Y%m%d')}-{slugify(title)}"
        debate_id, n = base, 2
        while self.path(debate_id).exists():
            debate_id = f"{base}-{n}"
            n += 1
        d = self.path(debate_id)
        d.mkdir(parents=True)
        parts = [f"# {title}", "", problem]
        for label, text in context_texts:
            parts += ["", f"## Context: {label}", "", text]
        (d / "problem.md").write_text("\n".join(parts) + "\n")
        (d / "transcript.jsonl").touch()
        self.write_state(
            debate_id,
            {
                "id": debate_id,
                "title": title,
                "status": "created",
                "round": 0,
                "max_rounds": 5,
                "quorum": "2/3",
                "roster": None,
                "last_completed_phase": None,
                "proposals": {},
                "critiques": {},
                "candidate": None,
                "votes": {},
                "abstained": [],
                "human_decision": None,
            },
        )
        self.rebuild_index()
        return debate_id

    def append_event(self, debate_id, event: dict):
        event = {"ts": _now(), **event}
        with (self.path(debate_id) / "transcript.jsonl").open("a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_events(self, debate_id) -> list:
        text = (self.path(debate_id) / "transcript.jsonl").read_text()
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def write_state(self, debate_id, state: dict):
        _atomic_write(
            self.path(debate_id) / "state.json",
            json.dumps(state, indent=2, ensure_ascii=False),
        )

    def read_state(self, debate_id) -> dict:
        return json.loads((self.path(debate_id) / "state.json").read_text())

    def read_problem(self, debate_id) -> str:
        return (self.path(debate_id) / "problem.md").read_text()

    def write_summary(self, debate_id, markdown: str):
        _atomic_write(self.path(debate_id) / "summary.md", markdown)

    def read_summary(self, debate_id) -> str:
        p = self.path(debate_id) / "summary.md"
        return p.read_text() if p.exists() else ""

    def list_ids(self) -> list:
        if not self.root.exists():
            return []
        debate_ids = []
        for entry in self.root.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            debate_path = self.path(entry.name)
            if (debate_path / "state.json").exists():
                debate_ids.append(entry.name)
        return sorted(debate_ids)

    def rebuild_index(self):
        entries = []
        for did in self.list_ids():
            state = self.read_state(did)
            entries.append(
                {
                    "id": did,
                    "title": state["title"],
                    "status": state["status"],
                    "round": state["round"],
                }
            )
        self.root.mkdir(exist_ok=True)
        _atomic_write(self.root / "index.json", json.dumps(entries, indent=2))

    def _acquire_lock(self, path: Path, info: dict, force: bool) -> None:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        with _lock_transition(path.parent):
            try:
                fd = os.open(path, flags)
            except FileExistsError:
                holder = _read_lock(path)
                if not force and not _is_stale(holder):
                    raise LockError(
                        f"debate is locked by pid {holder.get('pid')} on "
                        f"{holder.get('host')} since {holder.get('started_at')}; "
                        "use --force if that run is dead"
                    )
                why = "forced" if force else "stale"
                print(
                    f"breaking {why} lock from pid {holder.get('pid')}",
                    file=sys.stderr,
                )
                path.unlink(missing_ok=True)
                fd = os.open(path, flags)
            with os.fdopen(fd, "w") as lock_file:
                json.dump(info, lock_file)

    def _release_lock(self, path: Path, run_id: str) -> None:
        with _lock_transition(path.parent):
            if _read_lock(path).get("run_id") == run_id:
                path.unlink(missing_ok=True)

    @contextlib.contextmanager
    def run_lock(self, debate_id: str, force: bool = False):
        """Hold debates/<id>/run.lock for the duration of a run.

        The original design listed concurrent runs of one debate as a
        non-goal but never enforced it: two `debate run` processes both
        append to transcript.jsonl and race state.json.
        """
        d = self.path(debate_id)
        if not (d / "state.json").exists():
            raise FileNotFoundError(f"no such debate: {debate_id}")
        path = d / "run.lock"
        info = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": _now(),
            "run_id": uuid.uuid4().hex,
        }
        self._acquire_lock(path, info, force)
        try:
            yield info
        finally:
            self._release_lock(path, info["run_id"])


def render_summary(state: dict) -> str:
    lines = [
        f"# Debate: {state['title']}",
        "",
        f"- **Status:** {state['status']}",
        f"- **Round:** {state['round']} / {state['max_rounds']}",
    ]
    decision = state.get("human_decision")
    candidate = state.get("candidate")
    if decision:
        lines += ["", f"## Final decision — {decision['decision'].upper()}", ""]
        if candidate:
            lines += [
                f"Candidate from **{candidate['agent']}**:",
                "",
                candidate["text"],
                "",
            ]
        if decision.get("note"):
            lines += [f"> Human note: {decision['note']}", ""]
    elif candidate:
        lines += [
            "",
            f"## Current candidate (from {candidate['agent']}) — pending human decision",
            "",
            candidate["text"],
            "",
        ]
    if state.get("votes") or state.get("abstained"):
        lines += ["", "## Latest votes", "", "| Agent | Vote |", "|---|---|"]
        for agent, vote in state.get("votes", {}).items():
            lines.append(f"| {agent} | {vote['vote']} |")
        for agent in state.get("abstained", []):
            lines.append(f"| {agent} | abstained |")
    if state.get("proposals"):
        lines += ["", "## Current proposals", ""]
        for agent, text in state["proposals"].items():
            lines += [f"### {agent}", "", text, ""]
    if state.get("critiques"):
        lines += ["## Latest critiques", ""]
        for agent, text in state["critiques"].items():
            lines += [f"### {agent}", "", text, ""]
    return "\n".join(lines) + "\n"
