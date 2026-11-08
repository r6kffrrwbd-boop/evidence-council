# Changelog

## 0.2.0 — 2026-11-08
- **Premises:** `premises[]` in the input, labelled `observed` / `not_verified` and sent to providers as
  `OBSERVED:` / `NOT VERIFIED:`. Premises naming an evidence fact are checked; a contradicted premise is
  reported first, raises uncertainty to high, and blocks the candidate lesson.
- **Background jobs:** `review --async` and `jobs list|status|result|cancel|remove`; exit code 8 while running.
- `task.answer_extract` (regex) to compare the core value of an answer (fixes a false rejection seen in a real run).

## 0.1.0 — 2026-11-08
- Initial release: CLI and Claude Code skill, JSON contract 1.0, provider adapters, privacy routing,
  deterministic verification, verdict rules, candidate lessons with versioned memory.
