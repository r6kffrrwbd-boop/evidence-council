# JSON contract (version 1.0)

Three documents, each with a JSON Schema shipped in the package:

| Kind | Schema | Produced by | Print it |
|---|---|---|---|
| input | `schemas/input.schema.json` | you / another program | `evidence-council schema input` |
| output (result / stage state) | `schemas/output.schema.json` | `review`, `propose`, `compare`, `verify`, `verdict` | `evidence-council schema output` |
| lesson | `schemas/lesson.schema.json` | `review` (as `candidate_lesson`) | `evidence-council schema lesson` |

`schema_version` is `"1.0"`. Fields are only ever *added* within 1.x; a breaking change bumps the major version.

## Input

```json
{
  "schema_version": "1.0",
  "task": {
    "id": "incident-2026-11-02",
    "title": "When did the outage start?",
    "prompt": "Using only the evidence, when (UTC, HH:MM) did errors start?",
    "domain": "research",            // coding | research | business | coaching | general
    "kind": "objective",             // objective | subjective
    "answer_format": "text",         // text | number | code | json | choice
    "answer_extract": "\\b(\\d{2}:\\d{2})\\b",  // optional regex: the core value used for grouping and reference/fact checks
    "choices": [],                   // for answer_format=choice
    "tags": ["incident"]
  },
  "evidence": [
    {"id": "lb-log", "kind": "log", "title": "...", "content": "...",
     "source_ref": "lb-01/access.log:11802", "timestamp": "00:03:12.5", "recorded_at": "2026-11-02T15:05:00Z",
     "sensitive": false, "facts": [{"key": "first_error_utc", "value": "14:32"}]}
  ],
  "constraints": ["Standard library only."],
  "premises": [
    {"id": "scale", "statement": "The service runs as one replica", "status": "observed", "basis": "read deploy.yaml",
     "fact_key": "replicas", "value": 1},
    {"statement": "Write volume stays under 50/s", "status": "not_verified", "basis": "a week of production metrics"}
  ],
  "candidates": [{"id": "draft-1", "label": "...", "answer": "...", "claims": [ ... ]}],
  "checks": [{"id": "first-error", "type": "fact_answer", "fact_key": "first_error_utc"}],
  "rubric": [{"criterion": "clarity", "weight": 1, "description": "..."}],
  "privacy": {"level": "private", "local_only": false, "redact": true},
  "providers": {"requested": [], "exclude": [], "judges": [], "min_participants": 1},
  "options": {"timeout_s": 240, "deadline_s": 900, "retries": 1, "max_output_chars": 20000,
              "allow_code_execution": false, "create_lesson": true, "use_lessons": true}
}
```

(Comments are illustrative; real files are plain JSON. See `examples/inputs/`.)

### Evidence kinds

`document, log, observation, measurement, transcript, audio, recording, test_output, web, note, data`.
`audio`/`recording` content and URIs are **never forwarded** to any provider. `transcript` and items
with `sensitive: true` are withheld from cloud providers unless the operator allowlists one.
`timestamp` is an offset inside a source (e.g. `00:07:15.5`) or an ISO instant; `source_ref` says where
the item came from (`call-0193#t=435.5`, `file.py:42`, a URL).

### Premises

Load-bearing facts the task relies on, each declared `observed` (with `basis`: how it was confirmed)
or `not_verified` (with `basis`: what would confirm it). Providers receive them as `OBSERVED: …` /
`NOT VERIFIED: …` lines, because a provider told "I have not checked X" can answer "check X first",
and one told "X holds" never will. Premises are redacted like the rest of the payload.

A premise with `fact_key` and `value` is checked against evidence facts during verification:
`confirmed_by_evidence` or `contradicted_by_evidence`. A contradicted premise means the task's own
framing is wrong: it is put first in `verdict.rationale`, uncertainty becomes `high`, and no candidate
lesson is proposed. Premises still `not_verified` are listed in `verdict.unverified`. Output:
`premises[]` = `{id, statement, declared, status, basis, detail}`.

### Claims

Proposals and candidates carry claims:
`{"statement", "key"?, "value"?, "evidence_ids"?, "quote"?, "timestamp"?}`. A claim with a `key` is
checked against evidence `facts` with that key; `evidence_ids`, `quote` and `timestamp` are checked
by `citations`.

### Checks

| type | level | effect | required fields |
|---|---|---|---|
| `numeric_reference` | answer | **selecting** — recompute with a safe arithmetic evaluator and compare | `expression`, optional `tolerance` |
| `reference_value` | answer | **selecting** — compare with a known value you supply | `value`, optional `case_sensitive`, `tolerance` |
| `fact_answer` | answer | **selecting** — compare with an evidence fact | `fact_key` |
| `python_tests` | answer | **selecting** — run your tests against the code answer (needs `allow_code_execution`) | `test_code`, optional `module_name` (default `solution`), `timeout_s` |
| `command` | answer | **selecting** — run your command with the answer on stdin; exit 0 = pass (needs `allow_code_execution`) | `argv`, optional `timeout_s` |
| `regex` | answer | format — fail rejects, pass cannot select | `pattern` |
| `json_schema` | answer | format — fail rejects, pass cannot select | `schema` |
| `evidence_facts` | claim | keyed claims vs evidence facts; with `"decisive": true` also judges each option (pass can select, contradiction rejects) | — |
| `citations` | claim | cited ids exist, quotes found, timestamps match; `require_citations` fails uncited claims; with `"decisive": true` a failure rejects the option | — |

Set `task.answer_extract` whenever answers may carry harmless decoration (units, time zones, a
sentence around a value). Without it, `reference_value` and `fact_answer` compare the whole normalised
answer, so `"14:32 UTC"` does **not** equal `"14:32"` — a real run hit exactly this (see
`examples/real-runs/incident-evidence.run1-false-rejection.explain.md`).

If evidence is supplied and you define no claim-level check, `auto.evidence_facts` (when facts exist)
and `auto.citations` are added automatically (non-decisive).

`safe_eval` allows numbers, `+ - * / // % **`, parentheses and `abs round min max sum len count sqrt floor ceil`.

## Output

Top-level fields (all present after `review`):

| field | meaning |
|---|---|
| `run_id`, `created_at`, `tool_version`, `task_digest` | identity; `task_digest` is sha256 of the canonical input |
| `stages_completed` | subset of `input, proposals, comparison, verification, verdict, learning` |
| `input` | the input with defaults filled — **same sensitivity as your input** |
| `providers.used` | providers whose adapter actually ran and returned a usable proposal. Nothing else participated. |
| `providers.attempted[]` | per call: `provider, adapter, locality, role, status (ok/unavailable/timeout/error/cancelled/parse_error), attempts, duration_s, tokens, cost_usd, cost_note, error` |
| `providers.excluded[]` | `{provider, reason}` — not requested, privacy, unknown name |
| `privacy` | `level, local_only, findings` (counts of phones/emails/cards/secrets, never values) and `routing[]` (per provider: allowed, reason, included/excluded evidence, redaction counts) |
| `proposals[]` | `id, source (provider/candidate), origin, answer, normalized_answer, summary, assumptions, claims, tradeoffs, uncertainties, self_confidence, parse (json/text_fallback/input), untrusted: true` |
| `options[]` | proposals grouped by normalised answer: `option_id, display_answer, proposals, origins, support_count` |
| `disagreements[]` | `type (answer/claim/assumption/confidence), topic, positions` |
| `claims[]` | every claim with `status`: `supported, contradicted, citation_problem, citation_ok_content_unverified, unverified` |
| `verification` | `checks[]` (`check_id, type, auto, decisive, can_select, target_option/proposal/claim, status pass/fail/error/not_run, ran, detail, duration_s`), `summary {run, passed, failed, errored, not_run}`, `note`, `defined_checks` |
| `judgments[]` | subjective tasks only: `{judge, proposal_id, criterion, score 1-5, reason}` |
| `verdict` | see below |
| `uncertainty` | `{level: low/medium/high, reasons[]}` |
| `candidate_lesson` | a lesson document or `null` |
| `relevant_lessons[]` | accepted lessons given to proposers as context |
| `premises[]` | each input premise with its checked `status` (see Premises) |
| `warnings[]` | e.g. fewer than two providers, cancelled run |

## Verdict

```json
{"status": "supported", "selected_option": "B", "selected_answer": "11398661", "selected_proposals": ["p3"],
 "also_supported": [], "rationale": ["..."], "rejected": [{"option_id": "A", "answer": "...", "reasons": ["..."], "support_count": 2}],
 "unverified": ["claim p3.c1: ..."], "popular_option": "A", "overturned_popular": true,
 "vote_note": "Support counts are reported for transparency only; they were not used to select an answer."}
```

Rules, applied only to check results:

1. An option is **rejected** if any answer-level check (selecting or format) fails for it.
2. An option is **supported** if it is not rejected and at least one *selecting* check passed.
3. Exactly one supported → `supported`. Several → `multiple_supported` (ranked by fewer contradicted
   claims, then more passes; the ranking is reported as non-decisive). None supported but some not
   rejected → `insufficient_evidence`. All rejected → `all_rejected`. No proposals → `no_proposals`.
   `kind: subjective` → `subjective` (judges' rubric scores shown, `selected_option` is null).
4. How many proposals agree is never used to select. `overturned_popular` is true when the strict
   majority option was rejected and another option was selected.
