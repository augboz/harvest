"""Volume gate (spec sec 5): Aug does not want hundreds of rows.

The cap used to be purely a RATE limit — "at most N new roles per week" — which
says nothing about how big the bank gets. Nothing ever leaves a bank on its own,
so three weeks of runs stacked up to 31 open roles. The gate now works against
the bank's actual size:

- `bank_ceiling` is the target size of the open list — 10 roles to work through;
- every run may use whatever space is under that ceiling, but only the score gate
  decides if anything fills it. No schedule, no refill threshold: a good role
  found on a Tuesday goes in on Tuesday, and a quiet week adds nothing at all;
- `min_score` is the quality gate, and `max_per_company` stops one employer
  filling the bank (nine of the 31 rows were TikTok);
- the ceiling is a target, not a wall: a role that is exceptional
  (`priority_score`) or closing within the week may push up to `bank_overflow`
  past it, because missing one of those costs more than a slightly long list.
  Everything else is held to the ceiling strictly;
- everything else goes to Archive with its score, so nothing is lost and the
  dashboard's "More roles" page can still surface it.

`admit_all` turns the size caps off: every role clearing `min_score` is banked,
however full the list already is. That is only sane once the LLM is doing the
scoring — the caps were standing in for quality control while the cheap keyword
heuristic clustered everything at 80. `max_per_company` still applies, and
nothing is ever evicted to make room.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Job, matches_watchlist

# TikTok postings are filed under both names, and they are one employer for the
# purposes of "don't let one company fill the bank".
_COMPANY_ALIASES = {"bytedance": "tiktok", "tiktok bytedance": "tiktok"}
_COMPANY_NOISE = re.compile(
    r"\b(group|inc|ltd|limited|llc|corp|corporation|holdings|plc|sa|ag|gmbh)\b")


def company_key(name: str) -> str:
    """Loose company identity: 'TikTok', 'ByteDance' and 'Renault Group' should
    each count as one employer, not several."""
    k = re.sub(r"[^a-z0-9 ]", " ", str(name or "").lower())
    k = _COMPANY_NOISE.sub(" ", k)
    k = re.sub(r"\s+", " ", k).strip()
    return _COMPANY_ALIASES.get(k, k)


def open_slots(open_now: int, ceiling: int) -> int:
    """How many roles the pipeline may add this run: whatever space is left.

    There is no schedule and no waiting. Every run offers the space under the
    ceiling, and the score gate decides whether anything is actually worth
    putting in it — the bank fills up because good roles turned up, not because
    it was Monday or because the list had been worked down to some threshold.
    An empty slot is not an obligation to fill it.
    """
    return max(0, ceiling - open_now)


@dataclass
class GateResult:
    bank: list[Job] = field(default_factory=list)
    archive: list[Job] = field(default_factory=list)


def volume_gate(jobs: list[Job], slots: int, min_score: int, priority_score: int,
                *, headroom: int | None = None, max_per_company: int = 0,
                company_counts: dict[str, int] | None = None,
                overflow: int = 0, admit_all: bool = False,
                watchlist: set[str] | None = None,
                watchlist_relief: int = 0) -> GateResult:
    """`slots` = normal additions allowed; `headroom` = ceiling minus what is
    already in the bank, which only priority roles may dip into beyond `slots`.
    Omitting `headroom` means "no ceiling", so priority roles bypass `slots`
    freely — the pipeline always passes one. `overflow` is how far past the
    ceiling a priority or closing-this-week role may push; everything else is
    held to it strictly. `company_counts` seeds the
    per-company tally with what the bank already holds.
    """
    result = GateResult()
    if headroom is None:
        headroom = len(jobs)
    counts = dict(company_counts or {})

    def bank_eligible(j: Job) -> bool:
        # When two-stage scoring is on, only LLM-reviewed jobs may enter the bank;
        # heuristically pre-filtered ones are archived with their cheap score.
        if "llm_scored" in j.extra:
            return bool(j.extra["llm_scored"])
        return True

    # The bar a specific job must clear. Watchlist companies get relief:
    # min_score 75 archived every Sony/Disney/Nike intern role at 48-72 for
    # weeks while Aug said "these are the sort of jobs I want to hear about" —
    # a company he chose to watch should not need the same score as a random
    # board find. max_per_company still applies, so relief cannot flood.
    def bar(j: Job) -> int:
        if watchlist_relief and watchlist and matches_watchlist(j.company, watchlist):
            return min_score - watchlist_relief
        return min_score

    qualifying = sorted((j for j in jobs
                         if (j.score or 0) >= bar(j) and bank_eligible(j)),
                        key=lambda j: j.score or 0, reverse=True)
    below = [j for j in jobs
             if (j.score or 0) < bar(j) or not bank_eligible(j)]

    added = 0
    for job in qualifying:
        job.priority = (job.score or 0) >= priority_score
        # The ceiling is a target, not a wall: a role that is both strong and
        # closing this week is worth going over for, or it is simply missed.
        exceptional = job.priority or bool(job.extra.get("urgent"))
        limit = headroom + (overflow if exceptional else 0)
        key = company_key(job.company)
        if max_per_company and counts.get(key, 0) >= max_per_company:
            result.archive.append(job)
            continue
        # admit_all: the score is the only gate. Asked for after 33 roles scoring
        # 78-82 were archived in ten days for want of space while the bank held
        # roles the LLM later scored in the forties — a 95-scoring UK motorsport
        # placement losing its seat to whatever arrived first is not a cap doing
        # its job. Nothing is displaced to make room: the bank simply grows.
        # max_per_company still applies, so one employer cannot take it over.
        room = admit_all or added < limit
        if room and (admit_all or added < slots or exceptional):
            result.bank.append(job)
            counts[key] = counts.get(key, 0) + 1
            added += 1
        else:
            result.archive.append(job)
    result.archive.extend(below)
    return result
