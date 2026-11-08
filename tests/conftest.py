"""Shared helpers. All tests here are deterministic and offline: providers are MOCKS.

Real-provider tests live in tests/e2e and only run with EC_REAL=1 (`pytest -m real`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evidence_council.config import load_config  # noqa: E402


def mock(name: str, answer=None, *, locality: str = "local", behavior: str = "ok", claims=None, assumptions=None,
         confidence=0.8, **extra) -> dict:
    spec = {"name": name, "type": "mock", "locality": locality, "behavior": behavior, **extra}
    if answer is not None:
        spec["response"] = {"answer": answer, "summary": f"{name} summary", "assumptions": assumptions or [],
                            "claims": claims or [], "uncertainties": [], "confidence": confidence}
    return spec


@pytest.fixture
def make_config(tmp_path):
    """make_config([mock(...), ...]) -> (Config, config_path). Lesson memory isolated in tmp_path/home."""

    def _make(providers, defaults=None, privacy=None):
        cfg = {"providers": providers, "defaults": {"timeout_s": 5, "retries": 0, **(defaults or {})}}
        if privacy:
            cfg["privacy"] = privacy
        p = tmp_path / "config.json"
        p.write_text(json.dumps(cfg))
        return load_config(p, tmp_path / "home"), p

    return _make


def task(prompt="Question?", *, fmt="text", checks=None, evidence=None, level="public", candidates=None, **extra) -> dict:
    t = {"schema_version": "1.0",
         "task": {"id": extra.pop("task_id", "t1"), "prompt": prompt, "answer_format": fmt, "tags": extra.pop("tags", ["test"]),
                  "domain": extra.pop("domain", "general"), "kind": extra.pop("kind", "objective")},
         "privacy": {"level": level, **extra.pop("privacy", {})},
         "options": {"use_lessons": False, **extra.pop("options", {})}}
    if "answer_extract" in extra:
        t["task"]["answer_extract"] = extra.pop("answer_extract")
    if checks:
        t["checks"] = checks
    if evidence:
        t["evidence"] = evidence
    if candidates:
        t["candidates"] = candidates
    t.update(extra)
    return t


def run_cli(*args: str, cwd=None, env_extra=None, input_text=None):
    env = {k: v for k, v in os.environ.items() if k != "EVIDENCE_COUNCIL_ACTIVE"}
    env["PYTHONPATH"] = str(ROOT / "src")
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, "-m", "evidence_council", *args], capture_output=True, text=True,
                       cwd=cwd or ROOT, env=env, input=input_text, timeout=120)
    return p.returncode, p.stdout, p.stderr


def write(tmp_path, name, obj) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return str(p)
