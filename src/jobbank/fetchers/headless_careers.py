"""Career sites that only exist once JavaScript has run.

Dyson is the case that forced this. Their listings are not in the server-rendered
HTML, not in the sitemap, and not behind any reachable JSON endpoint — the two
API paths in their bundle (/api/earlycareers/location, /api/search/autocomplete)
both 404 from outside the app. Adzuna carries some of their postings but only
what Dyson chooses to syndicate. The same is true of ESA, Apple and most of the
F1 teams: the roles are real, they are simply invisible to anything that cannot
execute their JavaScript.

So this fetcher drives a real browser, and is deliberately conservative:

- opt-in per site in config (`headless_careers.sites`), never a blanket crawl;
- one browser for the whole run, pages fetched sequentially with a delay;
- if Playwright or its browser binary is missing, it logs and returns nothing —
  a missing optional dependency must never fail the weekly run;
- extraction is generic (anchors that look like job links) with a per-site
  selector override, because every one of these sites is bespoke.

Roles found here carry source="careers:<company>" and go through exactly the
same dedupe, filters and scoring as everything else.
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote_plus, urljoin, urlsplit

from ..models import Job
from ..settings import Settings
from . import BROWSER_UA, register

# An anchor is a job link if its href looks like one. Career sites are wildly
# inconsistent, so this is broad and the title check below does the real work.
JOB_HREF_RE = re.compile(r"/(job|jobs|vacancy|vacancies|position|opening|role|"
                         r"apply|career-?opportunit)[s]?[/\-=]", re.I)
# Accessible link text usually wraps the real title — Dyson's job links all read
# "More details about <role>" — so the wrapper is stripped rather than treated as
# a reason to reject the anchor.
PREFIX_RE = re.compile(r"^(more details about|read more about|find out more about|"
                       r"learn more about|apply (?:now )?(?:for|to)|view details for|"
                       r"view)\s+", re.I)
# Navigation furniture is matched WHOLE: a prefix match would throw away any real
# title that happens to start with one of these words.
NAV_EXACT_RE = re.compile(r"^(search|filter|apply|apply now|view all|view all jobs|see all|"
                          r"next|previous|back|home|sign in|log ?in|register|cookies?|"
                          r"privacy|accept|menu|all jobs|browse|more|learn more|read more|"
                          r"more details|view details|view jobs?|job search|our vacancies)$", re.I)

# Unlike every other source, this one scrapes a WHOLE careers board rather than
# running a scoped search, so a broad query like "engineer" returns the company's
# entire senior population. filters.py cannot weed those out here because a
# scraped card carries a title and little else — the years-of-experience and
# chartership rules have no description to read. So seniority is dropped at the
# source, where the signal actually is. Per-site `skip_senior: false` opts out.
SENIOR_TITLE_RE = re.compile(r"\b(senior|snr|staff|principal|lead|leader|head of|"
                             r"director|vp|chief|manager|expert|architect)\b", re.I)

DEFAULT_WAIT_MS = 6000
DEFAULT_MAX_PER_SITE = 120
POLITE_DELAY_S = 2.0


def _clean_title(title: str) -> str:
    return PREFIX_RE.sub("", " ".join(str(title or "").split())).strip(" -–—:")


def _looks_like_role(title: str) -> bool:
    t = " ".join(str(title or "").split())
    if not (8 <= len(t) <= 140):
        return False
    if NAV_EXACT_RE.match(t):
        return False
    return bool(re.search(r"[A-Za-z]{3}", t))


def _urls_for(site: dict) -> list[str]:
    """One URL per search term, or just the page itself when none are given."""
    queries = site.get("queries") or []
    if not queries:
        return [site["url"]]
    param = site.get("query_param", "q")
    sep = "&" if "?" in site["url"] else "?"
    return [f"{site['url']}{sep}{param}={quote_plus(str(q))}" for q in queries]


def _harvest(page, site: dict, base: str) -> list[Job]:
    """Pull (title, href, location) triples out of a rendered page."""
    selector = site.get("selector")
    if selector:
        raw = page.eval_on_selector_all(selector, """els => els.map(e => {
            const a = e.matches('a') ? e : e.querySelector('a');
            return {t: (e.innerText || '').trim(), h: a ? a.href : ''};
        })""")
    else:
        raw = page.eval_on_selector_all("a[href]", """els => els.map(e => (
            {t: (e.innerText || e.textContent || '').trim(), h: e.href}))""")

    company = site.get("company") or urlsplit(base).netloc
    out, seen = [], set()
    for item in raw:
        href, title = item.get("h") or "", item.get("t") or ""
        if not href or not title:
            continue
        if not selector and not JOB_HREF_RE.search(href):
            continue
        # the anchor text is often "Title\nLocation\nContract"
        parts = [p.strip() for p in title.splitlines() if p.strip()]
        role = _clean_title(parts[0] if parts else "")
        location = site.get("location", "")
        for p in parts[1:3]:
            if re.search(r"[A-Za-z]{3}", p) and len(p) < 60:
                location = p
                break
        if not _looks_like_role(role):
            continue
        if site.get("skip_senior", True) and SENIOR_TITLE_RE.search(role):
            continue
        url = urljoin(base, href)
        if url in seen:
            continue
        seen.add(url)
        out.append(Job(source=f"careers:{company.lower()}", company=company,
                       title=role, url=url, location=location,
                       description=" · ".join(parts[1:4])))
        if len(out) >= int(site.get("max", DEFAULT_MAX_PER_SITE)):
            break
    return out


@register("headless_careers")
def fetch(settings: Settings) -> list[Job]:
    cfg = settings.cfg.get("headless_careers") or {}
    sites = [s for s in (cfg.get("sites") or []) if s.get("url")]
    if not sites:
        return []
    if not cfg.get("enabled", True):
        print("  [headless_careers] disabled in config")
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [headless_careers] skipped: playwright not installed "
              "(pip install playwright && playwright install chromium)")
        return []

    jobs: list[Job] = []
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001 — browser binary missing, etc.
                print(f"  [headless_careers] skipped: cannot launch chromium ({exc})")
                return []
            ctx = browser.new_context(user_agent=BROWSER_UA,
                                      viewport={"width": 1400, "height": 1000})
            for i, site in enumerate(sites):
                name = site.get("company") or site["url"]
                seen_urls: set[str] = set()
                found_here = 0
                # These sites show ~10-20 roles and ignore paging in the URL, so
                # coverage comes from asking several times rather than scrolling:
                # Dyson's board hides its graduate roles behind ?q=graduate.
                for url in _urls_for(site):
                    try:
                        page = ctx.new_page()
                        page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        if site.get("wait_for"):
                            page.wait_for_selector(site["wait_for"], timeout=20000)
                        else:
                            page.wait_for_timeout(int(site.get("wait_ms", DEFAULT_WAIT_MS)))
                        for job in _harvest(page, site, url):
                            if job.url in seen_urls:
                                continue
                            seen_urls.add(job.url)
                            jobs.append(job)
                            found_here += 1
                        page.close()
                    except Exception as exc:  # noqa: BLE001 — one query must not stop the rest
                        print(f"  [headless_careers] {name} <{url}>: "
                              f"failed ({type(exc).__name__}: {exc})")
                    time.sleep(POLITE_DELAY_S)
                print(f"  [headless_careers] {name}: {found_here} role(s)")
                if i < len(sites) - 1:
                    time.sleep(POLITE_DELAY_S)
            browser.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  [headless_careers] aborted: {exc}")
    return jobs
