"""Welcome to the Jungle — French 6-month stage market (spec Tier 4).
The React site is backed by a public Algolia search index; we query it directly.
Public search-only credentials are the site's own client keys.
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from ..models import Job
from ..settings import Settings
from . import client, register

ALGOLIA_URL = "https://csekhvms53-dsn.algolia.net/1/indexes/*/queries"
APP_ID = "CSEKHVMS53"
API_KEY = "4bd8f6215d0cc52b26430765769e65a0"
INDEX = "wk_cms_jobs_production"
QUERIES = ["software engineer", "data engineer", "machine learning", "audio software"]


@register("wttj")
def fetch(settings: Settings) -> list[Job]:
    if not settings.cfg.get("placement_boards", {}).get("welcometothejungle", True):
        return []
    # the public search key is referer-restricted to welcometothejungle.com
    headers = {"x-algolia-application-id": APP_ID, "x-algolia-api-key": API_KEY,
               "Content-Type": "application/json",
               "Referer": "https://www.welcometothejungle.com/",
               "Origin": "https://www.welcometothejungle.com"}
    jobs: list[Job] = []
    seen: set[str] = set()
    with client() as http:
        for query in QUERIES:
            params = urlencode({
                "query": query, "hitsPerPage": 40, "page": 0,
                "facetFilters": '[["contract_type_names.en:Internship"],'
                                '["offices.country_code:FR"]]',
            })
            body = {"requests": [{"indexName": INDEX, "params": params}]}
            resp = http.post(ALGOLIA_URL, headers=headers, json=body)
            if resp.status_code != 200:
                # facet names can drift; retry without the country facet
                params2 = urlencode({"query": query, "hitsPerPage": 40, "page": 0,
                                     "facetFilters": '[["contract_type_names.en:Internship"]]'})
                resp = http.post(ALGOLIA_URL, headers=headers,
                                 json={"requests": [{"indexName": INDEX, "params": params2}]})
                if resp.status_code != 200:
                    continue
            hits = resp.json().get("results", [{}])[0].get("hits", [])
            for h in hits:
                org = h.get("organization") or {}
                slug = h.get("slug", "")
                oslug = org.get("slug", "")
                url = (f"https://www.welcometothejungle.com/en/companies/{oslug}/jobs/{slug}"
                       if oslug and slug else "")
                oid = h.get("objectID", "")
                if not url or oid in seen:
                    continue
                seen.add(oid)
                office = h.get("office") or {}
                jobs.append(Job(
                    source="wttj",
                    company=org.get("name", ""),
                    title=h.get("name", ""),
                    url=url,
                    location=office.get("city", "") or "France",
                    description="",
                    posted=_date(h.get("published_at")),
                    ats_id=f"wttj-{oid}",
                    extra={"contract": (h.get("contract_type_names") or {}).get("en", "")},
                ))
    return jobs


def _date(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except (ValueError, TypeError, AttributeError):
        return None
