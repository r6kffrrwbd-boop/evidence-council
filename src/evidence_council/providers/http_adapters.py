"""HTTP adapters: Ollama (local) and any OpenAI-compatible chat endpoint (xAI Grok, OpenRouter, LM Studio...).

API keys are read from the environment variable named in config (``api_key_env``)
at call time; they are never stored, logged or written to output.
"""

from __future__ import annotations

import os
import threading
import urllib.parse

from .base import Adapter, Availability, CancelToken, ProviderError, RawResponse, http_json

_OLLAMA_LOCKS: dict[str, threading.Lock] = {}


def _is_local_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def list_ollama_models(base_url: str, timeout_s: float = 3) -> list[dict]:
    data = http_json(base_url.rstrip("/") + "/api/tags", None, timeout_s, CancelToken())
    return list(data.get("models", []))


class OllamaAdapter(Adapter):
    """One Ollama model. Calls are serialised per server to avoid memory thrash on small machines."""

    adapter_type = "ollama"
    default_locality = "local"

    def __init__(self, spec, defaults):
        super().__init__(spec, defaults)
        self.base_url = spec.get("base_url", "http://127.0.0.1:11434").rstrip("/")
        if not _is_local_url(self.base_url):
            self.locality = spec.get("locality", "cloud")
        _OLLAMA_LOCKS.setdefault(self.base_url, threading.Lock())

    def check(self) -> Availability:
        if not self.model:
            return Availability(False, "no Ollama model configured/installed (run: ollama pull <model>)")
        try:
            names = [m.get("name") for m in list_ollama_models(self.base_url)]
        except ProviderError as e:
            return Availability(False, f"Ollama server not reachable at {self.base_url} ({e}); start it with: ollama serve")
        if self.model not in names:
            return Availability(False, f"model {self.model} not installed (run: ollama pull {self.model})")
        return Availability(True, "server reachable, model installed")

    def _call(self, prompt: str, timeout_s: float, cancel: CancelToken) -> RawResponse:
        payload = {
            "model": self.model, "prompt": prompt, "stream": False,
            "options": {"temperature": float(self.spec.get("temperature", 0.2)),
                        "num_predict": int(self.spec.get("num_predict", 1200))},
        }
        if not getattr(self, "probe_mode", False):
            payload["format"] = "json"   # council prompts ask for one JSON object
        if self.spec.get("think") is not None:
            payload["think"] = bool(self.spec["think"])
        elif any(self.model.startswith(p) for p in ("qwen3", "deepseek-r1", "gpt-oss")):
            payload["think"] = False
        lock = _OLLAMA_LOCKS[self.base_url]
        # waiting for the lock counts against the timeout
        if not lock.acquire(timeout=max(1.0, timeout_s)):
            raise ProviderError("timed out waiting for the local Ollama server", status="timeout")
        try:
            try:
                data = http_json(self.base_url + "/api/generate", payload, timeout_s, cancel)
            except ProviderError as e:
                if "does not support thinking" in str(e) and "think" in payload:
                    payload.pop("think")
                    data = http_json(self.base_url + "/api/generate", payload, timeout_s, cancel)
                else:
                    raise
        finally:
            lock.release()
        tokens = {"input": int(data.get("prompt_eval_count", 0)), "output": int(data.get("eval_count", 0))}
        return RawResponse(text=str(data.get("response", "")), tokens=tokens, cost_usd=0.0, cost_note="local model")


class OpenAICompatAdapter(Adapter):
    """POST {base_url}/chat/completions. Available only when the configured API-key env var is set."""

    adapter_type = "openai_compat"
    default_locality = "cloud"

    def __init__(self, spec, defaults):
        super().__init__(spec, defaults)
        self.base_url = spec.get("base_url", "").rstrip("/")
        self.key_env = spec.get("api_key_env", "")
        if self.base_url and _is_local_url(self.base_url) and "locality" not in spec:
            self.locality = "local"

    def check(self) -> Availability:
        if not self.base_url or not self.model:
            return Availability(False, "base_url and model must be set in config")
        if self.key_env and not os.environ.get(self.key_env):
            return Availability(False, f"environment variable {self.key_env} is not set")
        return Availability(True, f"{self.key_env or 'no key'} configured")

    def _call(self, prompt: str, timeout_s: float, cancel: CancelToken) -> RawResponse:
        headers = {}
        if self.key_env:
            headers["Authorization"] = "Bearer " + os.environ.get(self.key_env, "")
        payload = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                   "temperature": float(self.spec.get("temperature", 0.2))}
        data = http_json(self.base_url + "/chat/completions", payload, timeout_s, cancel, headers=headers)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError("unexpected response shape (no choices[0].message.content)")
        u = data.get("usage") or {}
        tokens = {"input": int(u.get("prompt_tokens", 0)), "output": int(u.get("completion_tokens", 0))} if u else None
        return RawResponse(text=text, tokens=tokens, cost_note="see provider billing; not computed locally")
