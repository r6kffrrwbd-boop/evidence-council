# Lessons and versioned memory

The council learns from **verified outcomes**, not from confident opinions.

## Lifecycle

1. `review` emits `candidate_lesson` only when the verdict is `supported` (a selecting check passed)
   and `options.create_lesson` is true. Nothing is stored.
2. `evidence-council lessons check RESULT.json` runs the acceptance checks and prints a report.
3. `evidence-council lessons accept RESULT.json` re-runs them and, only if **every** check passes,
   appends the lesson to `<home>/memory/lessons.jsonl` (default `~/.local/share/evidence-council/memory/`).
4. Later reviews with matching domain/tags include accepted lessons in the proposal prompt, labelled
   *"context only; may not apply and may be outdated"*. A lesson never substitutes for a check.
5. `evidence-council lessons retire ID --reason …` appends a retirement entry.

## Acceptance checks

| check | rule |
|---|---|
| `schema_valid` | matches `lesson.schema.json` |
| `is_candidate` | status is `candidate` |
| `verdict_supported` | source verdict was `supported` |
| `has_passing_selecting_check` | at least one selecting check ran and passed — an opinion cannot become a lesson |
| `only_deterministic_evidence` | every linked check is a deterministic check type (no model judgments) |
| `no_private_data` | no phone/e-mail/card/secret patterns in the statement, answer or rejected answers |
| `privacy_level_allows` | source privacy level is not `restricted` (private tasks get generic statements without answers) |
| `no_instruction_injection` | statement contains no instruction-override patterns ("ignore previous", "CLAUDE.md", "rm -rf", …) |
| `statement_length` | 20–1000 characters |
| `linked_to_run` | run id and task digest present |
| `provenance_matches_result` | run id, digest, verdict status and every linked check result match the result document |
| `not_duplicate` | not already in memory |
| `no_conflict` | no accepted lesson with the same `subject_key` and a different verified answer (retire it first) |

## Memory format

Append-only JSON Lines. Each entry: `seq`, `op` (`accept`/`retire`), `lesson_id`, `lesson` (for
accept), `reason` (for retire), `at`, `prev_hash`, `hash` (sha256 over the entry). The chain is
verified on every read; a hand-edited log fails with an integrity error. `memory_version` is the
last `seq`. Nothing is rewritten or deleted.

evidence-council never writes lessons anywhere else, and never edits CLAUDE.md, AGENTS.md or other
agents' instructions.
