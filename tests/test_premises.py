"""Premise labelling: OBSERVED / NOT VERIFIED reach providers; premises naming a fact are checked."""

import json

from conftest import mock, run_cli, task, write
from evidence_council.contract import validate
from evidence_council.council import run_review
from evidence_council.explain import reading

EV = [{"id": "cfg", "kind": "data", "content": "replicas: 1", "facts": [{"key": "replicas", "value": 1}]}]
CHECK = [{"id": "ref", "type": "reference_value", "value": "SQLite"}]


def _prompt(rec):
    return "\n".join(json.loads(l)["prompt"] for l in rec.read_text().splitlines())


def test_premises_are_labelled_in_prompt_and_redacted(make_config, tmp_path):
    rec = tmp_path / "p.jsonl"
    cfg, _ = make_config([mock("cloud-m", "SQLite", locality="cloud", record_prompts_to=str(rec))])
    prem = [{"statement": "The service runs as a single replica", "status": "observed", "basis": "checked deploy.yaml"},
            {"statement": "Write volume stays under 50/s; owner reachable at +1 (555) 201-3344", "status": "not_verified",
             "basis": "a week of production metrics"}]
    run_review(task("Which database?", premises=prem), cfg)
    p = _prompt(rec)
    assert "OBSERVED: The service runs as a single replica (confirmed by: checked deploy.yaml)" in p
    assert "NOT VERIFIED: Write volume stays under 50/s" in p and "(would be confirmed by: a week of production metrics)" in p
    assert "+1 (555) 201-3344" not in p and "[PHONE_1]" in p          # premises are minimised like everything else


def test_not_verified_premise_is_reported_and_raises_uncertainty(make_config):
    cfg, _ = make_config([mock("a", "SQLite"), mock("b", "SQLite")])
    res = run_review(task("Which database?", checks=CHECK, premises=[
        {"id": "load", "statement": "Write volume stays under 50/s", "status": "not_verified", "basis": "production metrics"}]), cfg)
    assert res["verdict"]["status"] == "supported"
    assert res["premises"][0]["status"] == "not_verified"
    assert res["verdict"]["unverified"][0].startswith("premise load (NOT VERIFIED)")
    assert any("unverified premise" in r for r in res["uncertainty"]["reasons"])
    assert res["uncertainty"]["level"] != "low"
    assert any("NOT VERIFIED premises" in m for m in reading(res)["must_not_claim"])


def test_premise_confirmed_by_evidence(make_config):
    cfg, _ = make_config([mock("a", "SQLite")])
    res = run_review(task("Which database?", evidence=EV, checks=CHECK, premises=[
        {"id": "single", "statement": "one replica", "status": "not_verified", "fact_key": "replicas", "value": 1}]), cfg)
    p = res["premises"][0]
    assert p["declared"] == "not_verified" and p["status"] == "confirmed_by_evidence"
    assert not any("premise single" in u for u in res["verdict"]["unverified"])


def test_contradicted_premise_flags_framing_and_blocks_lesson(make_config):
    cfg, _ = make_config([mock("a", "SQLite"), mock("b", "SQLite")])
    res = run_review(task("Which database?", evidence=EV, checks=CHECK, premises=[
        {"id": "scale", "statement": "We run three replicas", "status": "observed", "basis": "team said so",
         "fact_key": "replicas", "value": 3}]), cfg)
    assert res["premises"][0]["status"] == "contradicted_by_evidence"
    assert "replicas=3" in res["premises"][0]["detail"] and "cfg=1" in res["premises"][0]["detail"]
    assert res["verdict"]["rationale"][0].startswith("Premise scale is contradicted by the evidence")
    assert res["uncertainty"]["level"] == "high"
    assert res["candidate_lesson"] is None
    r = reading(res)
    assert any("premise was contradicted" in m for m in r["must_not_claim"])
    assert any(x.startswith("scale: contradicted_by_evidence") for x in r["premises"])


def test_premise_schema_and_cli_explain(tmp_path):
    bad = task("q", premises=[{"statement": "x", "status": "probably"}])
    assert any("premises[0].status" in p for p in validate(bad, "input"))
    cfg = write(tmp_path, "c.json", {"providers": [mock("a", "SQLite")]})
    t = write(tmp_path, "t.json", task("q", checks=CHECK, premises=[{"statement": "single node", "status": "not_verified"}]))
    out = tmp_path / "r.json"
    assert run_cli("review", t, "--config", cfg, "--home", str(tmp_path / "h"), "-o", str(out))[0] == 0
    rc, md, _ = run_cli("explain", str(out))
    assert rc == 0 and "**Premises:**" in md and "premise1: not_verified - single node" in md
