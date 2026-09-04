"""hyrd.dev and RateMyPlacement (now higherin.com) are JS-only single-page apps:
their listings are not present in server-rendered HTML and the underlying data
endpoints sit behind robots-disallowed paths (RateMyPlacement) or unexposed
client XHR (hyrd). A polite scraper can't reach them without a headless browser,
so they're registered as honest logged skips until a Playwright pass is added
(deferred to the optional Phase 4 browser-agent work).

Verified 2026-07-15 — see research/phase2-sources.md.
"""
from __future__ import annotations

from ..models import Job
from ..settings import Settings
from . import register


@register("hyrd")
def fetch_hyrd(settings: Settings) -> list[Job]:
    if settings.cfg.get("placement_boards", {}).get("hyrd", True):
        print("  [hyrd] skipped: JS-only SPA, needs headless render (deferred). "
              "Low-volume beta board.")
    return []


@register("ratemyplacement")
def fetch_ratemyplacement(settings: Settings) -> list[Job]:
    if settings.cfg.get("placement_boards", {}).get("ratemyplacement", True):
        print("  [ratemyplacement] skipped: higherin.com cards are JS-rendered and the "
              "data API is robots-disallowed; needs headless render (deferred).")
    return []
