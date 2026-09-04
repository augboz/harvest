"""HiringCafe (hiringcafe.com) — aggregates 2.8M+ jobs across 46 ATS platforms.

The old POST /api/search-jobs is dead (405). The current path is the SSR route
`GET /?searchState=<url-encoded JSON>` whose HTML embeds a __NEXT_DATA__ script
with props.pageProps.ssrHits. Cloudflare-protected, so we use curl_cffi's browser
impersonation and skip gracefully if challenged (spec: fall back to Apify — not
built; logged instead). Verified 2026-07-15.
"""
from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import quote

from ..models import Job
from ..settings import Settings
from . import register

BASE = "https://hiringcafe.com/?searchState="


@register("hiringcafe")
def fetch(settings: Settings) -> list[Job]:
    cfg = settings.cfg.get("hiringcafe", {})
    queries = cfg.get("queries", [])
    max_per = cfg.get("max_per_query", 40)
    commitment_types = cfg.get("commitment_types", ["Internship"])
    try:
        from curl_cffi import requests as creq
    except ImportError:
        print("  [hiringcafe] skipped: curl_cffi not installed (pip install -e .[scrape])")
        return []
    from bs4 import BeautifulSoup

    jobs: list[Job] = []
    seen: set[str] = set()
    for query in queries:
        state = {"searchQuery": query, "commitmentTypes": commitment_types,
                 "locations": [], "page": 0}
        url = BASE + quote(json.dumps(state))
        try:
            resp = creq.get(url, impersonate="chrome", timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"  [hiringcafe] {query!r} failed: {exc}")
            continue
        if resp.status_code != 200:
            print(f"  [hiringcafe] HTTP {resp.status_code} on {query!r} "
                  "(likely Cloudflare) — skipping")
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag or not tag.string:
            continue
        try:
            hits = json.loads(tag.string)["props"]["pageProps"].get("ssrHits", [])
        except (KeyError, json.JSONDecodeError):
            continue
        for h in hits[:max_per]:
            info = h.get("job_information") or {}
            v5 = h.get("v5_processed_job_data") or {}
            company = (h.get("enriched_company_data") or {}).get("name", "")
            url_apply = h.get("apply_url", "")
            if not url_apply or url_apply in seen or h.get("is_expired"):
                continue
            seen.add(url_apply)
            cities = v5.get("workplace_cities") or []
            jobs.append(Job(
                source="hiringcafe",
                company=company,
                title=info.get("title") or v5.get("core_job_title", ""),
                url=url_apply,
                location=", ".join(cities) or (v5.get("workplace_countries") or [""])[0],
                description=" ".join(str(v5.get(k, "")) for k in
                                     ("requirements_summary", "role_activities")),
                # HiringCafe only offers estimated_publish_date, which is its
                # own guess rather than the employer's date. A card that says
                # "3 weeks ago" should mean it, so this source contributes no
                # posting date at all.
                posted=None,
                ats_id=str(h.get("id") or h.get("objectID") or ""),
                extra={"ats": h.get("source", "")}))
    return jobs


def _date(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except (ValueError, TypeError, AttributeError):
        return None
