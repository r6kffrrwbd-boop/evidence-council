# Test results

Recorded on 2026-11-08, macOS 14 (Intel x86_64), CPython 3.12.13. Re-run them yourself; nothing here
should be taken on trust.

## 1. Deterministic suite (MOCK providers, offline)

`pytest` → **102 passed** (real-provider test deselected by default).

| File | Tests | Covers |
|---|---|---|
| `tests/test_contract.py` | 22 | every example input/output validates; invalid inputs report `$.path`; defaults (privacy `private`, no code execution); task input cannot carry privacy overrides; JSON syntax errors give line/column + hint |
| `tests/test_disagreement.py` | 3 | answer / keyed-claim / assumption / confidence disagreements; unanimous agreement still `insufficient_evidence`; proposers never see each other or the draft candidates |
| `tests/test_provider_failure.py` | 6 | timeout, error, unavailable, transient-then-ok (retried), malformed output, all-fail (exit 3, result still written), cancellation, run deadline, unknown provider, secret redaction in errors |
| `tests/test_insufficient_evidence.py` | 6 | no checks; errored check; self-contradictory evidence; code checks without permission (`not_run`); format-only check cannot select; `all_rejected` |
| `tests/test_privacy.py` | 9 | private default excludes cloud; recordings never forwarded (even locally); `restricted` not overridable; `--local-only`; operator allowlist still redacts; transcripts withheld from cloud; secrets redacted for all; `privacy` dry-run and `--show-payload`; exit 4 |
| `tests/test_lessons.py` | 14 | candidate only from verified outcomes; accept → versioned memory; rejections (tampered verdict, injection text, PII, no selecting check, model-judgment evidence, restricted, length, missing provenance); conflict until retired; tamper detection; CLI flow writes only `memory/lessons.jsonl` and leaves `CLAUDE.md` untouched; lessons are context, never a substitute for a check |
| `tests/test_overturn.py` | 5 | **popular wrong answer overturned** by recomputation (CLI), by unit tests (sandboxed), and by log evidence; fabricated citations caught; regression for the false overturn found in a real run |
| `tests/test_cli_tools.py` | 25 | every tool has `--help` with examples; SKILL.md documents every tool, its reference links resolve, and every command in it parses; `explain --json` interpretation of supported vs insufficient results; staged pipeline equals `review`; stage-order and JSON error formats; recursion guard (exit 5); `providers`, `selftest`, `codes` |
| `tests/test_subjective_and_safety.py` | 12 | subjective task → no winner, judges never score their own proposal; safe arithmetic evaluator accepts arithmetic and rejects code; macOS sandbox blocks network and home reads; sandbox timeout |

`evidence-council selftest` (also run by `install.sh`): 9/9 PASS — offline mock council overturns a
2-vs-1 wrong product with a recomputation check.

## 2. Real end-to-end runs (NOT mocked)

`scripts/run_real_e2e.sh` against the providers on this machine. Raw results and explanations are in
`examples/real-runs/`.

**Probe** (`providers-probe.json`): callable = `codex`, `ollama:qwen3.5:9b`, `ollama:qwen3.5:4b`.
Not callable: `claude` (headless login expired), `gemini` (Google: UNSUPPORTED_CLIENT), `hermes`
(its configured OpenRouter model returns HTTP 404), `grok` (no `XAI_API_KEY`).

| Task | Participants | Answers | Checks actually run | Verdict |
|---|---|---|---|---|
| `real-arithmetic` (48·37 + 23·59 − 418) | codex, qwen3.5:9b, qwen3.5:4b | codex **2715**; 9b 1846; 4b 1408 | `recompute` (numeric_reference) ×3 options | `supported` 2715. Both local models' answers rejected by recomputation. No majority existed, so `overturned_popular` is false. |
| `coding-leap-year` | codex, qwen3.5:9b, qwen3.5:4b | three different correct implementations | `unit-tests` (python_tests, sandboxed) ×3 — all pass | `multiple_supported`: tests cannot distinguish them; the tool says so instead of picking a "winner" |
| `incident-evidence` run 1 | codex, qwen3.5:9b, qwen3.5:4b | "14:32 UTC" ×2, "14:32" ×1 | `first-error` (fact_answer), auto citations/facts | **Bug found:** strict text comparison rejected "14:32 UTC" and falsely reported an overturned majority. Kept as `incident-evidence.run1-false-rejection.*`. Fixed with author-controlled `task.answer_extract`; regression test added. |
| `incident-evidence` run 2 (after fix) | codex, qwen3.5:9b, qwen3.5:4b | all three: 14:32 (with/without "UTC") — one option | `first-error` (fact_answer) pass; auto evidence_facts and citations | `supported` 14:32 UTC; no rejection, no false overturn |

Real-run timings: codex 12–24 s per call (~20k tokens, CLI agent context); qwen3.5:9b 175–366 s and
qwen3.5:4b 51–463 s per call on CPU.

**Honest limits.** The overturn of a *popular* wrong answer is demonstrated deterministically (mocks,
`selftest`, `test_overturn.py`). In the real runs, local models were wrong but never formed a majority,
so no real popular-answer overturn occurred. Claude, Gemini, Hermes and Grok adapters are implemented
but were not callable here, so they are unverified against live services.
