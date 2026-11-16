"""File tools the agents may call, confined to the workspace.

There is deliberately no shell tool: agents cannot run commands. Only the Evidence checker
runs commands, and only the ones you approved in the plan.

Read-only toolset (Intake, Critic, Evaluator): ``list_files``, ``read_file``, ``search``.
Write toolset (Builder, Optimizer) adds ``write_file`` and ``replace_in_file``.
Each agent also gets one ``submit`` tool whose arguments are its structured answer.

Repeated-work guard: the same call with the same arguments, with no file change in
between, is answered from the first result and flagged; past a threshold the agent is
stopped as stuck.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from ..util import canonical_json, redact_secrets
from .budget import Budget
from .events import Reporter
from .messages import ToolCall, ToolResult, ToolSpec
from .workspace import ToolError, Workspace, _read_text

MAX_READ_LINES = 400
MAX_WRITE_BYTES = 200_000


class AgentStuck(Exception):
    pass


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


READ_TOOLS = [
    ToolSpec("list_files", "List project files (relative paths). Optional 'path' limits to a folder.",
             _obj({"path": {"type": "string", "description": "folder relative to project root; default '.'"}}, [])),
    ToolSpec("read_file", "Read a text file with line numbers. Use start_line/end_line for large files (max 400 lines per call).",
             _obj({"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"])),
    ToolSpec("search", "Find lines containing 'text' (plain text, case-sensitive) in project files. Returns up to 50 matches as path:line: text.",
             _obj({"text": {"type": "string"}, "path": {"type": "string", "description": "folder or file to limit the search"}}, ["text"])),
]
WRITE_TOOLS = [
    ToolSpec("write_file", "Create or overwrite a text file with the full new content.",
             _obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])),
    ToolSpec("replace_in_file", "Replace one exact occurrence of 'old' with 'new' in a file. 'old' must match exactly once, including whitespace.",
             _obj({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, ["path", "old", "new"])),
]


class Toolbox:
    def __init__(self, ws: Workspace, agent: str, reporter: Reporter, budget: Budget, writable: bool,
                 submit: ToolSpec, validate_submit: Callable[[dict[str, Any]], list[str]] | None = None,
                 max_repeat: int = 2):
        self.ws = ws
        self.agent = agent
        self.reporter = reporter
        self.budget = budget
        self.writable = writable
        self.submit_spec = submit
        self.validate_submit = validate_submit
        self.max_repeat = max_repeat
        self.submitted: dict[str, Any] | None = None
        self.files_written: list[str] = []
        self.repeats: list[str] = []
        self._seen: dict[str, tuple[int, str]] = {}
        self._writes = 0

    def specs(self) -> list[ToolSpec]:
        return READ_TOOLS + (WRITE_TOOLS if self.writable else []) + [self.submit_spec]

    def execute(self, call: ToolCall) -> ToolResult:
        self.budget.before_tool_call()
        key = f"{self._writes}|{call.name}|{canonical_json(call.args)}"
        if call.name not in ("write_file", "replace_in_file", self.submit_spec.name) and key in self._seen:
            n, first = self._seen[key]
            self._seen[key] = (n + 1, first)
            msg = (f"Repeated call: you already called {call.name} with these exact arguments {n} time(s) and nothing changed since. "
                   f"Earlier result follows; do not call it again.\n{first[:1500]}")
            self.repeats.append(f"{call.name} {canonical_json(call.args)[:120]}")
            self.reporter.warn(f"repeated work: {self.agent} called {call.name} with identical arguments ({n + 1}x)")
            if n + 1 > self.max_repeat + 1:
                raise AgentStuck(f"{self.agent} repeated {call.name} with identical arguments {n + 1} times")
            self.reporter.tool(self.agent, call.name, call.args, "repeated call — returned earlier result", True)
            return ToolResult(call.id, call.name, msg, is_error=True)
        try:
            content = self._dispatch(call)
            is_error = False
        except ToolError as e:
            content, is_error = f"ERROR: {e}", True
        except (TypeError, KeyError, ValueError) as e:
            content, is_error = f"ERROR: bad arguments for {call.name}: {e}", True
        content = redact_secrets(content)[0]
        if not is_error and call.name not in ("write_file", "replace_in_file", self.submit_spec.name):
            self._seen[key] = (1, content)
        first_line = content.splitlines()[0] if content else ""
        self.reporter.tool(self.agent, call.name, call.args, first_line, is_error)
        return ToolResult(call.id, call.name, content, is_error=is_error)

    # -- tools ------------------------------------------------------------------------
    def _dispatch(self, call: ToolCall) -> str:
        a = call.args or {}
        n = call.name
        if n == self.submit_spec.name:
            problems = self.validate_submit(a) if self.validate_submit else []
            if problems:
                raise ToolError("submission rejected; fix these and call it again:\n- " + "\n- ".join(problems[:15]))
            self.submitted = a
            return "submitted"
        if n == "list_files":
            base = a.get("path") or "."
            files = self.ws.files()
            if base not in (".", ""):
                self.ws.resolve(base)
                prefix = base.rstrip("/") + "/"
                files = [f for f in files if f.startswith(prefix) or f == base]
            shown = files[:300]
            more = f"\n... and {len(files) - 300} more" if len(files) > 300 else ""
            return f"{len(files)} files\n" + "\n".join(shown) + more
        if n == "read_file":
            p = self.ws.resolve(a["path"])
            if not p.is_file():
                raise ToolError(f"no such file: {a['path']}")
            text = _read_text(p)
            if text is None:
                raise ToolError(f"{a['path']} is binary or not UTF-8")
            lines = text.splitlines()
            start = max(1, int(a.get("start_line") or 1))
            end = min(len(lines), int(a.get("end_line") or start + MAX_READ_LINES - 1), start + MAX_READ_LINES - 1)
            body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1))
            tail = f"\n[lines {start}-{end} of {len(lines)}]" if (start > 1 or end < len(lines)) else ""
            return f"{a['path']} ({len(lines)} lines)\n{body}{tail}"
        if n == "search":
            needle = a["text"]
            if not needle:
                raise ToolError("text must not be empty")
            scope = a.get("path") or "."
            files = self.ws.files()
            if scope not in (".", ""):
                self.ws.resolve(scope)
                files = [f for f in files if f == scope or f.startswith(scope.rstrip("/") + "/")]
            hits = []
            for rel in files:
                t = _read_text(self.ws.root / rel)
                if t is None:
                    continue
                for i, line in enumerate(t.splitlines(), 1):
                    if needle in line:
                        hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(hits) >= 50:
                            break
                if len(hits) >= 50:
                    break
            return f"{len(hits)} matches\n" + "\n".join(hits)
        if n in ("write_file", "replace_in_file"):
            if not self.writable:
                raise ToolError(f"{self.agent} has read-only access")
            p = self.ws.resolve(a["path"], write=True)
            if n == "write_file":
                content = a["content"]
                if len(content.encode()) > MAX_WRITE_BYTES:
                    raise ToolError("file too large (limit 200 KB)")
                existed = p.exists()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                msg = f"{'overwrote' if existed else 'created'} {a['path']} ({len(content.splitlines())} lines)"
            else:
                if not p.is_file():
                    raise ToolError(f"no such file: {a['path']}")
                text = _read_text(p)
                if text is None:
                    raise ToolError(f"{a['path']} is binary or not UTF-8")
                old = a["old"]
                count = text.count(old) if old else 0
                if count == 0:
                    raise ToolError(f"'old' text not found in {a['path']}; read the file again and copy the exact text, including indentation")
                if count > 1:
                    raise ToolError(f"'old' text occurs {count} times in {a['path']}; include more surrounding lines so it matches exactly once")
                p.write_text(text.replace(old, a["new"], 1), encoding="utf-8")
                msg = f"replaced 1 occurrence in {a['path']}"
            self._writes += 1
            if a["path"] not in self.files_written:
                self.files_written.append(a["path"])
            return msg
        raise ToolError(f"unknown tool '{n}'. Available: {', '.join(t.name for t in self.specs())}")


def as_json_block(obj: Any, limit: int = 20000) -> str:
    s = json.dumps(obj, indent=1, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "\n...[truncated]"


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")
