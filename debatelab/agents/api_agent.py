"""Adapter for HTTP LLM APIs. Three thin drivers, no provider SDKs."""
import json
import os
import urllib.error
import urllib.request

from . import models
from .base import Agent, AgentError


def _openai_request(model, base_url, api_key, prompt):
    url = f"{(base_url or 'https://api.openai.com/v1').rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    return url, headers, body


def _openai_parse(data):
    return data["choices"][0]["message"]["content"]


def _openai_models_request(base_url, api_key):
    url = f"{(base_url or 'https://api.openai.com/v1').rstrip('/')}/models"
    return url, {"Authorization": f"Bearer {api_key}"}


def _openai_models_parse(data):
    return [m["id"] for m in data["data"]]


def _anthropic_request(model, base_url, api_key, prompt):
    url = f"{(base_url or 'https://api.anthropic.com').rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }
    return url, headers, body


def _anthropic_parse(data):
    return "".join(b["text"] for b in data["content"] if b["type"] == "text")


def _anthropic_models_request(base_url, api_key):
    url = f"{(base_url or 'https://api.anthropic.com').rstrip('/')}/v1/models"
    return url, {"x-api-key": api_key, "anthropic-version": "2023-06-01"}


def _google_request(model, base_url, api_key, prompt):
    root = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
    url = f"{root}/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    return url, headers, body


def _google_parse(data):
    return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])


def _google_models_request(base_url, api_key):
    root = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
    return f"{root}/v1beta/models", {"x-goog-api-key": api_key}


def _google_models_parse(data):
    return [m["name"].removeprefix("models/") for m in data["models"]]


DRIVERS = {
    "openai": (_openai_request, _openai_parse),
    "anthropic": (_anthropic_request, _anthropic_parse),
    "google": (_google_request, _google_parse),
}

MODEL_LISTS = {
    "openai": (_openai_models_request, _openai_models_parse),
    "anthropic": (_anthropic_models_request, _openai_models_parse),
    "google": (_google_models_request, _google_models_parse),
}


class ApiAgent(Agent):
    def __init__(self, name, provider, model=None, api_key_env=None,
                 base_url=None, timeout=180):
        super().__init__(name)
        if provider not in DRIVERS:
            raise ValueError(f"unknown provider: {provider}")
        self.provider = provider
        self.model = model  # optional pin; None means auto-select per task
        self.api_key_env = api_key_env
        self.base_url = base_url
        self.timeout = timeout
        self._available: list[str] | None = None  # discovered lazily

    def ask(self, prompt: str, task: str = models.DEEP) -> str:
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise AgentError(f"{self.name}: env var {self.api_key_env} is not set")
        build, parse = DRIVERS[self.provider]
        url, headers, body = build(
            self._model_for(task, api_key), self.base_url, api_key, prompt
        )
        data = self._request(url, headers, body)
        try:
            return parse(data).strip()
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise AgentError(f"{self.name}: unexpected response shape: {e!r}")

    def _model_for(self, task: str, api_key: str) -> str:
        if self.model:
            return self.model
        if self._available is None:
            list_request, list_parse = MODEL_LISTS[self.provider]
            url, headers = list_request(self.base_url, api_key)
            data = self._request(url, headers)
            try:
                available = list_parse(data)
            except (KeyError, TypeError, AttributeError) as e:
                raise AgentError(f"{self.name}: unexpected model list shape: {e!r}")
            if not available:
                raise AgentError(f"{self.name}: provider lists no models")
            self._available = available
        return models.choose_model(self._available, task) or self._available[0]

    def _request(self, url, headers, body=None):
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_response = resp.read().decode()
        except urllib.error.HTTPError as e:
            raise AgentError(f"{self.name}: HTTP {e.code}: {e.read().decode()[:500]}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise AgentError(f"{self.name}: request failed: {e}")
        try:
            return json.loads(raw_response)
        except json.JSONDecodeError as e:
            raise AgentError(f"{self.name}: unexpected response shape: {e!r}")
