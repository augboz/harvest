"""Fetcher registry + failure-isolated runner (spec sec 8: any single source
may fail without crashing the run)."""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

import httpx

from ..models import Job
from ..settings import Settings

USER_AGENT = "harvest/0.1 (personal internship aggregator)"
# Some boards (fluidjobs) 403 any non-browser UA; use a realistic one there.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def client(browser: bool = False) -> httpx.Client:
    ua = BROWSER_UA if browser else USER_AGENT
    return httpx.Client(headers={"User-Agent": ua}, timeout=30, follow_redirects=True)


# Codes that mean "the far end is having a moment", not "your query is wrong".
# 429 is included so a caller can retry politely, but callers that treat rate
# limiting specially (trackr, adzuna) still see it in the returned response.
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524})


def fetch_with_retry(http: httpx.Client, method: str, url: str,
                     attempts: int = 3, base_delay: float = 2.0,
                     **kwargs) -> httpx.Response | None:
    """One request, retried through the failures that are the REMOTE side having
    a bad moment rather than anything wrong with what we asked for.

    Adzuna answered a single 503 mid-run and `raise_for_status()` took the whole
    source down with it — every other term and country included, none of which
    had been tried yet. A transient upstream blip should cost one query, not a
    source.

    Returns the last response, so the caller decides what a persistent failure
    means; returns None when every attempt died at the transport layer (DNS,
    timeout, reset connection) and there is no response to inspect.
    """
    resp: httpx.Response | None = None
    for i in range(attempts):
        try:
            resp = http.request(method, url, **kwargs)
        except httpx.TransportError:
            resp = None
        else:
            if resp.status_code not in TRANSIENT_STATUS:
                return resp
        if i < attempts - 1:
            time.sleep(base_delay * (i + 1))
    return resp


def json_or_none(resp: httpx.Response | None) -> dict | list | None:
    """A 200 is not a promise of JSON.

    formulacareers answered 200 with something that was not JSON — an edge/WAF
    page, most likely — and the bare `.json()` raised JSONDecodeError, which
    took the source down for the run. Callers get None and skip that page.
    """
    if resp is None or resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:          # json.JSONDecodeError subclasses this
        return None


@dataclass
class FetchResult:
    per_source: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    jobs: list[Job] = field(default_factory=list)


FetcherFn = Callable[[Settings], list[Job]]

# populated by register(); order = fetch order
_REGISTRY: dict[str, FetcherFn] = {}


def register(name: str):
    def deco(fn: FetcherFn) -> FetcherFn:
        _REGISTRY[name] = fn
        return fn
    return deco


def registry() -> dict[str, FetcherFn]:
    # import side-effect registration
    from . import (github_boards, adzuna, reed, ats,        # Phase 1  # noqa: F401
                   jobspy, hiringcafe, trackr, motorsport,  # Phase 2  # noqa: F401
                   aidevboard, wttj, gradcracker, ashby, headless_boards,  # noqa: F401
                   workday, pinpoint,  # Phase 2: the `careers` watchlist  # noqa: F401
                   headless_careers)  # employer sites that need a real browser  # noqa: F401
    return dict(_REGISTRY)


def run_all(settings: Settings, only: list[str] | None = None) -> FetchResult:
    result = FetchResult()
    for name, fn in registry().items():
        if only and name not in only:
            continue
        start = time.time()
        try:
            jobs = fn(settings)
            for j in jobs:
                j.source = j.source or name
            result.jobs.extend(jobs)
            result.per_source[name] = len(jobs)
            print(f"  [{name}] {len(jobs)} jobs in {time.time()-start:.1f}s")
        except Exception as exc:  # noqa: BLE001 — isolation is the contract
            result.per_source[name] = 0
            result.failures[name] = f"{type(exc).__name__}: {exc}"
            print(f"  [{name}] FAILED: {exc}\n{traceback.format_exc(limit=3)}")
    return result
