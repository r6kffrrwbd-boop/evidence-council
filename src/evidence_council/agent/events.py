"""Readable live progress plus an append-only event log.

Shown to the user: phase changes, each tool call (name + short arguments) and its outcome,
what each agent *says* in its visible reply (truncated), test results, security findings,
and the reason a task goes back for another pass. Never shown: provider reasoning state
(signatures, encrypted reasoning) — the agent does not request or display chain-of-thought.

Every line and every logged event passes through secret redaction first.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from ..util import now_iso, redact_secrets


def _short(v: Any, n: int = 60) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = s.replace("\n", "⏎")
    return s if len(s) <= n else s[: n - 1] + "…"


class Reporter:
    def __init__(self, log_path: Path | None = None, stream: TextIO | None = None, color: bool | None = None):
        self.log_path = log_path
        self.stream = stream or sys.stdout
        tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        self.color = tty and not os.environ.get("NO_COLOR") if color is None else color
        self.lines: list[str] = []

    # -- low level -------------------------------------------------------
    def _c(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.color else s

    def emit(self, line: str) -> None:
        line = redact_secrets(line)[0]
        self.lines.append(line)
        print(line, file=self.stream, flush=True)

    def event(self, _event: str, **data: Any) -> None:
        if not self.log_path:
            return
        rec = {"at": now_iso(), "event": _event, **data}
        text = redact_secrets(json.dumps(rec, ensure_ascii=False, default=str))[0]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(text + "\n")

    # -- semantic helpers ------------------------------------------------
    def phase(self, title: str, detail: str = "") -> None:
        self.emit("")
        self.emit(self._c("1;36", f"━━ {title} ━━") + (f"  {detail}" if detail else ""))
        self.event("phase", title=title, detail=detail)

    def info(self, msg: str) -> None:
        self.emit(f"  {msg}")
        self.event("info", msg=msg)

    def warn(self, msg: str) -> None:
        self.emit(self._c("33", f"  ! {msg}"))
        self.event("warning", msg=msg)

    def says(self, agent: str, text: str, usage: dict[str, Any]) -> None:
        if text:
            self.emit(f"  {agent} says: {_short(text, 300)}")
        self.event("model_turn", agent=agent, visible_text=text[:2000], **usage)

    def tool(self, agent: str, name: str, args: dict[str, Any], outcome: str, is_error: bool) -> None:
        shown = ", ".join(f"{k}={_short(v, 40)}" for k, v in args.items() if k not in ("content", "new", "old"))
        if "content" in args:
            shown += f", content=<{len(str(args['content']))} chars>"
        if "old" in args:
            shown += f", old=<{len(str(args['old']))} chars>, new=<{len(str(args.get('new', '')))} chars>"
        mark = self._c("31", "✗") if is_error else "▸"
        self.emit(f"  {mark} {agent} → {name}({shown}) — {_short(outcome, 110)}")
        self.event("tool_call", agent=agent, tool=name, args={k: (v if k not in ("content", "new", "old") else f"<{len(str(v))} chars>") for k, v in args.items()},
                   outcome=outcome[:500], is_error=is_error)

    def test(self, r: dict[str, Any]) -> None:
        st = r["status"]
        mark = {"passed": self._c("32", "✓"), "failed": self._c("31", "✗"), "hung": self._c("31", "⏱"), "error": self._c("31", "✗"),
                "not_run": "–"}.get(st, "?")
        extra = ""
        if r.get("tests", {}).get("ran") is not None:
            t = r["tests"]
            extra = f", {t['ran']} tests, {t.get('failed', 0)} failing"
        exit_s = f"exit {r['exit_code']}" if r.get("exit_code") is not None else st
        self.emit(f"  {mark} {r['kind']} {r['id']}: {st} ({exit_s}, {r.get('duration_s', 0):.2f}s{extra})")
        if st in ("failed", "hung", "error") and r.get("failure_excerpt"):
            for ln in r["failure_excerpt"].splitlines()[:6]:
                self.emit(f"      {ln}")
        self.event("check_result", **{k: v for k, v in r.items() if k not in ("stdout_tail", "stderr_tail")})

    def returned(self, to: str, reasons: list[str]) -> None:
        self.emit(self._c("35", f"  ↺ Returned to {to} for another pass because:"))
        for r in reasons:
            self.emit(f"     - {_short(r, 220)}")
        self.event("returned", to=to, reasons=reasons)

    def spend(self, summary: dict[str, Any]) -> None:
        self.emit(self._c("2", f"  $ spent ${summary['spent_usd']:.4f} of ${summary['max_cost_usd']:.2f} · "
                               f"{summary['model_calls']}/{summary['max_model_calls']} model calls · "
                               f"{summary['tool_calls']} tool calls · {summary['elapsed_s']:.0f}s of {summary['max_wall_s']}s"))
        self.event("budget", **summary)
