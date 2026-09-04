"""Verify a job's link actually resolves before it reaches the Job Bank.

Aug clicked through from a digest and landed on a dead page, so links are now
checked. Only the handful of rows about to be banked are fetched (~10-20 requests
a run), not the thousands that were merely scored.

Deliberately conservative: only an unambiguous "this posting is gone" (404/410)
drops a row. Timeouts, 403s and connection errors are common on ATS hosts that
block bots and must NOT be treated as evidence the job doesn't exist.
"""
from __future__ import annotations

import re

import httpx

from .models import Job

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DEAD_STATUSES = {404, 410}
# Lever/Greenhouse "apply" URLs are the form, not the posting — link to the posting
# so Aug can read it first (and they 404 more often once a role closes).
_APPLY_SUFFIX_RE = re.compile(r"/apply/?$", re.I)


def canonical_url(url: str) -> str:
    return _APPLY_SUFFIX_RE.sub("", url or "")


def check(url: str, timeout: float = 15.0) -> tuple[bool, str, str]:
    """Returns (alive, final_url, note). alive=False ONLY for 404/410."""
    if not url:
        return False, url, "no link"
    try:
        with httpx.Client(headers={"User-Agent": BROWSER_UA}, timeout=timeout,
                          follow_redirects=True) as http:
            resp = http.get(url)
    except Exception as exc:  # noqa: BLE001 — unreachable != gone
        return True, url, f"unverified ({type(exc).__name__})"
    if resp.status_code in DEAD_STATUSES:
        return False, url, f"HTTP {resp.status_code}"
    final = str(resp.url) or url
    if resp.status_code >= 400:
        return True, final, f"unverified (HTTP {resp.status_code})"
    return True, final, "ok"


def verify_jobs(jobs: list[Job]) -> tuple[list[Job], list[tuple[Job, str]]]:
    """Split into (alive, dead). Rewrites each surviving job's url to the
    canonical, post-redirect address."""
    alive: list[Job] = []
    dead: list[tuple[Job, str]] = []
    for job in jobs:
        job.url = canonical_url(job.url)
        ok, final, note = check(job.url)
        if not ok:
            dead.append((job, f"dead link ({note})"))
            continue
        if final and final != job.url:
            job.url = final
        alive.append(job)
    return alive, dead
