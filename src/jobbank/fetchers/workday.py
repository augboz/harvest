"""Workday-hosted career sites — the first half of the Phase 2 `careers` gap.

Nearly half the watchlist (40 of 84 companies) is typed `ats: careers`, and
nothing has ever fetched any of it: `ats.py` covers greenhouse and lever, ashby
has its own fetcher, and everything else was counted and skipped. Every run log
has said so in a line that is easy to read past — "63 watchlist companies await
the Phase 2 workday/careers-page fetchers" — while McLaren, Ferrari, Airbus and
every F1 team sat there untouched.

Workday is the cleanest slice of that gap: its career sites are backed by a
public JSON endpoint (`/wday/cxs/{tenant}/{site}/jobs`), it paginates properly,
and `myworkdayjobs.com/robots.txt` explicitly allows the career-site paths.

Two calls per role, not one: the search endpoint returns titles and locations
only, and `filters.py` rejects anything with no software/data/AI signal in
`title + description`. So the detail endpoint is called too — but only for
postings whose title already looks like an internship and whose location is not
obviously out of reach, and never more than `detail_cap` of them per site. A
2000-job tenant would otherwise mean 2000 extra requests for the sake of a
handful of roles.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from ..models import Job
from ..settings import Settings
from . import client, fetch_with_retry, json_or_none, register

# Worth opening the description for. Deliberately looser than filters.py — this
# only decides which postings are worth a second request, and the real filtering
# happens later with the description in hand.
# "industrial" and "junior" on their own pulled in staff roles — "Industrial
# Process Engineer", "Industrial Architect". "industrial" only ever meant
# "industrial placement", so that is what it asks for.
WANTED_TITLE = re.compile(
    r"\b(intern|internship|placement|year in industry|industrial (placement|year)|"
    r"apprentice|graduate|working student|student|trainee|campus|"
    r"praktikum|stage|alternan)\w*", re.I)

# "Posted Today" / "Posted 30+ Days Ago" / "Posted Yesterday"
POSTED_DAYS = re.compile(r"(\d+)\+?\s*days?", re.I)


def _posted_date(text: str) -> date | None:
    t = str(text or "").lower()
    if "today" in t:
        return date.today()
    if "yesterday" in t:
        return date.today() - timedelta(days=1)
    m = POSTED_DAYS.search(t)
    if m:
        return date.today() - timedelta(days=int(m.group(1)))
    return None


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


# Workday's country reference ids are shared between tenants — the same GUID
# means "United Kingdom" on Airbus and on Thales. Not universally, though:
# NVIDIA's wd5 tenant rejects the facet outright with a 400, so a faceted search
# that fails falls back to an unfiltered one rather than losing the company.
# Without the filter a search returns whatever Workday ranks first across the
# whole world, which for Airbus meant 152 roles and not one of them in the UK.
COUNTRY_FACET = {
    "UK": "29247e57dbaf46fb855b224e03170bc7",
    "France": "54c5b6971ffb4bf0b116fe7651ec789a",
    "Germany": "dcc5b7608d8644b3a93716604e78e995",
    "Spain": "bd34c524a6a04ae6915f5d96fa086199",
    "Netherlands": "9696868b09c64d52a62ee13b052383cc",
    "Italy": "8cd04a563fd94da7b06857a79faaf815",
}


def _search(http, host, tenant, site, term, limit, offset, facets=None):
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    body = {"appliedFacets": facets or {}, "limit": limit, "offset": offset,
            "searchText": term}
    data = json_or_none(fetch_with_retry(http, "POST", url, json=body, headers=hdrs))
    if data is None and facets:
        return None, True          # signal: this tenant will not take the facet
    return data, False


@register("workday")
def fetch(settings: Settings) -> list[Job]:
    cfg = settings.cfg.get("workday") or {}
    sites = cfg.get("sites") or []
    if not sites:
        print("  [workday] skipped: no sites configured")
        return []
    terms = cfg.get("terms") or ["intern"]
    per_page = int(cfg.get("per_page", 20))
    max_pages = int(cfg.get("max_pages", 3))
    detail_cap = int(cfg.get("detail_cap", 25))

    jobs: list[Job] = []
    seen: set[str] = set()
    failed: list[str] = []
    with client() as http:
        for entry in sites:
            name = entry.get("name") or entry.get("tenant") or "?"
            host, tenant, site = entry.get("host"), entry.get("tenant"), entry.get("site")
            if not (host and tenant and site):
                print(f"  [workday] {name}: needs host, tenant and site — skipped")
                continue
            wanted = [c for c in (entry.get("countries", cfg.get("countries")) or [])
                      if c in COUNTRY_FACET]
            # One country at a time, not all six OR'd together. Filtering on the
            # set returned France and Germany for page after page and buried the
            # UK entirely — 138 roles, two of them British. Each country gets its
            # own pages, so a small country cannot be crowded out by a big one.
            queries = ([({"locationCountry": [COUNTRY_FACET[c]]}, c) for c in wanted]
                       or [(None, "anywhere")])
            found, details, unfiltered = 0, 0, False
            for facets, country in list(queries):
                if unfiltered and facets is not None:
                    continue          # the tenant has already refused to filter
                for term in terms:
                    for page in range(max_pages):
                        data, facet_rejected = _search(http, host, tenant, site, term,
                                                       per_page, page * per_page, facets)
                        if facet_rejected:
                            if not unfiltered:
                                print(f"  [workday] {name}: tenant will not take a "
                                      f"country filter; searching unfiltered")
                            unfiltered = True
                            facets = None
                            data, _ = _search(http, host, tenant, site, term,
                                              per_page, page * per_page, None)
                        if data is None:
                            failed.append(f"{name}/{country}/{term!r}")
                            break
                        postings = data.get("jobPostings") or []
                        if not postings:
                            break
                        for p in postings:
                            path = p.get("externalPath") or ""
                            url = f"https://{host}/{site}{path}"
                            if not path or url in seen:
                                continue
                            title = p.get("title") or ""
                            if not WANTED_TITLE.search(title):
                                continue
                            seen.add(url)
                            description = ""
                            # one extra request, and only while there is budget for it
                            if details < detail_cap:
                                det = json_or_none(fetch_with_retry(
                                    http, "GET",
                                    f"https://{host}/wday/cxs/{tenant}/{site}{path}"))
                                details += 1
                                info = (det or {}).get("jobPostingInfo") or {}
                                description = _strip(info.get("jobDescription", ""))[:8000]
                            jobs.append(Job(
                                source="workday",
                                company=name,
                                title=title,
                                url=url,
                                location=p.get("locationsText", ""),
                                description=description,
                                posted=_posted_date(p.get("postedOn", "")),
                                ats_id="wd-" + (p.get("bulletFields") or [""])[0],
                                extra={"tenant": tenant, "site": site},
                            ))
                            found += 1
                        if len(postings) < per_page:
                            break
            print(f"  [workday] {name}: {found} role(s) worth keeping "
                  f"({details} description(s) fetched)")
    if failed:
        print(f"  [workday] gave up on {len(failed)} query/queries after retries: "
              f"{', '.join(failed[:5])}")
        if not jobs:
            raise RuntimeError("every Workday query failed: " + ", ".join(failed[:5]))
    return jobs
