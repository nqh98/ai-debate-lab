import json
import threading
import urllib.request

import pytest

from debatelab.cli import make_server
from debatelab.store import DebateStore


@pytest.fixture
def running_server(tmp_path):
    store = DebateStore(tmp_path / "debates")
    store.create("Viewer test", "problem text")
    srv = make_server(0, str(tmp_path))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode()


def test_root_serves_viewer(running_server):
    status, body = get(running_server + "/")
    assert status == 200
    assert "AI Debate Lab" in body


def test_debates_index_served(running_server):
    status, body = get(running_server + "/debates/index.json")
    assert status == 200
    entries = json.loads(body)
    assert entries[0]["title"] == "Viewer test"


def test_debate_state_served(running_server):
    _, body = get(running_server + "/debates/index.json")
    debate_id = json.loads(body)[0]["id"]
    status, body = get(f"{running_server}/debates/{debate_id}/state.json")
    assert status == 200
    assert json.loads(body)["status"] == "created"
