"""Motorsport/F1 boards — the key F1 source (spec Tier 1). Structures verified
2026-07-15 (see research/phase2-sources.md):

- fluidjobs.com    : server-rendered HTML, path categories, needs a browser UA
- formulacareers.com: JS page backed by a WP Job Manager AJAX endpoint (POST)
- motorsportjobs.com: server-rendered Drupal HTML, taxonomy paths, ?page= 0-indexed
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from ..models import Job
from ..settings import Settings
from . import client, fetch_with_retry, json_or_none, register

REL_RE = re.compile(r"(\d+)\s*(h|hour|d|day|w|week|mo|month)", re.I)


def _rel_date(text: str, today: date) -> date | None:
    m = REL_RE.search(text or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith("h"):
        return today
    if unit.startswith("d"):
        return today - timedelta(days=n)
    if unit.startswith("w"):
        return today - timedelta(weeks=n)
    if unit.startswith("mo") or unit == "month":
        return today - timedelta(days=30 * n)
    return None


# --- fluidjobs -------------------------------------------------------------
FLUID_CATS = ["internship-placements", "it-and-technology-management"]


@register("fluidjobs")
def fetch_fluidjobs(settings: Settings) -> list[Job]:
    import httpx
    today = date.today()
    jobs: list[Job] = []
    seen: set[str] = set()
    with client(browser=True) as http:
        for cat in FLUID_CATS:
            for page in range(1, 4):
                try:
                    resp = http.get(f"https://fluidjobs.com/jobs/{cat}",
                                    params={"page": page}, timeout=60)
                except httpx.HTTPError as exc:  # slow host; keep any earlier pages
                    print(f"  [fluidjobs] {cat} page {page} timed out ({exc}); moving on")
                    break
                if resp.status_code != 200:
                    break
                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.select("div.job-listings-item")
                if not cards:
                    break
                for card in cards:
                    link = card.select_one("a.job-details-link")
                    if not link:
                        continue
                    title = link.get_text(strip=True)
                    href = link.get("href", "")
                    url = href if href.startswith("http") else f"https://fluidjobs.com{href}"
                    if not title or url in seen:
                        continue
                    seen.add(url)
                    company = card.select_one('a.job-info-link-item[href^="/companies/"]')
                    loc = card.select_one('a.job-info-link-item[href^="/jobs/in-"]')
                    date_span = card.find_all("span")
                    rel = date_span[-1].get_text(strip=True) if date_span else ""
                    jid = card.get("data-jobid") or card.get("data-jobId") or ""
                    jobs.append(Job(
                        source="fluidjobs",
                        company=company.get_text(strip=True) if company else "",
                        title=title, url=url,
                        location=loc.get_text(strip=True) if loc else "",
                        posted=_rel_date(rel, today),
                        ats_id=f"fluid-{jid}" if jid else "",
                    ))
                if not soup.select_one('link[rel="next"], a[rel="next"]'):
                    break
    return jobs


# --- formulacareers (WP Job Manager AJAX) ----------------------------------
FC_AJAX = "https://formulacareers.com/jm-ajax/get_listings/"


def _fc_page(http, category: str, page: int) -> tuple[dict | None, str | None]:
    """One page of the AJAX endpoint, plus WHY it failed when it did.

    The reason matters. "we got a reply and it was not JSON" means the endpoint
    has moved or is behind a WAF, and somebody has to go and look. "we never got
    a reply at all" means the network between here and there did not work, which
    on a GitHub runner is routine and says nothing about the source. Collapsing
    the two into None made a CI-only connect timeout page as a dead endpoint
    while the site answered a laptop in two seconds.

    The endpoint answers a GitHub Actions runner with 200 and a body that is not
    JSON, while the identical request from a laptop returns JSON — an edge block
    keyed on how the client looks, not on what it asked for. That is the same
    wall gradcracker and hiringcafe hit, and the same answer: curl_cffi's
    browser TLS impersonation. Plain httpx stays as the fallback for when
    curl_cffi is not installed (`pip install -e .[scrape]`).
    """
    data = {"search_categories[]": category, "per_page": 50,
            "orderby": "featured", "order": "DESC", "page": page,
            "form_data": "search_keywords=&search_location="}
    try:
        from curl_cffi import requests as creq
    except ImportError:
        creq = None
    if creq is not None:
        try:
            resp = creq.post(FC_AJAX, data=data, impersonate="chrome", timeout=30)
            if resp.status_code == 200:
                return resp.json(), None
            print(f"  [formulacareers] HTTP {resp.status_code} via curl_cffi; "
                  f"trying plain httpx")
        except Exception as exc:  # noqa: BLE001 — any failure just means fall back
            print(f"  [formulacareers] curl_cffi attempt failed ({exc}); "
                  f"trying plain httpx")
    resp = fetch_with_retry(http, "POST", FC_AJAX, data=data)
    if resp is None:
        # every attempt raised before a reply arrived: timeout, DNS, TLS.
        return None, "unreachable"
    data_json = json_or_none(resp)
    return (data_json, None) if data_json is not None else (None, "bad-response")


@register("formulacareers")
def fetch_formulacareers(settings: Settings) -> list[Job]:
    jobs: list[Job] = []
    seen: set[str] = set()
    # Categories whose FIRST page never came back as usable JSON. One bad page
    # deep into a category is a blip; both categories dead on page one is the
    # endpoint having changed or gone behind a WAF, which is worth surfacing.
    dead: dict[str, str] = {}
    with client(browser=True) as http:
        for category in ("f1", "motorsport"):
            page, max_pages = 1, 1
            while page <= max_pages and page <= 5:
                data, reason = _fc_page(http, category, page)
                if data is None:
                    print(f"  [formulacareers] {reason} for {category} "
                          f"page {page}; skipping the rest of it")
                    if page == 1:
                        dead[category] = reason or "bad-response"
                    break
                max_pages = data.get("max_num_pages", 1) or 1
                soup = BeautifulSoup(data.get("html", ""), "html.parser")
                for li in soup.select("li.job_listing"):
                    a = li.find("a", href=True)
                    title_el = li.select_one("div.position h3") or li.find("h3")
                    if not a or not title_el:
                        continue
                    url = a["href"]
                    if url in seen:
                        continue
                    seen.add(url)
                    company = li.select_one("div.company strong")
                    loc = li.select_one("div.location")
                    time_el = li.select_one("ul.meta li.date time")
                    posted = None
                    if time_el and time_el.get("datetime"):
                        try:
                            posted = datetime.fromisoformat(time_el["datetime"]).date()
                        except ValueError:
                            posted = None
                    jtype = li.select_one("ul.meta li.job-type")
                    jobs.append(Job(
                        source="formulacareers",
                        company=company.get_text(strip=True) if company else category.upper(),
                        title=title_el.get_text(strip=True), url=url,
                        location=loc.get_text(strip=True) if loc else "",
                        posted=posted,
                        extra={"job_type": jtype.get_text(strip=True) if jtype else "",
                               "board_category": category}))
                page += 1
    if len(dead) == 2 and not jobs:
        # Only a reply we could not parse is evidence about the SOURCE. If we
        # never reached it, the thing that broke is the network we are running
        # on, and failing the run would be blaming the site for the runner.
        if "unreachable" in dead.values():
            print("  [formulacareers] never reached the endpoint (network, not "
                  "the site) — reporting 0 jobs rather than a source failure")
            return jobs
        raise RuntimeError(
            "formulacareers returned no usable JSON for either category — "
            "the jm-ajax endpoint has probably moved or is being blocked")
    return jobs


# --- motorsportjobs (Drupal) -----------------------------------------------
# 2026-09-01: the board dropped its contract-type taxonomy. /en/jobs/internship,
# /student-internship and /industrial-placement still answer 200 — Drupal serves
# the page shell for any path — but with zero listings, so this source reported
# 0 jobs for weeks while looking alive. The taxonomy is now industry and
# discipline; these are the facets that exist, with their counts on the day:
#   industry/formula-1-10611 (203)  industry/motorsport-10477 (65)
#   mechanical-engineering-0 (363)  racing-team (150)
# There is no keyword search: every ?q=-style parameter is ignored and the page
# comes back unfiltered, so the facet path IS the filter.
MSJ_PATHS = ["industry/formula-1-10611", "industry/motorsport-10477"]
MSJ_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


@register("motorsportjobs")
def fetch_motorsportjobs(settings: Settings) -> list[Job]:
    cfg = getattr(settings, "cfg", {}).get("motorsportjobs") or {}
    paths = cfg.get("paths") or MSJ_PATHS
    max_pages = int(cfg.get("max_pages", 3))
    jobs: list[Job] = []
    seen: set[str] = set()
    empty: list[str] = []
    with client(browser=True) as http:
        for path in paths:
            before = len(jobs)
            for page in range(0, max_pages):
                resp = http.get(f"https://www.motorsportjobs.com/en/jobs/{path}",
                                params={"page": page} if page else {})
                if resp.status_code != 200:
                    break
                soup = BeautifulSoup(resp.text, "html.parser")
                links = soup.select("a.recruiter-job-link")
                if not links:
                    break
                for link in links:
                    url = link.get("href", "")
                    title = link.get_text(strip=True).replace("�", "")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    card = link.find_parent("article") or link.find_parent("div", class_="views-row") or soup
                    company = card.select_one("div.organization-name a")
                    # NOT "div.location span": the first span in there wraps an
                    # SVG whose <title> is the word "Location", so every job came
                    # out located in "Location".
                    loc = (card.select_one("div.location > span:not(.svg-icon)")
                           or card.select_one("div.location"))
                    date_el = card.select_one("span.date")
                    posted = None
                    if date_el:
                        dm = MSJ_DATE_RE.search(date_el.get_text(" ", strip=True))
                        if dm:
                            try:
                                posted = date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
                            except ValueError:
                                posted = None
                    node_id = (card.get("id") or "").replace("node-", "")
                    jobs.append(Job(
                        source="motorsportjobs",
                        company=company.get_text(strip=True) if company else "",
                        title=title, url=url,
                        location=loc.get_text(strip=True) if loc else "",
                        posted=posted,
                        ats_id=f"msj-{node_id}" if node_id else ""))
                if len(links) < 5:
                    break
            if len(jobs) == before:
                empty.append(path)
    # Every path empty is the taxonomy having moved again. Reporting 0 jobs is
    # what hid the last move: the health check reads it as "not necessarily
    # broken" and nobody looks. Partial emptiness is normal — a facet can
    # legitimately run dry — so only a clean sweep is worth failing on.
    if paths and len(empty) == len(paths):
        raise RuntimeError(
            "motorsportjobs returned no listings for any of "
            f"{', '.join(paths)} — the facet paths have probably moved again "
            "(check the facet links on /en/jobs)")
    return jobs
