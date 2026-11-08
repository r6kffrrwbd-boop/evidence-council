"""REAL end-to-end runs against providers installed on this machine. Not mocked.

Opt-in only:  EC_REAL=1 pytest -m real tests/e2e -s
Uses whatever `evidence-council providers --probe` finds callable; skips if fewer than two.
Results are written to examples/real-runs/ so they can be inspected.
"""

import json
import os
from pathlib import Path

import pytest

from evidence_council.cli import main as cli_main  # noqa: F401  (import check)
from evidence_council.config import load_config
from evidence_council.council import run_review
from evidence_council.explain import reading
from evidence_council.providers import CancelToken, build_adapters

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.real, pytest.mark.skipif(os.environ.get("EC_REAL") != "1", reason="set EC_REAL=1 to run real-provider tests")]


def _callable(cfg):
    names = []
    for a in build_adapters(cfg.providers, cfg.defaults):
        if a.check().available:
            a.probe_mode = True
            r = a.generate("Reply with exactly the word PONG and nothing else.", CancelToken(), timeout_s=180, retries=0)
            a.probe_mode = False
            if r.status == "ok":
                names.append(a.name)
    return names


def test_real_council_numeric_task():
    cfg = load_config(os.environ.get("EC_REAL_CONFIG"))
    names = _callable(cfg)
    if len(names) < 2:
        pytest.skip(f"need two callable providers, found {names}")
    task = json.loads((ROOT / "examples/inputs/real-arithmetic.json").read_text())
    res = run_review(task, cfg, requested=names)
    out = ROOT / "examples/real-runs" / "real-arithmetic.result.json"
    out.write_text(json.dumps(res, indent=2))
    r = reading(res)
    print(json.dumps(r, indent=2))
    assert res["providers"]["used"], "no provider produced a proposal"
    assert all(a["adapter"] != "mock" for a in res["providers"]["attempted"])
    assert res["verification"]["summary"]["run"] >= 1
    # the selected answer, if any, must be the recomputed value - never a popular wrong one
    if res["verdict"]["status"] == "supported":
        from evidence_council.parsing import parse_number
        from evidence_council.safe_eval import safe_eval
        expected = safe_eval(task["checks"][0]["expression"])
        assert parse_number(res["verdict"]["selected_answer"]) == expected
