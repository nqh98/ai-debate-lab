import json
import os
import socket
import subprocess
import sys

import pytest

from debatelab.store import DebateStore, LockError


def make(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    return store, did, tmp_path / "debates" / did / "run.lock"


def dead_pid():
    """A PID that is certainly not running: spawn and reap a process."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_run_lock_writes_holder_info_and_removes_it_on_exit(tmp_path):
    store, did, lock = make(tmp_path)
    with store.run_lock(did):
        info = json.loads(lock.read_text())
        assert info["pid"] == os.getpid()
        assert info["host"] == socket.gethostname()
        assert info["started_at"] and info["run_id"]
    assert not lock.exists()


def test_run_lock_is_released_when_the_run_raises(tmp_path):
    store, did, lock = make(tmp_path)
    with pytest.raises(RuntimeError):
        with store.run_lock(did):
            raise RuntimeError("boom")
    assert not lock.exists()


def test_run_lock_refuses_a_second_holder(tmp_path):
    store, did, _ = make(tmp_path)
    with store.run_lock(did):
        with pytest.raises(LockError, match="locked by pid"):
            with store.run_lock(did):
                pass


def test_run_lock_breaks_a_stale_same_host_lock(tmp_path):
    store, did, lock = make(tmp_path)
    lock.write_text(json.dumps({
        "pid": dead_pid(), "host": socket.gethostname(),
        "started_at": "2026-07-14T00:00:00+00:00", "run_id": "old",
    }))
    with store.run_lock(did):
        assert json.loads(lock.read_text())["pid"] == os.getpid()
    assert not lock.exists()


def test_run_lock_refuses_a_foreign_host_lock_unless_forced(tmp_path):
    store, did, lock = make(tmp_path)
    holder = json.dumps({
        "pid": 1, "host": "some-other-host",
        "started_at": "2026-07-14T00:00:00+00:00", "run_id": "old",
    })
    lock.write_text(holder)
    with pytest.raises(LockError, match="--force"):
        with store.run_lock(did):
            pass
    lock.write_text(holder)
    with store.run_lock(did, force=True):
        assert json.loads(lock.read_text())["pid"] == os.getpid()


def test_run_lock_refuses_an_unreadable_lock_rather_than_guessing(tmp_path):
    """A half-written lock must not read as stale: breaking it would let two
    runs proceed. Refusing is the safe direction to err."""
    store, did, lock = make(tmp_path)
    lock.write_text("not json at all")
    with pytest.raises(LockError):
        with store.run_lock(did):
            pass


def test_cli_run_exits_cleanly_when_the_debate_is_locked(tmp_path, monkeypatch):
    """The lock must be checked before any config or agent work, so a locked
    debate is refused with the holder's details rather than a config error."""
    from debatelab import cli

    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    monkeypatch.setattr(cli, "get_store", lambda: store)
    monkeypatch.chdir(tmp_path)

    def locked(*a, **k):
        raise LockError("debate is locked by pid 999 on host-x since then")

    monkeypatch.setattr(DebateStore, "run_lock", locked)
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", did])
    assert "locked by pid 999" in str(exc.value)


def test_run_lock_reports_a_missing_debate_clearly(tmp_path):
    store = DebateStore(tmp_path / "debates")
    store.create("T", "problem")  # creates the root
    with pytest.raises(FileNotFoundError, match="no such debate"):
        with store.run_lock("20260714-nope"):
            pass
