"""Reed.co.uk official API (spec sec 12).

Auth: HTTP Basic, key as username, empty password.
The search response omits dates and external URLs, so anything that clears a
cheap keyword pre-filter gets a /jobs/{id} detail call for expirationDate,
externalUrl and contractType.
"""
from __future__ import annotations

from datetime import datetime

from ..models import Job
from ..enrich import format_salary
from ..settings import Settings
from . import client, register

SEARCH = "https://www.reed.co.uk/api/1.0/search"
DETAIL = "https://www.reed.co.uk/api/1.0/jobs/{job_id}"
PAGE = 100  # resultsToTake caps at 100

# Title-level prefilter applied BEFORE the (rate-limited) detail calls, so it has to
# match the profile's role shape: an internship profile and a graduate-scheme
# profile want opposite titles, and one profile's defaults would silently
# discard the other's roles before its filters ever saw them. Overridable per profile via reed.prefilter/prefilter_block.
PREFILTER = ("intern", "placement", "co-op", "coop", "stage", "sandwich", "year in industry")
PREFILTER_BLOCK = ("quant", "trading", "trader", "hedge fund")
MAX_DETAIL_CALLS = 150  # keep within polite limits per run


def _parse_reed_date(s: str):
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except (ValueError, TypeError):
        return None


@register("reed")
def fetch(settings: Settings) -> list[Job]:
    key = settings.env("REED_API_KEY")
    if not key:
        print("  [reed] skipped: REED_API_KEY not set")
        return []
    cfg = settings.cfg["reed"]
    prefilter = tuple(cfg.get("prefilter", PREFILTER))
    prefilter_block = tuple(cfg.get("prefilter_block", PREFILTER_BLOCK))
    auth = (key, "")
    candidates: dict[int, dict] = {}
    with client() as http:
        for term in cfg["keywords"]:
            skip = 0
            while skip < cfg.get("max_results_per_term", 100):
                resp = http.get(SEARCH, auth=auth, params={
                    "keywords": term, "resultsToTake": PAGE, "resultsToSkip": skip})
                resp.raise_for_status()
                results = resp.json().get("results", [])
                for r in results:
                    title = (r.get("jobTitle") or "").lower()
                    if not any(k in title for k in prefilter):
                        continue
                    if any(k in title for k in prefilter_block):
                        continue
                    candidates[r["jobId"]] = r
                if len(results) < PAGE:
                    break
                skip += PAGE

        jobs: list[Job] = []
        for job_id, r in list(candidates.items())[:MAX_DETAIL_CALLS]:
            detail = {}
            try:
                dresp = http.get(DETAIL.format(job_id=job_id), auth=auth)
                if dresp.status_code == 200:
                    detail = dresp.json()
            except Exception:  # noqa: BLE001 — detail is enrichment, not fatal
                pass
            url = (detail.get("externalUrl")
                   or r.get("jobUrl")
                   or f"https://www.reed.co.uk/jobs/{job_id}")
            jobs.append(Job(
                source="reed",
                company=r.get("employerName", ""),
                title=r.get("jobTitle", ""),
                url=url,
                location=r.get("locationName", ""),
                description=detail.get("jobDescription", r.get("jobDescription", "")),
                posted=_parse_reed_date(detail.get("datePosted", "")),
                deadline=_parse_reed_date(detail.get("expirationDate", "")),
                salary=format_salary(detail.get("minimumSalary") or r.get("minimumSalary"),
                                     detail.get("maximumSalary") or r.get("maximumSalary"),
                                     "gb"),
                ats_id=str(job_id),
                extra={"contract_type": detail.get("contractType", "")},
            ))
    return jobs
