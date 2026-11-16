"""Hard limits on model calls, tool calls, wall time and API spend.

Limits are part of the approved plan. The CLI may lower them for a run, never raise them.
Before every model call the budget refuses the call if its worst-case cost (estimated
input + the full output-token allowance) could push spend past ``max_cost_usd``, so the
limit holds even when a reply is unexpectedly long.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, fields
from typing import Any

from .messages import ModelTurn


@dataclass
class Limits:
    max_model_calls: int = 40
    max_tool_calls: int = 300
    max_wall_s: int = 1800
    max_cost_usd: float = 0.50
    max_builder_rounds: int = 3
    max_optimizer_attempts: int = 2
    max_turns_per_agent: int = 16
    max_repeat_tool_calls: int = 2
    command_timeout_s: int = 120

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Limits":
        d = d or {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: type(getattr(cls(), k))(v) for k, v in d.items() if k in known})

    def lowered_by(self, overrides: dict[str, Any]) -> "Limits":
        """Apply CLI overrides, but only downwards."""
        out = Limits(**asdict(self))
        for k, v in overrides.items():
            if v is None or not hasattr(out, k):
                continue
            cur = getattr(out, k)
            setattr(out, k, min(cur, type(cur)(v)))
        return out

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, limits: Limits):
        self.limits = limits
        self.t0 = time.monotonic()
        self.spent_usd = 0.0
        self.model_calls = 0
        self.tool_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.reasoning_tokens = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def remaining_s(self) -> float:
        return self.limits.max_wall_s - self.elapsed()

    def check_time(self) -> None:
        if self.elapsed() > self.limits.max_wall_s:
            raise BudgetExceeded(f"time limit reached ({self.limits.max_wall_s}s)")

    def before_model_call(self, worst_case_usd: float) -> None:
        self.check_time()
        if self.model_calls >= self.limits.max_model_calls:
            raise BudgetExceeded(f"model-call limit reached ({self.limits.max_model_calls} calls)")
        if self.spent_usd + worst_case_usd > self.limits.max_cost_usd:
            raise BudgetExceeded(
                f"spend limit: ${self.spent_usd:.4f} spent; the next call could cost up to ${worst_case_usd:.4f}, "
                f"which would exceed the ${self.limits.max_cost_usd:.2f} limit")

    def after_model_call(self, turn: ModelTurn) -> None:
        self.model_calls += 1
        self.spent_usd += turn.cost_usd
        self.input_tokens += turn.usage.input_tokens
        self.output_tokens += turn.usage.output_tokens
        self.reasoning_tokens += turn.usage.reasoning_tokens

    def before_tool_call(self) -> None:
        self.check_time()
        if self.tool_calls >= self.limits.max_tool_calls:
            raise BudgetExceeded(f"tool-call limit reached ({self.limits.max_tool_calls})")
        self.tool_calls += 1

    def summary(self) -> dict[str, Any]:
        return {"spent_usd": round(self.spent_usd, 6), "max_cost_usd": self.limits.max_cost_usd,
                "model_calls": self.model_calls, "max_model_calls": self.limits.max_model_calls,
                "tool_calls": self.tool_calls, "elapsed_s": round(self.elapsed(), 1), "max_wall_s": self.limits.max_wall_s,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "reasoning_tokens": self.reasoning_tokens}
