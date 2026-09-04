"""Hard filters (spec sec 2). Rule-based, no LLM. A job must pass ALL.

Each filter returns a rejection reason string or None. Rejections are counted
per filter for the Stats tab. Filters must work on title-only rows (GitHub
boards carry no description): absence of information never rejects, except for
the type check where an internship/placement signal is required somewhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .models import Job

# --- type ---------------------------------------------------------------
INTERN_RE = re.compile(
    r"\b(intern(ship)?s?|placements?|co[- ]?op|stage|stagiaire|year in industry|"
    r"industrial placement|off[- ]cycle|sandwich (year|course|placement)|working student|werkstudent)\b", re.I)
NOT_INTERN_RE = re.compile(
    r"\b(new grad(uate)?|graduate (scheme|program(me)?|engineer|analyst|role)|"
    r"full[- ]time|permanent|staff|senior|principal|lead|director|manager)\b", re.I)

# --- quant exclusion (absolute, spec sec 1/2) -----------------------------
QUANT_RE = re.compile(
    r"\b(quant|quantitative|trading|trader|market[- ]mak(ing|er)|hedge fund|"
    r"prop(rietary)? trading|hft|high[- ]frequency)\b", re.I)
# Known quant/HF/market-maker firms that surface even under a "Tech" industry
# tag; the exclusion is absolute regardless of the role's software framing.
QUANT_FIRMS = {
    "jane street", "citadel", "citadel securities", "two sigma", "de shaw", "d.e. shaw",
    "d. e. shaw", "jump trading", "jump", "optiver", "imc", "imc trading", "susquehanna",
    "sig", "hudson river trading", "hrt", "drw", "millennium", "point72", "qube",
    "g-research", "g research", "squarepoint", "tower research", "virtu", "flow traders",
    "akuna", "maven securities", "xtx markets", "quadrature", "wintermute", "radix trading",
    # added 2026-07-22 after GSA Capital slipped into the bank via a generic
    # "Software Developer - Intern" title with no quant keywords anywhere.
    "gsa capital", "man group", "ahl", "marshall wace", "brevan howard", "winton",
    "balyasny", "exoduspoint", "schonfeld", "verition", "cubist", "pdt partners",
    "rokos", "capula", "eisler", "systematica", "worldquant", "aqr", "bridgewater",
    "renaissance technologies", "five rings", "headlands", "old mission", "belvedere",
    "chicago trading", "peak6", "wolverine", "vatic", "engineers gate", "graham capital",
    "aquatic capital", "voleon", "florin court", "transmarket", "allston trading",
    # commodity/energy trading houses — still "trading" under the hard exclusion
    "castleton commodities", "vitol", "trafigura", "glencore", "gunvor", "mercuria",
    "freepoint commodities", "hartree", "onyx capital",
}
# Company-name shapes that reliably indicate a fund / trading shop even when the
# role title is a plain "Software Engineer". Deliberately excludes Capital One
# (a card issuer with ordinary SWE roles), which is not a quant employer.
QUANT_COMPANY_RE = re.compile(
    r"\b(asset management|investment management|capital management|capital partners|"
    r"hedge|securities|quantitative|prop(rietary)? (trading|shop))\b", re.I)

# --- non-software fields --------------------------------------------------
NON_TECH_TITLE_RE = re.compile(
    r"\b(mechanical|electrical|civil|structural|aerodynamic(s|ist)?|composites?|"
    r"manufacturing|sales|marketing|recruit(er|ing|ment)|talent|finance|accounting|"
    r"audit|tax|legal|paralegal|hr|human resources|supply chain|procurement|"
    r"business development|account (executive|manager)|nurse|pharmac|"
    # added 2026-07-22: these reached the bank despite containing no tech words
    # ("Urban Design Learning Administrator", "Customer Solution Architect")
    r"administrator|admin assistant|customer (solution|success|service)|"
    r"urban design|architecture assistant|quantity survey|teaching|logistics)\b", re.I)
# Consulting/advisory: Aug explicitly doesn't want these, even when tagged with a
# tech keyword (e.g. "AI & Management Consulting", "Value Engineering").
# "advisory" was here but "advisor"/"adviser" was not, so Auto Trader's "Early
# Careers Advisor - 12 month FTC" (an HR role) passed the title filter — and, the
# employer being a tech company, passed the description tech-signal check too.
# Checked against all 1,355 titles the pipeline has ever seen: adding
# advisor/adviser rejects exactly three, none of them engineering.
CONSULTING_RE = re.compile(
    r"\b(consult(ant|ing|ancy)?|advis(or|er)s?|advisory|value engineering|"
    r"business analyst|management consult|strategy analyst|solution consultant)\b",
    re.I)
TECH_RE = re.compile(
    r"\b(software|developer|develop(ment)?|data|machine learning|ml|ai|artificial intelligence|"
    r"computer|comput(e|ing)|program(mer|ming)|full[- ]stack|backend|back[- ]end|frontend|"
    r"front[- ]end|devops|cloud|platform|infrastructure|cyber|security|audio|dsp|telemetry|"
    r"simulation|embedded|firmware|robotics|analytics|informatique|logiciel|données|it|"
    # AI-lab role titles that carry no "software"/"data" keyword but are prime
    # targets (e.g. Mistral "Applied Scientist / Research Engineer Internship")
    r"research engineer|applied scientist|research scientist|scientist|deep learning|"
    r"nlp|computer vision|llm|inference|research intern)\b", re.I)

# --- qualifications (strict, spec sec 2) ----------------------------------
PHD_RE = re.compile(
    r"\b(phd|ph\.d|doctoral|doctorate)\b(?![^.]{0,40}\b(or|not)\b)", re.I)
PHD_REQUIRED_RE = re.compile(
    r"(pursuing|enrolled in|must (be|have)|require[sd]?|completed)[^.\n]{0,60}\b(phd|ph\.d|doctora(l|te))\b"
    r"|\b(phd|ph\.d)\b[^.\n]{0,40}\b(required|candidates? only|students? only|program)\b"
    r"|research scientist", re.I)
MASTERS_ONLY_RE = re.compile(
    r"(master'?s?|msc|m\.s\.)[^.\n]{0,40}\b(only|required|candidates? only|students? only)\b"
    r"(?![^.\n]{0,30}(bachelor|undergraduate))", re.I)
YEARS_EXP_RE = re.compile(r"\b([2-9]|1[0-9])\+?\s*(?:or more\s*)?years?[^.\n]{0,40}"
                          r"(professional|industry|work(ing)?|relevant)?\s*experience", re.I)
YEARS_EXP_REQUIRED_RE = re.compile(
    r"(minimum|at least|require[sd]?|must have)[^.\n]{0,30}\b([2-9]|1[0-9])\+?\s*years", re.I)
GRAD_WINDOW_RE = re.compile(
    r"(graduat\w+|class of)[^.\n]{0,50}\b(20\d\d)", re.I)
COMPLETED_DEGREE_RE = re.compile(
    r"(completed|hold[s]?|obtained)[^.\n]{0,30}(degree|bachelor|master)", re.I)

# Aug graduates June 2028; acceptable stated graduation years
OK_GRAD_YEARS = {2027, 2028, 2029}

# --- timing ---------------------------------------------------------------
SEASON_YEAR_RE = re.compile(
    r"\b(summer|spring|fall|autumn|winter|january|june|july|august|september)\s*(20\d\d)\b", re.I)
YEAR_RE = re.compile(r"\b(20\d\d)\b")

# --- ITAR / US-person -----------------------------------------------------
ITAR_TEXT_RE = re.compile(
    r"\bitar\b|u\.?s\.? person|us citizens? (only|required)|"
    r"(must|required to) be (a )?u\.?s\.? citizen|"
    r"(active|current)? ?(security|ts/sci) clearance (is )?required|export control", re.I)
ITAR_COMPANIES = {
    "spacex", "blue origin", "anduril", "lockheed martin", "northrop grumman",
    "raytheon", "rtx", "l3harris", "general atomics", "united launch alliance",
    "ula", "relativity space", "firefly aerospace", "boeing defense",
}


@dataclass
class FilterStats:
    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, reason: str) -> None:
        key = reason.split(":")[0]
        self.counts[key] = self.counts.get(key, 0) + 1


def check_type(job: Job) -> str | None:
    text = f"{job.title} {job.extra.get('contract_type','')} {job.extra.get('commitment','')}"
    if not INTERN_RE.search(text) and not INTERN_RE.search(job.description[:2000]):
        return "type: no internship/placement signal"
    if NOT_INTERN_RE.search(job.title):
        return "type: graduate scheme / permanent role"
    return None


# --- finance employers -----------------------------------------------------
# Separate from the quant exclusion above, which only catches funds and trading
# shops: Goldman Sachs is an investment bank, and it was taking 5 of the 50 More
# roles slots. Aug does not want finance, full stop (2026-08-18) — the same call
# that removed Bloomberg from the watchlist.
#
# NOT a blanket ban on anything that touches money. Stripe and Monzo are on his
# watchlist by choice, so payments and fintech PRODUCT companies stay: the line
# is the institution, not the sector it serves.
FINANCE_FIRMS = {
    "goldman sachs", "goldman", "jpmorgan", "jpmorganchase", "jp morgan", "chase",
    "morgan stanley", "bank of america", "merrill", "citigroup", "citibank", "citi",
    "wells fargo", "barclays", "hsbc", "natwest", "lloyds", "santander", "nationwide",
    "ubs", "credit suisse", "deutsche bank", "commerzbank", "bnp paribas",
    "societe generale", "credit agricole", "ing", "rabobank", "abn amro",
    "nomura", "mizuho", "mufg", "sumitomo", "standard chartered", "rbc",
    "royal bank of canada", "td bank", "scotiabank", "bmo", "cibc", "macquarie",
    "jefferies", "lazard", "rothschild", "evercore", "moelis", "houlihan lokey",
    "blackrock", "vanguard", "fidelity", "american fidelity", "state street",
    "northern trust", "schroders", "abrdn", "legal and general", "m&g",
    "invesco", "franklin templeton", "pimco", "amundi", "capital one",
    "world bank", "imf", "european investment bank", "bank of china",
    "allianz", "aviva", "axa", "zurich insurance", "prudential", "legal & general",
    "aig", "chubb", "hiscox", "admiral", "direct line", "lv=", "swiss re", "munich re",
    "moody's", "s&p global", "fitch ratings", "experian", "equifax", "transunion",
}
# Name shapes that give an institution away even when it is not on the list
FINANCE_NAME_RE = re.compile(
    "(?<![a-z])(bank|bancorp|banking|insurance|insurers?|assurance|reinsurance|"
    "asset management|wealth management|investment (bank|management|advisors?)|"
    "building society|credit union|mutual (fund|insurance)|brokerage|"
    "financial (services|group|corporation)|farm bureau)(?![a-z])", re.I)

# "Bristol Capital Ltd" — a bare Capital SUFFIX is an investment firm nearly
# every time. Anchored at the end so "Capital One" and "Capital Radio" are safe.
CAPITAL_SUFFIX_RE = re.compile(
    "(?<![a-z])capital( (ltd|limited|llc|inc|plc|lp|group|holdings|"
    "management|partners|advisors?))*$", re.I)

# Payments and fintech. These were allowed through while Stripe and Monzo were
# on the watchlist; both came off on 2026-08-18 ("really no finance"), so the
# exception went with them. Deliberately NOT bare "square", "block", "mercury"
# or "ramp": Square Enix, Mercury Systems and Blockchain shops are not banks.
FINTECH_FIRMS = {
    "stripe", "monzo", "starling bank", "revolut", "wise", "transferwise",
    "adyen", "klarna", "checkout.com", "plaid", "gocardless", "truelayer",
    "paypal", "squareup", "block inc", "modulr", "zilch", "sumup", "nubank",
    "n26", "chime", "affirm", "marqeta", "rapyd", "curve", "moneybox",
}


def _named_firm(company: str, firms) -> bool:
    """Does this company name CONTAIN one of these firms as whole words?

    Plain substring matching quietly dropped anything containing a short firm
    name: "sig" (SIG) matched De-sig-n Studio, Signal AI and Sigma Design, and
    "ing" (ING) matched McLaren Rac-ing. Padding both sides makes the match
    token-wise while still allowing a firm name inside a longer one.
    """
    name = " ".join(str(company or "").lower().replace(".", "").replace(",", "").split())
    padded = f" {name} "
    return any(f" {firm}" + ' ' in padded for firm in
               (f.replace(".", "") for f in firms))


def _is_finance_employer(company: str) -> bool:
    name = " ".join(str(company or "").lower().replace(".", "").replace(",", "").split())
    if _named_firm(company, FINANCE_FIRMS) or _named_firm(company, FINTECH_FIRMS):
        return True
    return bool(FINANCE_NAME_RE.search(name) or CAPITAL_SUFFIX_RE.search(name))


def check_field(job: Job) -> str | None:
    if QUANT_RE.search(f"{job.title} {job.company}") or QUANT_RE.search(job.description):
        return "quant: quant/trading exclusion"
    company_norm = job.company.lower().replace(".", "").replace(",", "")
    if _named_firm(job.company, QUANT_FIRMS):
        return "quant: known quant/HF/market-maker firm"
    if QUANT_COMPANY_RE.search(job.company) and "capital one" not in company_norm:
        return "quant: fund/trading-shop company name"
    if _is_finance_employer(job.company):
        return "field: finance employer (excluded)"
    if CONSULTING_RE.search(job.title):
        return "field: consulting/advisory (excluded)"
    if NON_TECH_TITLE_RE.search(job.title) and not TECH_RE.search(job.title):
        return "field: non-software discipline"
    if not TECH_RE.search(f"{job.title} {job.description}"):
        return "field: no software/data/AI signal"
    return None


PHD_TITLE_RE = re.compile(r"\bphd\b|\bph\.d\b|\bdoctoral\b|research scientist", re.I)


def check_qualifications(job: Job) -> str | None:
    if PHD_TITLE_RE.search(job.title):
        return "qualifications: PhD/research-track role (title)"
    text = job.description
    if not text:
        return None  # boards without descriptions can't fail a strict text parse
    for m in PHD_REQUIRED_RE.finditer(text):
        # not PhD-required if the same context also welcomes Master's/Bachelor's/
        # undergrad students (Aug is an MEng ~ Master's-level) — e.g. "Master's or PhD"
        window = text[max(0, m.start() - 60):m.end() + 60].lower()
        if not re.search(r"master|bachelor|undergrad|msc|beng|meng|penultimate", window):
            return "qualifications: PhD/research-track required"
    if MASTERS_ONLY_RE.search(text):
        return "qualifications: Master's-only candidate pool"
    m = YEARS_EXP_REQUIRED_RE.search(text) or YEARS_EXP_RE.search(text)
    if m and "intern" not in job.title.lower():
        return f"qualifications: {m.group(0).strip()[:60]}"
    for m in GRAD_WINDOW_RE.finditer(text):
        year = int(m.group(2))
        if 2020 < year < 2027 and year not in OK_GRAD_YEARS:
            return f"qualifications: graduation window {year} incompatible with 2028"
    if COMPLETED_DEGREE_RE.search(text):
        return "qualifications: completed degree required"
    return None


def check_timing(job: Job) -> str | None:
    """Reject only when timing is determinable AND incompatible: explicit
    2026-or-earlier season, or an explicitly 2028+ season with no 2027 mention."""
    text = f"{job.title} {job.start} {job.duration} {job.description[:3000]}"
    seasons = [(m.group(1).lower(), int(m.group(2))) for m in SEASON_YEAR_RE.finditer(text)]
    if seasons:
        years = {y for _, y in seasons}
        if years and max(years) <= 2026:
            return f"timing: {max(years)} cycle already conflicts with 2027 window"
        if 2027 not in years and min(years) >= 2028:
            return "timing: 2028+ cycle outside the 2027 window"
    else:
        title_years = {int(y) for y in YEAR_RE.findall(job.title)}
        if title_years and max(title_years) <= 2026:
            return "timing: 2026-or-earlier cycle in title"
    return None


def check_itar(job: Job) -> str | None:
    company = job.company.lower()
    if any(c in company for c in ITAR_COMPANIES):
        return "itar: US launch/defence employer cannot hire non-US persons"
    if ITAR_TEXT_RE.search(job.description):
        return "itar: US-person/clearance requirement in description"
    return None


# Sources that are a company's own live ATS board: the listing being present
# means the role is still open, so an old "posted" date does NOT imply staleness
# (unlike aggregators, where old = stale repost). Only a passed deadline drops these.
EVERGREEN_SOURCES = ("ashby", "greenhouse", "lever", "trackr", "formulacareers",
                     "motorsportjobs", "fluidjobs", "gradcracker", "wttj")


def check_freshness(job: Job, freshness_days: int, today: date | None = None) -> str | None:
    today = today or date.today()
    if job.deadline and job.deadline < today:
        return "freshness: deadline passed"
    if any(job.source.startswith(s) for s in EVERGREEN_SOURCES):
        return None  # live board listing = still open regardless of posted age
    if (job.posted and job.posted < today - timedelta(days=freshness_days)
            and not (job.deadline and job.deadline >= today)):
        return f"freshness: older than {freshness_days} days with no future deadline"
    return None


def check_link(job: Job) -> str | None:
    """A row must point at ONE posting — a search or careers landing page is a
    place to look for jobs, not a job (the Apple trackr entry)."""
    from .models import is_posting_url
    if not is_posting_url(job.url):
        return "link: landing/search page, not a posting"
    return None


def check_company_rules(job: Job, rules) -> str | None:
    """Config-driven per-company constraints (config.yaml company_rules).

    Aug has one TikTok application left; a rule keeps everything but a
    Europe-based role from even reaching the scorer."""
    for rule in rules or []:
        if str(rule.get("company", "")).lower() not in job.company.lower():
            continue
        wanted = [str(w).lower() for w in rule.get("location_any") or []]
        if wanted and not any(w in job.location.lower() for w in wanted):
            return (f"company rule: {rule.get('company')} outside "
                    f"wanted locations")
    return None


def apply_hard_filters(jobs: list[Job], freshness_days: int,
                       stats: FilterStats | None = None,
                       today: date | None = None,
                       company_rules: list | None = None) -> tuple[list[Job], list[tuple[Job, str]]]:
    stats = stats if stats is not None else FilterStats()
    passed: list[Job] = []
    rejected: list[tuple[Job, str]] = []
    for job in jobs:
        reason = (check_company_rules(job, company_rules)
                  or check_link(job) or check_type(job) or check_field(job)
                  or check_qualifications(job)
                  or check_timing(job) or check_itar(job)
                  or check_freshness(job, freshness_days, today))
        if reason:
            stats.bump(reason)
            rejected.append((job, reason))
        else:
            passed.append(job)
    return passed, rejected
