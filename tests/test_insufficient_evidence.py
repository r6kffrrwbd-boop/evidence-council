"""The council says 'insufficient_evidence' rather than inventing certainty."""

from conftest import mock, task
from evidence_council.council import run_review
from evidence_council.explain import reading


def test_no_checks_means_insufficient(make_config):
    cfg, _ = make_config([mock("a", "Paris"), mock("b", "Lyon")])
    res = run_review(task("Capital?"), cfg)
    v = res["verdict"]
    assert v["status"] == "insufficient_evidence" and v["selected_answer"] is None
    assert sorted(v["unrejected_options"]) == ["A", "B"]
    r = reading(res)
    assert r["can_act_on_answer"] is False and r["answer"] is None
    assert any("Do not present any answer as verified" in m for m in r["must_not_claim"])


def test_errored_reference_check_is_not_a_pass(make_config):
    cfg, _ = make_config([mock("a", "14:32"), mock("b", "14:00")])
    res = run_review(task("When?", checks=[{"id": "f", "type": "fact_answer", "fact_key": "missing_key"}],
                          evidence=[{"id": "e1", "kind": "log", "content": "x", "facts": [{"key": "other", "value": 1}]}]), cfg)
    assert {c["status"] for c in res["verification"]["checks"] if c["check_id"] == "f"} == {"error"}
    assert res["verdict"]["status"] == "insufficient_evidence"


def test_self_contradictory_evidence_is_flagged(make_config):
    cfg, _ = make_config([mock("a", "14:32")])
    ev = [{"id": "e1", "kind": "log", "facts": [{"key": "start", "value": "14:32"}]},
          {"id": "e2", "kind": "document", "facts": [{"key": "start", "value": "14:00"}]}]
    res = run_review(task("When?", checks=[{"id": "f", "type": "fact_answer", "fact_key": "start"}], evidence=ev), cfg)
    c = next(c for c in res["verification"]["checks"] if c["check_id"] == "f")
    assert c["status"] == "error" and "self-contradictory" in c["detail"]
    assert res["verdict"]["status"] == "insufficient_evidence"


def test_code_checks_do_not_run_without_permission(make_config):
    cfg, _ = make_config([mock("a", "def f():\n    return 1\n")])
    res = run_review(task("Write f", fmt="code", checks=[{"id": "t", "type": "python_tests", "test_code": "from solution import f\nassert f()==1"}]), cfg)
    c = res["verification"]["checks"][0]
    assert c["status"] == "not_run" and c["ran"] is False and "allow_code_execution" in c["detail"]
    assert res["verdict"]["status"] == "insufficient_evidence"


def test_format_check_alone_cannot_select(make_config):
    cfg, _ = make_config([mock("a", "2026-01-01")])
    res = run_review(task("date?", checks=[{"id": "iso", "type": "regex", "pattern": r"^\d{4}-\d{2}-\d{2}$"}]), cfg)
    assert res["verification"]["checks"][0]["status"] == "pass"
    assert res["verdict"]["status"] == "insufficient_evidence"


def test_all_rejected(make_config):
    cfg, _ = make_config([mock("a", "5"), mock("b", "6")])
    res = run_review(task("n?", fmt="number", checks=[{"id": "r", "type": "numeric_reference", "expression": "2+2"}]), cfg)
    assert res["verdict"]["status"] == "all_rejected"
    assert len(res["verdict"]["rejected"]) == 2
