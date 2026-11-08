"""A failing, slow, malformed or missing provider never fails the council."""

import json
import threading
import time

from conftest import mock, run_cli, task, write
from evidence_council.council import run_review
from evidence_council.providers import CancelToken


def test_mixed_failures_isolated(make_config):
    cfg, _ = make_config([
        mock("ok", "7"),
        mock("slow", "7", behavior="timeout", timeout_s=1),
        mock("broken", "7", behavior="error"),
        mock("gone", "7", behavior="unavailable"),
        mock("flaky", "7", behavior="transient_then_ok", retries=1),
        mock("rambling", behavior="malformed", response_text="Honestly I think it's 8, roughly."),
    ])
    t0 = time.monotonic()
    res = run_review(task("n?", fmt="number", checks=[{"id": "ref", "type": "numeric_reference", "expression": "3+4"}]), cfg)
    assert time.monotonic() - t0 < 30
    st = {a["provider"]: a for a in res["providers"]["attempted"]}
    assert st["slow"]["status"] == "timeout"
    assert st["broken"]["status"] == "error" and "simulated" in st["broken"]["error"]
    assert st["gone"]["status"] == "unavailable"
    assert st["flaky"]["status"] == "ok" and st["flaky"]["attempts"] == 2      # retried once
    assert st["rambling"]["status"] == "ok"
    # only adapters that ran successfully are participants
    assert sorted(res["providers"]["used"]) == ["flaky", "ok", "rambling"]
    ramble = next(p for p in res["proposals"] if p["origin"] == "rambling")
    assert ramble["parse"] == "text_fallback"
    assert res["verdict"]["status"] == "supported" and res["verdict"]["selected_answer"] == "7"


def test_all_providers_fail_writes_result_and_exits_3(make_config, tmp_path):
    _, cpath = make_config([mock("x", behavior="error"), mock("y", behavior="unavailable")])
    out = tmp_path / "r.json"
    rc, _, err = run_cli("review", write(tmp_path, "t.json", task("q")), "--config", str(cpath), "-o", str(out))
    assert rc == 3
    assert "hint:" in err
    res = json.loads(out.read_text())
    assert res["verdict"]["status"] == "no_proposals" and res["providers"]["used"] == []


def test_cancellation_stops_in_flight_calls(make_config):
    cfg, _ = make_config([mock("slow1", "1", delay_s=20, timeout_s=60), mock("slow2", "1", delay_s=20, timeout_s=60)])
    tok = CancelToken()
    threading.Timer(0.5, tok.set).start()
    t0 = time.monotonic()
    res = run_review(task("q"), cfg, cancel=tok)
    assert time.monotonic() - t0 < 10
    assert {a["status"] for a in res["providers"]["attempted"]} == {"cancelled"}
    assert any("cancelled" in w for w in res["warnings"])


def test_deadline_bounds_total_time(make_config):
    cfg, _ = make_config([mock("slow", "1", delay_s=30, timeout_s=60)], defaults={"deadline_s": 2})
    t0 = time.monotonic()
    res = run_review(task("q"), cfg)
    assert time.monotonic() - t0 < 10
    assert res["providers"]["attempted"][0]["status"] == "timeout"


def test_unknown_requested_provider_is_reported_not_fatal(make_config):
    cfg, _ = make_config([mock("a", "1")])
    res = run_review(task("q"), cfg, requested=["a", "does-not-exist"])
    assert res["providers"]["used"] == ["a"]
    assert any(e["provider"] == "does-not-exist" for e in res["providers"]["excluded"])


def test_secrets_in_provider_errors_are_redacted(make_config):
    from evidence_council.providers.base import Adapter, ProviderError, Availability

    class Leaky(Adapter):
        adapter_type = "leaky"
        def check(self):
            return Availability(True, "")
        def _call(self, prompt, timeout_s, cancel):
            raise ProviderError("auth failed for key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX")

    r = Leaky({"name": "leaky"}, {}).generate("hi")
    assert "ABCDEFGHIJKLMNOP" not in r.error and "[REDACTED:openai_key]" in r.error
