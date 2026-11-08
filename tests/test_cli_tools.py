"""Claude-facing contract: every tool is invokable, documented, and its results are interpreted correctly.

These tests exercise the tools exactly as SKILL.md tells Claude to: as shell commands
with JSON in/out, then `explain --json` to interpret the result.
"""

import json
import re

import pytest

from conftest import ROOT, mock, run_cli, task, write

TOOLS = ["review", "propose", "compare", "verify", "verdict", "explain", "providers", "privacy", "validate", "schema", "lessons", "jobs", "selftest", "codes"]
SKILL = (ROOT / "skill" / "SKILL.md").read_text()


@pytest.mark.parametrize("tool", TOOLS)
def test_every_tool_has_help_with_examples(tool):
    rc, out, _ = run_cli(tool, "--help")
    assert rc == 0
    if tool != "codes":
        assert "example" in out.lower(), f"{tool} --help lacks examples"


def test_top_level_help_lists_tools():
    rc, out, _ = run_cli("--help")
    assert rc == 0
    for t in TOOLS:
        assert t in out


def test_skill_md_documents_every_tool_and_reference():
    for t in TOOLS:
        if t != "codes":
            assert f"evidence-council {t}" in SKILL, f"SKILL.md does not explain when to call `{t}`"
    for ref in re.findall(r"\]\((references/[^)]+)\)", SKILL):
        assert (ROOT / "skill" / ref).exists() or (ROOT / "docs" / ref.split("/", 1)[1]).exists(), ref


def test_skill_md_commands_parse():
    """Every `evidence-council <tool>` line in SKILL.md code blocks uses a real tool and valid flags."""
    from evidence_council.cli import build_parser
    ap = build_parser()
    blocks = re.findall(r"```bash\n(.*?)```", SKILL, re.S)
    cmds = [l.strip() for b in blocks for l in b.splitlines() if l.strip().startswith("evidence-council ")]
    assert len(cmds) >= 8
    for c in cmds:
        argv = c.split()[1:]
        argv = [a for a in argv if not a.startswith(("&&", "|"))]
        if "&&" in c or "|" in c:
            argv = c.split("&&")[0].split("|")[0].split()[1:]
        try:
            ap.parse_args(argv)
        except SystemExit as e:
            if e.code != 0:
                pytest.fail(f"SKILL.md command does not parse: {c}")


def _review(tmp_path, providers, t, *extra):
    cfg = write(tmp_path, "cfg.json", {"providers": providers, "defaults": {"timeout_s": 5, "retries": 0}})
    out = tmp_path / "result.json"
    rc, _, err = run_cli("review", write(tmp_path, "task.json", t), "--config", cfg, "--home", str(tmp_path / "h"), "-o", str(out), *extra)
    return rc, out, err


def test_interpret_supported_result(tmp_path):
    rc, out, _ = _review(tmp_path, [mock("m1", "9"), mock("m2", "8")],
                         task("3*3?", fmt="number", checks=[{"id": "ref", "type": "numeric_reference", "expression": "3*3"}]))
    assert rc == 0
    rc, o, _ = run_cli("explain", str(out), "--json")
    r = json.loads(o)
    assert r["status"] == "supported" and r["answer"] == "9" and r["can_act_on_answer"] is True
    assert r["participants"] == ["m1 (MOCK)", "m2 (MOCK)"]
    assert r["checks_run"] == ["ref (numeric_reference)"]
    assert any("real multi-model run" in m for m in r["must_not_claim"])
    rc, md, _ = run_cli("explain", str(out))
    assert "## Council verdict: supported" in md and "**Answer:** 9" in md and "Checks actually run" in md


def test_interpret_insufficient_result(tmp_path):
    rc, out, _ = _review(tmp_path, [mock("m1", "yes"), mock("m2", "yes")], task("Is it good?"))
    r = json.loads(run_cli("explain", str(out), "--json")[1])
    assert r["status"] == "insufficient_evidence" and r["answer"] is None and r["can_act_on_answer"] is False
    assert r["checks_run"] == []


def test_staged_pipeline_matches_review(tmp_path):
    cfg = write(tmp_path, "cfg.json", {"providers": [mock("m1", "9"), mock("m2", "8")], "defaults": {"timeout_s": 5}})
    t = write(tmp_path, "t.json", task("3*3?", fmt="number", checks=[{"id": "ref", "type": "numeric_reference", "expression": "3*3"}]))
    s = str(tmp_path / "s.json")
    assert run_cli("propose", t, "--config", cfg, "-o", s)[0] == 0
    st = json.loads(open(s).read())
    assert st["stages_completed"] == ["input", "proposals"]
    for stage in ("compare", "verify", "verdict"):
        rc, _, err = run_cli(stage, s, "--config", cfg, "-o", s)
        assert rc == 0, err
    st = json.loads(open(s).read())
    assert st["stages_completed"] == ["input", "proposals", "comparison", "verification", "verdict", "learning"]
    assert st["verdict"]["selected_answer"] == "9"


def test_stage_order_error_is_actionable(tmp_path):
    cfg = write(tmp_path, "cfg.json", {"providers": [mock("m1", "9")]})
    s = str(tmp_path / "s.json")
    run_cli("propose", write(tmp_path, "t.json", task("q")), "--config", cfg, "-o", s)
    rc, _, err = run_cli("verdict", s, "--config", cfg)
    assert rc == 7 and "'verification' stage" in err and "evidence-council verify" in err and "hint:" in err
    rc, _, err = run_cli("verify", write(tmp_path, "t2.json", task("q")), "--config", cfg)
    assert rc == 7 and "propose" in err


def test_json_error_format(tmp_path):
    rc, _, err = run_cli("review", str(tmp_path / "missing.json"), "--error-format", "json")
    e = json.loads(err)
    assert rc == 2 and e["exit_code"] == 2 and e["hint"]


def test_recursion_guard(tmp_path):
    rc, _, err = run_cli("review", write(tmp_path, "t.json", task("q")), env_extra={"EVIDENCE_COUNCIL_ACTIVE": "1"})
    assert rc == 5 and "recursive" in err.lower() or "inside a council" in err


def test_providers_tool_json_without_probe(tmp_path):
    cfg = write(tmp_path, "cfg.json", {"providers": [mock("m1", "1"), mock("m2", "1", behavior="unavailable")]})
    rc, out, _ = run_cli("providers", "--json", "--config", cfg)
    d = json.loads(out)
    assert rc == 0 and d["probed"] is False
    assert {p["name"]: p["available"] for p in d["providers"]} == {"m1": True, "m2": False}


def test_selftest_tool():
    rc, out, _ = run_cli("selftest")
    assert rc == 0 and "selftest: ok" in out and "FAIL" not in out


def test_codes_tool():
    rc, out, _ = run_cli("codes")
    codes = json.loads(out)
    assert set(codes) >= {"0", "2", "3", "4", "5", "6", "7", "8"}
