"""Target-company ATS watchlist: Greenhouse + Lever public JSON boards (Phase 1).
Workday and plain careers-page entries are Phase 2; they're counted and logged
so the Run Log shows what the watchlist is not yet covering.

A 404 on a board slug is logged, never fatal (slug may be wrong or board moved).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..models import Job
from ..settings import Settings
from . import client, fetch_with_retry, json_or_none, register

GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
LEVER = "https://api.lever.co/v0/postings/{slug}?mode=json"
TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", html or "")).strip()


@register("ats_watchlist")
def fetch(settings: Settings) -> list[Job]:
    jobs: list[Job] = []
    pending_phase2 = 0
    # Boards given up on after retries. This sweeps 63 companies and used to lose
    # all of them to one bad moment on any single board: a bare .get() let a
    # dropped connection propagate ("[Errno 104] Connection reset by peer" took
    # the whole source down, 4768 jobs, on 2026-08-24) and raise_for_status()
    # did the same for one 5xx. One board is now worth one board.
    failed: list[str] = []
    attempted = 0
    with client() as http:
        for entry in settings.watchlist:
            ats = entry.get("ats")
            name, slug = entry.get("name", "?"), entry.get("slug", "")
            if ats == "greenhouse" and slug:
                attempted += 1
                resp = fetch_with_retry(http, "GET", GREENHOUSE.format(slug=slug))
                if resp is not None and resp.status_code == 404:
                    print(f"  [ats_watchlist] greenhouse board not found: {name} ({slug})")
                    continue
                payload = json_or_none(resp)
                if payload is None:
                    failed.append(f"{name}/greenhouse "
                                  f"({resp.status_code if resp is not None else 'no response'})")
                    continue
                for j in payload.get("jobs", []):
                    jobs.append(Job(
                        source="greenhouse",
                        company=name,
                        title=j.get("title", ""),
                        url=j.get("absolute_url", ""),
                        location=(j.get("location") or {}).get("name", ""),
                        description=_strip_html(j.get("content", ""))[:8000],
                        # first_published is when the role went up; updated_at is
                        # when someone last edited it. Using the latter made a
                        # six-month-old posting that had a typo fixed yesterday
                        # report itself as posted yesterday. When Greenhouse does
                        # not give us the real date we record none: "we don't
                        # know" is a true answer and "yesterday" was not.
                        posted=_iso_date(j.get("first_published")),
                        ats_id=f"gh-{j.get('id')}",
                    ))
            elif ats == "lever" and slug:
                attempted += 1
                resp = fetch_with_retry(http, "GET", LEVER.format(slug=slug))
                if resp is not None and resp.status_code == 404:
                    print(f"  [ats_watchlist] lever board not found: {name} ({slug})")
                    continue
                payload = json_or_none(resp)
                if payload is None:
                    failed.append(f"{name}/lever "
                                  f"({resp.status_code if resp is not None else 'no response'})")
                    continue
                for j in payload:
                    cats = j.get("categories") or {}
                    jobs.append(Job(
                        source="lever",
                        company=name,
                        title=j.get("text", ""),
                        url=j.get("hostedUrl", ""),
                        location=cats.get("location", ""),
                        description=(j.get("descriptionPlain") or "")[:8000],
                        posted=_ms_date(j.get("createdAt")),
                        ats_id=f"lv-{j.get('id')}",
                        extra={"commitment": cats.get("commitment", "")},
                    ))
            else:
                pending_phase2 += 1
    if pending_phase2:
        print(f"  [ats_watchlist] {pending_phase2} watchlist companies await the "
              f"Phase 2 workday/careers-page fetchers")
    if failed:
        print(f"  [ats_watchlist] gave up on {len(failed)}/{attempted} board(s) after "
              f"retries: {', '.join(failed[:5])}")
        # A few flaky boards out of 63 is a Tuesday. Every board failing is an
        # outage (or a dead credential/network) and must not read as a quiet zero.
        if not jobs:
            raise RuntimeError("every watchlist board failed: " + ", ".join(failed[:5]))
    return jobs


def _iso_date(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, TypeError, AttributeError):
        return None


def _ms_date(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None
