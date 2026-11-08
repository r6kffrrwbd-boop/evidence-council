"""Small shared helpers: hashing, time, JSON I/O, secret redaction for logs."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from .errors import CouncilError, EXIT_USAGE

# Patterns for credentials that must never be logged or forwarded. Conservative on purpose.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}")),
    ("openai_key", re.compile(r"sk-(?:proj-|or-v1-|or-)?[A-Za-z0-9_\-]{16,}")),
    ("xai_key", re.compile(r"xai-[A-Za-z0-9]{16,}")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
]


def redact_secrets(text: str) -> tuple[str, dict[str, int]]:
    """Replace credential-looking substrings with ``[REDACTED:<kind>]``. Returns (text, counts)."""
    counts: dict[str, int] = {}
    for kind, pat in SECRET_PATTERNS:
        text, n = pat.subn(f"[REDACTED:{kind}]", text)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return text, counts


def safe_log(msg: str) -> None:
    """Write a diagnostic line to stderr with secrets redacted."""
    clean, _ = redact_secrets(msg)
    print(f"[evidence-council] {clean}", file=sys.stderr, flush=True)


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_run_id() -> str:
    return "run-" + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_digest(obj: Any) -> str:
    data = obj if isinstance(obj, (bytes, str)) else canonical_json(obj)
    if isinstance(data, str):
        data = data.encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_json(path: str | os.PathLike[str]) -> Any:
    """Read JSON from a path (``-`` = stdin) with actionable errors."""
    if str(path) == "-":
        raw = sys.stdin.read()
        where = "stdin"
    else:
        p = Path(path)
        if not p.exists():
            raise CouncilError(f"file not found: {p}", hint="Check the path, or pass '-' to read JSON from stdin.", exit_code=EXIT_USAGE)
        if p.is_dir():
            raise CouncilError(f"expected a JSON file but got a directory: {p}", exit_code=EXIT_USAGE)
        raw = p.read_text(encoding="utf-8")
        where = str(p)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise CouncilError(
            f"{where} is not valid JSON: {e.msg} at line {e.lineno} column {e.colno}",
            hint="Fix the JSON syntax (a trailing comma or unquoted key is the usual cause). Validate with: evidence-council validate FILE",
            exit_code=EXIT_USAGE,
        ) from None


def write_json(obj: Any, path: str | os.PathLike[str] | None) -> None:
    """Write JSON to ``path`` atomically, or stdout when path is None/'-'."""
    text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    if path is None or str(path) == "-":
        sys.stdout.write(text)
        return
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 40)] + f"\n...[truncated {len(text) - limit + 40} chars]"


CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_untrusted(text: Any, limit: int) -> str:
    """Normalise untrusted provider text: str, no control chars, bounded length."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False) if isinstance(text, (dict, list)) else str(text)
    text = CONTROL_CHARS.sub("", text)
    text, _ = redact_secrets(text)
    return truncate(text, limit)
