"""`evidence-council agent ...` — the coding agent's command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

from ..errors import CouncilError, EXIT_OK, EXIT_USAGE
from ..util import read_json, write_json

AGENT_EPILOG = textwrap.dedent("""
    workflow:
      1. intake    turn a plain-language request into a checklist (reads the project; changes nothing, runs nothing)
      2. show      re-print the checklist after you edit plan.json
      3. approve   you approve the exact plan (typed confirmation); edits afterwards cancel the approval
      4. run       Builder -> Evidence checker -> Critic (loop) -> Optimizer -> Final evaluator, on a copy/branch
      5. report    print the evidence report again

    examples:
      evidence-council agent providers
      evidence-council agent intake --project ~/code/shop "people who buy more than 10 should get 10% off"
      evidence-council agent intake --project ~/code/shop --setup journeys.json "..."
      evidence-council agent approve ~/.local/share/evidence-council/agent-runs/agent-20261116T101500Z-1a2b3c
      evidence-council agent run ~/.local/share/evidence-council/agent-runs/agent-20261116T101500Z-1a2b3c --max-cost 0.25
      evidence-council agent report RUN_DIR --json

    Runs need an API key in the environment (default provider: Gemini, GEMINI_API_KEY).
    --script FILE uses the offline scripted test model instead (no AI, for tests and demos).
    Nothing is deployed, committed or pushed; commands not in the approved plan never run.
    """).strip("\n")


def add_agent_parser(sub, common) -> None:
    p = sub.add_parser("agent", help="evidence-checked coding agent: intake / approve / run / report",
                       description="An evidence-checked coding agent that changes a COPY (or new branch) of your project only "
                                   "after you approve a checklist of requirements, commands and customer journeys.",
                       epilog=AGENT_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    common(p)
    asub = p.add_subparsers(dest="agent_cmd", metavar="ACTION")

    def model_opts(q):
        q.add_argument("--provider", choices=["gemini", "openai", "anthropic"], help="model provider (default from config: gemini)")
        q.add_argument("--model", help="model id for the provider (default from config)")
        q.add_argument("--script", metavar="FILE", help="use the offline scripted test model (no AI; tests/demos)")

    q = asub.add_parser("providers", help="show the configured model provider and whether it is ready (no request sent)"); common(q)
    q.add_argument("--json", action="store_true")
    model_opts(q)

    q = asub.add_parser("intake", help="request -> checklist + plan.json in a new run folder (changes nothing)"); common(q)
    q.add_argument("request", nargs="?", help="what you want, in your own words")
    q.add_argument("--request-file", metavar="FILE", help="read the request from a text file")
    q.add_argument("--project", required=True, metavar="DIR", help="your project folder (read only during intake)")
    q.add_argument("--setup", metavar="FILE", help="JSON with service/personas/load/optimizer/limits/security/project_mode")
    q.add_argument("--plan-file", metavar="FILE", help="use a hand-written plan instead of the AI Intake agent")
    q.add_argument("--max-cost", type=float, help="spend limit for intake in USD (default: plan limit)")
    model_opts(q)

    q = asub.add_parser("show", help="print the checklist of a run folder (re-validates plan.json)"); common(q)
    q.add_argument("run_dir")

    q = asub.add_parser("approve", help="approve the exact checklist (interactive, or --confirm FINGERPRINT)"); common(q)
    q.add_argument("run_dir")
    q.add_argument("--confirm", metavar="FINGERPRINT", help="the 12-character fingerprint printed with the checklist; "
                   "only pass it after the person has read and approved the checklist")

    q = asub.add_parser("run", help="run the approved plan"); common(q)
    q.add_argument("run_dir")
    q.add_argument("--max-cost", type=float, help="lower the spend limit (USD); cannot raise the approved one")
    q.add_argument("--max-calls", type=int, help="lower the model-call limit")
    q.add_argument("--max-minutes", type=float, help="lower the time limit")
    q.add_argument("--no-lessons", action="store_true", help="do not store lessons from this run")
    model_opts(q)

    q = asub.add_parser("report", help="print the evidence report of a finished run"); common(q)
    q.add_argument("run_dir")
    q.add_argument("--json", action="store_true")


def _model(args, cfg):
    from .models import build_model
    return build_model(cfg.agent, getattr(args, "provider", None), getattr(args, "model", None), getattr(args, "script", None))


def cmd_agent(args, load_cfg) -> int:
    from . import plan as P
    from . import runner
    from .budget import Limits
    from .events import Reporter
    cfg = load_cfg(args)
    action = getattr(args, "agent_cmd", None)
    if not action:
        raise CouncilError("missing action", hint="Use: evidence-council agent {providers,intake,show,approve,run,report} --help", exit_code=EXIT_USAGE)
    if action == "providers":
        from .models import ADAPTERS, build_model
        rows = []
        for name in ADAPTERS:
            m = build_model(cfg.agent, name, args.model if args.provider == name else None)
            rows.append({**m.describe(), "default": name == cfg.agent.get("provider", "gemini")})
        if args.json:
            write_json({"providers": rows, "config": cfg.source}, None)
        else:
            for r in rows:
                mark = "ready" if r["available"] else "not ready"
                print(f"{'*' if r['default'] else ' '} {r['provider']:<10} {r['model'] or '(no model set)':<22} {mark}: {r['reason']}")
                print(f"             price/1M tokens: in ${r['price_per_1m']['input']}, out ${r['price_per_1m']['output']} ({r['price_source']})")
            print("\n* = default provider. Status checks send no request. Set keys with e.g.: export GEMINI_API_KEY=...")
        return EXIT_OK
    if action == "intake":
        request = args.request
        if args.request_file:
            request = Path(args.request_file).read_text(encoding="utf-8").strip()
        if not request and not args.plan_file:
            raise CouncilError("no request given", hint="Pass the request in quotes, or --request-file FILE.", exit_code=EXIT_USAGE)
        model = _model(args, cfg)
        ok, why = model.check()
        if not ok and not args.plan_file:
            raise CouncilError(f"the {model.provider} model is not ready: {why}",
                               hint="Set the API key environment variable (see `evidence-council agent providers`), or pass --plan-file.",
                               exit_code=EXIT_USAGE)
        lim = Limits(max_cost_usd=args.max_cost) if args.max_cost else None
        run_dir, _ = runner.intake(request or "", Path(args.project), model, cfg.home, Reporter(), args.setup, args.plan_file, lim)
        print(f"\nrun folder: {run_dir}")
        return EXIT_OK
    run_dir = Path(args.run_dir).expanduser().resolve()
    if action == "show":
        plan = P.load_plan(run_dir)
        print(P.render_checklist(plan, run_dir))
        probs = P.lint(plan)
        for pr in probs:
            print(f"! {pr}")
        approved = (run_dir / "approval.json").exists()
        if approved:
            rec = read_json(run_dir / "approval.json")
            same = rec.get("plan_fingerprint") == P.fingerprint(run_dir, plan)
            print(f"\napproval: {'valid' if same else 'NO LONGER VALID (plan changed after approval)'} ({rec.get('approved_at')})")
        else:
            print("\napproval: not approved yet")
        return EXIT_OK if not probs else EXIT_USAGE
    if action == "approve":
        from .journeys import write_scripts
        plan = P.load_plan(run_dir)
        write_scripts(plan, run_dir / "personas")
        fp = P.fingerprint(run_dir, plan)[7:19]
        if args.confirm:
            if args.confirm.strip() != fp:
                raise CouncilError("fingerprint does not match the current plan", hint=f"The plan changed or the value was mistyped. Re-read the checklist (agent show {run_dir}).",
                                   exit_code=EXIT_USAGE)
            method = "fingerprint confirmation (--confirm)"
        else:
            print(P.render_checklist(plan, run_dir))
            probs = P.lint(plan)
            if probs:
                raise CouncilError("the plan breaks a safety rule and cannot be approved", details=probs, exit_code=EXIT_USAGE)
            if not sys.stdin.isatty():
                raise CouncilError("approval needs a person: no terminal is attached",
                                   hint=f"Run this in a terminal, or pass --confirm {fp} after the person has approved the checklist.", exit_code=EXIT_USAGE)
            ans = input(f"\nType 'approve' to approve this exact plan ({fp}); anything else cancels: ").strip().lower()
            if ans != "approve":
                print("not approved; nothing will run")
                return EXIT_USAGE
            method = "typed confirmation in a terminal"
        rec = P.approve(run_dir, plan, method)
        print(f"approved {rec['plan_fingerprint'][7:19]} at {rec['approved_at']}. Next: evidence-council agent run {run_dir}")
        return EXIT_OK
    if action == "run":
        model = _model(args, cfg)
        ok, why = model.check()
        if not ok:
            raise CouncilError(f"the {model.provider} model is not ready: {why}", hint="See `evidence-council agent providers`.", exit_code=EXIT_USAGE)
        over = {"max_cost_usd": args.max_cost, "max_model_calls": args.max_calls,
                "max_wall_s": int(args.max_minutes * 60) if args.max_minutes else None}
        rep = runner.run(run_dir, model, cfg.home, Reporter(), over, save_lessons=not args.no_lessons)
        return EXIT_OK if rep["outcome"] == "completed" and not rep.get("counts", {}).get("failed") else 9
    if action == "report":
        rep = runner.load_report(run_dir)
        if args.json:
            write_json(rep, None)
        else:
            print(runner.render_report(rep))
        return EXIT_OK
    raise CouncilError(f"unknown action {action}", exit_code=EXIT_USAGE)


def _unused() -> None:  # keep json import for future --json outputs
    _ = json
