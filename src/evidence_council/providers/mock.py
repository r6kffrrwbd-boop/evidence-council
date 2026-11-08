"""Deterministic mock provider for tests, demos and the self-test. Never contacts anything.

Config entry::

    {"name": "mock-a", "type": "mock", "locality": "local" | "cloud",
     "behavior": "ok" | "timeout" | "error" | "transient_then_ok" | "malformed" | "unavailable",
     "response": {...JSON proposal...}  or  "response_text": "raw text",
     "judge_response": {...}, "delay_s": 0, "record_prompts_to": "/path/prompts.jsonl"}

Mock results are always labelled ``adapter: mock`` in outputs so they can never be
mistaken for a real provider run.
"""

from __future__ import annotations

import json
import time

from .base import Adapter, Availability, CancelToken, ProviderError, RawResponse


class MockAdapter(Adapter):
    adapter_type = "mock"
    default_locality = "local"

    def __init__(self, spec, defaults):
        super().__init__(spec, defaults)
        self.behavior = spec.get("behavior", "ok")
        self.calls = 0

    def check(self) -> Availability:
        if self.behavior == "unavailable":
            return Availability(False, "mock configured as unavailable")
        return Availability(True, "mock")

    def _call(self, prompt: str, timeout_s: float, cancel: CancelToken) -> RawResponse:
        self.calls += 1
        if self.spec.get("record_prompts_to"):
            with open(self.spec["record_prompts_to"], "a", encoding="utf-8") as f:
                f.write(json.dumps({"provider": self.name, "prompt": prompt}) + "\n")
        delay = float(self.spec.get("delay_s", 0))
        if self.behavior == "timeout":
            delay = timeout_s + 5
        t_end = time.monotonic() + delay
        while time.monotonic() < t_end:
            if cancel.is_set():
                raise ProviderError("cancelled", status="cancelled")
            if time.monotonic() - (t_end - delay) > timeout_s:
                raise ProviderError(f"timed out after {timeout_s:.0f}s", status="timeout")
            time.sleep(0.05)
        if self.behavior == "error":
            raise ProviderError("mock provider error (simulated)")
        if self.behavior == "transient_then_ok" and self.calls == 1:
            raise ProviderError("HTTP 503: simulated transient failure", transient=True)
        is_judge = "ROLE: JUDGE" in prompt
        if is_judge and "judge_response" in self.spec:
            body = self.spec["judge_response"]
            return RawResponse(text=body if isinstance(body, str) else json.dumps(body), tokens={"total": 0}, cost_usd=0.0, cost_note="mock")
        if self.behavior == "malformed":
            return RawResponse(text=self.spec.get("response_text", "I think the answer is probably fine.{{{"), cost_usd=0.0, cost_note="mock")
        if "response_text" in self.spec:
            return RawResponse(text=self.spec["response_text"], tokens={"total": 0}, cost_usd=0.0, cost_note="mock")
        return RawResponse(text=json.dumps(self.spec.get("response", {"answer": ""})), tokens={"total": 0}, cost_usd=0.0, cost_note="mock")
