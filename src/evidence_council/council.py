"""Council orchestrator. Each stage takes and returns the same *state* document (the output contract),
so stages can run together (``run_review``) or one at a time (``run_stage``)."""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import time
from typing import Any

from . import __version__
from .compare import build_options, collect_claims, find_disagreements
from .config import Config
from .contract import require_valid, with_defaults
from .errors import CouncilError, EXIT_INTERNAL, EXIT_RECURSION, EXIT_STAGE, EXIT_USAGE
from .lessons import LessonStore, make_candidate
from .parsing import candidate_to_proposal, extract_json_object, parse_proposal
from .privacy import build_payload, findings, plan_route, redact_for
from .prompts import judge_prompt, proposal_prompt
from .providers import RECURSION_ENV, Adapter, CancelToken, build_adapters, select_adapters
from .util import new_run_id, now_iso, safe_log, sha256_digest
from .verdict import decide
from .verify import annotate_claims, run_verification

STAGES = ["input", "proposals", "comparison", "verification", "verdict", "learning"]


def guard_recursion() -> None:
    if os.environ.get(RECURSION_ENV) == "1":
        raise CouncilError("refusing to start a council from inside a council provider call",
                           hint="evidence-council sets EVIDENCE_COUNCIL_ACTIVE=1 for provider subprocesses to prevent recursive councils. "
                                "Unset it only if you are certain this is not a nested call.",
                           exit_code=EXIT_RECURSION)


def init_state(input_doc: dict, *, local_only: bool = False) -> dict:
    require_valid(input_doc, "input", "task input")
    inp = with_defaults(input_doc)
    warnings = []
    if local_only and not inp["privacy"]["local_only"]:
        inp["privacy"]["local_only"] = True
        warnings.append("--local-only given: cloud providers excluded for this run")
    return {
        "schema_version": "1.0",
        "tool_version": __version__,
        "run_id": new_run_id(),
        "created_at": now_iso(),
        "stages_completed": ["input"],
        "input": inp,
        "task_digest": sha256_digest(input_doc),
        "providers": {"requested": list(inp["providers"]["requested"]), "used": [], "attempted": [], "excluded": []},
        "privacy": {"level": inp["privacy"]["level"], "local_only": inp["privacy"]["local_only"], "routing": [], "findings": findings(inp)},
        "proposals": [],
        "relevant_lessons": [],
        "warnings": warnings,
    }


def _require(state: dict, stage: str) -> None:
    if stage not in state.get("stages_completed", []):
        nxt = {"input": "evidence-council propose TASK.json", "proposals": "evidence-council propose",
               "comparison": "evidence-council compare", "verification": "evidence-council verify",
               "verdict": "evidence-council verdict"}[stage]
        raise CouncilError(f"state has not completed the '{stage}' stage", hint=f"Run `{nxt}` first, or use `evidence-council review` for the whole pipeline.",
                           exit_code=EXIT_STAGE)


def _mark(state: dict, stage: str) -> None:
    if stage not in state["stages_completed"]:
        state["stages_completed"].append(stage)


def _routes(state: dict, adapters: list[Adapter], config: Config) -> dict[str, Any]:
    allow = config.private_allowlist()
    routes = {}
    for a in adapters:
        routes[a.name] = plan_route(state["input"], a.name, a.locality, allow)
    return routes


# ---------------------------------------------------------------------------
# stages

def stage_proposals(state: dict, config: Config, cancel: CancelToken | None = None,
                    requested: list[str] | None = None, verbose: bool = False) -> dict:
    guard_recursion()
    _require(state, "input")
    cancel = cancel or CancelToken()
    inp = state["input"]
    d = {**config.defaults, **{k: v for k, v in inp["options"].items() if k in ("timeout_s", "deadline_s", "retries", "max_output_chars")}}
    req = requested if requested is not None else inp["providers"]["requested"]
    state["providers"]["requested"] = list(req)
    try:
        adapters = build_adapters(config.providers, config.defaults)
    except ValueError as e:
        raise CouncilError(str(e), hint="Fix the provider entry in your config (docs/providers.md).", exit_code=EXIT_USAGE)
    selected, excluded = select_adapters(adapters, list(req), inp["providers"]["exclude"])
    state["providers"]["excluded"].extend(excluded)
    routes = _routes(state, selected, config)
    state["privacy"]["routing"] = []
    runnable: list[tuple[Adapter, str]] = []
    if inp["options"].get("use_lessons", True):
        try:
            state["relevant_lessons"] = LessonStore(config.memory_dir).relevant(inp["task"]["domain"], inp["task"]["tags"])
        except CouncilError as e:
            state["warnings"].append(f"lesson memory unavailable: {e.message}")
    for a in selected:
        r = routes[a.name]
        if not r.allowed:
            state["providers"]["excluded"].append({"provider": a.name, "reason": r.reason})
            state["privacy"]["routing"].append(r.to_dict())
            continue
        payload = build_payload(inp, r)
        state["privacy"]["routing"].append(r.to_dict())
        runnable.append((a, proposal_prompt(payload, state["relevant_lessons"])))

    deadline = time.monotonic() + float(d.get("deadline_s", 900))
    results = []
    if runnable:
        with cf.ThreadPoolExecutor(max_workers=max(1, min(int(d.get("max_parallel", 4)), len(runnable)))) as ex:
            futs = {ex.submit(a.generate, prompt, cancel, inp["options"].get("timeout_s"), inp["options"].get("retries"), deadline): a
                    for a, prompt in runnable}
            try:
                for f in cf.as_completed(futs):
                    res = f.result()
                    if verbose:
                        safe_log(f"{res.provider}: {res.status} in {res.duration_s:.1f}s {res.error}")
                    results.append(res)
            except KeyboardInterrupt:
                cancel.set()
                raise
    order = {a.name: i for i, (a, _) in enumerate(runnable)}
    results.sort(key=lambda r: order.get(r.provider, 0))
    fmt = inp["task"]["answer_format"]
    limit = int(d.get("max_output_chars", 20000))
    n = 0
    for res in results:
        att = res.to_attempt("proposer")
        if res.status == "ok":
            n += 1
            prop = parse_proposal(res.text, f"p{n}", res.provider, fmt, limit, inp["task"].get("answer_extract"))
            if not prop["answer"].strip():
                att["status"], att["error"] = "parse_error", "provider returned no answer"
            else:
                prop["adapter"] = res.adapter
                state["proposals"].append(prop)
                state["providers"]["used"].append(res.provider)
        state["providers"]["attempted"].append(att)
    for c in inp["candidates"]:
        state["proposals"].append(candidate_to_proposal(c, fmt, inp["task"].get("answer_extract")))
    used = len(state["providers"]["used"])
    if list(req) == ["none"]:
        state["warnings"].append("candidate-only review: no providers were asked (providers.requested = ['none'])")
    elif used < 2:
        state["warnings"].append(f"only {used} independent provider(s) produced a proposal; disagreement analysis is limited")
    if cancel.is_set():
        state["warnings"].append("run was cancelled; results are partial")
    _mark(state, "proposals")
    return state


def stage_comparison(state: dict) -> dict:
    _require(state, "proposals")
    state["options"] = build_options(state["proposals"])
    state["disagreements"] = find_disagreements(state["proposals"], state["options"])
    state["claims"] = collect_claims(state["proposals"])
    _mark(state, "comparison")
    return state


def stage_verification(state: dict) -> dict:
    _require(state, "comparison")
    state["verification"] = run_verification(state["input"], state["proposals"], state["options"])
    state["claims"] = annotate_claims(state["claims"], state["verification"]["checks"])
    _mark(state, "verification")
    return state


def _run_judges(state: dict, config: Config, cancel: CancelToken) -> list[dict]:
    inp = state["input"]
    if inp["task"]["kind"] != "subjective" or not inp["rubric"] or not state["proposals"]:
        return []
    guard_recursion()
    adapters = build_adapters(config.providers, config.defaults)
    pool = inp["providers"]["judges"] or state["providers"]["used"]
    judges = [a for a in adapters if a.name in pool]
    routes = _routes(state, judges, config)
    out: list[dict] = []
    for a in judges:
        r = routes[a.name]
        if not r.allowed:
            state["providers"]["excluded"].append({"provider": a.name, "reason": "judge: " + r.reason})
            continue
        # independence: a judge never scores its own proposal
        props = [{"id": p["id"], "answer": redact_for(r, p["answer"])} for p in state["proposals"] if p["origin"] != a.name]
        if not props:
            continue
        payload = build_payload(inp, r)
        res = a.generate(judge_prompt(payload, props, inp["rubric"]), cancel)
        att = res.to_attempt("judge")
        if res.status == "ok":
            obj = extract_json_object(res.text, "scores")
            scores = (obj or {}).get("scores", [])
            valid_ids = {p["id"] for p in props}
            crit = {x["criterion"] for x in inp["rubric"]}
            got = 0
            for s in scores if isinstance(scores, list) else []:
                try:
                    if s["proposal_id"] in valid_ids and s["criterion"] in crit and 1 <= float(s["score"]) <= 5:
                        out.append({"judge": a.name, "proposal_id": s["proposal_id"], "criterion": s["criterion"],
                                    "score": float(s["score"]), "reason": str(s.get("reason", ""))[:500]})
                        got += 1
                except (KeyError, TypeError, ValueError):
                    continue
            if not got:
                att["status"], att["error"] = "parse_error", "judge returned no valid rubric scores"
        state["providers"]["attempted"].append(att)
    return out


def stage_verdict(state: dict, config: Config, cancel: CancelToken | None = None) -> dict:
    _require(state, "verification")
    state["judgments"] = _run_judges(state, config, cancel or CancelToken())
    verdict, uncertainty = decide(state["input"], state["proposals"], state["options"], state["verification"],
                                  state["claims"], state["judgments"], len(state["providers"]["used"]))
    state["verdict"], state["uncertainty"] = verdict, uncertainty
    _mark(state, "verdict")
    state["candidate_lesson"] = make_candidate(state)
    _mark(state, "learning")
    return state


def finalize(state: dict) -> dict:
    try:
        require_valid(state, "output", "council output")
    except CouncilError as e:  # our own output failing our contract is a bug
        raise CouncilError("internal error: produced output violates the output contract: " + e.message,
                           hint="Please report this with the input file.", exit_code=EXIT_INTERNAL, details=e.details)
    return state


def run_review(input_doc: dict, config: Config, *, requested: list[str] | None = None, local_only: bool = False,
               cancel: CancelToken | None = None, verbose: bool = False) -> dict:
    """Full pipeline: input -> proposals -> comparison -> verification -> verdict -> learning."""
    guard_recursion()
    state = init_state(input_doc, local_only=local_only)
    stage_proposals(state, config, cancel, requested, verbose)
    stage_comparison(state)
    stage_verification(state)
    stage_verdict(state, config, cancel)
    return finalize(state)


def run_stage(stage: str, doc: dict, config: Config, **kw: Any) -> dict:
    if stage == "propose":
        state = doc if "run_id" in doc else init_state(doc, local_only=kw.get("local_only", False))
        return finalize(stage_proposals(state, config, kw.get("cancel"), kw.get("requested"), kw.get("verbose", False)))
    if "run_id" not in doc:
        raise CouncilError(f"`{stage}` expects a state file produced by an earlier stage, not a task input",
                           hint="Run `evidence-council propose TASK.json -o state.json` first.", exit_code=EXIT_STAGE)
    require_valid(doc, "output", "state")
    if stage == "compare":
        return finalize(stage_comparison(doc))
    if stage == "verify":
        return finalize(stage_verification(doc))
    if stage == "verdict":
        return finalize(stage_verdict(doc, config, kw.get("cancel")))
    raise CouncilError(f"unknown stage {stage}", exit_code=EXIT_USAGE)


__all__ = ["run_review", "run_stage", "init_state", "STAGES", "guard_recursion", "json"]
