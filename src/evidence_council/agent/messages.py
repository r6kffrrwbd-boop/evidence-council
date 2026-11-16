"""Provider-neutral conversation types for the coding agent.

The agent workflow (intake, builder, critic, optimizer, evaluator) only ever sees these
types. Each model adapter translates them to and from its provider's wire format.

Preserving provider state
-------------------------
Providers attach state to their replies that must be sent back unchanged on the next
turn: Gemini ``thoughtSignature`` on parts, OpenAI ``reasoning`` items with
``encrypted_content``, Anthropic ``thinking`` / ``redacted_thinking`` blocks with
``signature``, and every tool-call ID. Rather than modelling each one, an assistant
:class:`Message` keeps the provider's reply verbatim in ``native`` and the adapter
replays it as-is. The neutral fields (``text``, ``tool_calls``) are a read-only view
used by the workflow and the progress display.

``native`` is opaque: it is never printed, never shown to the user, and never parsed
for content. The agent does not request or display private chain-of-thought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]


@dataclass
class ToolCall:
    id: str                      # provider's call id (or a synthetic one when the provider gives none)
    name: str
    args: dict[str, Any]
    id_from_provider: bool = True


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    role: Role
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    # Verbatim provider reply for assistant turns: {"provider": "gemini", "data": <native content>}.
    native: dict[str, Any] | None = None

    @staticmethod
    def user(text: str) -> "Message":
        return Message(role="user", text=text)

    @staticmethod
    def tool(results: list[ToolResult]) -> "Message":
        return Message(role="tool", tool_results=results)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]   # JSON Schema subset: type/properties/required/items/enum/description


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0       # includes reasoning tokens where the provider bills them as output
    reasoning_tokens: int = 0    # count only; content is never stored or shown

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens


@dataclass
class ModelTurn:
    message: Message
    usage: Usage
    stop_reason: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0
