"""Operator configuration (not part of the task input).

Lookup order: ``--config PATH`` > ``$EVIDENCE_COUNCIL_CONFIG`` >
``~/.config/evidence-council/config.json`` > built-in defaults.

Privacy overrides (``allow_private_to``) live here, never in the task input,
so a program that submits tasks cannot unlock cloud forwarding by itself.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import CouncilError, EXIT_USAGE

DEFAULT_CONFIG: dict[str, Any] = {
    "providers": [
        {"name": "codex", "type": "codex", "enabled": True, "timeout_s": 300},
        {"name": "claude", "type": "claude", "enabled": True, "timeout_s": 300},
        {"name": "gemini", "type": "gemini", "enabled": True, "timeout_s": 180},
        {"name": "hermes", "type": "hermes", "enabled": True, "timeout_s": 240, "locality": "cloud"},
        {"name": "ollama", "type": "ollama", "enabled": True, "models": "auto", "max_models": 2,
         "base_url": "http://127.0.0.1:11434", "timeout_s": 420},
        {"name": "grok", "type": "openai_compat", "enabled": True, "base_url": "https://api.x.ai/v1",
         "model": "grok-4", "api_key_env": "XAI_API_KEY", "timeout_s": 180},
        {"name": "openrouter", "type": "openai_compat", "enabled": False, "base_url": "https://openrouter.ai/api/v1",
         "model": "", "api_key_env": "OPENROUTER_API_KEY", "timeout_s": 180},
    ],
    "privacy": {"allow_private_to": []},
    "defaults": {
        "timeout_s": 240,
        "deadline_s": 900,
        "retries": 1,
        "min_participants": 1,
        "max_output_chars": 20000,
        "max_parallel": 4,
    },
}


def default_config_path() -> Path:
    env = os.environ.get("EVIDENCE_COUNCIL_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "evidence-council" / "config.json"


def default_home() -> Path:
    env = os.environ.get("EVIDENCE_COUNCIL_HOME")
    if env:
        return Path(env).expanduser()
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "evidence-council"


@dataclass
class Config:
    providers: list[dict[str, Any]]
    allow_private_to: list[str]
    defaults: dict[str, Any]
    home: Path
    source: str = "built-in defaults"
    extra_allow_private_to: list[str] = field(default_factory=list)

    @property
    def memory_dir(self) -> Path:
        return self.home / "memory"

    def private_allowlist(self) -> set[str]:
        return set(self.allow_private_to) | set(self.extra_allow_private_to)


def load_config(path: str | os.PathLike[str] | None = None, home: str | os.PathLike[str] | None = None) -> Config:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    source = "built-in defaults"
    p = Path(path).expanduser() if path else default_config_path()
    if path and not p.exists():
        raise CouncilError(f"config file not found: {p}", hint="Create it (see docs/providers.md) or omit --config to use defaults.", exit_code=EXIT_USAGE)
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise CouncilError(f"config {p} is not valid JSON: {e.msg} (line {e.lineno})", hint="Fix the syntax or move the file aside to use defaults.", exit_code=EXIT_USAGE) from None
        if not isinstance(user, dict):
            raise CouncilError(f"config {p} must be a JSON object", exit_code=EXIT_USAGE)
        if "providers" in user:
            if not isinstance(user["providers"], list) or not all(isinstance(x, dict) and "name" in x and "type" in x for x in user["providers"]):
                raise CouncilError(f"config {p}: 'providers' must be a list of objects with 'name' and 'type'", hint="See docs/providers.md for the provider entry format.", exit_code=EXIT_USAGE)
            cfg["providers"] = user["providers"]
        cfg["privacy"].update(user.get("privacy", {}))
        cfg["defaults"].update(user.get("defaults", {}))
        source = str(p)
    names = [x["name"] for x in cfg["providers"]]
    if len(names) != len(set(names)):
        raise CouncilError("config has duplicate provider names", hint="Provider names must be unique.", exit_code=EXIT_USAGE)
    return Config(
        providers=cfg["providers"],
        allow_private_to=list(cfg["privacy"].get("allow_private_to", [])),
        defaults=cfg["defaults"],
        home=Path(home).expanduser() if home else default_home(),
        source=source,
    )
