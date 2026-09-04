"""Weekly pipeline (spec sec 8):
fetch -> normalize -> dedupe -> hard filters -> LLM score -> volume gate
-> writers (sheet + digest) -> state commit.

Failure policy: any single source may fail without crashing the run; zero jobs
across ALL sources = loud alert (non-zero exit so the Actions run goes red).
"""
from __future__ import annotations

import json
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import fetchers
from .models import Job
from .settings import Settings
from .sheets import make_backend
from .digest import build_digest_html, send_digest
from .state import State
from .volume import company_key, open_slots, volume_gate

# A role you have already applied to is not clutter to gate on — it is your
# pipeline. Only roles still awaiting action count towards the bank's ceiling.
_ACTIONED = {"applied", "oa", "interview", "offer", "rejected"}


def _is_open(row: dict) -> bool:
    return str(row.get("Status") or "").strip().lower() not in _ACTIONED


def _next_monday_7utc() -> str:
    now = datetime.now(timezone.utc)
    days = (7 - now.weekday()) % 7 or 7
    nxt = (now + timedelta(days=days)).replace(hour=7, minute=0, second=0, microsecond=0)
    return nxt.strftime("%Y-%m-%d %H:%M UTC")


def run(settings: Settings, only_sources: list[str] | None = None,
        skip_digest: bool = False) -> int:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M") + "-" + uuid.uuid4().hex[:6]
    started = datetime.now(timezone.utc)
    t0 = time.time()
    print(f"== jobbank run {run_id} ==")

    # Profile-specific scoring/filtering: each candidate gets their own taxonomy,
    # hard filters, and rubric. Selected by settings.profile so this never touches
    # the other profile's live behaviour.
    from .profile_impl import filters_for, scorer_for
    _filters = filters_for(settings)
    FilterStats, apply_hard_filters = _filters.FilterStats, _filters.apply_hard_filters
    score_jobs = scorer_for(settings).score_jobs

    backend = make_backend(settings)
    print(f"sheet backend: {backend.mode}")
    backend.ensure_layout(settings.watchlist)
    if backend.mode == "google sheet":
        try:  # remove any native Table + migrate columns BEFORE appending new rows
            from .sheet_format import ensure_schema
            ensure_schema(backend.sh)
        except Exception as exc:  # noqa: BLE001
            print(f"  [sheet] schema normalise skipped: {exc}")
    overrides = backend.read_config_overrides()
    if overrides:
        settings.apply_sheet_overrides(overrides)
        print(f"config overrides from Stats tab: {overrides}")

    # Publish what this run is actually using, so the sheet (and Sally's board)
    # show real numbers instead of blank "inheriting the config file" cells.
    try:
        backend.write_config_values({
            "weekly_cap": settings.weekly_cap,
            "min_score": settings.min_score,
            "digest_to": settings.digest_to,
            "llm_top_n": settings.cfg.get("scorer", {}).get("llm_top_n", 50),
            "run_every_days": settings.run_every_days,
        })
    except Exception as exc:  # noqa: BLE001 — cosmetic, never fail a run for it
        print(f"  [config] publish skipped: {exc}")

    # Sally's cadence dial: run_every_days > 1 skips whole runs between checks —
    # no fetching, no LLM calls, zero token spend on the skipped days. The cron
    # still fires daily; this is the cheap early-exit. 3h of slack so a cron that
    # drifts a little never turns "every day" into "every other day".
    if settings.run_every_days > 1:
        last = State(settings.state_path).last_run_started()
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                due = last_dt + timedelta(days=settings.run_every_days, hours=-3)
                if datetime.now(timezone.utc) < due:
                    print(f"run skipped: run_every_days={settings.run_every_days}, "
                          f"last run {last[:16]}, next due {due.isoformat()[:16]}")
                    return 0
            except ValueError:
                pass   # unparseable timestamp — run rather than silently stall

    sheet_watchlist = backend.read_watchlist()
    if sheet_watchlist:
        # union: keep the user's sheet edits AND pick up companies newly added to
        # config (otherwise a once-seeded sheet tab silently ignores new watchlist
        # companies, so their roles never get the passion-employer score boost).
        seen_names = {e.get("name", "").lower() for e in sheet_watchlist}
        settings.watchlist = sheet_watchlist + [
            c for c in settings.watchlist if c["name"].lower() not in seen_names]
        new_to_sheet = [c for c in settings.watchlist
                        if c["name"].lower() not in seen_names]
        if new_to_sheet and hasattr(backend, "append_watchlist"):
            backend.append_watchlist(new_to_sheet)

    # 1. fetch (failure-isolated per source)
    print("fetching...")
    fetched = fetchers.run_all(settings, only=only_sources)
    from .enrich import enrich_timing, enrich_location
    for job in fetched.jobs:
        enrich_timing(job)
        enrich_location(job)
    if not fetched.jobs:
        print("!! LOUD ALERT: zero jobs across ALL sources — investigate immediately")
        backend.append_run_log([run_id, started.isoformat(), "", json.dumps(fetched.per_source),
                                0, "", 0, 0, 0, 0, json.dumps(fetched.failures),
                                "ZERO JOBS ACROSS ALL SOURCES"])
        return 2

    state = State(settings.state_path)

    now_open_alerts: list[str] = []
    # 1c. Paste-in tab: user-supplied URLs -> fetch/filter/score/file (sec 3)
    paste_meta = {"processed": 0, "bank": 0, "archive": 0, "jobs": []}
    try:
        from .pastein import process_paste_in
        paste_meta = process_paste_in(backend, settings, state)
    except Exception as exc:  # noqa: BLE001
        print(f"  [paste-in] skipped: {exc}")

    # 2/3. dedupe: against the EXISTING sheet rows (by URL + role signature +
    # fuzzy company/title) AND within-batch + state. Seeding the dedup with what's
    # already in the Job Bank means a role on the sheet can never be re-added, even
    # if its URL changed or the local state file diverged from the cloud cron's.
    threshold = settings.cfg["dedupe"]["fuzzy_threshold"]
    sheet_links = backend.existing_links()
    existing_sigs: set[str] = set()
    existing_texts: list[str] = []
    if hasattr(backend, "read_job_bank"):
        for r in backend.read_job_bank():
            company, role = r.get("Company", ""), r.get("Role", "")
            if company and role:
                stub = Job(source="", company=company, title=role, url=r.get("Link", ""))
                existing_sigs.add(stub.role_signature)
                existing_texts.append(stub.fuzzy_text)
    # Sort before dedup so the representative kept for near-duplicate roles is the
    # best one: watchlist companies first, then UK/EU/remote locations.
    _wl = {c["name"].lower() for c in settings.watchlist}

    def _dedup_priority(j: Job):
        loc = j.location.lower()
        uk_eu = any(k in loc for k in ("london", "united kingdom", "uk", "england",
                                       "scotland", "france", "paris", "remote", "europe",
                                       "germany", "netherlands", "ireland", "spain"))
        from .models import matches_watchlist
        return (not matches_watchlist(j.company, _wl), not uk_eu)  # False (0) sorts first

    fetched.jobs.sort(key=_dedup_priority)

    from rapidfuzz import fuzz, process
    fresh: list[Job] = []
    batch_keys: set[str] = set()
    batch_texts: list[str] = list(existing_texts)   # seed with what's on the sheet
    batch_sigs: set[str] = set(existing_sigs)        # collapse variants + sheet rows
    dupes = 0
    for job in fetched.jobs:
        if (job.dedupe_key in batch_keys or job.norm_url in sheet_links
                or job.role_signature in batch_sigs
                or state.is_seen_exact(job) or state.is_seen_fuzzy(job, threshold)
                or (batch_texts and process.extractOne(
                    job.fuzzy_text, batch_texts, scorer=fuzz.token_sort_ratio,
                    score_cutoff=threshold))):
            dupes += 1
            continue
        batch_keys.add(job.dedupe_key)
        batch_texts.append(job.fuzzy_text)
        batch_sigs.add(job.role_signature)
        fresh.append(job)
    print(f"dedupe: {len(fetched.jobs)} fetched -> {len(fresh)} new ({dupes} duplicates)")

    # 4. hard filters
    fstats = FilterStats()
    survivors, rejected = apply_hard_filters(
        fresh, settings.freshness_days, fstats,
        company_rules=settings.cfg.get("company_rules"))
    print(f"hard filters: {len(survivors)} passed, {len(rejected)} discarded {fstats.counts}")
    for job, reason in rejected:
        state.record(job, "filtered", reason)

    # 5. score — with whatever the dashboard has learned about the candidate's
    # taste since the last run (see feedback.py; context only, never a filter)
    try:
        settings.feedback_rows = backend.read_feedback()
        if settings.feedback_rows:
            print(f"feedback: {len(settings.feedback_rows)} recent signal(s) fed to the scorer")
    except Exception as exc:  # noqa: BLE001 — never fail a run over the feedback tab
        settings.feedback_rows = []
        print(f"  [feedback] skipped: {exc}")
    score_meta = score_jobs(survivors, settings)
    # the LLM fills duration/start from prose; re-run the regex enricher for any
    # it left null so those columns are never blank in the sheet
    for job in survivors:
        enrich_timing(job)
    scored = [j for j in survivors if j.score is not None]
    print(f"scored {len(scored)} jobs via {score_meta['mode']} "
          f"(est ${score_meta['est_cost_usd']})")

    # 5b. Opening Soon sync — AFTER scoring, on purpose. The confirmation note
    # carries the found posting's location and score onto the dashboard row, and
    # before this ran at step 1b every confirmation was unscored by construction.
    # A hit that was deduped or filtered out never met the scorer, so those are
    # scored one-off here: Aug asked why the OPEN NOW rows had no score, and the
    # answer was "because the evidence pass ran too early".
    try:
        from .opening_soon import load_seed, sync_opening_soon
        from .fetchers import trackr as trackr_mod
        extra = trackr_mod.opening_soon(settings) if (not only_sources or "trackr" in only_sources) else []

        def _score_one(job: Job):
            score_jobs([job], settings)
            return job.score

        added_soon, now_open_alerts = sync_opening_soon(
            backend, load_seed(settings.root), extra, jobs=fetched.jobs,
            score_job=_score_one)
        print(f"opening soon: +{added_soon} tracked, {len(now_open_alerts)} flagged NOW OPEN")
    except Exception as exc:  # noqa: BLE001 — never fail the run on Opening Soon
        print(f"  [opening_soon] skipped: {exc}")

    # 6. volume gate — sized against the bank itself, not just the rate of new
    # roles. A weekly cap alone never looks at how many roles are already sitting
    # there unread, which is how the bank reached 31 open rows.
    bank_rows = backend.read_job_bank()
    open_rows = [r for r in bank_rows if _is_open(r)]
    company_counts: dict[str, int] = {}
    for r in open_rows:
        key = company_key(r.get("Company", ""))
        company_counts[key] = company_counts.get(key, 0) + 1
    headroom = max(0, settings.bank_ceiling - len(open_rows))
    # Whatever space is under the ceiling is offered every run; min_score decides
    # whether anything deserves it. Runs on quiet days simply add nothing.
    slots = open_slots(len(open_rows), settings.bank_ceiling)
    # a role closing within the week is allowed past the ceiling (see volume.py)
    today = datetime.now(timezone.utc).date()
    for job in scored:
        job.extra["urgent"] = bool(job.deadline and 0 <= (job.deadline - today).days <= 7)
    already = state.banked_since(7)
    remaining_cap = max(0, settings.weekly_cap - already)
    slots = min(slots, remaining_cap)
    print(f"volume gate: {len(open_rows)} open in the bank (ceiling "
          f"{settings.bank_ceiling}) -> {slots} slot(s) available, "
          f"score >= {settings.min_score} to use one; "
          f"{already} banked in the last 7 days")
    _wl_names = {c["name"].lower() for c in settings.watchlist}
    gate = volume_gate(scored, slots, settings.min_score, settings.priority_score,
                       watchlist=_wl_names,
                       watchlist_relief=settings.watchlist_min_relief,
                       headroom=headroom, max_per_company=settings.max_per_company,
                       company_counts=company_counts,
                       overflow=settings.bank_overflow,
                       admit_all=settings.admit_all_qualifying)
    print(f"volume gate: {len(gate.bank)} -> Job Bank "
          f"({sum(j.priority for j in gate.bank)} priority), {len(gate.archive)} -> Archive")

    # 6b. verify links on the rows about to be banked — Aug should never click
    # through to a dead posting. Only 404/410 drops a row (see linkcheck).
    from .linkcheck import verify_jobs
    live_bank, dead_links = verify_jobs(gate.bank)
    if dead_links:
        print(f"link check: dropped {len(dead_links)} dead posting(s): "
              + "; ".join(f"{j.company} ({why})" for j, why in dead_links))
        gate.archive.extend(j for j, _ in dead_links)
    gate.bank = live_bank

    # 7. writers
    backend.append_job_bank(gate.bank)
    backend.append_archive(
        [(j, f"score {j.score} — bank full or company cap" if (j.score or 0) >= settings.min_score
          else f"score {j.score} — below MIN_SCORE") for j in gate.archive])
    for job in gate.bank:
        state.record(job, "bank", job.score_reason)
    for job in gate.archive:
        state.record(job, "archive", job.score_reason)

    scores = [j.score for j in scored] or [0]
    finished = datetime.now(timezone.utc)
    stats_row = [run_id, started.date().isoformat(), json.dumps(fetched.per_source),
                 len(fresh), json.dumps(fstats.counts), len(scored),
                 round(statistics.mean(scores), 1), statistics.median(scores),
                 len(gate.bank), len(gate.archive),
                 sum(j.priority for j in gate.bank),
                 len([s for s, n in fetched.per_source.items() if n > 0]),
                 json.dumps(fetched.failures) if fetched.failures else "",
                 score_meta["est_cost_usd"], _next_monday_7utc()]
    backend.append_stats_run(stats_row)
    backend.append_run_log([run_id, started.isoformat(), finished.isoformat(),
                            json.dumps(fetched.per_source), len(fresh),
                            json.dumps(fstats.counts), len(scored), len(gate.bank),
                            len(gate.archive), sum(j.priority for j in gate.bank),
                            json.dumps(fetched.failures) if fetched.failures else "",
                            f"scoring mode: {score_meta['mode']}"])

    # coloured dropdowns + colour rules (Google backend only; safe no-op elsewhere)
    if backend.mode == "google sheet":
        try:
            from .sheet_format import (apply_job_bank_formatting, backfill_timing,
                                       hide_utility_tabs, repair_bank_rows,
                                       complete_bank_rows)
            apply_job_bank_formatting(backend.sh)
            backfill_timing(backend.sh)
            hide_utility_tabs(backend.sh)
            complete_bank_rows(backend.sh, settings)
            repaired = repair_bank_rows(backend.sh)
            if repaired:
                print(f"  [sheet] normalised {repaired} row(s) added outside the pipeline")
            print("sheet formatting applied")
        except Exception as exc:  # noqa: BLE001 — cosmetic, never fail the run
            print(f"  [sheet] formatting skipped: {exc}")

    if not skip_digest:
        digest_added = list(gate.bank) + list(paste_meta.get("jobs", []))
        # The cron runs DAILY, so emailing every run would mean 7 mails a week,
        # most with nothing new. Only write when there's something worth saying —
        # plus a guaranteed Monday summary so the weekly rhythm is preserved.
        today = datetime.now(timezone.utc).date()
        urgent = [j for j in digest_added
                  if j.deadline and 0 <= (j.deadline - today).days <= 7]
        is_weekly = today.weekday() == 0          # Monday
        reasons = []
        if digest_added:
            reasons.append(f"{len(digest_added)} new role(s)")
        if now_open_alerts:
            reasons.append(f"{len(now_open_alerts)} NOW OPEN")
        if urgent:
            reasons.append(f"{len(urgent)} closing within 7 days")
        if fetched.failures:
            reasons.append(f"{len(fetched.failures)} source failure(s)")
        if is_weekly:
            reasons.append("Monday summary")

        if reasons:
            stats_line = (f"{sum(fetched.per_source.values())} fetched, {len(fresh)} new after "
                          f"dedupe, {len(scored)} scored, {len(gate.bank)} added, "
                          f"{len(gate.archive)} archived, {paste_meta['bank']} from paste-in. "
                          f"Scoring: {score_meta['mode']}.")
            sheet_id = settings.env("SHEET_ID")
            sheet_url = (f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
                         if sheet_id else "")
            delivery = send_digest(build_digest_html(digest_added, fetched.failures, stats_line,
                                                     now_open_alerts, sheet_url,
                                                     settings.env("DASHBOARD_URL")), settings)
            print(f"digest: {delivery} ({'; '.join(reasons)})")
        else:
            print("digest: skipped — nothing new, not Monday, no alerts")

    # 8. state commit
    state.record_run(run_id, started.isoformat(), finished.isoformat(),
                     json.dumps({"per_source": fetched.per_source,
                                 "filters": fstats.counts,
                                 "added": len(gate.bank)}))
    state.close()
    print(f"== done in {time.time()-t0:.0f}s ==")
    return 0
