"""Model adapter factory for the coding agent."""

from __future__ import annotations

from typing import Any

from ...errors import CouncilError, EXIT_USAGE
from .anthropic import AnthropicModel
from .base import ChatModel
from .gemini import GeminiModel
from .openai import OpenAIModel
from .scripted import ScriptedModel

ADAPTERS: dict[str, type[ChatModel]] = {"gemini": GeminiModel, "openai": OpenAIModel, "anthropic": AnthropicModel}


def build_model(agent_cfg: dict[str, Any], provider: str | None = None, model: str | None = None,
                script: str | None = None) -> ChatModel:
    """Build the configured chat model. ``script`` selects the offline scripted model."""
    defaults = {k: agent_cfg.get(k) for k in ("timeout_s", "retries", "temperature", "max_output_tokens") if agent_cfg.get(k) is not None}
    if script:
        return ScriptedModel(script, defaults)
    name = provider or agent_cfg.get("provider", "gemini")
    if name not in ADAPTERS:
        raise CouncilError(f"unknown agent provider '{name}'", hint=f"Use one of: {', '.join(sorted(ADAPTERS))}.", exit_code=EXIT_USAGE)
    settings = dict(agent_cfg.get("providers", {}).get(name, {}))
    if model:
        settings["model"] = model
    return ADAPTERS[name](settings, agent_cfg.get("pricing", {}), defaults)


__all__ = ["build_model", "ChatModel", "GeminiModel", "OpenAIModel", "AnthropicModel", "ScriptedModel", "ADAPTERS"]
