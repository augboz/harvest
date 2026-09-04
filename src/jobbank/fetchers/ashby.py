"""Ashby public job boards (api.ashbyhq.com) — used by many AI startups that
aren't on Greenhouse/Lever (e.g. Perplexity). Config-driven org list so Aug can
add companies without code. Pre-filtered to intern/placement titles to stay clean
(Ashby tags internships as employmentType=FullTime, so we match on the title).
"""
from __future__ import annotations

from datetime import datetime

import re

from ..models import Job
from ..settings import Settings
from . import client, register

API = "https://api.ashbyhq.com/posting-api/job-board/{org}"
# word-boundary match so "internal"/"international" don't count as "intern"
INTERN_RE = re.compile(r"\b(interns?|internships?|placements?|co[- ]?ops?|stages?|"
                       r"apprentice(ship)?s?|working student)\b", re.I)


@register("ashby")
def fetch(settings: Settings) -> list[Job]:
    orgs = settings.cfg.get("ashby", {}).get("orgs", [])
    jobs: list[Job] = []
    with client() as http:
        for org in orgs:
            resp = http.get(API.format(org=org))
            if resp.status_code != 200:
                print(f"  [ashby] board not found: {org} ({resp.status_code})")
                continue
            for j in resp.json().get("jobs", []):
                title = j.get("title", "")
                if not INTERN_RE.search(title):
                    continue
                url = j.get("jobUrl") or j.get("applyUrl") or ""
                if not url:
                    continue
                loc = j.get("location") or ""
                if j.get("isRemote"):
                    loc = (loc + " / Remote").strip(" /")
                jobs.append(Job(
                    source=f"ashby:{org}",
                    company=org.replace("-", " ").title(),
                    title=title,
                    url=url,
                    location=loc,
                    description=(j.get("descriptionPlain") or "")[:8000],
                    posted=_date(j.get("publishedAt")),
                    ats_id=f"ashby-{j.get('id')}",
                    extra={"department": j.get("department", "")},
                ))
    return jobs


def _date(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except (ValueError, TypeError, AttributeError):
        return None
