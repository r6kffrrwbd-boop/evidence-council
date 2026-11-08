"""Subjective tasks expose rubric judgments instead of a winner; verifier helpers are safe."""

import sys

import pytest

from conftest import mock, task
from evidence_council.council import run_review
from evidence_council.safe_eval import UnsafeExpression, safe_eval
from evidence_council.sandbox import python_argv, run_untrusted

RUBRIC = [{"criterion": "clarity", "weight": 1}, {"criterion": "empathy", "weight": 2}]


def judge(scores):
    return {"scores": scores}


def test_subjective_task_has_no_winner_and_judges_are_independent(make_config):
    cfg, _ = make_config([
        mock("a", "Open with a question.", judge_response=judge([
            {"proposal_id": "p2", "criterion": "clarity", "score": 4, "reason": "clear"},
            {"proposal_id": "p2", "criterion": "empathy", "score": 2, "reason": "cold"},
            {"proposal_id": "p1", "criterion": "clarity", "score": 5, "reason": "self-score attempt"}])),
        mock("b", "Open with empathy.", judge_response=judge([
            {"proposal_id": "p1", "criterion": "clarity", "score": 3, "reason": "ok"},
            {"proposal_id": "p1", "criterion": "empathy", "score": 5, "reason": "warm"}])),
    ])
    res = run_review(task("Best opening line for a renewal call?", kind="subjective", rubric=RUBRIC), cfg)
    v = res["verdict"]
    assert v["status"] == "subjective" and v["selected_option"] is None
    assert any("no objective winner" in r for r in v["rationale"])
    # judge 'a' (origin of p1) may not score p1: its self-score was discarded
    assert not any(j["judge"] == "a" and j["proposal_id"] == "p1" for j in res["judgments"])
    assert {j["judge"] for j in res["judgments"]} == {"a", "b"}
    assert {s["proposal_id"] for s in v["judgment_summary"]} == {"p1", "p2"}
    assert res["uncertainty"]["level"] == "medium"
    assert res["candidate_lesson"] is None


@pytest.mark.parametrize("expr,val", [("3847 * 2963", 11398661), ("2**10 - 24", 1000), ("count('strawberry', 'r')", 3), ("round(10/3, 2)", 3.33)])
def test_safe_eval_allows_arithmetic(expr, val):
    assert safe_eval(expr) == val


@pytest.mark.parametrize("expr", ["__import__('os').system('id')", "open('/etc/passwd').read()", "(1).__class__", "[x for x in range(9)]", "9**99999"])
def test_safe_eval_rejects_code(expr):
    with pytest.raises(UnsafeExpression):
        safe_eval(expr)


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")
def test_sandbox_blocks_network_and_home():
    code = """
import socket, os, pathlib
try:
    socket.create_connection(("1.1.1.1", 80), timeout=3); print("NET-OPEN")
except Exception: print("net-blocked")
try:
    open(os.path.expanduser("~real/.zshrc")); print("HOME-OPEN")
except Exception: print("home-blocked")
"""
    import pathlib
    real_home = str(pathlib.Path.home())
    r = run_untrusted(python_argv("t.py"), {"t.py": code.replace("~real", real_home)}, None, 20)
    assert r.sandboxed and r.returncode == 0
    assert "net-blocked" in r.stdout and "home-blocked" in r.stdout


def test_sandbox_timeout():
    r = run_untrusted(python_argv("t.py"), {"t.py": "while True: pass"}, None, 2)
    assert r.timed_out
