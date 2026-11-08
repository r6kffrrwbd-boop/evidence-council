# Integrating audio-sales-coach (or any program with private source data)

evidence-council is domain-neutral; this is one integration pattern. audio-sales-coach keeps the
recording and transcript on its side and sends only **timestamped observations** and **draft coaching
notes** for review.

## What to send

```json
{
  "schema_version": "1.0",
  "task": {"id": "coach-call-0193", "prompt": "Review the draft coaching notes against the observations.",
           "domain": "coaching", "answer_format": "text", "tags": ["sales-coaching"]},
  "evidence": [
    {"id": "call-0193", "kind": "recording", "source_ref": "call-0193"},
    {"id": "obs-2", "kind": "observation", "timestamp": "00:07:15.5", "source_ref": "call-0193#t=435.5",
     "content": "Rep quoted the annual price before the client asked about cost.",
     "facts": [{"key": "price_volunteered_early", "value": true}]}
  ],
  "candidates": [
    {"id": "draft-1", "answer": "Consider holding price until asked.",
     "claims": [{"statement": "Rep volunteered price early", "key": "price_volunteered_early", "value": true,
                 "evidence_ids": ["obs-2"], "timestamp": "00:07:15.5"}]}
  ],
  "checks": [{"id": "facts", "type": "evidence_facts", "decisive": true},
             {"id": "cites", "type": "citations", "require_citations": true, "decisive": true}],
  "privacy": {"level": "private"},
  "providers": {"requested": ["none"]}
}
```

Full example: `examples/inputs/audio-sales-coach-review.json`; its result:
`examples/outputs/audio-sales-coach-review.result.json`.

* The `recording` item carries **no content and no URI** you need to send; even if present, they are
  never forwarded. It exists so observations can reference the call.
* Do not send transcripts by default. If you do, they are withheld from cloud providers unless the
  operator allowlists one.
* `providers.requested: ["none"]` reviews the drafts purely against the observations — nothing is
  sent to any model. To also get independent alternative notes from a **local** model, request
  `["ollama"]`; cloud providers stay excluded at the default `private` level.

## What comes back

* `verdict.status`: `supported` when one draft's keyed claims all match the observation facts and
  its citations hold; drafts with contradicted claims or wrong timestamps are in `verdict.rejected`
  with the reason (e.g. `claimed next_step_agreed=True but evidence obs-3 records False`).
* `claims[]`: per-claim status (`supported`, `contradicted`, `citation_problem`, `unverified`).
* `uncertainty`, `verdict.unverified`: what nothing could check.

From Python: `from evidence_council.council import run_review; run_review(doc, load_config())`.
From a shell: `evidence-council review notes.json --output result.json` (exit 0 even when drafts are
rejected — read `verdict.status`).

Coaching quality itself (tone, priorities) is subjective: send `kind: "subjective"` with a `rubric`
and the council returns each judge's scores rather than inventing a winner.
