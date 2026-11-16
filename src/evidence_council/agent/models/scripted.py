"""Deterministic scripted model for offline tests and demos.

A script file maps each agent role ("intake", "builder", "critic", "optimizer",
"evaluator") to a list of turns, consumed in order::

    {"pricing": {"input": 0.30, "output": 2.50},
     "builder": [{"text": "...", "tool_calls": [{"name": "read_file", "args": {"path": "shop.py"}}],
                  "usage": {"input_tokens": 900, "output_tokens": 120}}]}

It never contacts a network. Like a real provider it attaches opaque state to every reply
(a fake ``signature``) and checks on the next call that the workflow sent every earlier
reply back unchanged and that each tool result answers a call id it issued. That makes
the provider-state preservation of the workflow testable without an API key.

Output from this model is labelled ``scripted`` everywhere: it is a test fixture, not AI.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ...providers.base import CancelToken, ProviderError
from ..messages import Message, ModelTurn, ToolCall, ToolSpec, Usage
from .base import ChatModel


class ScriptedModel(ChatModel):
    provider = "scripted"

    def __init__(self, script: dict[str, Any] | str | Path, defaults: dict[str, Any] | None = None):
        if not isinstance(script, dict):
            script = json.loads(Path(script).read_text(encoding="utf-8"))
        pricing = {"scripted": script.get("pricing", {"input": 0.30, "output": 2.50})}
        super().__init__({"model": "scripted", "api_key_env": ""}, pricing, defaults or {})
        self.script = script
        self.cursor: dict[str, int] = {}
        self.issued: dict[str, Any] = {}      # signature -> native data handed out
        self.call_ids: set[str] = set()
        self.calls: list[dict[str, Any]] = []  # what each call received (for tests)

    def check(self) -> tuple[bool, str]:
        return True, "scripted test model (no network)"

    def to_wire(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:
        return {"system": system, "messages": messages, "tools": [t.name for t in tools]}

    def from_wire(self, body: dict[str, Any]) -> ModelTurn:  # pragma: no cover - unused
        raise NotImplementedError

    def complete(self, system: str, messages: list[Message], tools: list[ToolSpec], *,
                 purpose: str = "", cancel: CancelToken | None = None, timeout_s: float | None = None) -> ModelTurn:
        self._verify_history(messages)
        role = purpose.split(":")[0] or "default"
        turns = self.script.get(role, [])
        i = self.cursor.get(role, 0)
        if i >= len(turns):
            raise ProviderError(f"scripted model has no turn {i + 1} for role '{role}' (script exhausted)", status="error")
        self.cursor[role] = i + 1
        spec = turns[i]
        self.calls.append({"role": role, "turn": i + 1, "tools": [t.name for t in tools], "n_messages": len(messages),
                           "last_user": next((m.text for m in reversed(messages) if m.role == "user"), "")})
        calls = []
        for j, c in enumerate(spec.get("tool_calls", [])):
            cid = f"scripted-{role}-{i + 1}-{j + 1}"
            self.call_ids.add(cid)
            calls.append(ToolCall(id=cid, name=c["name"], args=copy.deepcopy(c.get("args", {}))))
        sig = f"sig-{role}-{i + 1}"
        native = {"provider": self.provider, "data": {"signature": sig, "turn": i + 1}}
        self.issued[sig] = copy.deepcopy(native["data"])
        u = spec.get("usage", {})
        usage = Usage(input_tokens=int(u.get("input_tokens", 1000)), output_tokens=int(u.get("output_tokens", 150)))
        msg = Message(role="assistant", text=spec.get("text", ""), tool_calls=calls, native=native)
        turn = ModelTurn(message=msg, usage=usage, stop_reason="scripted")
        turn.cost_usd = self.cost(usage)
        return turn

    def _verify_history(self, messages: list[Message]) -> None:
        seen_calls: set[str] = set()
        for m in messages:
            if m.role == "assistant" and m.native and m.native.get("provider") == self.provider:
                data = m.native.get("data") or {}
                if self.issued.get(data.get("signature")) != data:
                    raise ProviderError("reasoning context was not sent back unchanged (scripted signature mismatch)", status="error")
                seen_calls |= {c.id for c in m.tool_calls}
            if m.role == "tool":
                for r in m.tool_results:
                    if r.call_id not in seen_calls:
                        raise ProviderError(f"tool result references unknown call id {r.call_id}", status="error")
