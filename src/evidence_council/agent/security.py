"""Security checks on the change and on the running local app. No model involved.

Diff scan (added lines only): hard-coded credentials, dangerous calls (``eval``, ``exec``,
``pickle.loads``, ``yaml.load`` without a safe loader, ``shell=True``, ``os.system``,
disabled TLS verification), SQL built with string formatting, unsafe HTML sinks
(``innerHTML``, ``document.write``, ``dangerouslySetInnerHTML``), weak hashes, dependency
changes, and edits to existing tests (which can hide a failure rather than fix it).

Service probe: requests a page that does not exist and a malformed query, and flags
responses that leak a stack trace or return a 5xx error.

Severity ``high`` sends the work back to the Builder; ``medium`` goes to the Critic as
evidence; ``info`` is reported only. This is a heuristic scan, not a security audit.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from typing import Any

from ..util import SECRET_PATTERNS

RULES: list[tuple[str, str, re.Pattern[str], str]] = [
    ("high", "hardcoded-credential", re.compile(r"""(?i)\b(api[_-]?key|secret|password|passwd|token|auth)\w*\s*[:=]\s*['"][^'"\s]{8,}['"]"""),
     "a credential-looking value is written into source code; read it from an environment variable instead"),
    ("high", "eval-exec", re.compile(r"(?<![\w.])(eval|exec)\s*\("), "eval/exec runs arbitrary code"),
    ("high", "pickle-load", re.compile(r"\bpickle\.loads?\s*\("), "unpickling untrusted data can execute code"),
    ("high", "yaml-unsafe", re.compile(r"\byaml\.load\s*\((?![^)]*SafeLoader)"), "yaml.load without SafeLoader can construct arbitrary objects"),
    ("high", "shell-true", re.compile(r"shell\s*=\s*True"), "shell=True with dynamic input allows command injection"),
    ("high", "os-system", re.compile(r"\bos\.(system|popen)\s*\("), "os.system/popen pass a string to the shell"),
    ("high", "tls-verify-off", re.compile(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED"), "TLS certificate verification disabled"),
    ("high", "sql-format", re.compile(r"""(?i)\.(execute|executemany|raw|query)\s*\(\s*(f['"]|['"][^'"]*['"]\s*(%|\.format|\+))"""),
     "SQL built with string formatting; use parameters"),
    ("medium", "html-sink", re.compile(r"\.innerHTML\s*=|document\.write\s*\(|dangerouslySetInnerHTML|\|\s*safe\b|mark_safe\s*\("),
     "inserting unescaped HTML can allow cross-site scripting"),
    ("medium", "weak-hash", re.compile(r"\b(md5|sha1)\s*\(", re.I), "MD5/SHA-1 are unsuitable for passwords or signatures"),
    ("medium", "debug-on", re.compile(r"(?i)\bdebug\s*=\s*True\b|app\.run\([^)]*debug\s*=\s*True"), "debug mode exposes internals"),
    ("medium", "bind-all", re.compile(r"""['"]0\.0\.0\.0['"]"""), "binding to all interfaces exposes the app on the network"),
]
DEP_FILES = re.compile(r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|setup\.py|setup\.cfg|Pipfile(\.lock)?|poetry\.lock|package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|go\.mod|Cargo\.toml|Gemfile(\.lock)?)$")
TEST_FILES = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.(py|go|js|ts)$|[^/]*\.(spec|test)\.(js|ts|jsx|tsx)$)")


def scan_diff(patch: str, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    cur, line_no = None, 0
    for raw in patch.splitlines():
        if raw.startswith("+++ "):
            cur = raw[6:] if raw.startswith("+++ b/") else None
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line_no = int(m.group(1)) - 1 if m else 0
            continue
        if raw.startswith("-") or cur is None:
            continue
        line_no += 1
        if not raw.startswith("+"):
            continue
        text = raw[1:]
        for kind, pat in SECRET_PATTERNS:
            if pat.search(text):
                findings.append({"severity": "high", "rule": f"secret:{kind}", "file": cur, "line": line_no,
                                 "why": "a credential pattern appears in the change", "text": "[redacted]"})
        for sev, rule, pat, why in RULES:
            if pat.search(text):
                findings.append({"severity": sev, "rule": rule, "file": cur, "line": line_no, "why": why, "text": text.strip()[:160]})
    for ch in changes:
        if DEP_FILES.search(ch["path"]):
            findings.append({"severity": "medium", "rule": "dependency-change", "file": ch["path"], "line": None,
                             "why": "dependency files changed; new packages need your review (the agent cannot install them)", "text": ch["status"]})
        if TEST_FILES.search(ch["path"]) and ch["status"] in ("modified", "deleted") and ch.get("removed", 0) > 0:
            findings.append({"severity": "medium", "rule": "existing-test-changed", "file": ch["path"], "line": None,
                             "why": f"an existing test file was {ch['status']} ({ch.get('removed', 0)} lines removed); check that no assertion was weakened",
                             "text": ch["status"]})
    return findings


def probe_service(base_url: str) -> list[dict[str, Any]]:
    findings = []
    probes = [("/ec-agent-probe-does-not-exist", "missing page"), ("/?ec_probe=%27%22%3C%3E%00", "malformed query")]
    for path, label in probes:
        try:
            with urllib.request.urlopen(base_url + path, timeout=5) as r:
                status, body = r.status, r.read(20000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, body = e.code, (e.read(20000) or b"").decode("utf-8", "replace") if e.fp else ""
        except Exception as e:  # hanging or refused
            findings.append({"severity": "medium", "rule": "probe-no-response", "file": None, "line": None,
                             "why": f"{label} request got no response: {type(e).__name__}", "text": path})
            continue
        if re.search(r"Traceback \(most recent call last\)|at [\w.$]+\([\w.]+:\d+\)|Exception in thread|stack trace", body):
            findings.append({"severity": "high", "rule": "stack-trace-leak", "file": None, "line": None,
                             "why": f"{label} response shows a stack trace to the visitor", "text": f"HTTP {status} {path}"})
        if status >= 500:
            findings.append({"severity": "medium", "rule": "server-error", "file": None, "line": None,
                             "why": f"{label} caused HTTP {status}", "text": path})
    return findings


def summarise(findings: list[dict[str, Any]]) -> dict[str, int]:
    out = {"high": 0, "medium": 0, "info": 0}
    for f in findings:
        out[f["severity"]] = out.get(f["severity"], 0) + 1
    return out
