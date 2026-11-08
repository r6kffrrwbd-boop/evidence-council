"""The council overturns a popular but wrong answer when objective evidence contradicts it."""

import json
import sys

import pytest

from conftest import ROOT, mock, run_cli, task, write
from evidence_council.council import run_review

LEAP_WRONG = "def is_leap(year):\n    return year % 4 == 0\n"
LEAP_RIGHT = "def is_leap(year):\n    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n"
LEAP_TESTS = """from solution import is_leap
assert is_leap(2024)
assert not is_leap(2023)
assert not is_leap(1900), "1900 is not a leap year"
assert is_leap(2000)
print("4 tests passed")
"""


def test_numeric_majority_overturned_via_cli(tmp_path):
    out = tmp_path / "r.json"
    rc, _, err = run_cli("review", str(ROOT / "examples/inputs/numeric-overturn.json"),
                         "--config", str(ROOT / "examples/configs/mock-overturn.json"), "--home", str(tmp_path), "-o", str(out))
    assert rc == 0, err
    res = json.loads(out.read_text())
    v = res["verdict"]
    assert v["status"] == "supported" and v["selected_answer"] == "11398661"
    assert v["overturned_popular"] is True and v["popular_option"] == "A"
    rejected = v["rejected"][0]
    assert rejected["support_count"] == 2 and "computed 3847 * 2963 = 11398661" in rejected["reasons"][0]
    ran = [c for c in res["verification"]["checks"] if c["ran"]]
    assert {c["check_id"] for c in ran} == {"product"} and len(ran) == 2   # one check per option, both actually ran


@pytest.mark.skipif(sys.platform == "win32", reason="posix sandbox")
def test_code_majority_overturned_by_tests(make_config):
    cfg, _ = make_config([mock("a", LEAP_WRONG), mock("b", "```python\n" + LEAP_WRONG + "```"), mock("c", LEAP_RIGHT)])
    res = run_review(task("Write is_leap(year).", fmt="code", domain="coding",
                          checks=[{"id": "unit", "type": "python_tests", "test_code": LEAP_TESTS}],
                          options={"allow_code_execution": True}), cfg)
    v = res["verdict"]
    assert len(res["options"]) == 2                       # fenced and unfenced wrong answers grouped
    assert v["status"] == "supported" and v["overturned_popular"] is True
    sel = next(o for o in res["options"] if o["option_id"] == v["selected_option"])
    assert sel["origins"] == ["c"]
    fail = next(c for c in res["verification"]["checks"] if c["status"] == "fail")
    assert "1900 is not a leap year" in fail["detail"]
    passed = next(c for c in res["verification"]["checks"] if c["status"] == "pass")
    if sys.platform == "darwin":
        assert "(sandboxed)" in passed["detail"]


def test_evidence_fact_overturns_majority(make_config):
    ev = [
        {"id": "status-page", "kind": "web", "content": "Status page: degraded performance from 14:00 UTC.", "recorded_at": "2026-11-02T15:00:00Z"},
        {"id": "lb-log", "kind": "log", "content": "14:31:59 200 OK\n14:32:04 502 Bad Gateway (first error)", "source_ref": "lb.log:1180",
         "facts": [{"key": "first_error_utc", "value": "14:32"}]},
    ]
    claim_wrong = [{"statement": "errors began at 14:00", "key": "first_error_utc", "value": "14:00", "evidence_ids": ["status-page"],
                    "quote": "degraded performance from 14:00 UTC"}]
    claim_right = [{"statement": "first 502 at 14:32:04", "key": "first_error_utc", "value": "14:32", "evidence_ids": ["lb-log"],
                    "quote": "14:32:04 502 Bad Gateway"}]
    cfg, _ = make_config([mock("a", "14:00", claims=claim_wrong), mock("b", "14:00", claims=claim_wrong), mock("c", "14:32", claims=claim_right)])
    res = run_review(task("When did errors start (UTC, HH:MM)?", evidence=ev,
                          checks=[{"id": "first-error", "type": "fact_answer", "fact_key": "first_error_utc"}]), cfg)
    v = res["verdict"]
    assert v["status"] == "supported" and v["selected_answer"] == "14:32" and v["overturned_popular"]
    statuses = {c["origin"]: c["status"] for c in res["claims"]}
    assert statuses == {"a": "contradicted", "b": "contradicted", "c": "supported"}
    auto = {c["check_id"] for c in res["verification"]["checks"] if c["auto"]}
    assert auto == {"auto.evidence_facts", "auto.citations"}


def test_fabricated_citation_detected(make_config):
    claims = [{"statement": "the log says so", "evidence_ids": ["nonexistent"]},
              {"statement": "quote", "evidence_ids": ["e1"], "quote": "this text is not in the log"}]
    cfg, _ = make_config([mock("a", "x", claims=claims)])
    res = run_review(task("q", evidence=[{"id": "e1", "kind": "log", "content": "real log line"}]), cfg)
    details = [c["detail"] for c in res["verification"]["checks"] if c["status"] == "fail"]
    assert any("does not exist" in d for d in details) and any("quote not found" in d for d in details)


def test_formatting_difference_is_not_a_false_overturn(make_config):
    """Regression from a REAL run (examples/real-runs/incident-evidence.run1-false-rejection.*):
    '14:32 UTC' and '14:32' are the same answer; with answer_extract they must not be split or rejected."""
    ev = [{"id": "lb-log", "kind": "log", "content": "14:32:04 502", "facts": [{"key": "first_error_utc", "value": "14:32"}]}]
    cfg, _ = make_config([mock("a", "14:32 UTC"), mock("b", "14:32 UTC"), mock("c", "14:32")])
    chk = [{"id": "first-error", "type": "fact_answer", "fact_key": "first_error_utc"}]
    strict = run_review(task("When?", evidence=ev, checks=chk), cfg)
    assert strict["verdict"]["overturned_popular"] is True          # documents the strict behaviour without extraction
    res = run_review(task("When?", evidence=ev, checks=chk, answer_extract=r"\d{2}:\d{2}"), cfg)
    assert len(res["options"]) == 1 and res["options"][0]["support_count"] == 3
    assert res["verdict"]["status"] == "supported" and res["verdict"]["overturned_popular"] is False
    assert res["verdict"]["rejected"] == []
