"""CLI: python -m jobbank run [--source name ...] [--no-digest]
      python -m jobbank doctor
      python -m jobbank health [--source name ...]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .settings import Settings


def doctor(settings: Settings) -> int:
    checks = {
        "ADZUNA_APP_ID/KEY": bool(settings.env("ADZUNA_APP_ID") and settings.env("ADZUNA_APP_KEY")),
        "REED_API_KEY": bool(settings.env("REED_API_KEY")),
        "ANTHROPIC_API_KEY (LLM scoring)": bool(settings.env("ANTHROPIC_API_KEY")),
        "SHEET_ID (Google Sheet)": bool(settings.env("SHEET_ID")),
        "Google service account": bool(settings.env("GOOGLE_SERVICE_ACCOUNT_FILE")
                                       or settings.env("GOOGLE_SERVICE_ACCOUNT_JSON")),
        "Digest (RESEND_API_KEY or SMTP_HOST)": bool(settings.env("RESEND_API_KEY")
                                                     or settings.env("SMTP_HOST")),
    }
    for name, ok in checks.items():
        print(f"  {'OK ' if ok else '-- '} {name}" + ("" if ok else "  (missing -> feature degrades to skip/dry-run)"))
    print(f"\n  config: cap={settings.weekly_cap} min_score={settings.min_score} "
          f"priority>={settings.priority_score} freshness={settings.freshness_days}d")
    print(f"  watchlist companies: {len(settings.watchlist)}")
    return 0


def _llm_health(settings: Settings) -> tuple[bool, str]:
    """One tiny call to the configured scoring model.

    Costs about two thousandths of a penny, and is the only thing that would have
    caught the 2026-08-25 credit outage: the pipeline treats a dead LLM as a soft
    failure, scores everything with the cheap keyword heuristic and exits 0, so
    nothing anywhere said the expensive half of the pipeline had stopped working.

    A missing key is a skip, not a failure — a profile may legitimately run
    heuristic-only.
    """
    key = settings.env("ANTHROPIC_API_KEY")
    if not key:
        return True, "skipped: no ANTHROPIC_API_KEY set"
    import anthropic
    model = settings.cfg["scorer"]["model"]
    try:
        anthropic.Anthropic(api_key=key).messages.create(
            model=model, max_tokens=4,
            messages=[{"role": "user", "content": "ping"}])
        return True, f"{model} answered"
    except Exception as exc:  # noqa: BLE001 — anything here is a real failure
        return False, f"{type(exc).__name__}: {exc}"


def health(settings: Settings, only: list[str] | None = None) -> int:
    """Ask every source for jobs and report which ones are broken.

    The fetch phase and nothing else: no scoring, no LLM spend, no sheet writes.
    That makes it cheap enough to run on a schedule, which is the point — a
    source that quietly stops answering costs nothing today and everything by
    the time you notice the bank has gone thin.

    Exit code is what a scheduler reads: non-zero when a source raised. A source
    returning zero jobs is reported but NOT a failure — a thin day is normal,
    and failing on it would train you to ignore the alert.
    """
    from .fetchers import run_all

    result = run_all(settings, only=only)
    empty = [n for n, c in result.per_source.items()
             if c == 0 and n not in result.failures]
    total = sum(result.per_source.values())
    print(f"\n  {len(result.per_source)} source(s) checked, {total} job(s) seen")
    if empty:
        print(f"  no jobs (not necessarily broken): {', '.join(sorted(empty))}")
    llm_ok, llm_detail = _llm_health(settings)
    print(f"  LLM scoring: {'ok — ' if llm_ok else 'FAILED — '}{llm_detail}")
    if not llm_ok:
        print("    Every job would fall back to keyword scoring, and none of it "
              "would be eligible for the Job Bank.")

    if result.failures:
        print(f"\n  {len(result.failures)} SOURCE(S) FAILED:")
        for name, err in sorted(result.failures.items()):
            print(f"    {name}: {err}")
    elif llm_ok:
        print("  no source failures")
    return 0 if (llm_ok and not result.failures) else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="jobbank")
    sub = parser.add_subparsers(dest="cmd", required=True)
    runp = sub.add_parser("run", help="execute the weekly pipeline")
    runp.add_argument("--source", action="append",
                      help="run only these fetchers (repeatable)")
    runp.add_argument("--no-digest", action="store_true")
    runp.add_argument("--root", help="profile directory (default: repo root)")
    doctorp = sub.add_parser("doctor", help="check configured credentials")
    doctorp.add_argument("--root", help="profile directory (default: repo root)")
    healthp = sub.add_parser("health",
                             help="fetch from every source and report which are broken")
    healthp.add_argument("--root", help="profile directory (default: repo root)")
    healthp.add_argument("--source", action="append",
                         help="check only these fetchers (repeatable)")
    formatp = sub.add_parser("format", help="(re)apply coloured dropdowns + backfill timing on the sheet")
    formatp.add_argument("--root", help="profile directory (default: repo root)")
    backp = sub.add_parser("backfill-descriptions",
                           help="fill empty Description cells from the live postings")
    backp.add_argument("--root", help="profile directory (default: repo root)")
    backp.add_argument("--limit", type=int, default=200,
                       help="max rows to fetch per tab (default 200)")
    sugp = sub.add_parser("suggest-filters",
                          help="propose hard-filter rules from your removal feedback")
    sugp.add_argument("--root", help="profile directory (default: repo root)")
    sub.add_parser("mcp", help="serve the job bank as MCP tools over stdio "
                               "(needs the [mcp] extra; profiles are per tool call)")
    ragp = sub.add_parser("rag", help="retrieval layer: snapshot | build | search | stats | eval "
                                      "(needs the [rag] extra)")
    ragp.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.cmd == "rag":
        from .rag.cli import main as rag_main
        return rag_main(args.rest)

    if args.cmd == "mcp":
        from .mcp_server import mcp
        mcp.run()
        return 0

    settings = Settings(root=Path(args.root) if args.root else None)
    if args.cmd == "doctor":
        return doctor(settings)
    if args.cmd == "health":
        return health(settings, only=args.source)
    if args.cmd == "format":
        from .sheets import make_backend
        backend = make_backend(settings)
        if backend.mode != "google sheet":
            print("format needs a live Google Sheet (set SHEET_ID); local xlsx is dry-run only")
            return 1
        backend.ensure_layout(settings.watchlist)
        from .sheet_format import (apply_job_bank_formatting, backfill_timing,
                                   ensure_schema, hide_utility_tabs,
                                   repair_bank_rows, complete_bank_rows)
        ensure_schema(backend.sh)
        apply_job_bank_formatting(backend.sh)
        n = backfill_timing(backend.sh)
        hide_utility_tabs(backend.sh)
        complete_bank_rows(backend.sh, settings)
        repair_bank_rows(backend.sh)
        print(f"formatting applied; timing backfilled on {n} rows")
        return 0
    if args.cmd == "backfill-descriptions":
        from .backfill import backfill_descriptions
        from .sheets import make_backend
        backend = make_backend(settings)
        if backend.mode != "google sheet":
            print("backfill needs a live Google Sheet (set SHEET_ID)")
            return 1
        stats = backfill_descriptions(backend.sh, limit=args.limit)
        print(f"backfill: {stats['filled']} filled of {stats['attempted']} attempted "
              f"({stats['scanned']} rows scanned)")
        return 0
    if args.cmd == "suggest-filters":
        from .sheets import make_backend
        from .suggest import report
        backend = make_backend(settings)
        rows = backend.read_feedback(limit=200)
        print(f"{len(rows)} feedback row(s) on record")
        report(rows, settings)
        return 0
    from .pipeline import run
    return run(settings, only_sources=args.source, skip_digest=args.no_digest)


if __name__ == "__main__":
    sys.exit(main())
