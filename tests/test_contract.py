"""JSON contract validation (input, output, lesson)."""

import json
from pathlib import Path

import pytest

from conftest import ROOT, run_cli, task, write
from evidence_council.contract import validate, with_defaults
from evidence_council.errors import CouncilError

EXAMPLES = ROOT / "examples"


@pytest.mark.parametrize("path", sorted((EXAMPLES / "inputs").glob("*.json")), ids=lambda p: p.name)
def test_example_inputs_are_valid(path):
    assert validate(json.loads(path.read_text()), "input") == []


@pytest.mark.parametrize("path", sorted((EXAMPLES / "outputs").glob("*.json")), ids=lambda p: p.name)
def test_example_outputs_are_valid(path):
    doc = json.loads(path.read_text())
    assert validate(doc, "output") == []
    if doc.get("candidate_lesson"):
        assert validate(doc["candidate_lesson"], "lesson") == []


def test_minimal_input_valid_and_defaults():
    doc = {"schema_version": "1.0", "task": {"prompt": "hi"}}
    assert validate(doc, "input") == []
    d = with_defaults(doc)
    assert d["privacy"]["level"] == "private"          # minimum-data default
    assert d["options"]["allow_code_execution"] is False
    assert d["task"]["kind"] == "objective"


@pytest.mark.parametrize("bad,needle", [
    ({"schema_version": "1.0", "task": {}}, "'prompt' is a required property"),
    ({"schema_version": "2.0", "task": {"prompt": "x"}}, "schema_version"),
    ({"schema_version": "1.0", "task": {"prompt": "x"}, "privacy": {"level": "secret"}}, "privacy.level"),
    ({"schema_version": "1.0", "task": {"prompt": "x"}, "checks": [{"id": "t", "type": "python_tests"}]}, "'test_code' is a required property"),
    ({"schema_version": "1.0", "task": {"prompt": "x"}, "privacy": {"allow_private_to": ["codex"]}}, "Additional properties"),
    ({"schema_version": "1.0", "task": {"prompt": "x"}, "evidence": [{"id": "bad id!", "kind": "log"}]}, "evidence[0].id"),
])
def test_invalid_inputs_report_paths(bad, needle):
    probs = validate(bad, "input")
    assert probs and any(needle in p for p in probs), probs


def test_task_input_cannot_unlock_cloud_forwarding():
    # privacy overrides are operator config only; the input schema rejects them
    doc = task(privacy={"allow_cloud": True})
    assert validate(doc, "input")


def test_duplicate_evidence_ids_rejected():
    doc = task(evidence=[{"id": "e1", "kind": "log"}, {"id": "e1", "kind": "log"}])
    with pytest.raises(CouncilError) as e:
        with_defaults(doc)
    assert "duplicate evidence ids" in e.value.message and e.value.hint


def test_validate_cli_reports_json_and_exit_code(tmp_path):
    rc, out, err = run_cli("validate", write(tmp_path, "bad.json", {"schema_version": "1.0", "task": {}}))
    assert rc == 2
    res = json.loads(out)
    assert res["valid"] is False and res["kind"] == "input" and res["problems"]
    assert "hint:" in err
    rc, out, _ = run_cli("validate", str(EXAMPLES / "inputs" / "numeric-overturn.json"))
    assert rc == 0 and json.loads(out)["valid"] is True


def test_invalid_json_syntax_gives_line_and_hint(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text('{"schema_version": "1.0",}')
    rc, _, err = run_cli("review", str(p))
    assert rc == 2
    assert "not valid JSON" in err and "line 1" in err and "hint:" in err


def test_schema_cli_prints_each_schema():
    for kind in ("input", "output", "lesson"):
        rc, out, _ = run_cli("schema", kind)
        assert rc == 0 and json.loads(out)["$schema"].startswith("https://json-schema.org")
