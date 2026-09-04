"""Does a tracked programme have a real posting behind it yet?

Opening Soon rows carry a PREDICTED open date — most of the seed is labelled
"Predicted from last year", which means somebody guessed a plausible month and
wrote the 1st of it. The date passing is therefore not evidence of anything, and
flipping a row to OPEN NOW on the date alone turns that guess into an assertion
the digest then leads with.

This module answers the only question that should flip a row: has a posting of
the right kind, from the right employer, actually appeared? It matches against
the jobs the run already fetched, so confirming costs no extra requests.

Matching is deliberately strict. A false "it's open, go and apply" wastes a real
trip to a careers site and teaches you to distrust the flag; a missed
confirmation just leaves the row saying "due to open", which the dashboard
already renders as "check it". When in doubt, say less.
"""
from __future__ import annotations

import re

from .models import is_posting_url

# What KIND of thing a row is tracking, so a Senior Aerodynamicist vacancy is
# never taken as proof that the industrial placement opened.
_KIND_WORDS = (
    "placement", "internship", "intern", "graduate", "apprentice", "apprenticeship",
    "trainee", "year in industry", "co-op", "coop", "scheme", "programme", "program",
)

# Words that identify the corporate entity rather than the business: dropping
# them lets "Red Bull Racing" match "Red Bull Technology" without letting
# "McLaren Racing" match "McLaren Automotive", which is a different employer.
_NOISE = re.compile(
    r"\b(ltd|limited|plc|inc|llc|corp|corporation|gmbh|ag|sa|bv|nv|group|holdings|"
    r"team|racing|f1|formula\s*1|motorsport|uk|usa|international)\b", re.I)


def company_key(name: str) -> str:
    """A comparable identity for an employer name."""
    n = re.sub(r"[^a-z0-9 ]", " ", str(name or "").lower())
    n = _NOISE.sub(" ", n)
    return " ".join(n.split())


def same_employer(a: str, b: str) -> bool:
    """Two names for the same employer.

    Exact match, or one name's words being wholly contained in the other's — but
    only when the shorter has at least two words. Without that floor a one-word
    key swallows anything starting with it, and "McLaren" would confirm McLaren
    Racing's placement from a McLaren Automotive posting.
    """
    ka, kb = company_key(a), company_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    ta, tb = set(ka.split()), set(kb.split())
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(short) >= 2 and short <= long_


def kinds(programme: str) -> set[str]:
    p = str(programme or "").lower()
    return {w for w in _KIND_WORDS if w in p}


def build_index(jobs) -> list[tuple[set[str], object]]:
    """Normalise every fetched employer name once.

    Every tracked row is now checked on every run, so doing this per row meant
    re-normalising thousands of job records dozens of times a run.
    """
    out = []
    for job in jobs or []:
        key = company_key(getattr(job, "company", ""))
        if key:
            out.append((set(key.split()), job))
    return out


def find_in_index(company: str, programme: str, index,
                  exclude=None) -> object | None:
    """The BEST posting that proves this programme has opened, or None.

    Best, not first: Mercedes' "Industrial Placement (Software/Data)" was being
    confirmed by an HR placement posting because that one happened to come
    earlier in the fetch order. Any kind-matching posting still proves the
    programme opened; among them, the one sharing the most programme words is
    the one the row should point at — and the one worth scoring.
    """
    want = company_key(company)
    if not want or not index:
        return None
    want_tokens = set(want.split())
    wanted = kinds(programme)
    prog_words = {w for w in re.findall(r"[a-z]+", str(programme or "").lower())
                  if len(w) > 2}
    exclude = exclude or frozenset()
    best, best_overlap = None, -1
    for tokens, job in index:
        if str(getattr(job, "url", "")) in exclude:
            continue          # the dashboard said "not this one" — respect it
        if not is_posting_url(getattr(job, "url", "")):
            continue          # a careers/search page proves nothing is open
        if want_tokens != tokens:
            short, long_ = ((want_tokens, tokens) if len(want_tokens) <= len(tokens)
                            else (tokens, want_tokens))
            if not (len(short) >= 2 and short <= long_):
                continue
        title = str(getattr(job, "title", "") or "").lower()
        # If the row never said what kind of opening it is, any posting from that
        # employer is the best evidence available.
        if wanted and not any(k in title for k in wanted):
            continue
        overlap = len(prog_words & set(re.findall(r"[a-z]+", title)))
        if overlap > best_overlap:
            best, best_overlap = job, overlap
    return best


def find_live_posting(company: str, programme: str, jobs) -> object | None:
    """Convenience for one lookup against raw fetched jobs."""
    return find_in_index(company, programme, build_index(jobs))
