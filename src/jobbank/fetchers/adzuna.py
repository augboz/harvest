"""Adzuna official API (free trial tier — keep queries targeted, spec sec 12)."""
from __future__ import annotations

from datetime import datetime

from ..models import Job
from ..enrich import format_salary
from ..settings import Settings
from . import client, fetch_with_retry, json_or_none, register

BASE = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"


@register("adzuna")
def fetch(settings: Settings) -> list[Job]:
    app_id = settings.env("ADZUNA_APP_ID")
    app_key = settings.env("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        print("  [adzuna] skipped: ADZUNA_APP_ID/ADZUNA_APP_KEY not set")
        return []
    cfg = settings.cfg["adzuna"]
    default_countries = cfg["countries"]
    jobs: list[Job] = []
    seen_urls: set[str] = set()
    # Queries given up on after retries. A blip on one country/term must not cost
    # the others — but if EVERY query fails that is an outage, and the run log
    # should say so rather than reporting a quiet zero.
    failed: list[str] = []
    attempted = 0
    with client() as http:
        for term in cfg["terms"]:
            q = term["q"] if isinstance(term, dict) else term
            countries = (term.get("countries") if isinstance(term, dict) else None) or default_countries
            for country in countries:
                for page in range(1, cfg.get("max_pages", 1) + 1):
                    attempted += 1
                    resp = fetch_with_retry(
                        http, "GET", BASE.format(country=country, page=page), params={
                            "app_id": app_id, "app_key": app_key, "what": q,
                            "results_per_page": cfg.get("results_per_page", 50),
                            "max_days_old": settings.freshness_days,
                            "content-type": "application/json",
                        })
                    if resp is not None and resp.status_code == 429:
                        print(f"  [adzuna] rate-limited on {country}/{q!r}; stopping early")
                        return jobs
                    payload = json_or_none(resp)
                    if payload is None:
                        # Retries are already spent. Give this query up and move
                        # on to the next country rather than aborting the source.
                        why = resp.status_code if resp is not None else "no response"
                        failed.append(f"{country}/{q!r} ({why})")
                        break
                    results = payload.get("results", [])
                    for r in results:
                        url = r.get("redirect_url", "")
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        posted = None
                        if r.get("created"):
                            posted = datetime.fromisoformat(
                                r["created"].replace("Z", "+00:00")).date()
                        jobs.append(Job(
                            source="adzuna",
                            company=(r.get("company") or {}).get("display_name", ""),
                            title=r.get("title", ""),
                            url=url,
                            location=(r.get("location") or {}).get("display_name", ""),
                            description=r.get("description", ""),
                            posted=posted,
                            salary=format_salary(
                                r.get("salary_min"), r.get("salary_max"), country,
                                str(r.get("salary_is_predicted", "")) == "1"),
                            ats_id=str(r.get("id", "")),
                            extra={"country": country,
                                   "contract_time": r.get("contract_time", "")},
                        ))
                    if len(results) < cfg.get("results_per_page", 50):
                        break  # no further pages
    if failed:
        print(f"  [adzuna] gave up on {len(failed)}/{attempted} quer(y|ies) after "
              f"retries: {', '.join(failed[:5])}")
        # Everything failed: that is an outage, not a thin week. Raise so the
        # runner records it as a source failure instead of a silent zero.
        if not jobs:
            raise RuntimeError("every Adzuna query failed: " + ", ".join(failed[:5]))
    return jobs
