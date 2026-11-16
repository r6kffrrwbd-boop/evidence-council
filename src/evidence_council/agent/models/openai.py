"""OpenAI Responses API adapter (``POST /responses``).

Status: message mapping is unit-tested offline; it has NOT been run against the real
API in this repository (no key was available). Set ``agent.provider = "openai"`` and
``agent.providers.openai.model`` to use it.

Wire mapping
------------
* user text      -> ``{"role": "user", "content": [{"type": "input_text", "text": ...}]}``
* assistant turn -> every output item exactly as returned (``reasoning`` items with
  ``encrypted_content``, ``function_call`` items with ``call_id``, ``message`` items).
  ``store: false`` + ``include: ["reasoning.encrypted_content"]`` makes reasoning state
  travel with the conversation instead of being stored server-side.
* tool results   -> ``{"type": "function_call_output", "call_id": ..., "output": ...}``
"""

from __future__ import annotations

import copy
import json
from typing import Any

from ...providers.base import CancelToken, ProviderError
from ..messages import Message, ModelTurn, ToolCall, ToolSpec, Usage
from .base import ChatModel, post_json


class OpenAIModel(ChatModel):
    provider = "openai"

    def to_wire(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "user":
                items.append({"role": "user", "content": [{"type": "input_text", "text": m.text}]})
            elif m.role == "assistant":
                if m.native and m.native.get("provider") == self.provider:
                    items.extend(copy.deepcopy(m.native["data"]))
                else:
                    if m.text:
                        items.append({"role": "assistant", "content": [{"type": "output_text", "text": m.text}]})
                    items += [{"type": "function_call", "call_id": c.id, "name": c.name, "arguments": json.dumps(c.args)}
                              for c in m.tool_calls]
            elif m.role == "tool":
                items += [{"type": "function_call_output", "call_id": r.call_id,
                           "output": ("ERROR: " if r.is_error else "") + r.content} for r in m.tool_results]
        body: dict[str, Any] = {"model": self.model, "input": items, "store": False,
                                "include": ["reasoning.encrypted_content"], "max_output_tokens": self.max_output_tokens}
        if system:
            body["instructions"] = system
        if self.settings.get("reasoning"):
            body["reasoning"] = self.settings["reasoning"]
        elif self.settings.get("send_temperature", True):
            body["temperature"] = self.temperature
        if tools:
            body["tools"] = [{"type": "function", "name": t.name, "description": t.description,
                              "parameters": t.parameters, "strict": False} for t in tools]
        return body

    def from_wire(self, body: dict[str, Any]) -> ModelTurn:
        if body.get("error"):
            raise ProviderError(f"OpenAI error: {body['error']}", status="error")
        output = body.get("output") or []
        texts, calls = [], []
        for item in output:
            t = item.get("type")
            if t == "message":
                texts += [c.get("text", "") for c in item.get("content", []) if c.get("type") == "output_text"]
            elif t == "function_call":
                try:
                    args = json.loads(item.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {"_unparseable_arguments": item.get("arguments", "")}
                calls.append(ToolCall(id=item["call_id"], name=item.get("name", ""), args=args))
        u = body.get("usage") or {}
        usage = Usage(input_tokens=int(u.get("input_tokens", 0)), output_tokens=int(u.get("output_tokens", 0)),
                      reasoning_tokens=int((u.get("output_tokens_details") or {}).get("reasoning_tokens", 0)))
        msg = Message(role="assistant", text="".join(texts).strip(), tool_calls=calls,
                      native={"provider": self.provider, "data": output})
        return ModelTurn(message=msg, usage=usage, stop_reason=str(body.get("status", "")))

    def _request(self, body: dict[str, Any], timeout_s: float, cancel: CancelToken) -> dict[str, Any]:
        return post_json(f"{self.base_url}/responses", body, {"Authorization": f"Bearer {self._key()}"}, timeout_s, cancel)
