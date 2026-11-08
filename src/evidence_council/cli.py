"""`evidence-council` command-line interface.

Each subcommand is one tool with a JSON input and a JSON output. Run
``evidence-council <tool> --help`` for its contract and examples. Errors are printed
as ``error: ...`` / ``hint: ...`` on stderr (or one JSON object with
``--error-format json``) and exit with a stable code (see ``evidence-council codes``).
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import textwrap
import traceback
from importlib import resources
from typing import Any

from . import __version__
from .config import load_config
from .contract import KINDS, detect_kind, load_schema, validate
from .errors import EXIT_CODES, EXIT_INTERNAL, EXIT_LESSON_REJECTED, EXIT_NO_PROVIDERS, EXIT_OK, EXIT_USAGE, CouncilError
from .util import read_json, write_json

EPILOGS = {
    "review": """
        Runs every stage: proposals -> comparison -> verification -> verdict -> learning.
        Input: a task file matching `evidence-council schema input`. Output: a result matching
        `evidence-council schema output` (written to --output, or stdout).

        Exit codes: 0 review completed (read verdict.status - it may still be
        insufficient_evidence); 3 fewer usable participants than required; 2 invalid input;
        4 privacy blocked every provider and there were no candidates; 5 recursive call refused.

        examples:
          evidence-council review task.json --output result.json
          evidence-council review task.json --providers codex,ollama --output result.json
          evidence-council review task.json --local-only --output result.json
          evidence-council review coaching.json --providers none -o result.json   # review input candidates only
          evidence-council review task.json -o result.json && evidence-council explain result.json
          evidence-council review task.json --async        # returns a job id immediately
        """,
    "jobs": """
        Background reviews started with `review --async`. Jobs live in <home>/jobs/<job-id>/
        (task copy, log, result) with owner-only permissions.

        `result` prints the result JSON (or --explain for the plain-language reading) when the
        job is done; exit 8 while it is still running. `cancel` stops provider calls cleanly and
        keeps a partial result. `remove` deletes a finished job's directory.

        examples:
          evidence-council review task.json --async
          evidence-council jobs list
          evidence-council jobs status job-20261108T201500Z-1a2b3c4d
          evidence-council jobs result job-20261108T201500Z-1a2b3c4d --explain
          evidence-council jobs result job-20261108T201500Z-1a2b3c4d -o result.json
          evidence-council jobs cancel job-20261108T201500Z-1a2b3c4d
          evidence-council jobs remove job-20261108T201500Z-1a2b3c4d
        """,
    "propose": """
        Stage 1 only: ask the selected providers independently (none sees another's answer).
        Accepts a task input or an existing state; writes a state with stages_completed
        ending in "proposals".

        examples:
          evidence-council propose task.json -o state.json
          evidence-council propose task.json --providers ollama -o state.json
        """,
    "compare": """
        Stage 2: group proposals into answer options; list differing answers, keyed-claim
        values, assumptions and confidence. Decides nothing.

        example:
          evidence-council compare state.json -o state.json
        """,
    "verify": """
        Stage 3: run the input's objective checks (plus automatic evidence checks) with no
        model involvement. Code-executing checks need options.allow_code_execution=true.

        example:
          evidence-council verify state.json -o state.json
        """,
    "verdict": """
        Stage 4+5: choose the best-supported option from check results (never by vote),
        state what remains unverified, and propose a candidate lesson when verified.
        Subjective tasks: rubric judges (never scoring their own proposal) instead of a winner.

        example:
          evidence-council verdict state.json -o result.json
        """,
    "explain": """
        Plain-language reading of a result, including what must NOT be claimed.
        Use --json for the machine-readable reading that Claude relays.

        examples:
          evidence-council explain result.json
          evidence-council explain result.json --json
        """,
    "providers": """
        List configured providers and whether each is callable. Without --probe nothing is
        sent to any model (binary/server/login checks only). --probe sends a one-line
        test prompt to each available provider.

        examples:
          evidence-council providers
          evidence-council providers --probe --json
        """,
    "privacy": """
        Dry run: show which providers would receive the task and exactly what each would
        receive (after minimisation and redaction). Calls no provider.

        examples:
          evidence-council privacy coaching.json
          evidence-council privacy coaching.json --show-payload codex
          evidence-council privacy task.json --allow-private-to codex
        """,
    "validate": """
        Validate a JSON file against the input, output or lesson contract.
        Prints {"valid": bool, "kind": ..., "problems": [...]}; exit 0 if valid, 2 if not.

        examples:
          evidence-council validate task.json
          evidence-council validate result.json --kind output
        """,
    "schema": """
        Print a JSON Schema of the public contract.

        examples:
          evidence-council schema input
          evidence-council schema output > output.schema.json
        """,
    "lessons": """
        Candidate lessons are never applied automatically. `check` runs the acceptance
        checks; `accept` runs them and appends the lesson to versioned memory only if all
        pass (exit 6 otherwise); `retire` appends a retirement entry (nothing is deleted).
        Memory: <home>/memory/lessons.jsonl (hash-chained, append-only).

        examples:
          evidence-council lessons list
          evidence-council lessons check result.json
          evidence-council lessons accept result.json
          evidence-council lessons retire lesson-0123456789abcdef --reason "superseded by new tests"
        """,
    "selftest": """
        Offline self-test with deterministic MOCK providers: two agree on a wrong product,
        one is right, and a recomputation check must overturn the majority.
        Contacts no provider; exit 0 on success.

        example:
          evidence-council selftest
        """,
}


def _fmt(text: str) -> str:
    return textwrap.dedent(text).strip("\n")


def _common(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("common options")
    g.add_argument("--config", metavar="PATH", help="operator config JSON (default: $EVIDENCE_COUNCIL_CONFIG or ~/.config/evidence-council/config.json)")
    g.add_argument("--home", metavar="DIR", help="state directory for lesson memory (default: $EVIDENCE_COUNCIL_HOME or ~/.local/share/evidence-council)")
    g.add_argument("--verbose", "-v", action="store_true", help="progress and diagnostics on stderr (secrets redacted)")
    g.add_argument("--error-format", choices=["text", "json"], default="text", help="how to print errors on stderr")


def _provider_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--providers", metavar="LIST", help="comma-separated provider names (overrides input providers.requested); 'none' = candidates only")
    p.add_argument("--local-only", action="store_true", help="exclude every cloud provider for this run")
    p.add_argument("--allow-private-to", metavar="NAME", action="append", default=[],
                   help="operator override: allow private-level data to this cloud provider for this run (repeatable)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="evidence-council",
        description="Ask several AI providers for independent proposals, compare them, verify claims against objective evidence, "
                    "and return a justified verdict (or 'insufficient_evidence').",
        epilog=_fmt("""
            tools:
              review     full pipeline (most common)
              propose / compare / verify / verdict   run one stage on a state file
              explain    plain-language reading of a result
              providers  which providers are callable on this machine
              privacy    dry-run of what each provider would receive
              validate   check a file against the JSON contract
              schema     print the input/output/lesson JSON Schema
              lessons    list / check / accept / retire learned lessons
              jobs       background reviews (review --async): list / status / result / cancel / remove
              selftest   offline end-to-end check with mock providers
              codes      list exit codes

            examples:
              evidence-council providers
              evidence-council review task.json --output result.json
              evidence-council explain result.json
            """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version", version=f"evidence-council {__version__} (contract 1.0)")
    sub = ap.add_subparsers(dest="tool", metavar="TOOL")

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_, description=help_, epilog=_fmt(EPILOGS.get(name, "")),
                           formatter_class=argparse.RawDescriptionHelpFormatter)
        _common(p)
        return p

    p = add("review", "Run the whole council on a task file.")
    p.add_argument("task", help="task input JSON (or '-' for stdin)")
    p.add_argument("--output", "-o", metavar="PATH", help="write result JSON here (default: stdout)")
    p.add_argument("--markdown", action="store_true", help="also print the plain-language explanation to stderr")
    p.add_argument("--async", dest="run_async", action="store_true",
                   help="run in the background and print a job id at once (see `evidence-council jobs --help`)")
    _provider_opts(p)

    p = add("propose", "Stage: collect independent proposals.")
    p.add_argument("task", help="task input JSON or an existing state JSON")
    p.add_argument("--output", "-o", metavar="PATH")
    _provider_opts(p)

    for name, h in (("compare", "Stage: compare proposals and list disagreements."),
                    ("verify", "Stage: run objective checks."),
                    ("verdict", "Stage: decide from check results and propose a candidate lesson.")):
        p = add(name, h)
        p.add_argument("state", help="state JSON from the previous stage")
        p.add_argument("--output", "-o", metavar="PATH")

    p = add("explain", "Explain a result in plain language.")
    p.add_argument("result", help="result JSON from review/verdict")
    p.add_argument("--json", action="store_true", help="print the structured reading instead of markdown")

    p = add("providers", "Show which providers are callable.")
    p.add_argument("--probe", action="store_true", help="send a one-line test prompt to each available provider")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--include-disabled", action="store_true")

    p = add("privacy", "Dry-run privacy routing for a task.")
    p.add_argument("task")
    p.add_argument("--show-payload", metavar="NAME", help="print the exact minimised payload this provider would receive")
    _provider_opts(p)

    p = add("validate", "Validate JSON against the contract.")
    p.add_argument("file")
    p.add_argument("--kind", choices=KINDS, help="contract to use (default: detected)")

    p = add("schema", "Print a JSON Schema.")
    p.add_argument("kind", choices=KINDS)

    p = add("lessons", "Manage candidate and accepted lessons.")
    lsub = p.add_subparsers(dest="lessons_cmd", metavar="ACTION")
    lp = lsub.add_parser("list", help="list accepted/retired lessons"); _common(lp)
    lp.add_argument("--all", action="store_true", help="include retired lessons")
    lc = lsub.add_parser("check", help="run acceptance checks without accepting"); _common(lc)
    lc.add_argument("file", help="review result JSON (or a lesson JSON with --result)")
    lc.add_argument("--result", metavar="PATH", help="result the lesson came from (for provenance)")
    la = lsub.add_parser("accept", help="accept into memory if every check passes"); _common(la)
    la.add_argument("file")
    la.add_argument("--result", metavar="PATH")
    lr = lsub.add_parser("retire", help="retire an accepted lesson"); _common(lr)
    lr.add_argument("lesson_id")
    lr.add_argument("--reason", required=True)

    p = add("jobs", "Manage background reviews started with --async.")
    jsub = p.add_subparsers(dest="jobs_cmd", metavar="ACTION")
    jl = jsub.add_parser("list", help="list jobs, newest first"); _common(jl)
    for name, h in (("status", "show one job's status"), ("cancel", "cancel a running job (keeps a partial result)"),
                    ("remove", "delete a finished job's directory")):
        jp = jsub.add_parser(name, help=h); _common(jp)
        jp.add_argument("job_id")
    jr = jsub.add_parser("result", help="print a finished job's result (exit 8 while running)"); _common(jr)
    jr.add_argument("job_id")
    jr.add_argument("--explain", action="store_true", help="print the plain-language reading instead of JSON")
    jr.add_argument("--output", "-o", metavar="PATH", help="copy the result JSON here")

    add("selftest", "Offline self-test with mock providers.")
    add("codes", "List exit codes.")
    return ap


# ---------------------------------------------------------------------------
# command implementations

def _cfg(args):
    cfg = load_config(getattr(args, "config", None), getattr(args, "home", None))
    cfg.extra_allow_private_to = list(getattr(args, "allow_private_to", []) or [])
    return cfg


def _requested(args) -> list[str] | None:
    if getattr(args, "providers", None) is None:
        return None
    return [x.strip() for x in args.providers.split(",") if x.strip()]


def _cancel_token():
    from .providers import CancelToken
    tok = CancelToken()

    def handler(signum, frame):
        if tok.is_set():
            raise KeyboardInterrupt
        tok.set()
        print("[evidence-council] cancelling provider calls (Ctrl-C again to abort)", file=sys.stderr)

    try:
        signal.signal(signal.SIGINT, handler)
    except ValueError:  # not main thread
        pass
    return tok


def _min_participants(result: dict, cfg) -> int:
    m = result["input"]["providers"].get("min_participants")
    return int(cfg.defaults.get("min_participants", 1) if m is None else m)


def cmd_review(args) -> int:
    from .council import guard_recursion, run_review
    from .explain import render_markdown
    cfg = _cfg(args)
    doc = read_json(args.task)
    if args.run_async:
        from . import jobs
        from .contract import require_valid, with_defaults
        guard_recursion()
        require_valid(doc, "input", "task input")
        with_defaults(doc)  # surface duplicate-id errors now, not in the background
        info = jobs.submit(doc, home=cfg.home, config_path=args.config, providers=_requested(args),
                           local_only=args.local_only, allow_private_to=list(args.allow_private_to or []))
        write_json(info, None)
        print(f"[evidence-council] started {info['job_id']}; fetch with: {info['next']}", file=sys.stderr)
        return EXIT_OK
    res = run_review(doc, cfg, requested=_requested(args), local_only=args.local_only, cancel=_cancel_token(), verbose=args.verbose)
    write_json(res, args.output)
    if args.output:
        v = res["verdict"]
        print(f"[evidence-council] verdict: {v['status']}"
              + (f" -> {v['selected_answer'][:80]!r}" if v.get("selected_answer") else "")
              + f" | participants: {', '.join(res['providers']['used']) or 'none'} | wrote {args.output}", file=sys.stderr)
    if args.markdown:
        print(render_markdown(res), file=sys.stderr)
    return _exit_for(res, cfg)


def _exit_for(res: dict, cfg) -> int:
    used = len(res["providers"]["used"])
    has_candidates = any(p["source"] == "candidate" for p in res["proposals"])
    requested_none = res["providers"].get("requested") == ["none"]
    if not used and not has_candidates:
        blocked = [r for r in res["privacy"]["routing"] if not r["allowed"]]
        if blocked and len(blocked) == len(res["privacy"]["routing"]):
            print("error: privacy policy excluded every selected provider and there are no candidates\n"
                  "hint: use a local provider (e.g. --providers ollama), or have the operator allowlist a provider with --allow-private-to NAME",
                  file=sys.stderr)
            return 4
        print("error: no provider produced a usable proposal\nhint: run `evidence-council providers --probe` to see which providers work",
              file=sys.stderr)
        return EXIT_NO_PROVIDERS
    if not requested_none and used < _min_participants(res, cfg):
        print(f"error: only {used} provider(s) participated; min_participants is {_min_participants(res, cfg)}\n"
              "hint: lower providers.min_participants or make more providers available (evidence-council providers --probe)", file=sys.stderr)
        return EXIT_NO_PROVIDERS
    return EXIT_OK


def cmd_stage(args) -> int:
    from .council import run_stage
    cfg = _cfg(args)
    stage = args.tool
    doc = read_json(args.task if stage == "propose" else args.state)
    kw: dict[str, Any] = {"cancel": _cancel_token(), "verbose": args.verbose}
    if stage == "propose":
        kw.update(requested=_requested(args), local_only=args.local_only)
    res = run_stage(stage, doc, cfg, **kw)
    write_json(res, args.output)
    return EXIT_OK


def cmd_explain(args) -> int:
    from .explain import reading, render_markdown
    res = read_json(args.result)
    probs = validate(res, "output")
    if probs:
        raise CouncilError(f"not a council result: {probs[0]}", hint="Pass the file written by `evidence-council review --output`.", details=probs)
    if args.json:
        write_json(reading(res), None)
    else:
        sys.stdout.write(_md_safe(render_markdown(res)))
    return EXIT_OK


def _md_safe(s: str) -> str:
    return s


def cmd_providers(args) -> int:
    from .providers import build_adapters, CancelToken
    cfg = _cfg(args)
    adapters = build_adapters(cfg.providers, cfg.defaults, include_disabled=args.include_disabled)
    rows = []
    for a in adapters:
        av = a.check()
        row = {**a.describe(), "available": av.available, "reason": av.reason, "enabled": a.spec.get("enabled", True)}
        if args.probe and av.available:
            a.probe_mode = True
            r = a.generate('Reply with exactly the word PONG and nothing else.', CancelToken(), timeout_s=min(a.timeout_s, 180), retries=0)
            row["probe"] = {"status": r.status, "duration_s": round(r.duration_s, 1), "reply": r.text.strip()[:40],
                            "error": r.error[:200], "tokens": r.tokens}
            row["callable"] = r.status == "ok"
            row["probe"]["followed_instruction"] = r.text.strip().strip('."').upper() == "PONG"
        rows.append(row)
    out = {"config": cfg.source, "probed": bool(args.probe), "providers": rows,
           "callable": [r["name"] for r in rows if r.get("callable", r["available"] if not args.probe else False)]}
    if args.json:
        write_json(out, None)
    else:
        print(f"config: {cfg.source}")
        for r in rows:
            mark = "OK " if r.get("callable", r["available"]) else "-- "
            line = f"{mark} {r['name']:<24} {r['adapter']:<14} {r['locality']:<6} {r['reason']}"
            if "probe" in r:
                pr = r["probe"]
                line += f"\n      probe: {pr['status']} in {pr['duration_s']}s" + (f" reply={pr['reply']!r}" if pr["reply"] else "") + (f" error={pr['error']}" if pr["error"] else "")
            print(line)
        if not args.probe:
            print("\n(availability is a cheap check; use --probe to confirm each provider really answers)")
    return EXIT_OK


def cmd_privacy(args) -> int:
    from .council import init_state
    from .privacy import build_payload, plan_route
    from .providers import build_adapters, select_adapters
    cfg = _cfg(args)
    state = init_state(read_json(args.task), local_only=args.local_only)
    inp = state["input"]
    adapters = build_adapters(cfg.providers, cfg.defaults)
    req = _requested(args)
    selected, excluded = select_adapters(adapters, req if req is not None else inp["providers"]["requested"], inp["providers"]["exclude"])
    routes = []
    payload_out = None
    for a in selected:
        r = plan_route(inp, a.name, a.locality, cfg.private_allowlist())
        if r.allowed:
            pl = build_payload(inp, r)
            if args.show_payload and a.name == args.show_payload:
                payload_out = pl
        routes.append(r.to_dict())
    if args.show_payload and payload_out is None:
        raise CouncilError(f"provider '{args.show_payload}' would receive nothing (excluded or not selected)",
                           hint="See the routing table without --show-payload for the reason.")
    out = payload_out if payload_out is not None else {"level": inp["privacy"]["level"], "local_only": inp["privacy"]["local_only"],
                                                        "findings": state["privacy"]["findings"], "routing": routes, "excluded": excluded}
    write_json(out, None)
    return EXIT_OK


def cmd_validate(args) -> int:
    doc = read_json(args.file)
    kind = args.kind or detect_kind(doc)
    probs = validate(doc, kind)
    write_json({"valid": not probs, "kind": kind, "problems": probs}, None)
    if probs:
        print(f"hint: compare with `evidence-council schema {kind}` and the files in examples/", file=sys.stderr)
    return EXIT_OK if not probs else EXIT_USAGE


def cmd_schema(args) -> int:
    write_json(load_schema(args.kind), None)
    return EXIT_OK


def cmd_lessons(args) -> int:
    from .lessons import LessonStore, check_candidate, load_lesson_and_result
    cfg = _cfg(args)
    store = LessonStore(cfg.memory_dir)
    c = args.lessons_cmd
    if c is None:
        raise CouncilError("missing lessons action", hint="Use one of: list, check, accept, retire (see `evidence-council lessons --help`).")
    if c == "list":
        st = list(store.state().values())
        if not args.all:
            st = [l for l in st if l["status"] == "accepted"]
        write_json({"memory": str(store.log), "memory_version": store.version(), "lessons": st}, None)
        return EXIT_OK
    if c == "retire":
        e = store.retire(args.lesson_id, args.reason)
        write_json({"retired": args.lesson_id, "memory_version": e["seq"]}, None)
        return EXIT_OK
    doc = read_json(args.file)
    lesson, result = load_lesson_and_result(doc, read_json(args.result) if args.result else None)
    rep = check_candidate(lesson, store, result)
    out = rep.to_dict()
    if c == "check":
        write_json(out, None)
        return EXIT_OK if rep.accepted else EXIT_LESSON_REJECTED
    if not rep.accepted:
        write_json({**out, "stored": False}, None)
        print(f"error: lesson rejected; failed checks: {', '.join(out['failed'])}\nhint: see each failed check's detail above; nothing was written to memory",
              file=sys.stderr)
        return EXIT_LESSON_REJECTED
    e = store.accept(lesson, rep)
    write_json({**out, "stored": True, "memory": str(store.log), "memory_version": e["seq"]}, None)
    return EXIT_OK


def cmd_selftest(args) -> int:
    import os
    import tempfile
    from .config import load_config
    from .council import run_review
    from .explain import reading
    fx = resources.files("evidence_council").joinpath("fixtures")
    task = json.loads(fx.joinpath("selftest_task.json").read_text())
    with tempfile.TemporaryDirectory() as td:
        cpath = os.path.join(td, "c.json")
        with open(cpath, "w") as f:
            f.write(fx.joinpath("selftest_config.json").read_text())
        cfg = load_config(cpath, td)
        res = run_review(task, cfg)
    r = reading(res)
    checks = {
        "three mock proposals collected": len(res["proposals"]) == 3,
        "two answer options found": len(res["options"]) == 2,
        "answer disagreement detected": any(d["type"] == "answer" for d in res["disagreements"]),
        "recomputation check ran": res["verification"]["summary"]["run"] >= 2,
        "verdict supported": res["verdict"]["status"] == "supported",
        "minority answer 11398661 selected": res["verdict"]["selected_answer"] == "11398661",
        "popular wrong answer overturned": res["verdict"]["overturned_popular"] is True,
        "participants labelled MOCK": all(p.endswith("(MOCK)") for p in r["participants"]),
        "candidate lesson proposed (not accepted)": (res.get("candidate_lesson") or {}).get("status") == "candidate",
    }
    for k, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {k}")
    ok = all(checks.values())
    print("selftest: " + ("ok (offline, mock providers only)" if ok else "FAILED"))
    return EXIT_OK if ok else EXIT_INTERNAL


def cmd_jobs(args) -> int:
    from . import jobs
    from .errors import EXIT_PENDING
    from .explain import render_markdown
    cfg = _cfg(args)
    c = args.jobs_cmd
    if c is None:
        raise CouncilError("missing jobs action", hint="Use one of: list, status, result, cancel, remove (see `evidence-council jobs --help`).")
    if c == "list":
        write_json({"jobs": jobs.list_jobs(cfg.home)}, None)
        return EXIT_OK
    if c == "status":
        write_json(jobs.status(cfg.home, args.job_id), None)
        return EXIT_OK
    if c == "cancel":
        write_json(jobs.cancel(cfg.home, args.job_id), None)
        return EXIT_OK
    if c == "remove":
        write_json(jobs.remove(cfg.home, args.job_id), None)
        return EXIT_OK
    st = jobs.status(cfg.home, args.job_id)
    if st["status"] == jobs.RUNNING:
        print(f"error: job {args.job_id} is still running ({st.get('elapsed_s', '?')}s elapsed)\n"
              f"hint: try again later: evidence-council jobs result {args.job_id}", file=sys.stderr)
        return EXIT_PENDING
    if not st.get("result"):
        tail = jobs.log_tail(cfg.home, args.job_id)
        raise CouncilError(f"job {args.job_id} {st['status']} without a result: {st.get('error', '')}",
                           hint="Log tail:\n" + tail if tail else "Re-run the review in the foreground to see the error.",
                           exit_code=st.get("exit_code") or EXIT_INTERNAL)
    res = read_json(st["result"])
    if st["status"] == jobs.CANCELLED:
        print(f"[evidence-council] job {args.job_id} was cancelled; this is a partial result", file=sys.stderr)
    if args.output:
        write_json(res, args.output)
    if args.explain:
        sys.stdout.write(render_markdown(res))
    elif not args.output:
        write_json(res, None)
    return EXIT_OK


def cmd_codes(args) -> int:
    write_json({str(k): v for k, v in EXIT_CODES.items()}, None)
    return EXIT_OK


COMMANDS = {"review": cmd_review, "propose": cmd_stage, "compare": cmd_stage, "verify": cmd_stage, "verdict": cmd_stage,
            "explain": cmd_explain, "providers": cmd_providers, "privacy": cmd_privacy, "validate": cmd_validate,
            "schema": cmd_schema, "lessons": cmd_lessons, "selftest": cmd_selftest, "codes": cmd_codes,
            "jobs": cmd_jobs}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "_run-job":  # internal: detached runner for `review --async`
        from . import jobs
        return jobs.run_job(argv[1])
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.tool:
        ap.print_help()
        return EXIT_USAGE
    try:
        return COMMANDS[args.tool](args)
    except CouncilError as e:
        if getattr(args, "error_format", "text") == "json":
            print(json.dumps(e.to_dict()), file=sys.stderr)
        else:
            print(f"error: {e.message}", file=sys.stderr)
            if e.hint:
                print(f"hint: {e.hint}", file=sys.stderr)
            for d in e.details[:10]:
                print(f"  - {d}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        print("error: aborted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return EXIT_OK
    except Exception as e:  # pragma: no cover - bug path
        print(f"error: internal error: {type(e).__name__}: {e}\nhint: re-run with --verbose and report the traceback", file=sys.stderr)
        if getattr(args, "verbose", False):
            traceback.print_exc()
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
