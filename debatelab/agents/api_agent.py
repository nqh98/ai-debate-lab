"""Adapter for HTTP LLM APIs. Three thin drivers, no provider SDKs."""
import json
import os
import urllib.error
import urllib.request

from .base import Agent, AgentError


def _openai_request(model, base_url, api_key, prompt):
    url = f"{(base_url or 'https://api.openai.com/v1').rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    return url, headers, body


def _openai_parse(data):
    return data["choices"][0]["message"]["content"]


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


def _google_request(model, base_url, api_key, prompt):
    root = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
    url = f"{root}/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    return url, headers, body


def _google_parse(data):
    return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])


DRIVERS = {
    "openai": (_openai_request, _openai_parse),
    "anthropic": (_anthropic_request, _anthropic_parse),
    "google": (_google_request, _google_parse),
}


class ApiAgent(Agent):
    def __init__(self, name, provider, model, api_key_env, base_url=None, timeout=180):
        super().__init__(name)
        if provider not in DRIVERS:
            raise ValueError(f"unknown provider: {provider}")
        self.provider = provider
        self.model = model
        self.api_key_env = api_key_env
        self.base_url = base_url
        self.timeout = timeout

    def ask(self, prompt: str) -> str:
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise AgentError(f"{self.name}: env var {self.api_key_env} is not set")
        build, parse = DRIVERS[self.provider]
        url, headers, body = build(self.model, self.base_url, api_key, prompt)
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_response = resp.read().decode()
        except urllib.error.HTTPError as e:
            raise AgentError(f"{self.name}: HTTP {e.code}: {e.read().decode()[:500]}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise AgentError(f"{self.name}: request failed: {e}")
        try:
            data = json.loads(raw_response)
            return parse(data).strip()
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError) as e:
            raise AgentError(f"{self.name}: unexpected response shape: {e!r}")
