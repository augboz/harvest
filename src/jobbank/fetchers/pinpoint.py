"""Pinpoint-hosted career sites — Phase 2, part two.

Aston Martin F1 is the reason this exists: an actual Formula 1 team, on the
watchlist since the start, typed `ats: careers` and therefore fetched by nothing.
Its listings sit behind `{company}.pinpointhq.com/postings.json` — one request,
plain JSON, full descriptions included, and a robots.txt that disallows only the
admin paths.

Pinpoint is common among UK employers, which is the point: the same four lines of
config add any of them. Contrast McLaren Racing and ESA, both of which answer
`Disallow: /` and cannot be fetched at all.

One request per company, and the descriptions come with it — no detail round
trip, unlike Workday.
"""
from __future__ import annotations

import re
from datetime import datetime

from ..models import Job
from ..settings import Settings
from . import client, fetch_with_retry, json_or_none, register


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _where(loc) -> str:
    """Pinpoint gives a structured location; the sheet wants "City, Country"."""
    if not isinstance(loc, dict):
        return str(loc or "")
    city = loc.get("city") or loc.get("name") or ""
    region = loc.get("province") or loc.get("country") or ""
    # "Silverstone, England" reads better to enrich.normalize_location than either
    # half alone, and England is what it needs to resolve the UK.
    return ", ".join(x for x in (city, region) if x)


def _date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _text(posting) -> str:
    """The body is split across several optional sections; keep them all, since
    filters.py needs a tech signal somewhere in title + description and the
    interesting words are as likely to be under "skills" as under "description".
    """
    parts = [posting.get("description"), posting.get("key_responsibilities"),
             posting.get("skills_knowledge_expertise")]
    return _strip(" ".join(p for p in parts if p))[:8000]


@register("pinpoint")
def fetch(settings: Settings) -> list[Job]:
    cfg = settings.cfg.get("pinpoint") or {}
    sites = cfg.get("sites") or []
    if not sites:
        print("  [pinpoint] skipped: no sites configured")
        return []

    jobs: list[Job] = []
    failed: list[str] = []
    with client() as http:
        for entry in sites:
            name = entry.get("name") or entry.get("slug") or "?"
            slug = entry.get("slug")
            if not slug:
                print(f"  [pinpoint] {name}: needs a slug — skipped")
                continue
            data = json_or_none(fetch_with_retry(
                http, "GET", f"https://{slug}.pinpointhq.com/postings.json"))
            if data is None:
                failed.append(name)
                continue
            postings = data.get("data") if isinstance(data, dict) else data
            found = 0
            for p in postings or []:
                a = p.get("attributes", p)
                url = a.get("url") or ""
                title = a.get("title") or ""
                if not url or not title:
                    continue
                jobs.append(Job(
                    source="pinpoint",
                    company=name,
                    title=title,
                    url=url,
                    location=_where(a.get("location")),
                    description=_text(a),
                    deadline=_date(a.get("deadline_at")),
                    ats_id="pp-" + str(a.get("id") or ""),
                    extra={"employment_type": a.get("employment_type_text") or "",
                           "workplace_type": a.get("workplace_type_text") or ""},
                ))
                found += 1
            print(f"  [pinpoint] {name}: {found} role(s)")
    if failed:
        print(f"  [pinpoint] gave up on {len(failed)} board(s) after retries: "
              f"{', '.join(failed[:5])}")
        if not jobs:
            raise RuntimeError("every Pinpoint board failed: " + ", ".join(failed[:5]))
    return jobs
