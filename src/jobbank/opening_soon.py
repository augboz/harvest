"""Opening Soon tracking (spec sec 6).

- Seeds the Opening Soon tab from config + any source-provided upcoming entries.
- Dedupes against existing rows (company|programme).
- On each run checks rows whose expected open date has passed, and flips them to
  "OPEN NOW" ONLY when a matching posting has actually been fetched. Most open
  dates are guesses ("Predicted from last year"), so the date passing is not
  evidence; a row with no posting behind it becomes "Due to open" and is checked
  again next run. Only a confirmed row raises a NOW OPEN alert for the digest.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml
from dateutil import parser as dateparser

from .models import OpeningSoonEntry
from .opening_soon_evidence import build_index, find_in_index

MAX_NEW_PER_RUN = 60  # keep the Opening Soon tab curated, not flooded
# A predicted date that has passed with nothing behind it. Distinct from the
# prediction itself, so the row is not re-announced every run, and distinct from
# OPEN NOW, which now means a posting was actually found.
DUE_STATUS = "Due to open"
# A row that WAS confirmed open but whose posting has not been seen for several
# runs. Deliberately does not contain the words "OPEN NOW": the dashboard treats
# that substring as "live", and a vanished posting is the opposite of live.
GONE_STATUS = "Posting gone (was open)"
# One missed run is LinkedIn blocking a CI IP, not a closed posting. Three
# consecutive days of absence is a posting that actually came down.
GONE_AFTER_DAYS = 3

_CONFIRMED_RE = re.compile(r"Confirmed open on (\d{4}-\d{2}-\d{2})")
_SEEN_RE = re.compile(r"\| seen (\d{4}-\d{2}-\d{2})")
_KNOWN_RE = re.compile(r"found (.+?) at (https?://\S+?)(?:\s*\||\s*$)")
# Postings the dashboard rejected for this row ("not this one — keep watching").
# Space-separated URLs at the note's tail; carried through every rewrite, or a
# skipped posting would come straight back on the next run.
_SKIP_RE = re.compile(r"\|\s*skip ([^|]+?)\s*$")


def _skips(note: str) -> set[str]:
    m = _SKIP_RE.search(str(note or ""))
    return set(m.group(1).split()) if m else set()


def _skip_seg(skips) -> str:
    return f" | skip {' '.join(sorted(skips))}" if skips else ""


def _confirm_note(hit, first: str, today: date, score_job=None,
                  skips=frozenset()) -> str:
    """The OPEN NOW note, machine-parseable at both ends.

    `Confirmed open on D: found 'Title' at URL | loc L | score N | seen D2`
    The dashboard reads url/loc/score out of this to fill the row's Location and
    Score cells — Opening Soon has no columns for them, and growing the schema
    for two derived values is how the Archive drifted.
    """
    score = getattr(hit, "score", None)
    if score is None and score_job is not None:
        try:
            score = score_job(hit)
        except Exception:  # noqa: BLE001 — an unscored confirmation still confirms
            score = None
    parts = [f"Confirmed open on {first}: found {hit.title!r} at {hit.url}"]
    if getattr(hit, "location", ""):
        parts.append(f"loc {hit.location}")
    if score is not None:
        parts.append(f"score {int(score)}")
    parts.append(f"seen {today.isoformat()}")
    return " | ".join(parts) + _skip_seg(skips)


def _refresh_open_now(backend, row, index, today: date, score_job=None) -> None:
    """Re-verify an OPEN NOW row instead of trusting it forever.

    The note was written once and never touched again, so its URL pointed at
    whatever posting existed on confirmation day — which for Google was a
    LinkedIn listing that answered "Job not found" three weeks later while the
    row still said OPEN NOW.
    """
    from .opening_soon_evidence import find_in_index
    prev = str(row.get("Notes") or "")
    skips = _skips(prev)
    m = _CONFIRMED_RE.search(prev)
    first = m.group(1) if m else today.isoformat()
    hit = find_in_index(row.get("Company", ""), row.get("Programme", ""), index,
                        exclude=skips)
    if hit is not None:
        note = _confirm_note(hit, first, today, score_job, skips)
        if note != prev:
            backend.set_opening_soon_status(row["row"], "OPEN NOW", note)
        return
    m = _SEEN_RE.search(prev)
    last_seen = _parse_date(m.group(1) if m else first)
    if last_seen is None or (today - last_seen).days < GONE_AFTER_DAYS:
        return              # a missed run or two is the fetcher's problem
    known = _KNOWN_RE.search(prev)
    tail = f" Last known: {known.group(1)} at {known.group(2)}" if known else ""
    backend.set_opening_soon_status(
        row["row"], GONE_STATUS,
        f"Not seen since {last_seen.isoformat()}; the posting has probably "
        f"closed.{tail}{_skip_seg(skips)}")


def load_seed(root: Path) -> list[OpeningSoonEntry]:
    path = root / "config" / "opening_soon_seed.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [OpeningSoonEntry(source="seed", **e) for e in data.get("entries", [])]


def _parse_date(s: str) -> date | None:
    if not s:
        return None
    try:
        return dateparser.parse(s, dayfirst=False).date()
    except (ValueError, OverflowError, TypeError):
        return None


def sync_opening_soon(backend, seed: list[OpeningSoonEntry],
                      extra: list[OpeningSoonEntry] | None = None,
                      today: date | None = None,
                      jobs: list | None = None,
                      score_job=None) -> tuple[int, list[str]]:
    """Append new entries, and confirm or defer rows whose open date has passed.

    `jobs` is what the run fetched. A row is only called OPEN NOW when one of
    those postings matches it, so the flag means "there is something to apply
    to", not "the date somebody guessed has arrived".
    Returns (num_added, now_open_alerts)."""
    today = today or date.today()
    existing = backend.read_opening_soon()
    existing_keys = {f"{e['Company'].lower()}|{e['Programme'].lower()}" for e in existing}

    incoming = list(seed) + list(extra or [])
    fresh, seen = [], set(existing_keys)
    for e in incoming:
        if e.key not in seen:
            fresh.append(e)
            seen.add(e.key)
    # sort soonest-opening first and cap so the tab stays curated, not flooded
    fresh.sort(key=lambda e: e.expected_open or "9999")
    fresh = fresh[:MAX_NEW_PER_RUN]
    if fresh:
        backend.append_opening_soon(fresh)

    # Every tracked row is checked, whatever its date says. A row with no date at
    # all is the normal case now — we only record a date when we have a basis for
    # one — and such a row still deserves to be spotted the day it opens.
    index = build_index(jobs)
    now_open: list[str] = []
    for row in existing:
        status = str(row.get("Status") or "")
        # dismissed from the dashboard: the row stays as a record, but flipping it
        # back to OPEN NOW would resurrect something deliberately put down
        if status.lower().startswith("dismissed"):
            continue
        if status == "OPEN NOW":
            _refresh_open_now(backend, row, index, today, score_job)
            continue
        opened = _parse_date(row.get("Expected open date", ""))
        skips = _skips(row.get("Notes"))
        hit = find_in_index(row.get("Company", ""), row.get("Programme", ""), index,
                            exclude=skips)
        if hit is None and not (opened and opened <= today):
            continue          # nothing found, and nothing was expected yet
        if hit is not None:
            note = _confirm_note(hit, today.isoformat(), today, score_job, skips)
            backend.set_opening_soon_status(row["row"], "OPEN NOW", note)
            now_open.append(f"{row['Company']} — {row['Programme']}")
        elif status != DUE_STATUS and not status.startswith("Posting gone"):
            # A gone row must not churn back to "Due to open" — it was open, it
            # went; the honest states from here are "found again" or silence.
            # Say it once. Rewriting this every run would churn the sheet and
            # bury whatever the row said before.
            note = (f"Predicted to open {opened.isoformat()}; checked "
                    f"{today.isoformat()} and no matching posting has appeared yet"
                    f"{_skip_seg(skips)}")
            backend.set_opening_soon_status(row["row"], DUE_STATUS, note)
    return len(fresh), now_open
