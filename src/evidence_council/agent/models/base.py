"""Chat-model adapter interface for the coding agent.

An adapter implements :meth:`ChatModel._request` (one HTTP round trip, provider format in
and out) plus two pure translation functions so they can be unit-tested offline:

* ``to_wire(system, messages, tools)``  -> request body
* ``from_wire(body)``                   -> :class:`ModelTurn`

:meth:`ChatModel.complete` adds what every provider needs: a budget pre-check, bounded
retries for transient errors (429 / 5xx / timeouts), cost accounting and cancellation.
Adding OpenAI or Anthropic support therefore means writing these two functions and a
request method; the agent workflow does not change.

Credentials: the API key is read from the environment variable named in config
(``api_key_env``) at call time, sent in a request header (never a URL), and never
logged. Error bodies pass through :func:`redact_secrets` before they reach any output.
"""

from __future__ import annotations

import os
import time
from typing import Any

from ...providers.base import CancelToken, ProviderError, http_json
from ...util import redact_secrets
from ..messages import Message, ModelTurn, ToolSpec, Usage


class ChatModel:
    provider = "base"

    def __init__(self, settings: dict[str, Any], pricing: dict[str, Any], defaults: dict[str, Any]):
        self.settings = settings
        self.model: str = settings.get("model", "")
        self.base_url: str = (settings.get("base_url") or "").rstrip("/")
        self.api_key_env: str = settings.get("api_key_env", "")
        self.timeout_s = float(settings.get("timeout_s", defaults.get("timeout_s", 120)))
        self.retries = int(settings.get("retries", defaults.get("retries", 2)))
        self.temperature = float(settings.get("temperature", defaults.get("temperature", 0.2)))
        self.max_output_tokens = int(settings.get("max_output_tokens", defaults.get("max_output_tokens", 8192)))
        price = pricing.get(self.model)
        self.price_known = price is not None
        self.price = price or pricing.get("fallback", {"input": 15.0, "output": 75.0})

    # -- availability -------------------------------------------------
    def check(self) -> tuple[bool, str]:
        if not self.model:
            return False, f"no model configured for provider '{self.provider}' (set agent.providers.{self.provider}.model in config)"
        if self.api_key_env and not os.environ.get(self.api_key_env):
            return False, f"environment variable {self.api_key_env} is not set"
        return True, "ready (no request sent)"

    def describe(self) -> dict[str, Any]:
        ok, why = self.check()
        return {"provider": self.provider, "model": self.model, "available": ok, "reason": why,
                "api_key_env": self.api_key_env or None, "price_per_1m": self.price,
                "price_source": "config" if self.price_known else "conservative fallback (model not in pricing table)"}

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        if self.api_key_env and not key:
            raise ProviderError(f"environment variable {self.api_key_env} is not set", status="unavailable")
        return key

    # -- cost ---------------------------------------------------------
    def cost(self, usage: Usage) -> float:
        return (usage.input_tokens * float(self.price["input"]) + usage.output_tokens * float(self.price["output"])) / 1_000_000

    def estimate_max_cost(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> float:
        """Upper-bound estimate for one call, used to refuse a call that could break the budget."""
        chars = len(system) + sum(len(m.text) + sum(len(str(c.args)) for c in m.tool_calls)
                                  + sum(len(r.content) for r in m.tool_results) for m in messages)
        chars += sum(len(t.description) + len(str(t.parameters)) for t in tools)
        est_in = chars // 3 + 200
        return self.cost(Usage(input_tokens=est_in, output_tokens=self.max_output_tokens))

    # -- to implement ---------------------------------------------------
    def to_wire(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def from_wire(self, body: dict[str, Any]) -> ModelTurn:  # pragma: no cover
        raise NotImplementedError

    def _request(self, body: dict[str, Any], timeout_s: float, cancel: CancelToken) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    # -- shared behaviour ----------------------------------------------
    def complete(self, system: str, messages: list[Message], tools: list[ToolSpec], *,
                 purpose: str = "", cancel: CancelToken | None = None, timeout_s: float | None = None) -> ModelTurn:
        """One model turn. Raises ProviderError after retries are exhausted."""
        cancel = cancel or CancelToken()
        body = self.to_wire(system, messages, tools)
        attempt = 0
        t0 = time.monotonic()
        while True:
            attempt += 1
            try:
                raw = self._request(body, float(timeout_s or self.timeout_s), cancel)
                turn = self.from_wire(raw)
                turn.cost_usd = self.cost(turn.usage)
                turn.duration_s = time.monotonic() - t0
                return turn
            except ProviderError as e:
                if e.transient and attempt <= self.retries and not cancel.is_set():
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise ProviderError(redact_secrets(str(e))[0][:600], status=e.status, transient=False) from None


def post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout_s: float, cancel: CancelToken) -> dict[str, Any]:
    return http_json(url, body, timeout_s, cancel, headers=headers)
