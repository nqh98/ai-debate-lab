import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from debatelab.agents import models
from debatelab.agents.api_agent import ApiAgent, DRIVERS, MODEL_LISTS
from debatelab.agents.base import AgentError, ErrorKind


class Recorder(BaseHTTPRequestHandler):
    calls = []
    payload = {}
    raw_payload = None
    status = 200
    models_payload = {}
    extra_headers = {}

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        self._respond(body, Recorder.raw_payload or json.dumps(Recorder.payload).encode())

    def do_GET(self):
        self._respond(None, json.dumps(Recorder.models_payload).encode())

    def _respond(self, body, data):
        Recorder.calls.append(
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        self.send_response(Recorder.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in Recorder.extra_headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Recorder.calls = []
    Recorder.raw_payload = None
    Recorder.status = 200
    Recorder.models_payload = {}
    Recorder.extra_headers = {}
    srv = HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(
        target=lambda: srv.serve_forever(poll_interval=0.01),
        daemon=True,
    )
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    thread.join(timeout=1)
    srv.server_close()


def test_known_drivers():
    assert set(DRIVERS) == {"openai", "anthropic", "google"}
    assert set(MODEL_LISTS) == set(DRIVERS)


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


def test_no_model_discovers_and_selects_per_task(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    Recorder.models_payload = {
        "data": [{"id": "gpt-9-mini"}, {"id": "gpt-9-pro"}]
    }
    Recorder.payload = {"choices": [{"message": {"content": "ok"}}]}
    agent = ApiAgent("gpt", "openai", None, "TEST_KEY", base_url=server)
    assert agent.ask("hello", task=models.DEEP) == "ok"
    assert agent.ask("hello", task=models.FAST) == "ok"
    list_calls = [c for c in Recorder.calls if c["path"] == "/models"]
    chat_calls = [c for c in Recorder.calls if c["path"] == "/chat/completions"]
    assert len(list_calls) == 1  # discovery is cached
    assert chat_calls[0]["body"]["model"] == "gpt-9-pro"
    assert chat_calls[1]["body"]["model"] == "gpt-9-mini"


def test_no_model_falls_back_to_first_listed(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    Recorder.models_payload = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
    Recorder.payload = {"choices": [{"message": {"content": "ok"}}]}
    agent = ApiAgent("gpt", "openai", None, "TEST_KEY", base_url=server)
    assert agent.ask("hello") == "ok"
    chat = [c for c in Recorder.calls if c["path"] == "/chat/completions"][0]
    assert chat["body"]["model"] == "model-a"


def test_no_model_and_empty_list_raises(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    Recorder.models_payload = {"data": []}
    agent = ApiAgent("gpt", "openai", None, "TEST_KEY", base_url=server)
    with pytest.raises(AgentError, match="lists no models"):
        agent.ask("hello")


def test_google_model_discovery_strips_prefix(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    Recorder.models_payload = {
        "models": [{"name": "models/g-pro"}, {"name": "models/g-flash"}]
    }
    Recorder.payload = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}}]
    }
    agent = ApiAgent("gm", "google", None, "TEST_KEY", base_url=server)
    assert agent.ask("hello", task=models.FAST) == "ok"
    chat = [c for c in Recorder.calls if ":generateContent" in c["path"]][0]
    assert chat["path"] == "/v1beta/models/g-flash:generateContent"


def test_pinned_model_skips_discovery(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    Recorder.payload = {"choices": [{"message": {"content": "ok"}}]}
    agent = ApiAgent("gpt", "openai", "pinned", "TEST_KEY", base_url=server)
    assert agent.ask("hello") == "ok"
    assert all(c["path"] != "/models" for c in Recorder.calls)


def make_agent(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    Recorder.payload = {"error": "nope"}
    return ApiAgent("gpt", "openai", "gpt-5", "TEST_KEY", base_url=server)


def test_rate_limit_is_classified_and_carries_retry_after(server, monkeypatch):
    agent = make_agent(server, monkeypatch)
    Recorder.status = 429
    Recorder.extra_headers = {"Retry-After": "5"}
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.RATE_LIMIT
    assert exc.value.retryable is True
    assert exc.value.retry_after == 5.0


def test_rate_limit_without_a_retry_after_header(server, monkeypatch):
    agent = make_agent(server, monkeypatch)
    Recorder.status = 429
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.RATE_LIMIT
    assert exc.value.retry_after is None


def test_http_date_retry_after_is_ignored_rather_than_parsed(server, monkeypatch):
    agent = make_agent(server, monkeypatch)
    Recorder.status = 429
    Recorder.extra_headers = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.RATE_LIMIT
    assert exc.value.retry_after is None


def test_negative_retry_after_is_ignored(server, monkeypatch):
    agent = make_agent(server, monkeypatch)
    Recorder.status = 429
    Recorder.extra_headers = {"Retry-After": "-1"}
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.RATE_LIMIT
    assert exc.value.retry_after is None


def test_enormous_retry_after_is_ignored(server, monkeypatch):
    agent = make_agent(server, monkeypatch)
    Recorder.status = 429
    Recorder.extra_headers = {"Retry-After": "9" * 400}
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.RATE_LIMIT
    assert exc.value.retry_after is None


def test_server_errors_are_retryable(server, monkeypatch):
    for status in (500, 502, 503, 504):
        agent = make_agent(server, monkeypatch)
        Recorder.status = status
        with pytest.raises(AgentError) as exc:
            agent.ask("hello")
        assert exc.value.kind is ErrorKind.SERVER_ERROR, status
        assert exc.value.retryable is True, status


def test_auth_failures_are_not_retryable(server, monkeypatch):
    for status in (401, 403):
        agent = make_agent(server, monkeypatch)
        Recorder.status = status
        with pytest.raises(AgentError) as exc:
            agent.ask("hello")
        assert exc.value.kind is ErrorKind.AUTH, status
        assert exc.value.retryable is False, status


def test_other_client_errors_are_not_retryable(server, monkeypatch):
    agent = make_agent(server, monkeypatch)
    Recorder.status = 400
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.CLIENT_ERROR
    assert exc.value.retryable is False


def test_unparseable_body_is_bad_response_and_not_retryable(server, monkeypatch):
    agent = make_agent(server, monkeypatch)
    Recorder.raw_payload = b"this is not json"
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.BAD_RESPONSE
    assert exc.value.retryable is False


def test_unexpected_json_shape_is_bad_response(server, monkeypatch):
    agent = make_agent(server, monkeypatch)
    Recorder.payload = {"unexpected": "shape"}
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.BAD_RESPONSE
    assert exc.value.retryable is False


def test_missing_api_key_is_auth_and_not_retryable(monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    agent = ApiAgent("gpt", "openai", "gpt-5", "TEST_KEY")
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.AUTH
    assert exc.value.retryable is False


def test_unreachable_host_is_timeout_and_retryable(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    agent = ApiAgent(
        "gpt", "openai", "gpt-5", "TEST_KEY",
        base_url="http://127.0.0.1:1",
    )
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.TIMEOUT
    assert exc.value.retryable is True
