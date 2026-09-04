"""Trackr (the-trackr.com) — clean open JSON API (verified 2026-07-15).
`GET https://api.the-trackr.com/programmes?region=&industry=&season=&type=`.
Feeds the Job Bank (live programmes) AND the Opening Soon tab (future open dates).
Industry is Tech only — Finance is deliberately excluded (quant hard-filter).
"""
from __future__ import annotations

import time
from datetime import date

from dateutil import parser as dateparser

from ..models import Job, OpeningSoonEntry
from ..settings import Settings
from . import client, register

API = "https://api.the-trackr.com/programmes"
# Kept deliberately small: Trackr rate-limits hard (HTTP 429). The old
# 3 regions x 2 seasons x 3 types matrix — issued TWICE per run, once for jobs and
# once for Opening Soon — was 36 requests and got the source throttled to zero
# results silently. EU/France returned 0 rows on probing; 2028 isn't populated.
REGIONS = ["UK"]
TYPES = ["industrial-placements", "summer-internships", "off-cycle-internships"]
SEASONS = ["2027"]


def _d(s):
    if not s:
        return None
    try:
        return dateparser.parse(s).date()
    except (ValueError, OverflowError, TypeError):
        return None


_CACHE: tuple[list[Job], list[OpeningSoonEntry]] | None = None


def _get(http, params, attempts: int = 3):
    """Polite GET with backoff. Trackr rate-limits (HTTP 429) and used to fail
    silently to zero jobs for the whole run."""
    for i in range(attempts):
        resp = http.get(API, params=params)
        if resp.status_code != 429:
            return resp
        time.sleep(2 * (i + 1))
    return resp


def _pull(settings: Settings) -> tuple[list[Job], list[OpeningSoonEntry]]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE          # fetch() and opening_soon() must not double-fetch
    if not settings.cfg.get("trackr", {}).get("enabled", True):
        return [], []
    today = date.today()
    jobs: list[Job] = []
    soon: list[OpeningSoonEntry] = []
    seen: set[str] = set()
    throttled = 0
    with client() as http:
        for region in REGIONS:
            for season in SEASONS:
                for typ in TYPES:
                    try:
                        time.sleep(0.6)      # ~1 req/0.6s keeps us under the limit
                        resp = _get(http, {"region": region, "industry": "Tech",
                                           "season": season, "type": typ})
                        if resp.status_code == 429:
                            throttled += 1
                    except Exception:  # noqa: BLE001
                        continue
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    rows = data if isinstance(data, list) else data.get("programmes", [])
                    for p in rows:
                        company = (p.get("company") or {}).get("name") or p.get("companyName") or ""
                        name = p.get("name", "")
                        url = p.get("url") or ""
                        opens, closes = _d(p.get("openingDate")), _d(p.get("closingDate"))
                        key = f"{company.lower()}|{name.lower()}|{season}"
                        if not company or not name or key in seen:
                            continue
                        seen.add(key)
                        locs = p.get("locations") or []
                        location = ", ".join(str(x) for x in locs) if locs else region
                        # Opening Soon = a genuinely future open date (not just a
                        # listing missing its URL — those aren't actionable yet).
                        if opens and opens > today:
                            soon.append(OpeningSoonEntry(
                                company=company, programme=f"{name} ({typ}, {season})",
                                expected_open=opens.isoformat(),
                                expected_close=closes.isoformat() if closes else "",
                                status="Announced" if url else "Predicted from last year",
                                link=url, notes=f"Trackr {region} Tech", source="trackr"))
                            continue
                        if not url:
                            continue  # not yet actionable (no apply link, no future date)
                        if closes and closes < today:
                            continue  # already closed
                        jobs.append(Job(
                            source="trackr", company=company, title=name, url=url,
                            location=location, deadline=closes, posted=opens,
                            start=season, ats_id=f"trackr-{p.get('id','')}",
                            extra={"programme_type": typ, "region": region}))
    if throttled:
        print(f"  [trackr] rate-limited on {throttled} request(s) despite backoff")
    if not jobs and not soon:
        print("  [trackr] returned nothing - check the API is still reachable")
    _CACHE = (jobs, soon)
    return jobs, soon


@register("trackr")
def fetch(settings: Settings) -> list[Job]:
    return _pull(settings)[0]


def opening_soon(settings: Settings) -> list[OpeningSoonEntry]:
    """Called by the pipeline's Opening Soon sync (spec sec 6)."""
    try:
        return _pull(settings)[1]
    except Exception as exc:  # noqa: BLE001
        print(f"  [trackr] opening-soon pull failed: {exc}")
        return []
