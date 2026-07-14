import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from debatelab.agents.api_agent import ApiAgent, DRIVERS
from debatelab.agents.base import AgentError


class Recorder(BaseHTTPRequestHandler):
    calls = []
    payload = {}
    raw_payload = None
    status = 200

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        Recorder.calls.append(
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        data = Recorder.raw_payload or json.dumps(Recorder.payload).encode()
        self.send_response(Recorder.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Recorder.calls = []
    Recorder.raw_payload = None
    Recorder.status = 200
    srv = HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_known_drivers():
    assert set(DRIVERS) == {"openai", "anthropic", "google"}


def test_openai_driver_roundtrip(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    Recorder.payload = {"choices": [{"message": {"content": " hi there "}}]}
    agent = ApiAgent("gpt", "openai", "gpt-5", "TEST_KEY", base_url=server)
    assert agent.ask("hello") == "hi there"
    call = Recorder.calls[0]
    assert call["path"] == "/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["body"]["model"] == "gpt-5"
    assert call["body"]["messages"] == [{"role": "user", "content": "hello"}]


def test_anthropic_driver_roundtrip(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-ant")
    Recorder.payload = {"content": [{"type": "text", "text": "claude says hi"}]}
    agent = ApiAgent("cl", "anthropic", "claude-fable-5", "TEST_KEY", base_url=server)
    assert agent.ask("hello") == "claude says hi"
    call = Recorder.calls[0]
    assert call["path"] == "/v1/messages"
    assert call["headers"]["X-Api-Key"] == "sk-ant"
    assert call["body"]["max_tokens"] == 4096


def test_google_driver_roundtrip(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "g-key")
    Recorder.payload = {
        "candidates": [{"content": {"parts": [{"text": "gemini says hi"}]}}]
    }
    agent = ApiAgent("gm", "google", "gemini-pro", "TEST_KEY", base_url=server)
    assert agent.ask("hello") == "gemini says hi"
    call = Recorder.calls[0]
    assert call["path"] == "/v1beta/models/gemini-pro:generateContent"
    assert call["headers"]["X-Goog-Api-Key"] == "g-key"


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    agent = ApiAgent("x", "openai", "gpt-5", "NOPE_KEY")
    with pytest.raises(AgentError, match="NOPE_KEY is not set"):
        agent.ask("hello")


def test_http_error_raises(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    Recorder.status = 500
    Recorder.payload = {"error": "boom"}
    agent = ApiAgent("x", "openai", "gpt-5", "TEST_KEY", base_url=server)
    with pytest.raises(AgentError, match="HTTP 500"):
        agent.ask("hello")


def test_malformed_json_raises_agent_error(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    Recorder.raw_payload = b"not-json"
    agent = ApiAgent("x", "openai", "gpt-5", "TEST_KEY", base_url=server)
    with pytest.raises(AgentError, match="unexpected response shape"):
        agent.ask("hello")


def test_non_string_provider_content_raises_agent_error(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    Recorder.payload = {"choices": [{"message": {"content": 123}}]}
    agent = ApiAgent("x", "openai", "gpt-5", "TEST_KEY", base_url=server)
    with pytest.raises(AgentError, match="unexpected response shape"):
        agent.ask("hello")


def test_unknown_provider_rejected():
    with pytest.raises(ValueError, match="unknown provider"):
        ApiAgent("x", "mystery", "m", "KEY")
