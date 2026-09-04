"""Lightweight, deterministic enrichment of a Job's free-text timing fields
(duration / start) parsed from its title + description. Runs during normalize
so both the sheet and the filters benefit, and it works without any API key.
"""
from __future__ import annotations

import re

from .models import Job

_MONTHS_NUM_RE = re.compile(r"\b(\d{1,2})[\s-]*month", re.I)
_YEARLONG_RE = re.compile(
    r"\b(year[\s-]?long|year in industry|placement year|industrial placement|"
    r"12[\s-]?month|13[\s-]?month|sandwich (year|placement|course))\b", re.I)
_SEASON_RE = re.compile(
    r"\b(summer|spring|autumn|fall|winter|jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|"
    r"may|jun(e)?|jul(y)?|aug(ust)?|sep(tember)?|oct(ober)?|nov(ember)?|dec(ember)?)"
    r"\s*'?\s*(20\d\d)\b", re.I)
_YEAR_RE = re.compile(r"\b(20\d\d)\b")
_SUMMER_RE = re.compile(r"\bsummer\b", re.I)
_INTERNISH_RE = re.compile(
    r"\b(intern|interns|internship|internships|placement|placements|"
    r"stage|co[- ]?op)\b", re.I)


def extract_duration(text: str) -> str:
    if _YEARLONG_RE.search(text):
        return "12 months"
    m = _MONTHS_NUM_RE.search(text)
    if m:
        n = int(m.group(1))
        if 3 <= n <= 18:
            return f"{n} months"
    if re.search(r"\bco[\s-]?op\b", text, re.I):
        return "Co-op (4-8 months)"
    if re.search(r"\b(spring|insight)\s+(week|programme|program)\b", text, re.I):
        return "1-2 weeks"
    if _SUMMER_RE.search(text):
        return "~10-12 weeks (summer)"
    # last resort: say it's an internship of unstated length rather than blank,
    # so the column always tells Aug something
    if re.search(r"\bintern(ship)?\b", text, re.I):
        return "Internship (TBC)"
    if re.search(r"\bplacement\b", text, re.I):
        return "Placement (TBC)"
    return ""


def extract_start(text: str) -> str:
    m = _SEASON_RE.search(text)
    if m:
        season = m.group(1).title()
        # normalise month abbreviations to a clean label
        return f"{season} {m.group(len(m.groups()))}"
    if _SUMMER_RE.search(text):
        yrs = [int(y) for y in _YEAR_RE.findall(text)]
        future = [y for y in yrs if y >= 2027]
        return f"Summer {min(future)}" if future else "Summer"
    yrs = [int(y) for y in _YEAR_RE.findall(text) if 2026 <= int(y) <= 2029]
    if yrs:
        return str(min(yrs))
    # an honest placeholder beats a blank cell when the posting never says
    if _INTERNISH_RE.search(text):
        return "TBC"
    return ""


# --- location normalisation -------------------------------------------------
# Sources disagree wildly: "London, England, GB", "Edinburgh, Scotland, GB",
# "Banbury, Oxfordshire, United Kingdom", "Paris", "UK". Everything is reduced to
# a single "City, Country" shape (or just "Country" / "Remote" when that's all
# there is) so the column is scannable and the colour rules match reliably.
_COUNTRY_ALIASES = {
    "uk": "UK", "u.k.": "UK", "united kingdom": "UK", "gb": "UK", "gbr": "UK",
    "great britain": "UK", "england": "UK", "scotland": "UK", "wales": "UK",
    "northern ireland": "UK",
    "france": "France", "fr": "France", "fra": "France",
    "germany": "Germany", "de": "Germany", "deutschland": "Germany",
    "netherlands": "Netherlands", "nl": "Netherlands", "holland": "Netherlands",
    "spain": "Spain", "es": "Spain", "italy": "Italy", "it": "Italy",
    "ireland": "Ireland", "ie": "Ireland", "switzerland": "Switzerland",
    "belgium": "Belgium", "portugal": "Portugal", "poland": "Poland",
    "sweden": "Sweden", "denmark": "Denmark", "norway": "Norway",
    "finland": "Finland", "austria": "Austria", "czechia": "Czechia",
    "serbia": "Serbia", "romania": "Romania", "greece": "Greece",
    "usa": "USA", "us": "USA", "u.s.": "USA", "u.s.a.": "USA",
    "united states": "USA", "united states of america": "USA",
    "canada": "Canada",  # NB: bare "CA" is California, handled as a US state
    "india": "India", "china": "China", "japan": "Japan", "singapore": "Singapore",
    "australia": "Australia", "israel": "Israel", "uae": "UAE",
}
# cities common in the target regions whose country the posting often omits
_CITY_COUNTRY = {
    "london": "UK", "cambridge": "UK", "oxford": "UK", "manchester": "UK",
    "edinburgh": "UK", "glasgow": "UK", "bristol": "UK", "leeds": "UK",
    "birmingham": "UK", "sheffield": "UK", "reading": "UK", "brighton": "UK",
    "banbury": "UK", "brackley": "UK", "silverstone": "UK", "grove": "UK",
    "enstone": "UK", "woking": "UK", "milton keynes": "UK", "bicester": "UK",
    "northampton": "UK", "basildon": "UK", "guildford": "UK",
    "paris": "France", "lyon": "France", "toulouse": "France", "bordeaux": "France",
    "lille": "France", "nantes": "France", "marseille": "France", "nice": "France",
    "grenoble": "France", "rennes": "France", "sophia antipolis": "France",
    "berlin": "Germany", "munich": "Germany", "münchen": "Germany",
    "hamburg": "Germany", "amsterdam": "Netherlands", "eindhoven": "Netherlands",
    "dublin": "Ireland", "madrid": "Spain", "barcelona": "Spain",
    "milan": "Italy", "rome": "Italy", "turin": "Italy", "zurich": "Switzerland",
    "geneva": "Switzerland", "lausanne": "Switzerland", "brussels": "Belgium",
    "lisbon": "Portugal", "warsaw": "Poland", "stockholm": "Sweden",
    "copenhagen": "Denmark", "belgrade": "Serbia", "vienna": "Austria",
}
_US_STATES = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
    "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
    "va","wa","wv","wi","wy","dc",
    "california","texas","new york","washington","massachusetts","illinois",
    "colorado","georgia","florida","virginia","oregon","arizona","utah",
}
_CA_PROVINCES = {"on","bc","qc","ab","ns","mb","sk","ontario","quebec",
                 "british columbia","alberta"}
_POSTCODE_RE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9 ]{4,9}$")
_REMOTE_RE = re.compile(r"\b(remote|anywhere|work from home|wfh)\b", re.I)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
_NOISE_RE = re.compile(r"\b(undefined|null|none|n/?a|various|multiple locations)\b", re.I)


def normalize_location(raw: str) -> str:
    if not raw:
        return ""
    text = _NOISE_RE.sub(" ", raw)
    remote = bool(_REMOTE_RE.search(text))
    hybrid = bool(_HYBRID_RE.search(text))
    # split on commas, slashes, pipes and spaced dashes ("Remote - USA"), but not
    # on hyphens inside names ("Stratford-upon-Avon")
    parts = [p.strip(" \t-–—") for p in re.split(r"[,/|]|\s+[-–—]\s+", text)]
    parts = [p for p in parts
             if p and not _REMOTE_RE.fullmatch(p) and not _HYBRID_RE.fullmatch(p)]

    country = ""
    for p in reversed(parts):
        key = p.lower().strip(". ")
        if key in _COUNTRY_ALIASES:
            country = _COUNTRY_ALIASES[key]
            parts = parts[:parts.index(p)]
            break
        if key in _US_STATES:            # "Mountain View, CA" -> USA
            country = "USA"
            parts = parts[:parts.index(p)]
            break
        if key in _CA_PROVINCES:         # "Waterloo, ON" -> Canada
            country = "Canada"
            parts = parts[:parts.index(p)]
            break

    city = ""
    for p in parts:
        key = p.lower().strip(". ")
        if key in _COUNTRY_ALIASES:          # e.g. "England" used as a region
            if not country:
                country = _COUNTRY_ALIASES[key]
            continue
        if key in _US_STATES or key in _CA_PROVINCES:
            continue
        if _POSTCODE_RE.match(p) or p.isdigit():
            continue                      # postcode, not a city
        if len(p) > 1:
            city = p.strip()
            break

    if city and not country:
        country = _CITY_COUNTRY.get(city.lower(), "")
    if city:
        city = city.title() if city.islower() or city.isupper() else city

    if city and country:
        out = f"{city}, {country}"
    elif city:
        out = city
    elif country:
        out = country
    else:
        out = ""

    if remote:
        return f"Remote, {out}" if out else "Remote"
    if hybrid and out:
        return f"{out} (hybrid)"
    # never destroy information: if nothing parsed (e.g. a bare postcode), keep
    # the original text rather than blanking the cell
    return out or re.sub(r"\s+", " ", raw).strip()


def enrich_location(job: Job) -> None:
    norm = normalize_location(job.location)
    if norm:
        job.location = norm


def enrich_timing(job: Job) -> None:
    # scan the FULL description: duration/start are often stated late in the
    # posting ("The placement runs for 12 months from September 2027").
    text = f"{job.title}  {job.duration}  {job.start}  {job.description}"
    if not job.duration:
        job.duration = extract_duration(text)
    if not job.start:
        job.start = extract_start(text)


# --- salary ----------------------------------------------------------------
# Adzuna and Reed both return salary as a min/max pair of bare numbers, so the
# currency has to come from the country the query was made against. Only the
# countries either profile actually searches are listed; anything else falls
# back to no symbol rather than guessing wrong.
_CURRENCY = {
    "gb": "£", "us": "$", "ca": "C$", "au": "A$", "nz": "NZ$",
    "fr": "€", "de": "€", "nl": "€", "it": "€", "es": "€", "ie": "€",
    "be": "€", "at": "€", "pt": "€", "ch": "CHF ", "pl": "zł ",
    "in": "₹", "sg": "S$",
}


def format_salary(low: float | None, high: float | None, country: str = "gb",
                  estimated: bool = False) -> str:
    """A salary a person can read, or "" when the source gave us nothing.

    Sources report a missing bound as absent, null or 0 depending on the day, so
    all three are treated as "not stated" — "£0 - £45,000" is worse than silence.
    """
    sym = _CURRENCY.get((country or "").lower(), "")

    def money(n: float) -> str:
        return f"{sym}{int(round(n)):,}"

    lo = low if low and low > 0 else None
    hi = high if high and high > 0 else None
    if lo and hi and int(lo) != int(hi):
        out = f"{money(lo)} - {money(hi)}"
    elif lo:
        out = money(lo)
    elif hi:
        # a ceiling on its own is not the figure being offered, and printing it
        # bare would read as one
        out = f"up to {money(hi)}"
    else:
        return ""
    # Adzuna infers a figure when the employer did not publish one; saying so is
    # the difference between information and a rumour.
    return f"{out} (est.)" if estimated else out
