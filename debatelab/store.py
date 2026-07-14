"""File-backed debate storage: transcript.jsonl is the source of truth,
state.json is the derived checkpoint, summary.md the human-readable view.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path


def slugify(title: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:max_len].rstrip("-")
    return slug or "debate"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DebateStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, debate_id: str) -> Path:
        return self.root / debate_id

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
        p = self.path(debate_id) / "state.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        tmp.replace(p)

    def read_state(self, debate_id) -> dict:
        return json.loads((self.path(debate_id) / "state.json").read_text())

    def read_problem(self, debate_id) -> str:
        return (self.path(debate_id) / "problem.md").read_text()

    def write_summary(self, debate_id, markdown: str):
        (self.path(debate_id) / "summary.md").write_text(markdown)

    def read_summary(self, debate_id) -> str:
        p = self.path(debate_id) / "summary.md"
        return p.read_text() if p.exists() else ""

    def list_ids(self) -> list:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "state.json").exists())

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
        (self.root / "index.json").write_text(json.dumps(entries, indent=2))


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
