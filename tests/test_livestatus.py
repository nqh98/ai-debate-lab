"""Heartbeats and stall alerts: visibility instead of cancellation (spec §6)."""
import json

from debatelab.livestatus import LiveStatus
from debatelab.store import DebateStore


class FakeClock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t
    def advance(self, seconds):
        self.t += seconds


def make(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("t", "p")
    lines = []
    clock = FakeClock()
    live = LiveStatus(store, did, progress=lines.append, clock=clock)
    return store, did, lines, clock, live


def test_tick_writes_live_json_with_elapsed(tmp_path):
    store, did, lines, clock, live = make(tmp_path)
    live.set_phase(1, "propose")
    live.call_started("claude", "deep", stall_after=900)
    clock.advance(240)
    live.tick()
    payload = json.loads((store.path(did) / "live.json").read_text())
    assert payload["phase"] == "propose"
    [call] = payload["calls"]
    assert call["agent"] == "claude"
    assert call["elapsed_s"] == 240
    assert call["stalled"] is False
    assert any("claude" in line and "4m" in line for line in lines)


def test_stall_alert_fires_once_with_bell(tmp_path):
    store, did, lines, clock, live = make(tmp_path)
    live.set_phase(1, "propose")
    live.call_started("claude", "deep", stall_after=900)
    clock.advance(1020)
    live.tick()
    payload = json.loads((store.path(did) / "live.json").read_text())
    assert payload["calls"][0]["stalled"] is True
    bells = [line for line in lines if "\a" in line]
    assert len(bells) == 1
    assert "stall threshold" in bells[0]
    live.tick()
    assert len([line for line in lines if "\a" in line]) == 1  # no re-ring


def test_no_stall_when_threshold_is_none(tmp_path):
    store, did, lines, clock, live = make(tmp_path)
    live.set_phase(1, "propose")
    live.call_started("claude", "deep", stall_after=None)
    clock.advance(10_000)
    live.tick()
    payload = json.loads((store.path(did) / "live.json").read_text())
    assert payload["calls"][0]["stalled"] is False


def test_finished_call_leaves_live_json(tmp_path):
    store, did, lines, clock, live = make(tmp_path)
    live.set_phase(1, "propose")
    live.call_started("claude", "deep", stall_after=900)
    live.call_finished("claude")
    live.tick()
    payload = json.loads((store.path(did) / "live.json").read_text())
    assert payload["calls"] == []


def test_stop_deletes_live_json_and_joins_thread(tmp_path):
    store, did, lines, clock, live = make(tmp_path)
    live.start()
    live.set_phase(1, "propose")
    live.tick()
    assert (store.path(did) / "live.json").exists()
    live.stop()
    assert not (store.path(did) / "live.json").exists()
