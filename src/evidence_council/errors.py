"""Error type and exit codes shared by every tool.

Every user-facing failure is a :class:`CouncilError` carrying a one-line message,
an actionable ``hint`` and a stable exit code, so callers (humans, Claude, other
programs) can branch on the code and show the hint verbatim.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2          # bad arguments, unreadable file, invalid JSON / schema
EXIT_NO_PROVIDERS = 3   # fewer usable participants than required
EXIT_PRIVACY = 4        # privacy policy blocked the request
EXIT_RECURSION = 5      # called from inside a council run
EXIT_LESSON_REJECTED = 6  # candidate lesson failed acceptance checks
EXIT_STAGE = 7          # stage tool given a state missing a prerequisite stage
EXIT_PENDING = 8        # background job still running

EXIT_CODES = {
    EXIT_OK: "ok",
    EXIT_INTERNAL: "internal error (bug) - please report with the traceback from --verbose",
    EXIT_USAGE: "usage / input validation error",
    EXIT_NO_PROVIDERS: "not enough usable providers",
    EXIT_PRIVACY: "blocked by privacy policy",
    EXIT_RECURSION: "recursive council call refused",
    EXIT_LESSON_REJECTED: "candidate lesson rejected by acceptance checks",
    EXIT_STAGE: "missing prerequisite stage",
    EXIT_PENDING: "background job still running",
}


class CouncilError(Exception):
    """A failure the caller can act on."""

    def __init__(self, message: str, hint: str = "", exit_code: int = EXIT_USAGE, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.exit_code = exit_code
        self.details = details or []

    def to_dict(self) -> dict:
        return {
            "error": self.message,
            "hint": self.hint,
            "exit_code": self.exit_code,
            "details": self.details,
        }
