"""Paste-in tab processing (spec sec 3): URLs Aug drops in himself (LinkedIn
alerts, word of mouth). Each run fetches, extracts a Job, filters, scores, and
files the result back to the row. Paste-in items are user-curated so they bypass
the weekly cap (like priority) when they clear MIN_SCORE.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .enrich import enrich_timing
from .models import Job
from .profile_impl import filters_for, scorer_for
from .settings import Settings

TAG_RE = re.compile(r"<[^>]+>")


def _fetch_html(url: str) -> str | None:
    import httpx
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
    try:
        r = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
        if r.status_code == 200:
            return r.text
        if r.status_code in (403, 429):
            try:
                from curl_cffi import requests as creq
                r2 = creq.get(url, impersonate="chrome", timeout=30)
                if r2.status_code == 200:
                    return r2.text
            except ImportError:
                pass
    except Exception:  # noqa: BLE001
        return None
    return None


def _extract_job(url: str, notes: str) -> Job | None:
    html = _fetch_html(url)
    host = urlsplit(url).netloc.replace("www.", "")
    if not html:
        # can't read the page (JS/login wall e.g. LinkedIn) — still create a
        # minimal Job from the note text so it can be scored, honestly labelled.
        if not notes:
            return None
        return Job(source="paste-in", company=host, title=notes[:120], url=url,
                   description=notes)
    soup = BeautifulSoup(html, "html.parser")

    def meta(prop):
        el = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return el.get("content", "").strip() if el else ""

    title = meta("og:title") or (soup.title.get_text(strip=True) if soup.title else "")
    h1 = soup.find("h1")
    if h1 and len(h1.get_text(strip=True)) > 5:
        title = h1.get_text(strip=True)
    company = meta("og:site_name") or host.split(".")[0].title()
    desc = meta("og:description") or meta("description")
    body = TAG_RE.sub(" ", html)
    body = re.sub(r"\s+", " ", body)[:6000]
    return Job(source="paste-in", company=company, title=title or notes or host,
               url=url, description=f"{desc} {body}"[:8000],
               extra={"paste_note": notes})


def process_paste_in(backend, settings: Settings, state) -> dict:
    rows = backend.read_paste_in()
    if not rows:
        return {"processed": 0, "bank": 0, "archive": 0}
    print(f"paste-in: processing {len(rows)} URL(s)")
    # Must match the profile — one profile's filters reject graduate schemes and
    # another's reject internships, so the wrong pair here inverts every verdict.
    _filters = filters_for(settings)
    FilterStats, apply_hard_filters = _filters.FilterStats, _filters.apply_hard_filters
    score_jobs = scorer_for(settings).score_jobs
    bank, archive = [], []
    for row in rows:
        job = _extract_job(row["url"], row.get("notes", ""))
        if job is None:
            backend.mark_paste_in(row["row"], "could not read URL")
            continue
        enrich_timing(job)
        _passed, rejected = apply_hard_filters([job], settings.freshness_days, FilterStats())
        if rejected:
            reason = rejected[0][1]
            backend.mark_paste_in(row["row"], f"filtered: {reason}")
            state.record(job, "filtered", f"paste-in: {reason}")
            continue
        score_jobs([job], settings)
        if (job.score or 0) >= settings.min_score:
            job.priority = (job.score or 0) >= settings.priority_score
            bank.append(job)
            backend.mark_paste_in(row["row"], f"scored {job.score} -> Job Bank")
            state.record(job, "bank", f"paste-in: {job.score_reason}")
        else:
            archive.append(job)
            backend.mark_paste_in(row["row"], f"scored {job.score} -> Archive (below {settings.min_score})")
            state.record(job, "archive", f"paste-in: {job.score_reason}")
    if bank:
        backend.append_job_bank(bank)
    if archive:
        backend.append_archive([(j, f"paste-in score {j.score}") for j in archive])
    return {"processed": len(rows), "bank": len(bank), "archive": len(archive),
            "jobs": bank}
