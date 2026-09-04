"""AI Dev Board (aidevboard.com) — clean public REST API of curated AI/ML roles.
No 'intern' seniority level exists, so we pull level=junior and post-filter
title/description for internship signals. Low volume, skews senior (spec Tier 1).
"""
from __future__ import annotations

import re as _re
from datetime import datetime

from ..models import Job
from ..settings import Settings
from . import client, register

API = "https://aidevboard.com/api/v1/jobs"
INTERN_RE = _re.compile(r"\b(interns?|internships?|placements?|co[- ]?ops?|stages?)\b", _re.I)


@register("aidevboard")
def fetch(settings: Settings) -> list[Job]:
    if not settings.cfg.get("aidevjobs", {}).get("enabled", True):
        return []
    jobs: list[Job] = []
    seen: set[str] = set()
    with client() as http:
        for q in ("intern", "internship", "placement"):
            page = 1
            while page <= 3:
                resp = http.get(API, params={"q": q, "level": "junior",
                                             "limit": 50, "page": page})
                if resp.status_code != 200:
                    break
                data = resp.json()
                for j in data.get("jobs", []):
                    title = j.get("title", "")
                    # require an internship signal in the TITLE (the API has no
                    # intern level, so description-matching pulls in too much noise)
                    if not INTERN_RE.search(title):
                        continue
                    url = j.get("apply_url") or j.get("url") or ""
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    jobs.append(Job(
                        source="aidevboard",
                        company=j.get("company_name", ""),
                        title=title,
                        url=url,
                        location=j.get("location", "") or ("Remote" if j.get("workplace") == "remote" else ""),
                        description=(j.get("description") or "")[:8000],
                        posted=_date(j.get("published_at")),
                        deadline=_date(j.get("expires_at")),
                        ats_id=f"aidb-{j.get('id')}",
                        extra={"tags": ",".join(j.get("tags", []) or [])},
                    ))
                if not data.get("has_next"):
                    break
                page += 1
    return jobs


def _date(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except (ValueError, TypeError, AttributeError):
        return None
