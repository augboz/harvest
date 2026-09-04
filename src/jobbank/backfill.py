"""Backfill the Description column from the live posting text.

Rows written before descriptions were stored (and rows from fetchers that carry
no posting text of their own) reach the sheet with an empty Description, so the
dashboard has nothing to show and falls back to fetching on every card open.
This fills them in once, newest-and-highest-scoring first, so the roles that
actually surface in the dashboard get done even under a low --limit.

Best-effort by design: plenty of job sites block server-side fetches, and a row
that can't be read is simply left empty rather than filled with an error string.
"""
from __future__ import annotations

import re

from concurrent.futures import ThreadPoolExecutor

import httpx

from .sheets import ARCHIVE, JOB_BANK, TAG_RE, first_lines, strip_markup

# The dashboard only reads the tail of the Archive (see ARCHIVE_SCAN in Code.gs),
# so filling high-scoring rows from deep history wastes fetches on roles nobody
# will ever see. Restrict to the same window the dashboard does.
ARCHIVE_WINDOW = 800

# A real browser UA; several boards 403 anything that looks automated.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


# Containers ATS platforms actually put the job body in. Trying these before any
# size heuristic avoids leading with breadcrumbs and "Apply now" chrome.
_JD_SELECTORS = (
    '[data-testid*="description" i]', '[class*="job-description" i]',
    '[class*="jobDescription" i]', '[id*="job-description" i]',
    '[id*="jobDescription" i]', '[class*="job-details" i]',
    '[class*="posting-content" i]', '[class*="description" i]',
    '[id*="description" i]', "article", "main",
)


# Text that came from a search-results or nav shell rather than the job body.
# Showing this is worse than showing nothing: a results page lists OTHER roles,
# so the blurb ends up describing a job the row isn't for.
_CHROME_RE = re.compile(
    r"jobs? matched|search results|back to (jobs|last) search|skip to (main|content)|"
    r"cookie|accept all|sign in to continue|enable javascript|your browser|"
    # careers-site furniture: TikTok's pages open with a slogan and a language
    # switcher, which read as prose to the sentence test but say nothing
    r"discover a career|explore (our )?(careers|opportunities)|join our talent|"
    r"life at [a-z]|create an account|ready to take the next step|"
    r"all fields marked", re.I)
# A language switcher next to a copyright line is a page header, not a posting.
_LANG_SWITCH_RE = re.compile(r"(english|français|deutsch|español)\s*[一-鿿]", re.I)


def _looks_like_chrome(text: str) -> bool:
    if not text:
        return True
    if _CHROME_RE.search(text[:400]) or _LANG_SWITCH_RE.search(text[:200]):
        return True
    # a real JD paragraph has sentences; nav shells are fragment soup
    words = text.split()
    return len(words) < 12 or sum(w.islower() for w in words) < len(words) * 0.35


def _clean_lead(text: str) -> str:
    """Drop leading page chrome so the blurb starts at real prose.

    Extracted text often opens with breadcrumbs and metadata ("Back  Role  Company
    <  Internship  <  Atlanta, GA  <  1d ago"). Real sentences are long and
    contain lowercase words; nav fragments are short and title-cased, so skip
    forward to the first chunk that reads like a sentence.
    """
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    for n, part in enumerate(parts):
        words = part.split()
        if len(words) >= 8 and sum(w.islower() for w in words) >= len(words) * 0.4:
            return " ".join(parts[n:])
    return text


def _posting_text(client: httpx.Client, url: str) -> str:
    """The opening lines of a posting, or "" if it can't be read."""
    if not url or not url.startswith("http"):
        return ""
    try:
        resp = client.get(url)
        if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
            return ""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "noscript",
                         "form", "button", "aside"]):
            tag.decompose()

        best = ""
        for sel in _JD_SELECTORS:
            for el in soup.select(sel):
                text = " ".join(el.get_text(" ", strip=True).split())
                if len(text) > len(best):
                    best = text
            if len(best) > 400:      # a real JD body; stop before vaguer selectors
                break
        if not best:
            best = " ".join(soup.get_text(" ", strip=True).split())
        text = _clean_lead(best)
        text = _demarkdown(text)
        return "" if _looks_like_chrome(text) else first_lines(text)
    except Exception:  # noqa: BLE001 — a dead/blocked posting is expected, not fatal
        return ""


# Some sources hand back markdown rather than prose — GitHub board READMEs and
# Ashby postings both do — so the sheet ends up showing "**Job Description**
# -------------------" before a single readable word. Strip the scaffolding.
_MD_RULE_RE = re.compile(r"[-=_]{3,}")
# Headings the scaffolding uses. Only ever stripped from text that actually
# arrived as markdown, so a real sentence containing the word "role" is safe.
_MD_HEAD_RE = re.compile(
    r"(company|job description|about the (job|role|team)|description|"
    r"role|position|overview)\s*:?\s*", re.I)
_LEAD_ZONE = 160


def _unescape_markup(text: str) -> str:
    """Kept as the name the repair path and its tests use; the logic now lives in
    sheets.strip_markup, which every description passes through on the way to the
    sheet rather than only the ones being repaired."""
    return strip_markup(text)


def _demarkdown(text: str) -> str:
    raw = _unescape_markup(text)
    had_markdown = "**" in raw or "##" in raw or bool(_MD_RULE_RE.search(raw))
    t = " ".join(_MD_RULE_RE.sub(" ", raw.replace("**", " ").replace("##", " ")).split())
    if had_markdown:
        # "Company Figma Job Description We're looking for…" — cut past the LAST
        # heading near the front, which takes the company name with it
        last = None
        for m in _MD_HEAD_RE.finditer(t[:_LEAD_ZONE]):
            last = m
        if last:
            t = t[last.end():]
    return t.strip(" -:\u2014")


# Aggregator pages (jobright, and most GitHub board mirrors) open with the same
# listing metadata the card already displays: "Mistral · 3 weeks ago  AI Data
# Solutions Intern  Palo Alto, CA  Internship Onsite Intern  <the actual text>".
# Repeating the company, title and location back at the reader wastes the whole
# blurb, so drop anything at the front we already know from the row itself.
_AGO_RE = re.compile(r"^\s*[^·|]{0,60}[·|]\s*\d+\s*\w+\s+ago\b\s*", re.I)
# The row's own Location is normalised ("USA"), so the raw "Palo Alto, CA" in the
# listing header never matches it — strip place fragments by shape instead.
_PLACE_RE = re.compile(r"^[A-Z][A-Za-z .'\-]{1,24},\s*(?:[A-Z]{2}|[A-Z][a-z]+)[,\s]*")
_JUNK_LEAD = ("internship", "intern", "onsite", "hybrid", "remote", "full-time",
              "full time", "part-time", "contract", "permanent", "graduate",
              "placement", "apply now", "save job", "new")


def _reads_as_prose(text: str) -> bool:
    """Does this already open like a sentence? Checked on a short window so that
    "Mistral is a company that…" is recognised as prose while
    "Mistral · 3 weeks ago  AI Data Solutions Intern  Palo Alto" is not."""
    words = text.split()[:10]
    if len(words) < 8:
        return False
    return sum(w[:1].islower() for w in words) >= len(words) * 0.4


def _strip_listing_lead(text: str, company: str = "", role: str = "",
                        location: str = "") -> str:
    t = " ".join(str(text or "").split())
    t = _AGO_RE.sub("", t)
    # "Exa Exa is an applied AI lab…" — the header name butted against the first
    # sentence, which already reads as prose, so drop the duplicate up front
    if company and len(company) > 2:
        doubled = f"{company} {company} "
        if t.lower().startswith(doubled.lower()):
            t = t[len(company) + 1:]
    known = [x for x in (company, role, location) if x and len(x) > 2]
    changed = True
    while changed and t:
        changed = False
        # tag words ("Internship", "Onsite") are never how a blurb opens, so they
        # go even once the rest reads as prose
        for word in _JUNK_LEAD:
            if t.lower().startswith(word):
                rest = t[len(word):]
                if not rest[:1].isalpha():       # a whole token, not a prefix
                    t = rest.lstrip(" -–—·,|:")
                    changed = True
        # the company name is also the SUBJECT of the first real sentence, so
        # only strip the things we already know while this still reads as a list
        if not _reads_as_prose(t):
            for phrase in known:
                if t.lower().startswith(phrase.lower()):
                    t = t[len(phrase):].lstrip(" -–—·,|:")
                    changed = True
            stripped = _PLACE_RE.sub("", t)
            if stripped != t:
                t, changed = stripped, True
    return t.strip(" -–—·,|:")


# A link that points at a company's whole board rather than one posting: the
# scraper can only find chrome there, and "Open posting" lands the reader on a
# list. Both are fixable by asking the board's API which role this row is.
_BOARD_RE = re.compile(
    r"^https?://(?:jobs\.ashbyhq\.com|boards\.greenhouse\.io|jobs\.lever\.co)"
    r"/([A-Za-z0-9_-]+)/?$", re.I)


def _from_board(client: httpx.Client, url: str, role: str) -> tuple[str, str]:
    """(description, canonical_url) for the posting on `url`'s board that best
    matches `role`. ("", "") when the board or the role cannot be found."""
    m = _BOARD_RE.match(str(url or "").strip())
    if not m or not role:
        return "", ""
    org = m.group(1)
    host = m.group(0).lower()
    if "ashbyhq" in host:
        api, key = f"https://api.ashbyhq.com/posting-api/job-board/{org}", "ashby"
    elif "greenhouse" in host:
        api, key = (f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs?content=true",
                    "greenhouse")
    else:
        api, key = f"https://api.lever.co/v0/postings/{org}?mode=json", "lever"
    try:
        resp = client.get(api)
        if resp.status_code != 200:
            return "", ""
        data = resp.json()
    except Exception:  # noqa: BLE001
        return "", ""

    if key == "ashby":
        items = [(j.get("title", ""), j.get("jobUrl", ""),
                  j.get("descriptionPlain") or j.get("description", ""))
                 for j in data.get("jobs", [])]
    elif key == "greenhouse":
        items = [(j.get("title", ""), j.get("absolute_url", ""), j.get("content", ""))
                 for j in data.get("jobs", [])]
    else:
        items = [(j.get("text", ""), j.get("hostedUrl", ""), j.get("descriptionPlain", ""))
                 for j in data]

    want = re.sub(r"[^a-z0-9 ]", " ", role.lower()).split()
    best, best_score = None, 0
    for title, link, body in items:
        got = set(re.sub(r"[^a-z0-9 ]", " ", str(title).lower()).split())
        score = sum(1 for w in want if w in got)
        if score > best_score:
            best, best_score = (title, link, body), score
    if not best or best_score < max(2, len(want) // 3):
        return "", ""
    body = _demarkdown(TAG_RE.sub(" ", str(best[2] or "")))
    return (first_lines(_clean_lead(body)) if body else ""), best[1]




def backfill_descriptions(sh, limit: int = 200, workers: int = 8) -> dict:
    """Fill empty Description cells on the Archive and Job Bank tabs.

    Returns {"scanned": n, "attempted": n, "filled": n}.
    """
    scanned = attempted = filled = 0
    for tab in (ARCHIVE, JOB_BANK):
        try:
            ws = sh.worksheet(tab)
        except Exception:  # noqa: BLE001
            continue
        vals = ws.get_all_values()
        if len(vals) < 2:
            continue
        head = vals[0]
        if "Description" not in head or "Link" not in head:
            continue
        d_i, l_i = head.index("Description"), head.index("Link")
        s_i = head.index("Score") if "Score" in head else None

        data = list(enumerate(vals[1:], start=2))
        if tab == ARCHIVE:
            data = data[-ARCHIVE_WINDOW:]

        col = chr(ord("A") + d_i) if d_i < 26 else None
        todo, cleanups = [], []
        for n, row in data:
            if not any(c.strip() for c in row):
                continue
            scanned += 1
            row = list(row) + [""] * (len(head) - len(row))
            existing = row[d_i].strip()
            if existing:
                # Text stored before the lead-stripping existed still opens with
                # breadcrumbs. Re-clean in place — costs no network.
                lead = _clean_lead(_strip_listing_lead(
                    _demarkdown(existing),
                    row[head.index("Company")] if "Company" in head else "",
                    row[head.index("Role")] if "Role" in head else "",
                    row[head.index("Location")] if "Location" in head else ""))
                cleaned = "" if _looks_like_chrome(lead) else first_lines(lead)
                if cleaned != existing and col:
                    cleanups.append({"range": f"{col}{n}", "values": [[cleaned]]})
                if not cleaned and row[l_i].strip():
                    todo.append((n, row[l_i], 0))   # re-fetch what we just blanked
                continue
            if not row[l_i].strip():
                continue
            score = 0
            if s_i is not None and str(row[s_i]).strip().lstrip("-").isdigit():
                score = int(row[s_i])
            todo.append((n, row[l_i], score))

        if cleanups and col:
            ws.batch_update(cleanups, value_input_option="USER_ENTERED")
            print(f"  [backfill] {tab}: re-cleaned {len(cleanups)} stored description(s)")

        # highest score first, then most recent — matches what the dashboard shows
        todo.sort(key=lambda t: (-t[2], -t[0]))
        todo = todo[:limit]
        if not todo:
            continue
        attempted += len(todo)

        with httpx.Client(headers={"User-Agent": _UA}, timeout=20,
                          follow_redirects=True) as client:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                texts = list(pool.map(lambda t: _posting_text(client, t[1]), todo))

        updates = [{"range": f"{col}{n}", "values": [[text]]}
                   for (n, _url, _s), text in zip(todo, texts) if text]
        if updates and col:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
            filled += len(updates)
        print(f"  [backfill] {tab}: {len(updates)}/{len(todo)} filled")

    return {"scanned": scanned, "attempted": attempted, "filled": filled}
