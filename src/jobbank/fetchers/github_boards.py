"""GitHub README job boards: speedyapply 2027 SWE/AI, vanshb03/Ouckah
Summer2027, jobright-ai. All are markdown pipe tables with HTML apply links;
column layouts differ per board so headers are mapped dynamically.

Formats verified 2026-07-15:
  speedyapply: | Company | Position | Location | [Salary] | Posting | Age |   (age like "9d")
  vanshb03:    | Company | Role | Location | Application/Link | Date Posted | ("Jul 09",
               closed roles = lock emoji, continuation company = arrow char)
  jobright:    | Company | Job Title | Location | Work Model | Date Posted |
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from ..models import Job
from ..settings import Settings
from . import client, register

HREF_RE = re.compile(r'href="([^"]+)"')
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
TAG_RE = re.compile(r"<[^>]+>")
AGE_RE = re.compile(r"^(\d+)\s*d$", re.I)
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
CLOSED_MARK = "\U0001F512"  # lock emoji used by vanshb03 for closed roles
CONTINUATION = "↳"     # arrow used for "same company as previous row"


def _cell_text(cell: str) -> str:
    """Visible text of a table cell: markdown link labels + strip HTML tags."""
    cell = MD_LINK_RE.sub(lambda m: m.group(1), cell)
    return TAG_RE.sub("", cell).replace("**", "").strip()


def _cell_link(cell: str) -> str:
    m = HREF_RE.search(cell)
    if m:
        return m.group(1)
    m = MD_LINK_RE.search(cell)
    return m.group(2) if m else ""


def _parse_posted(cell: str, today: date) -> date | None:
    text = _cell_text(cell)
    m = AGE_RE.match(text)
    if m:
        return today - timedelta(days=int(m.group(1)))
    m = re.match(r"^([A-Za-z]{3})\w*\s+(\d{1,2})$", text)
    if m and m.group(1).lower() in MONTHS:
        d = date(today.year, MONTHS[m.group(1).lower()], int(m.group(2)))
        if d > today + timedelta(days=7):   # "Dec 20" seen in July = last year
            d = d.replace(year=d.year - 1)
        return d
    return None


def _header_map(cells: list[str]) -> dict[str, int] | None:
    """Map semantic fields -> column index from a header row."""
    idx: dict[str, int] = {}
    for i, raw in enumerate(cells):
        h = raw.strip().lower()
        if "company" in h:
            idx["company"] = i
        elif any(k in h for k in ("position", "role", "job title", "title")):
            idx["title"] = i
        elif "location" in h:
            idx["location"] = i
        elif any(k in h for k in ("posting", "application", "link", "apply")):
            idx["apply"] = i
        elif any(k in h for k in ("age", "date")):
            idx["posted"] = i
        elif "salary" in h:
            idx["salary"] = i
        elif "work model" in h:
            idx["work_model"] = i
    return idx if {"company", "title"} <= idx.keys() else None


def parse_board_markdown(md: str, source: str, today: date | None = None) -> list[Job]:
    today = today or date.today()
    jobs: list[Job] = []
    header: dict[str, int] | None = None
    last_company = ""
    for line in md.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            header = None
            continue
        cells = [c for c in line.strip("|").split("|")]
        stripped = [c.strip() for c in cells]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in stripped if c):
            continue  # separator row
        maybe_header = _header_map(stripped)
        if maybe_header:
            header = maybe_header
            last_company = ""
            continue
        if not header or len(cells) < max(header.values()) + 1:
            continue

        company = _cell_text(cells[header["company"]])
        if company.startswith(CONTINUATION) or company in ("", "↳"):
            company = last_company
        else:
            last_company = company
        title = _cell_text(cells[header["title"]])
        apply_cell = cells[header["apply"]] if "apply" in header else ""
        url = _cell_link(apply_cell) or _cell_link(cells[header["title"]])
        if not company or not title or not url:
            continue
        if CLOSED_MARK in apply_cell:
            continue
        job = Job(
            source=source,
            company=company,
            title=title,
            url=url,
            location=_cell_text(cells[header["location"]]) if "location" in header else "",
            salary=_cell_text(cells[header["salary"]]) if "salary" in header else "",
            posted=_parse_posted(cells[header["posted"]], today) if "posted" in header else None,
        )
        if "work_model" in header:
            job.extra["work_model"] = _cell_text(cells[header["work_model"]])
        jobs.append(job)
    return jobs


@register("github_boards")
def fetch(settings: Settings) -> list[Job]:
    jobs: list[Job] = []
    with client() as http:
        for board in settings.cfg.get("github_boards", []):
            repo, branch = board["repo"], board.get("branch", "main")
            label = f"github:{repo.split('/')[0]}"
            for fname in board.get("files", ["README.md"]):
                url = f"https://raw.githubusercontent.com/{repo}/{branch}/{fname}"
                resp = http.get(url)
                resp.raise_for_status()
                jobs.extend(parse_board_markdown(resp.text, source=label))
    return jobs
