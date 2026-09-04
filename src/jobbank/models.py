"""Common Job dataclass shared by every fetcher (spec sec 8)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                   "utm_content", "ref", "src", "gh_src", "lever-source"}


def normalize_url(url: str) -> str:
    """Canonical form used for exact dedupe: strip tracking params + fragment,
    lowercase host, drop trailing slash."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower() or "https", parts.netloc.lower(),
                       path, urlencode(query), ""))


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


_LEGAL_SUFFIX_RE = re.compile(
    r"\b(ltd|limited|inc|incorporated|llc|plc|gmbh|sarl|sas|sa|bv|ab|oy|as|"
    r"co|corp|corporation|company|group|holdings|international)\b", re.I)


def norm_company(name: str) -> str:
    """Company name reduced to comparable tokens (no punctuation/legal suffixes)."""
    n = re.sub(r"[^\w\s]", " ", (name or "").lower())
    n = _LEGAL_SUFFIX_RE.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


def _has_subsequence(hay: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(hay):
        return False
    return any(hay[i:i + len(needle)] == needle for i in range(len(hay) - len(needle) + 1))


def matches_watchlist(company: str, watchlist_names: set[str]) -> bool:
    """Token-aware watchlist match.

    Exact equality silently failed for real targets — the watchlist says
    "Mistral" but postings say "Mistral AI"; "Sony / PlayStation" vs "Sony";
    "Haas F1" vs "Haas F1 Team". Matches when either name's tokens appear
    consecutively inside the other.
    """
    c = norm_company(company)
    if not c:
        return False
    ctok = c.split()
    for w in watchlist_names:
        wn = norm_company(w)
        if len(wn) < 3:
            continue
        wtok = wn.split()
        if _has_subsequence(ctok, wtok) or _has_subsequence(wtok, ctok):
            return True
    return False


_NOT_POSTING_PATH_RE = re.compile(
    r"/(search|careers|jobs|positions|openings|vacancies|opportunities|early-careers)/?$",
    re.I)
_SEARCH_QUERY_RE = re.compile(r"[?&](q|key|query|keyword|keywords|search(_?term)?)=", re.I)


def is_posting_url(url: str) -> bool:
    """Does this URL point at ONE job, rather than a place to look for jobs?

    Trackr handed the bank an Apple "posting" whose link was
    jobs.apple.com/en-gb/search?key=internship&team=... — a search page. It got
    LLM-scored 72 on the strength of its JS-shell "please enable Javascript"
    description and sat in the bank as if it were a role. The rule: either
    there is a specific job posting or there isn't.

    Deliberately conservative: only URL shapes that clearly cannot be a single
    posting are rejected (a bare domain, a path ending in /careers or /jobs, a
    search query string). A weird-but-specific posting URL must always pass.
    """
    u = str(url or "").strip()
    if not u:
        return False
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(u)
    except ValueError:
        return False
    path = parts.path.rstrip("/") or "/"
    if path == "/":
        return False                       # a bare domain is a website, not a job
    if "/search" in path.lower():
        return False
    if _NOT_POSTING_PATH_RE.search(path):
        return False
    if _SEARCH_QUERY_RE.search("?" + parts.query):
        return False
    return True


@dataclass
class OpeningSoonEntry:
    """A programme announced/predicted but not yet open (spec sec 6)."""
    company: str
    programme: str
    expected_open: str = ""     # free text or ISO date
    expected_close: str = ""
    status: str = "Announced"   # Announced | Predicted from last year | OPEN NOW
    link: str = ""
    notes: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        self.company = norm_text(self.company)
        self.programme = norm_text(self.programme)
        # YAML parses bare ISO dates into date objects; the sheet needs strings
        self.expected_open = str(self.expected_open) if self.expected_open else ""
        self.expected_close = str(self.expected_close) if self.expected_close else ""

    @property
    def key(self) -> str:
        return f"{self.company.lower()}|{self.programme.lower()}".strip()


@dataclass
class Job:
    source: str
    company: str
    title: str
    url: str
    location: str = ""
    description: str = ""          # full text when the source provides it
    posted: date | None = None
    deadline: date | None = None
    start: str = ""                # free text, e.g. "Summer 2027"
    duration: str = ""             # free text, e.g. "12 months"
    salary: str = ""
    ats_id: str = ""               # stable ID from the source/ATS when available
    extra: dict[str, Any] = field(default_factory=dict)

    # filled by the pipeline
    score: int | None = None
    score_reason: str = ""
    summary: str = ""        # what the role actually involves (LLM, factual)
    category: str = ""
    attainability_note: str = ""
    priority: bool = False

    def __post_init__(self) -> None:
        self.company = norm_text(self.company)
        self.title = norm_text(self.title)
        self.location = norm_text(self.location)
        self.url = (self.url or "").strip()

    @property
    def norm_url(self) -> str:
        return normalize_url(self.url)

    @property
    def dedupe_key(self) -> str:
        """Exact-match identity: ATS ID if present, else normalized URL."""
        basis = f"{self.source}:{self.ats_id}" if self.ats_id else self.norm_url
        return hashlib.sha1(basis.encode()).hexdigest()

    @property
    def fuzzy_text(self) -> str:
        return f"{self.company} {self.title}".lower()

    @property
    def role_signature(self) -> str:
        """Collapses near-identical same-company postings (e.g. a company listing
        BS/MS/by-location variants of the same intern role) to one signature."""
        t = self.title.lower()
        t = re.sub(r"\b(bs|ms|ba|ma|phd|bachelor'?s?|master'?s?|degree)\b", " ", t)
        t = re.sub(r"\b(summer|fall|autumn|winter|spring)\b|\b20\d\d\b", " ", t)
        t = re.sub(r"[-,/|()]", " ", t)
        t = re.sub(r"\b(intern(ship)?s?|placements?|programme?|program|student|"
                   r"co[- ]?op|20\d\d)\b", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return f"{self.company.lower()}|{t}"

    @property
    def haystack(self) -> str:
        """Everything the filters look at."""
        return " ".join([self.title, self.company, self.location,
                         self.duration, self.start, self.description]).lower()
