import json
from pathlib import Path

import pytest

from debatelab import store as store_mod
from debatelab.store import DebateStore, render_summary, slugify


def test_slugify():
    assert slugify("Should we use Rust?!") == "should-we-use-rust"
    assert slugify("   ") == "debate"
    assert len(slugify("x" * 100)) <= 40


def test_create_makes_files_and_id(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("Pick a DB", "Which database should we use?")
    d = store.path(did)
    assert did.endswith("-pick-a-db")
    assert (d / "problem.md").read_text().startswith("# Pick a DB")
    assert (d / "transcript.jsonl").exists()
    state = store.read_state(did)
    assert state["status"] == "created"
    assert state["round"] == 0 and state["max_rounds"] == 5
    assert state["human_decision"] is None


def test_create_includes_context(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem", [("notes.md", "some context")])
    text = store.read_problem(did)
    assert "## Context: notes.md" in text
    assert "some context" in text


def test_create_collision_gets_suffix(tmp_path):
    store = DebateStore(tmp_path / "debates")
    a = store.create("Same title", "p")
    b = store.create("Same title", "p")
    assert a != b and b.endswith("-2")


@pytest.mark.parametrize("debate_id", ["../outside", "/tmp/outside", "nested/id", "", ".", ".."])
def test_path_rejects_ids_outside_immediate_root_children(tmp_path, debate_id):
    store = DebateStore(tmp_path / "debates")
    with pytest.raises(ValueError, match="invalid debate id"):
        store.path(debate_id)


def test_path_rejects_symlinked_debate_directory_and_list_excludes_it(tmp_path):
    root = tmp_path / "debates"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "state.json").write_text('{"id": "example"}')
    (root / "example").symlink_to(outside, target_is_directory=True)
    store = DebateStore(root)

    with pytest.raises(ValueError, match="outside debate root|symlink"):
        store.path("example")
    assert store.list_ids() == []


def test_events_roundtrip_with_ts(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "p")
    store.append_event(
        did,
        {
            "round": 1,
            "phase": "propose",
            "agent": "a",
            "type": "proposal",
            "content": "hello",
        },
    )
    events = store.read_events(did)
    assert len(events) == 1
    assert events[0]["content"] == "hello"
    assert "ts" in events[0]


def test_state_roundtrip(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "p")
    state = store.read_state(did)
    state["status"] = "running"
    store.write_state(did, state)
    assert store.read_state(did)["status"] == "running"


def test_index_lists_debates(tmp_path):
    store = DebateStore(tmp_path / "debates")
    a = store.create("First", "p")
    b = store.create("Second", "p")
    index = json.loads((tmp_path / "debates" / "index.json").read_text())
    assert {e["id"] for e in index} == {a, b}
    assert all(e["status"] == "created" for e in index)


def test_render_summary_pending_and_decided():
    state = {
        "id": "x",
        "title": "T",
        "status": "awaiting_human",
        "round": 2,
        "max_rounds": 5,
        "last_completed_phase": "vote",
        "proposals": {"a": "prop A"},
        "critiques": {"b": "crit B"},
        "candidate": {"agent": "a", "text": "final answer"},
        "votes": {
            "a": {"vote": "accept", "reason": "r"},
            "b": {"vote": "accept", "reason": "r"},
        },
        "abstained": ["c"],
        "human_decision": None,
    }
    md = render_summary(state)
    assert "pending human decision" in md
    assert "final answer" in md
    assert "| c | abstained |" in md

    state["human_decision"] = {"decision": "approved", "note": "ship it"}
    state["status"] = "approved"
    md = render_summary(state)
    assert "APPROVED" in md
    assert "ship it" in md


def test_atomic_write_replaces_content_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "summary.md"
    store_mod._atomic_write(p, "a" * 100)
    store_mod._atomic_write(p, "b")
    assert p.read_text() == "b"
    assert list(tmp_path.iterdir()) == [p]


def test_atomic_write_tmp_keeps_the_full_target_name(tmp_path, monkeypatch):
    """with_suffix('.json.tmp') would turn summary.md into summary.json.tmp;
    the tmp file must sit beside the target so replace() is a same-filesystem
    rename, which is what makes it atomic."""
    seen = {}
    original = Path.replace

    def spy(self, target):
        seen["tmp"] = self.name
        seen["dir"] = self.parent
        return original(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    store_mod._atomic_write(tmp_path / "summary.md", "x")
    assert seen["tmp"] == "summary.md.tmp"
    assert seen["dir"] == tmp_path


def test_write_summary_and_rebuild_index_leave_no_tmp_behind(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    store.write_summary(did, "# hi")
    store.rebuild_index()
    root = tmp_path / "debates"
    assert not (root / "index.json.tmp").exists()
    assert not (root / did / "summary.md.tmp").exists()
    assert not (root / did / "state.json.tmp").exists()
    assert (root / did / "summary.md").read_text() == "# hi"
