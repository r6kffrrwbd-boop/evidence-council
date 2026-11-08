"""Privacy routing: minimum data per provider; private data never reaches cloud providers without operator config."""

import json

from conftest import mock, run_cli, task, write
from evidence_council.council import run_review

PHONE = "+1 (555) 201-3344"
AUDIO_SECRET = "RAW-AUDIO-BYTES-SHOULD-NEVER-TRAVEL"


def coaching_task(level="private", **kw):
    return task(
        "Review the draft coaching note against the observations.",
        level=level,
        domain="coaching",
        evidence=[
            {"id": "rec1", "kind": "recording", "uri": "file:///calls/2026-11-01.wav", "content": AUDIO_SECRET, "source_ref": "call-1"},
            {"id": "tr1", "kind": "transcript", "content": "Client: call me back on " + PHONE, "source_ref": "call-1", "timestamp": "00:01:10"},
            {"id": "obs1", "kind": "observation", "content": "Rep interrupted the client twice; client phone " + PHONE,
             "timestamp": "00:03:12.5", "source_ref": "call-1#t=192.5"},
        ],
        **kw,
    )


def _prompts(rec):
    return [json.loads(l) for l in rec.read_text().splitlines()] if rec.exists() else []


def test_private_default_excludes_cloud(make_config, tmp_path):
    rec = tmp_path / "p.jsonl"
    cfg, _ = make_config([mock("local-m", "ok", record_prompts_to=str(rec)), mock("cloud-m", "ok", locality="cloud", record_prompts_to=str(rec))])
    res = run_review(coaching_task(), cfg)
    assert res["providers"]["used"] == ["local-m"]
    ex = next(e for e in res["providers"]["excluded"] if e["provider"] == "cloud-m")
    assert "privacy.level=private" in ex["reason"] and "--allow-private-to" in ex["reason"]
    assert {p["provider"] for p in _prompts(rec)} == {"local-m"}


def test_audio_never_forwarded_even_locally(make_config, tmp_path):
    rec = tmp_path / "p.jsonl"
    cfg, _ = make_config([mock("local-m", "ok", record_prompts_to=str(rec))])
    res = run_review(coaching_task(), cfg)
    text = "\n".join(p["prompt"] for p in _prompts(rec))
    assert AUDIO_SECRET not in text and "2026-11-01.wav" not in text
    assert 'id="rec1"' in text and 'withheld="true"' in text      # reference stays citable
    route = res["privacy"]["routing"][0]
    assert any(e["id"] == "rec1" for e in route["excluded_evidence"])
    assert PHONE in text                                           # local provider: data stays on machine


def test_restricted_cannot_be_overridden(make_config, tmp_path):
    cfg, _ = make_config([mock("cloud-m", "ok", locality="cloud"), mock("local-m", "ok")], privacy={"allow_private_to": ["cloud-m"]})
    cfg.extra_allow_private_to = ["cloud-m"]
    res = run_review(coaching_task(level="restricted"), cfg)
    assert res["providers"]["used"] == ["local-m"]


def test_local_only_flag(make_config):
    cfg, _ = make_config([mock("cloud-m", "ok", locality="cloud"), mock("local-m", "ok")])
    res = run_review(task("public q", level="public"), cfg, local_only=True)
    assert res["providers"]["used"] == ["local-m"] and res["privacy"]["local_only"] is True


def test_operator_allowlist_permits_but_still_minimises(make_config, tmp_path):
    rec = tmp_path / "p.jsonl"
    cfg, _ = make_config([mock("cloud-m", "ok", locality="cloud", record_prompts_to=str(rec))])
    cfg.extra_allow_private_to = ["cloud-m"]                      # == --allow-private-to cloud-m
    res = run_review(coaching_task(), cfg)
    assert res["providers"]["used"] == ["cloud-m"]
    text = rec.read_text()
    assert PHONE not in text and "[PHONE_1]" in text                # still redacted (redact defaults true)
    assert AUDIO_SECRET not in text
    route = res["privacy"]["routing"][0]
    assert route["redactions"]["phone"] >= 1


def test_public_level_cloud_gets_redacted_and_no_transcript(make_config, tmp_path):
    rec = tmp_path / "p.jsonl"
    cfg, _ = make_config([mock("cloud-m", "ok", locality="cloud", record_prompts_to=str(rec))])
    res = run_review(coaching_task(level="public"), cfg)
    text = rec.read_text()
    assert PHONE not in text and "Client: call me back" not in text   # transcript withheld from cloud
    route = res["privacy"]["routing"][0]
    assert {e["id"] for e in route["excluded_evidence"]} == {"rec1", "tr1"}


def test_secrets_redacted_for_every_provider(make_config, tmp_path):
    rec = tmp_path / "p.jsonl"
    cfg, _ = make_config([mock("local-m", "ok", record_prompts_to=str(rec))])
    run_review(task("Why does this fail? token=ghp_abcdefghijklmnopqrstuvwxyz0123", level="public"), cfg)
    text = rec.read_text()
    assert "ghp_abcdefghijklmnop" not in text and "[REDACTED:github_token]" in text


def test_privacy_dry_run_cli(make_config, tmp_path):
    _, cpath = make_config([mock("local-m", "ok"), mock("cloud-m", "ok", locality="cloud")])
    tpath = write(tmp_path, "t.json", coaching_task(level="internal"))
    rc, out, _ = run_cli("privacy", tpath, "--config", str(cpath))
    assert rc == 0
    plan = json.loads(out)
    assert plan["findings"]["pii_counts"]["phone"] >= 2
    allowed = {r["provider"]: r["allowed"] for r in plan["routing"]}
    assert allowed == {"local-m": True, "cloud-m": True}
    rc, out, _ = run_cli("privacy", tpath, "--config", str(cpath), "--show-payload", "cloud-m")
    payload = json.loads(out)
    blob = json.dumps(payload)
    assert PHONE not in blob and AUDIO_SECRET not in blob and "tr1" not in [e["id"] for e in payload["evidence"]]
    rc, _, err = run_cli("privacy", write(tmp_path, "p.json", coaching_task()), "--config", str(cpath), "--show-payload", "cloud-m")
    assert rc == 2 and "would receive nothing" in err


def test_privacy_blocks_everything_exit_code(make_config, tmp_path):
    _, cpath = make_config([mock("cloud-m", "ok", locality="cloud")])
    rc, _, err = run_cli("review", write(tmp_path, "t.json", coaching_task()), "--config", str(cpath), "-o", str(tmp_path / "r.json"))
    assert rc == 4 and "--allow-private-to" in err
