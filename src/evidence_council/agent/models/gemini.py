"""Gemini API adapter (generativelanguage ``models/{model}:generateContent``).

Wire mapping
------------
* user text        -> ``{"role": "user", "parts": [{"text": ...}]}``
* assistant turn   -> the ``candidates[0].content`` object exactly as Gemini returned it.
  Its parts may carry ``thoughtSignature``; Gemini requires these back on later turns
  when function calling is used, so the content is replayed verbatim.
* tool results     -> ``{"role": "user", "parts": [{"functionResponse": {"name", "id"?, "response"}}]}``
  ``id`` is echoed only when Gemini supplied one on the matching ``functionCall``.
* tools            -> ``{"functionDeclarations": [...]}`` with the schema reduced to the
  OpenAPI subset Gemini accepts.

Parts marked ``"thought": true`` (thought summaries, only sent if requested — we do not
request them) are kept inside ``native`` for replay and excluded from visible text.
The key goes in the ``x-goog-api-key`` header, never in the URL.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from ...providers.base import CancelToken, ProviderError
from ..messages import Message, ModelTurn, ToolCall, ToolSpec, Usage
from .base import ChatModel, post_json

_SCHEMA_KEYS = {"type", "description", "properties", "required", "items", "enum", "nullable", "format"}


def gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k not in _SCHEMA_KEYS:
            continue
        if k == "properties":
            out[k] = {name: gemini_schema(sub) for name, sub in v.items()}
        elif k == "items":
            out[k] = gemini_schema(v)
        else:
            out[k] = v
    return out


class GeminiModel(ChatModel):
    provider = "gemini"

    def to_wire(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        call_has_id: dict[str, bool] = {}
        for m in messages:
            if m.role == "user":
                contents.append({"role": "user", "parts": [{"text": m.text}]})
            elif m.role == "assistant":
                for c in m.tool_calls:
                    call_has_id[c.id] = c.id_from_provider
                if m.native and m.native.get("provider") == self.provider:
                    contents.append(copy.deepcopy(m.native["data"]))
                else:  # produced by another adapter (not used within one run): rebuild neutrally
                    parts: list[dict[str, Any]] = [{"text": m.text}] if m.text else []
                    parts += [{"functionCall": {"name": c.name, "args": c.args}} for c in m.tool_calls]
                    contents.append({"role": "model", "parts": parts})
            elif m.role == "tool":
                parts = []
                for r in m.tool_results:
                    fr: dict[str, Any] = {"name": r.name, "response": {"error" if r.is_error else "output": r.content}}
                    if call_has_id.get(r.call_id, False):
                        fr["id"] = r.call_id
                    parts.append({"functionResponse": fr})
                contents.append({"role": "user", "parts": parts})
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": self.temperature, "maxOutputTokens": self.max_output_tokens},
        }
        thinking = self.settings.get("thinking_config")
        if thinking:
            body["generationConfig"]["thinkingConfig"] = thinking
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t.name, "description": t.description, "parameters": gemini_schema(t.parameters)} for t in tools]}]
            body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        return body

    def from_wire(self, body: dict[str, Any]) -> ModelTurn:
        cands = body.get("candidates") or []
        if not cands:
            reason = (body.get("promptFeedback") or {}).get("blockReason", "no candidates returned")
            raise ProviderError(f"Gemini returned no answer ({reason})", status="error")
        cand = cands[0]
        content = cand.get("content") or {"role": "model", "parts": []}
        content.setdefault("role", "model")
        texts, calls = [], []
        for i, part in enumerate(content.get("parts", [])):
            if part.get("thought"):
                continue
            if "text" in part:
                texts.append(part["text"])
            if "functionCall" in part:
                fc = part["functionCall"]
                pid = fc.get("id")
                calls.append(ToolCall(id=pid or f"gemini-call-{len(calls) + 1}-{i}", name=fc.get("name", ""),
                                      args=fc.get("args") or {}, id_from_provider=bool(pid)))
        um = body.get("usageMetadata") or {}
        thoughts = int(um.get("thoughtsTokenCount", 0))
        usage = Usage(input_tokens=int(um.get("promptTokenCount", 0)),
                      output_tokens=int(um.get("candidatesTokenCount", 0)) + thoughts, reasoning_tokens=thoughts)
        msg = Message(role="assistant", text="".join(texts).strip(), tool_calls=calls,
                      native={"provider": self.provider, "data": content})
        return ModelTurn(message=msg, usage=usage, stop_reason=str(cand.get("finishReason", "")))

    def _request(self, body: dict[str, Any], timeout_s: float, cancel: CancelToken) -> dict[str, Any]:
        url = f"{self.base_url}/models/{self.model}:generateContent"
        return post_json(url, body, {"x-goog-api-key": self._key()}, timeout_s, cancel)


def describe_wire(body: dict[str, Any]) -> str:  # debugging aid used by tests
    return json.dumps(body, sort_keys=True)[:2000]
