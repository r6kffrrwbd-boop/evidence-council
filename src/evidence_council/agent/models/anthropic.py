"""Anthropic Messages API adapter (``POST /messages``).

Status: message mapping is unit-tested offline; it has NOT been run against the real
API in this repository (no key was available). Set ``agent.provider = "anthropic"`` and
``agent.providers.anthropic.model`` to use it.

Wire mapping
------------
* user text      -> ``{"role": "user", "content": [{"type": "text", "text": ...}]}``
* assistant turn -> the ``content`` blocks exactly as returned. ``thinking`` blocks carry a
  ``signature`` and ``redacted_thinking`` blocks carry ``data``; both must be sent back
  unchanged and in order before the ``tool_use`` blocks they precede.
* tool results   -> one user message holding every ``tool_result`` for the previous turn,
  each with ``tool_use_id`` matching the ``tool_use`` block's ``id``.
"""

from __future__ import annotations

import copy
from typing import Any

from ...providers.base import CancelToken, ProviderError
from ..messages import Message, ModelTurn, ToolCall, ToolSpec, Usage
from .base import ChatModel, post_json

API_VERSION = "2023-06-01"


class AnthropicModel(ChatModel):
    provider = "anthropic"

    def to_wire(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "user":
                out.append({"role": "user", "content": [{"type": "text", "text": m.text}]})
            elif m.role == "assistant":
                if m.native and m.native.get("provider") == self.provider:
                    out.append({"role": "assistant", "content": copy.deepcopy(m.native["data"])})
                else:
                    blocks: list[dict[str, Any]] = [{"type": "text", "text": m.text}] if m.text else []
                    blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in m.tool_calls]
                    out.append({"role": "assistant", "content": blocks})
            elif m.role == "tool":
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content, "is_error": r.is_error}
                    for r in m.tool_results]})
        body: dict[str, Any] = {"model": self.model, "max_tokens": self.max_output_tokens, "messages": out}
        if system:
            body["system"] = system
        if self.settings.get("thinking"):
            body["thinking"] = self.settings["thinking"]   # temperature must stay default with thinking on
        else:
            body["temperature"] = self.temperature
        if tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        return body

    def from_wire(self, body: dict[str, Any]) -> ModelTurn:
        if body.get("type") == "error":
            raise ProviderError(f"Anthropic error: {body.get('error')}", status="error")
        blocks = body.get("content") or []
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        calls = [ToolCall(id=b["id"], name=b.get("name", ""), args=b.get("input") or {}) for b in blocks if b.get("type") == "tool_use"]
        u = body.get("usage") or {}
        usage = Usage(input_tokens=int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0) or 0)
                      + int(u.get("cache_creation_input_tokens", 0) or 0),
                      output_tokens=int(u.get("output_tokens", 0)))
        msg = Message(role="assistant", text="".join(texts).strip(), tool_calls=calls,
                      native={"provider": self.provider, "data": blocks})
        return ModelTurn(message=msg, usage=usage, stop_reason=str(body.get("stop_reason", "")))

    def _request(self, body: dict[str, Any], timeout_s: float, cancel: CancelToken) -> dict[str, Any]:
        headers = {"x-api-key": self._key(), "anthropic-version": API_VERSION}
        return post_json(f"{self.base_url}/messages", body, headers, timeout_s, cancel)
