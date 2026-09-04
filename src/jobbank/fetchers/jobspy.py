"""JobSpy: Indeed, LinkedIn, Glassdoor, Google Jobs, ZipRecruiter (spec Tier 2).

Best-effort by design: LinkedIn/Glassdoor scraping violates ToS and gets rate
limited, so failures are logged, never fatal. Heavy dep (pandas) lives behind an
optional install; if python-jobspy isn't present the source cleanly skips.
Install for CI with: pip install -e .[jobspy]
"""
from __future__ import annotations

from datetime import date

from ..models import Job
from ..settings import Settings
from . import register


@register("jobspy")
def fetch(settings: Settings) -> list[Job]:
    cfg = settings.cfg.get("jobspy", {})
    if not cfg.get("enabled", True):
        print("  [jobspy] disabled in config")
        return []
    try:
        from jobspy import scrape_jobs
    except ImportError:
        print("  [jobspy] skipped: python-jobspy not installed (pip install -e .[jobspy])")
        return []

    sites = cfg.get("sites", ["indeed", "google"])
    terms = cfg.get("terms", [])
    locations = cfg.get("locations", [])
    results_wanted = cfg.get("results_wanted", 20)
    hours_old = cfg.get("hours_old", 220)
    proxies = cfg.get("proxies") or None

    jobs: list[Job] = []
    seen: set[str] = set()
    for term in terms:
        # A term may pin its own locations, as adzuna's already can: searching
        # "motorsport graduate engineer" in Singapore is a wasted request when
        # every F1 team is in England, and the term x location cross-product is
        # what pushes this source towards the CI timeout.
        if isinstance(term, dict):
            want = term.get("locations") or []
            here = [loc_cfg for loc_cfg in locations
                    if (loc_cfg.get("location") if isinstance(loc_cfg, dict)
                        else loc_cfg) in want] or locations
            term_q = term.get("q", "")
        else:
            here, term_q = locations, term
        for loc in here:
            location = loc.get("location") if isinstance(loc, dict) else loc
            country = loc.get("country_indeed", "UK") if isinstance(loc, dict) else "UK"
            try:
                df = scrape_jobs(
                    site_name=sites, search_term=term_q, location=location,
                    results_wanted=results_wanted, hours_old=hours_old,
                    country_indeed=country, proxies=proxies, verbose=0)
            except Exception as exc:  # noqa: BLE001 — best-effort per spec
                print(f"  [jobspy] {term_q!r}@{location!r} failed: {type(exc).__name__}: {exc}")
                continue
            if df is None or df.empty:
                continue
            for _, r in df.iterrows():
                url = str(r.get("job_url") or "")
                if not url or url in seen:
                    continue
                seen.add(url)
                posted = r.get("date_posted")
                try:
                    posted = posted.date() if hasattr(posted, "date") else (
                        date.fromisoformat(str(posted)[:10]) if posted else None)
                except (ValueError, TypeError):
                    posted = None
                jobs.append(Job(
                    source=f"jobspy:{r.get('site', '')}",
                    company=str(r.get("company") or ""),
                    title=str(r.get("title") or ""),
                    url=url,
                    location=str(r.get("location") or location or ""),
                    description=str(r.get("description") or "")[:8000],
                    posted=posted,
                    ats_id=str(r.get("id") or ""),
                ))
    return jobs
