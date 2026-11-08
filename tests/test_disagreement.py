"""Comparison stage surfaces differing answers, claims and assumptions — and decides nothing."""

from conftest import mock, task
from evidence_council.council import run_review


def test_disagreements_detected(make_config):
    cfg, _ = make_config([
        mock("a", "Use PostgreSQL", assumptions=["data is relational"], claims=[{"statement": "x", "key": "max_rows", "value": "10M"}]),
        mock("b", "use postgresql.", assumptions=["data is relational", "team knows SQL"], claims=[{"statement": "y", "key": "max_rows", "value": "1B"}]),
        mock("c", "Use MongoDB", assumptions=["schema changes weekly"], confidence=0.2),
    ])
    res = run_review(task("Which database?"), cfg)
    opts = res["options"]
    assert [o["support_count"] for o in opts] == [2, 1]          # normalisation groups a+b
    types = {d["type"] for d in res["disagreements"]}
    assert {"answer", "claim", "assumption", "confidence"} <= types
    claim = next(d for d in res["disagreements"] if d["type"] == "claim")
    assert claim["topic"] == "value of 'max_rows'" and len(claim["positions"]) == 2
    # no objective checks: popularity must not produce a winner
    assert res["verdict"]["status"] == "insufficient_evidence"
    assert res["verdict"]["selected_option"] is None


def test_unanimous_agreement_is_not_verification(make_config):
    cfg, _ = make_config([mock("a", "42"), mock("b", "42"), mock("c", "42")])
    res = run_review(task("Answer?", fmt="number"), cfg)
    assert len(res["options"]) == 1 and res["disagreements"] == []
    assert res["verdict"]["status"] == "insufficient_evidence"
    assert any("agreement is not verification" in r for r in res["verdict"]["rationale"])
    assert res["uncertainty"]["level"] == "high"
    assert res["candidate_lesson"] is None


def test_proposers_never_see_each_other_or_candidates(make_config, tmp_path):
    rec = tmp_path / "prompts.jsonl"
    cfg, _ = make_config([mock("a", "ALPHA-ANSWER", record_prompts_to=str(rec)), mock("b", "BETA-ANSWER", record_prompts_to=str(rec))])
    run_review(task("Q?", candidates=[{"id": "draft1", "answer": "DRAFT-CANDIDATE-TEXT"}]), cfg)
    prompts = rec.read_text()
    assert "ALPHA-ANSWER" not in prompts and "BETA-ANSWER" not in prompts
    assert "DRAFT-CANDIDATE-TEXT" not in prompts
    assert prompts.count("ROLE: INDEPENDENT PROPOSER") == 2
