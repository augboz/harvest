"""Gradcracker — UK computing placements & internships (spec Tier 4).
Cloudflare-protected: plain httpx gets 403, so we use curl_cffi's browser TLS
impersonation. If curl_cffi isn't installed the source cleanly skips.

Card structure verified 2026-07-15: job links match
  /hub/{id}/{company-slug}/work-placement-internship/{jobid}/{title-slug}
company is the URL slug, title is the link text, the enclosing card carries a
"Deadline: <Month Nth, YYYY>" string.
"""
from __future__ import annotations

import re

from dateutil import parser as dateparser

from ..models import Job
from ..settings import Settings
from . import register

LISTINGS = [
    "https://www.gradcracker.com/search/computing-technology/work-placements-internships",
    "https://www.gradcracker.com/search/computing-technology/computer-science-work-placements-internships",
]
JOB_RE = re.compile(r"/hub/\d+/([^/]+)/work-placement-internship/\d+/")
DEADLINE_RE = re.compile(r"Deadline:\s*([A-Za-z]+\s+\d+\w*,?\s*\d{4})")
LOCATION_RE = re.compile(r"Location\s+([A-Za-z][A-Za-z ,/&\-]+?)(?:\s+(?:Apply|Deadline|Salary|View|Save)|$)")


@register("gradcracker")
def fetch(settings: Settings) -> list[Job]:
    if not settings.cfg.get("placement_boards", {}).get("gradcracker", True):
        return []
    try:
        from curl_cffi import requests as creq
    except ImportError:
        print("  [gradcracker] skipped: curl_cffi not installed (pip install -e .[scrape])")
        return []
    from bs4 import BeautifulSoup

    jobs: list[Job] = []
    seen: set[str] = set()
    for url in LISTINGS:
        try:
            resp = creq.get(url, impersonate="chrome", timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"  [gradcracker] fetch failed {url}: {exc}")
            continue
        if resp.status_code != 200:
            print(f"  [gradcracker] HTTP {resp.status_code} on {url}")
            continue
        soup = BeautifulSoup(resp.content.decode("utf-8", "replace"), "html.parser")
        for a in soup.find_all("a", href=True):
            m = JOB_RE.search(a["href"])
            title = a.get_text(strip=True).replace("�", "-")
            if not m or not title or len(title) < 8:
                continue
            href = a["href"]
            if not href.startswith("http"):
                href = "https://www.gradcracker.com" + href
            if href in seen:
                continue
            seen.add(href)
            company = m.group(1).replace("-", " ").title()
            card_text = ""
            node = a
            for _ in range(10):
                node = node.parent
                if node is None:
                    break
                if "Deadline" in node.get_text():
                    card_text = node.get_text(" ", strip=True)
                    break
            deadline = None
            dm = DEADLINE_RE.search(card_text)
            if dm:
                try:
                    deadline = dateparser.parse(dm.group(1)).date()
                except (ValueError, OverflowError):
                    deadline = None
            lm = LOCATION_RE.search(card_text)
            jobs.append(Job(
                source="gradcracker",
                company=company,
                title=title,
                url=href,
                location=(lm.group(1).strip() if lm else "UK"),
                deadline=deadline,
                ats_id=f"gc-{href.split('/work-placement-internship/')[1].split('/')[0]}",
            ))
    return jobs
