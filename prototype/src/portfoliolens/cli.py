"""Command-line interface: the same pipeline as the API, with the human gates as explicit steps."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

from portfoliolens.backtest import SOURCES, run_backtest, write_report
from portfoliolens.config import PROJECT_ROOT, Settings
from portfoliolens.domain import Funding, Mandate, MandateProposal, Run
from portfoliolens.engine import q2
from portfoliolens.errors import InputValidationError, PortfolioLensError
from portfoliolens.pipeline import Coordinator


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except InputValidationError as exc:
        print("Input validation failed:", file=sys.stderr)
        for issue in exc.issues:
            where = f" row {issue.row}" if issue.row else ""
            print(f"  - [{issue.code}]{where}: {issue.message}", file=sys.stderr)
        return 1
    except PortfolioLensError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"Invalid JSON input: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"File error: {exc}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portfoliolens", description="SEBI equity mandate-compliance prototype")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="run a compliance check (Gate 1 asks you to confirm the mandate)")
    check.add_argument("--holdings", type=Path, required=True)
    check.add_argument("--sid", type=Path, required=True)
    check.add_argument("--as-of", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    check.add_argument("--reviewer", default=os.getenv("USERNAME") or os.getenv("USER") or "")
    check.add_argument("--mandate", type=Path, help="reviewer-corrected mandate JSON (replaces the proposal)")
    check.add_argument("--yes", action="store_true", help="confirm the proposed mandate after reviewing it")
    check.set_defaults(handler=_cmd_check)

    sign = commands.add_parser("sign-off", help="Gate 2 + 3: acknowledge review items and sign a run")
    sign.add_argument("run_id")
    sign.add_argument("--reviewer", required=True)
    sign.add_argument("--decision", choices=["approved", "escalated"], default="approved")
    sign.add_argument("--ack", nargs="*", default=[], help="review ids you have examined, e.g. RV-01 RV-02")
    sign.add_argument("--note", default="")
    sign.set_defaults(handler=_cmd_sign_off)

    ask = commands.add_parser("ask", help="ask a grounded question about a run")
    ask.add_argument("run_id")
    ask.add_argument("question")
    ask.set_defaults(handler=_cmd_ask)

    screen = commands.add_parser("screen", help="P1: which candidates can this fund buy without a new breach?")
    screen.add_argument("run_id")
    screen.add_argument("--candidates", type=Path, required=True, help="CSV with columns isin,name")
    screen.add_argument("--weight", type=_decimal, default=Decimal("2"), help="% of NAV to buy (default 2)")
    screen.add_argument("--funding", choices=[f.value for f in Funding], default=Funding.PRO_RATA.value)
    screen.add_argument("--sort-by", choices=["headroom", "name"], default="headroom")
    screen.set_defaults(handler=_cmd_screen)

    whatif = commands.add_parser("whatif", help="P1: simulate trades and compare compliance before/after")
    whatif.add_argument("run_id")
    whatif.add_argument("--trades", type=Path, required=True, help="CSV with columns action,isin,name,pct_of_nav")
    whatif.add_argument("--funding", choices=[f.value for f in Funding], default=Funding.CASH.value)
    whatif.set_defaults(handler=_cmd_whatif)

    backtest = commands.add_parser("backtest", help="run every case in data/cases and write a report")
    backtest.add_argument("--cases", type=Path, default=PROJECT_ROOT / "data" / "cases")
    backtest.add_argument("--source", choices=[*SOURCES, "both"], default="both")
    backtest.set_defaults(handler=_cmd_backtest)

    serve = commands.add_parser("serve", help="start the HTTP API (docs at /docs)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(handler=_cmd_serve)
    return parser


def _decimal(raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"'{raw}' is not a number") from exc


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _coordinator() -> Coordinator:
    return Coordinator.from_settings(Settings.from_env())


# --- commands ---------------------------------------------------------------------------


def _cmd_check(args: argparse.Namespace) -> int:
    if not args.reviewer.strip():
        print("Gate 1 needs a reviewer name: pass --reviewer.", file=sys.stderr)
        return 2
    coordinator = _coordinator()
    holdings_csv, sid_text = _read(args.holdings), _read(args.sid)
    summary = coordinator.validate_inputs(holdings_csv, sid_text)
    print(f"Inputs valid: {summary.rows} rows, net assets {summary.net_assets}.")

    proposal = coordinator.propose_mandate(sid_text)
    _print_proposal(proposal)
    corrected = Mandate.model_validate_json(_read(args.mandate)) if args.mandate else None
    if corrected is None and not args.yes:
        if not sys.stdin.isatty():
            print("Gate 1 needs a human: re-run with --yes after reviewing, or pass --mandate FILE.", file=sys.stderr)
            return 2
        try:
            answer = input("Gate 1: confirm this mandate? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            path = coordinator.store.path_for("proposals", proposal.proposal_id)
            print(f"Not confirmed. Correct the mandate as JSON and re-run with --mandate. Proposal: {path}")
            return 1

    confirmed = coordinator.confirm_mandate(proposal.proposal_id, args.reviewer, corrected, acknowledge_issues=True)
    run = coordinator.run_check(confirmed.mandate_id, holdings_csv, args.as_of)
    _print_run(run, coordinator)
    return 0


def _cmd_sign_off(args: argparse.Namespace) -> int:
    run = _coordinator().sign_off(args.run_id, args.reviewer, args.decision, list(args.ack), args.note)
    assert run.sign_off is not None
    print(
        f"Run {run.run_id} signed off by {run.sign_off.reviewer} ({run.sign_off.decision}) at {run.sign_off.signed_at}."
    )
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    answer = _coordinator().explain(args.run_id, args.question)
    print(answer.text)
    if answer.cited_ids:
        print(f"\nCited: {', '.join(answer.cited_ids)}  (source: {answer.source})")
    return 0


def _cmd_screen(args: argparse.Namespace) -> int:
    result = _coordinator().screen(
        args.run_id, _read(args.candidates), args.weight, Funding(args.funding), args.sort_by
    )
    print(f"Screening for run {result.run_id}: buy {result.weight_pct}% of NAV, funded by {result.funding.value}.")
    for candidate in result.candidates:
        verdict = "ELIGIBLE " if candidate.eligible else "EXCLUDED "
        bucket = candidate.bucket.value if candidate.bucket else "?"
        headroom = (
            f"headroom {candidate.post_trade_headroom_pp:+} pp" if candidate.post_trade_headroom_pp is not None else ""
        )
        print(f"  {verdict} {candidate.name} [{bucket}] {headroom}")
        if not candidate.eligible:
            for reason in candidate.reasons:
                print(f"      - {reason}")
    print(f"\n{result.note}")
    return 0


def _cmd_whatif(args: argparse.Namespace) -> int:
    result = _coordinator().what_if(args.run_id, _read(args.trades), Funding(args.funding))
    print(f"What-if for run {result.run_id}: {result.before_status.value} -> {result.after_status.value}")
    for delta in result.deltas:
        marker = "  !" if delta.worsened else "   "
        print(
            f"{marker} {delta.constraint_id}: {delta.before_status.value} ({q2(delta.before_margin):+}) -> "
            f"{delta.after_status.value} ({q2(delta.after_margin):+})"
        )
    print(f"New breaches: {result.new_breaches or 'none'}; resolved: {result.resolved_breaches or 'none'}")
    print(f"\n{result.note}")
    return 0


def _cmd_backtest(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    out_dir = settings.output_dir / "backtest"
    coordinator = Coordinator.from_settings(settings, store_dir=out_dir / "store")
    sources = SOURCES if args.source == "both" else (args.source,)
    report = run_backtest(coordinator, args.cases, settings.llm_label, sources)
    path = write_report(report, out_dir)
    for source in sources:
        passed, total = report.passed(source)
        print(f"{source:>12}: {passed}/{total} cases passed")
    if "extracted" in sources:
        correct, total = report.extraction_accuracy()
        print(f"  extraction: {correct}/{total} field checks correct")
    for outcome in report.outcomes:
        if not outcome.passed:
            print(f"  FAIL case {outcome.case_id} ({outcome.source}): {outcome.title}")
    print(f"Report: {path}")
    engine_failures = [o for o in report.outcomes if o.source == "ground_truth" and not o.passed]
    return 1 if engine_failures else 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import socket

    import uvicorn

    from portfoliolens.api import create_app

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if probe.connect_ex((args.host, args.port)) == 0:
            print(
                f"Port {args.port} is already in use (an earlier server may still be running). "
                f"Stop it, or pick another port with --port.",
                file=sys.stderr,
            )
            return 1
    print(f"PortfolioLens: http://{args.host}:{args.port}/   API docs: /docs   (Ctrl+C to stop)")
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


# --- printing ---------------------------------------------------------------------------


def _print_proposal(proposal: MandateProposal) -> None:
    def quoted(citation: object) -> str:
        quote = getattr(citation, "quote", None)
        return f'   <- "{quote}"' if quote else ""

    print(f"\nGATE 1: proposed mandate (extractor: {proposal.extractor})")
    print(f"  scheme    : {proposal.scheme_name or '-'}")
    print(f"  category  : {proposal.category.value if proposal.category else '-'}{quoted(proposal.category_citation)}")
    print(f"  benchmark : {proposal.benchmark or '-'}")
    print(f"  sector    : {proposal.sector or '-'}")
    print("  self-imposed limits:" + ("" if proposal.self_imposed_limits else " none"))
    for limit in proposal.self_imposed_limits:
        print(f"    - {limit.metric.value} {limit.comparator.value} {limit.threshold}{quoted(limit.citation)}")
    for issue in proposal.issues:
        print(f"  ! {issue}")


def _print_run(run: Run, coordinator: Coordinator) -> None:
    print(f"\nRun {run.run_id}: {run.result.status.value}")
    print("  pinned: " + ", ".join(f"{pin.kind} {pin.version}" for pin in run.pins))
    for finding in run.result.findings:
        print(f"  [{finding.status.value:<13}] {finding.summary}")
    if run.result.review_items:
        print("\nGate 2 review items:")
        for item in run.result.review_items:
            print(f"  {item.review_id} [{item.kind}] {item.detail}")
    print(f"\nReport: {coordinator.store.path_for('runs', run.run_id, '.md')}")
    acks = " ".join(item.review_id for item in run.result.review_items)
    print(f"Sign off: portfoliolens sign-off {run.run_id} --reviewer NAME" + (f" --ack {acks}" if acks else ""))
