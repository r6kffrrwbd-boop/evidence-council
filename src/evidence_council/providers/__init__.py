"""Provider registry: turn config entries into adapters and select participants."""

from __future__ import annotations

from typing import Any

from .base import Adapter, Availability, CallResult, CancelToken, ProviderError, RECURSION_ENV
from .cli_adapters import ClaudeAdapter, CodexAdapter, GeminiAdapter, HermesAdapter
from .http_adapters import OllamaAdapter, OpenAICompatAdapter, list_ollama_models
from .mock import MockAdapter

ADAPTER_TYPES: dict[str, type[Adapter]] = {
    "codex": CodexAdapter,
    "claude": ClaudeAdapter,
    "gemini": GeminiAdapter,
    "hermes": HermesAdapter,
    "ollama": OllamaAdapter,
    "openai_compat": OpenAICompatAdapter,
    "mock": MockAdapter,
}

__all__ = ["Adapter", "Availability", "CallResult", "CancelToken", "ProviderError", "RECURSION_ENV",
           "ADAPTER_TYPES", "build_adapters", "select_adapters"]


def build_adapters(providers: list[dict[str, Any]], defaults: dict[str, Any], include_disabled: bool = False) -> list[Adapter]:
    """Instantiate adapters for config entries. Ollama with ``models: "auto"`` expands to one adapter per model."""
    out: list[Adapter] = []
    for spec in providers:
        if not spec.get("enabled", True) and not include_disabled:
            continue
        cls = ADAPTER_TYPES.get(spec["type"])
        if cls is None:
            raise ValueError(f"unknown provider type '{spec['type']}' for '{spec['name']}' (known: {', '.join(ADAPTER_TYPES)})")
        if spec["type"] == "ollama" and "model" not in spec:
            models = spec.get("models", "auto")
            if models == "auto":
                try:
                    found = list_ollama_models(spec.get("base_url", "http://127.0.0.1:11434"))
                    found = [m for m in found if "embed" not in m.get("name", "")]
                    found.sort(key=lambda m: -int(m.get("size", 0)))
                    models = [m["name"] for m in found[: int(spec.get("max_models", 2))]]
                except ProviderError:
                    models = []
            if not models:
                out.append(cls({**spec, "model": ""}, defaults))
                continue
            for m in models:
                out.append(cls({**spec, "name": f"{spec['name']}:{m}", "model": m}, defaults))
        else:
            out.append(cls(spec, defaults))
    return out


def _matches(name: str, pattern: str) -> bool:
    return name == pattern or name.startswith(pattern + ":")


def select_adapters(adapters: list[Adapter], requested: list[str], exclude: list[str]) -> tuple[list[Adapter], list[dict[str, str]]]:
    """Apply requested/exclude lists. Returns (selected, excluded-with-reason)."""
    excluded: list[dict[str, str]] = []
    if requested == ["none"]:
        return [], [{"provider": a.name, "reason": "providers.requested is ['none'] (candidate-only review)"} for a in adapters]
    selected = adapters
    if requested:
        selected = [a for a in adapters if any(_matches(a.name, r) for r in requested)]
        for r in requested:
            if not any(_matches(a.name, r) for a in adapters):
                excluded.append({"provider": r, "reason": "requested but not configured/enabled (see `evidence-council providers`)"})
        for a in adapters:
            if a not in selected:
                excluded.append({"provider": a.name, "reason": "not in providers.requested"})
    final = []
    for a in selected:
        if any(_matches(a.name, x) for x in exclude):
            excluded.append({"provider": a.name, "reason": "in providers.exclude"})
        else:
            final.append(a)
    return final, excluded
