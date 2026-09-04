"""Sheet writers (spec sec 9). Two backends behind one interface:

- GoogleSheetBackend: gspread + service account (SHEET_ID set)
- LocalXlsxBackend: out/sheet_preview.xlsx dry-run preview (no SHEET_ID)

Both are APPEND-ONLY for job rows. The pipeline never edits an existing Job
Bank row, so the human-owned columns (Status, Notes) can never be
overwritten — that acceptance criterion holds by construction.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .models import Job

DASHBOARD = "Dashboard"
JOB_BANK = "Job Bank"
OPENING_SOON = "Opening Soon"
ARCHIVE = "Archive"
STATS = "Stats"
WATCHLIST = "Watchlist"
PASTE_IN = "Paste-in"
RUN_LOG = "Run Log"
FEEDBACK = "Feedback"

# Column order = reading priority: Category, Company, Role, Score, Location, Link
# first; less-important fields (incl. Added) pushed to the right.
# That priority order. Everything from HIDDEN_COLS onward is pushed to the end
# and hidden on the sheet (kept for provenance, not for reading).
# "Favourite" is human-owned like Status and Notes: the dashboard's heart writes
# it and moves the row to the top of the tab. The pipeline only ever writes it
# blank on a NEW row, and it is listed here because ensure_schema() clears any
# column on the tab that JOB_BANK_COLS does not name — a heart stored in a
# column Python had never heard of would be wiped by the next run.
JOB_BANK_COLS = ["Category", "Company", "Role", "Score", "Location", "Salary",
                 "Link", "Favourite", "Priority", "Deadline", "Opens", "Duration",
                 "Description", "Status", "Notes", "Score reason", "Source"]
# "Description" = what the role involves. "Score reason" = why it ranked where it
# did; useful but not what Aug reads day to day, so it sits in the hidden tail.
HIDDEN_COLS = ["Score reason", "Source"]
# Tabs Aug actually reads. Everything else (Archive/Stats/Watchlist/Paste-in/
# Run Log) is machinery — still written, just hidden so the tab bar stays clean.
# Any tab not created by this pipeline (e.g. a Gemini canvas) is left alone.
VISIBLE_TABS = [JOB_BANK, OPENING_SOON]
UTILITY_TABS = [ARCHIVE, STATS, WATCHLIST, PASTE_IN, RUN_LOG, FEEDBACK]
OPENING_SOON_COLS = ["Company", "Programme", "Expected open date",
                     "Expected close date", "Status", "Link", "Notes"]
# Duration + Score reason are carried so a role promoted back into the bank
# arrives complete, rather than with a placeholder note where its reasoning
# should be.
ARCHIVE_COLS = ["Added", "Score", "Category", "Company", "Role", "Location", "Salary",
                "Deadline", "Duration", "Link", "Source", "Description",
                "Score reason", "Disposition"]
WATCHLIST_COLS = ["Company", "ATS", "Slug / URL", "Notes"]
PASTE_IN_COLS = ["URL", "Notes", "Date added", "Processed", "Result"]
# Why a role was removed, and which roles were actually pursued. Read back into
# the scorer prompt as context (never as a hard rule) and by `suggest-filters`.
# Signal: "removed" (negative, with a reason) | "pursued" (positive, from a
# status change to Applied or beyond).
FEEDBACK_COLS = ["Date", "Signal", "Company", "Role", "Category", "Score",
                 "Reason", "Link"]
RUN_LOG_COLS = ["Run ID", "Started", "Finished", "Fetched/source", "After dedupe",
                "Discarded/filter", "Scored", "Added", "Archived", "Priority",
                "Failed sources", "Notes / discovery suggestions"]
# llm_top_n = how many roles get full LLM review per run (the main token cost
# lever); run_every_days = 1 for daily, N to skip runs between (zero tokens on
# skipped days). Both editable from the sheet/dashboard like the others.
STATS_CONFIG_KEYS = ["weekly_cap", "min_score", "digest_to", "llm_top_n",
                     "run_every_days"]
INT_CONFIG_KEYS = ("weekly_cap", "min_score", "llm_top_n", "run_every_days")
STATS_RUN_COLS = ["Run ID", "Date", "Jobs fetched (per source)", "After dedupe",
                  "Discarded (per filter)", "Scored", "Avg score", "Median score",
                  "Added", "Archived", "Priority", "Active sources", "Failed sources",
                  "LLM cost est ($)", "Next scheduled run"]


DESCRIPTION_CHARS = 320


TAG_RE = re.compile(r"<[^>]+>")
# Text that starts PART-WAY through a tag has already lost its opening "<", so
# only the attributes are left behind: class="content-intro">
ATTR_SOUP_RE = re.compile(
    r'(?:[a-zA-Z][a-zA-Z0-9_-]*="[^"]*"\s*)+>?'
    r'|(?:[a-zA-Z][a-zA-Z0-9_-]*=\S+\s*)+>')


def strip_markup(text: str) -> str:
    """Words, not markup — whatever shape the source sent.

    Some fetchers hand back real HTML (Reed returns the advert body verbatim),
    and some hand back HTML that was escaped on the way in, sometimes twice. A
    Reed row put `<p><strong>&#163;50,000 - &#163;60,000 &#43; Hybrid` on a card
    exactly as written. Entities are decoded first so escaped tags become real
    ones, then every tag goes, then the attribute soup left by a truncated tag.

    Lives here rather than in backfill because EVERY description now passes
    through it on the way to the sheet, not only the ones being repaired.
    """
    t = str(text or "")
    for _ in range(2):
        if "&" not in t:
            break
        t = html_lib.unescape(t)
    t = TAG_RE.sub(" ", t)
    return ATTR_SOUP_RE.sub(" ", t)


def first_lines(text: str, limit: int = DESCRIPTION_CHARS) -> str:
    """The opening few sentences of a job posting, cut on a sentence boundary.

    Deliberately short: this is a "do I want to click through?" blurb, not a
    summary of the role. Anyone who wants the detail opens the posting.
    """
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    window = text[:limit]
    # prefer to end on a sentence, but only if that doesn't lose most of the blurb
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if cut >= limit * 0.5:
        return window[:cut + 1]
    cut = window.rfind(" ")
    return (window[:cut] if cut > 0 else window).rstrip(",;:-") + "…"


def _description_for(job: Job) -> str:
    """What to show as the role's description.

    The opening lines of the actual posting, NOT an LLM summary. The LLM only
    ever reviewed the top slice by score, so summaries left everything else blank
    — and a couple of real sentences from the advert are more honest, cheaper,
    and identical in behaviour across profiles. Fetchers that carry no posting
    text yield "" here; the dashboard fetches those on demand instead.
    """
    return first_lines(strip_markup(job.description))


def _job_bank_values(job: Job, today: str) -> dict[str, Any]:
    reason = job.score_reason + (
        f" | Attainability: {job.attainability_note}" if job.attainability_note else "")
    return {
        "Category": job.category, "Company": job.company, "Role": job.title,
        "Score": job.score, "Location": job.location,
        # Only Adzuna, Reed and Gradcracker publish pay; every other source
        # leaves this empty and the card shows no salary pill. An empty cell is
        # honest, an invented figure is not.
        "Salary": job.salary, "Link": job.url,
        # yours to set from the dashboard's heart; never written by a run
        "Favourite": "",
        "Priority": "PRIORITY" if job.priority else "",
        "Deadline": job.deadline.isoformat() if job.deadline else "",
        # "Opens" = when applications opened (the posting date) for live roles;
        # Opening Soon promotions overwrite it with the programme's open date.
        "Opens": job.posted.isoformat() if job.posted else "",
        "Duration": job.duration, "Description": _description_for(job),
        "Status": "Not applied", "Notes": "",
        "Score reason": reason, "Source": job.source,
    }


def _job_bank_row(job: Job, today: str) -> list[Any]:
    """Order-independent: emit values in the current JOB_BANK_COLS order."""
    v = _job_bank_values(job, today)
    return [v[col] for col in JOB_BANK_COLS]


def _archive_row(job: Job, today: str, disposition: str) -> list[Any]:
    reason = job.score_reason + (
        f" | Attainability: {job.attainability_note}" if job.attainability_note else "")
    return [today, job.score, job.category, job.company, job.title, job.location,
            job.salary,
            job.deadline.isoformat() if job.deadline else "", job.duration, job.url,
            job.source, _description_for(job), reason, disposition]


class SheetBackend:
    """Interface. `mode` is shown in the run summary."""
    mode = "?"

    def ensure_layout(self, watchlist_seed: list[dict]) -> None: ...
    def existing_links(self) -> set[str]: return set()
    def read_config_overrides(self) -> dict[str, Any]: return {}
    def write_config_values(self, values: dict[str, Any]) -> None: ...
    def read_watchlist(self) -> list[dict] | None: return None
    def append_watchlist(self, entries: list[dict]) -> None: ...
    def append_job_bank(self, jobs: list[Job]) -> None: ...
    def append_archive(self, jobs: list[tuple[Job, str]]) -> None: ...
    def append_run_log(self, row: list[Any]) -> None: ...
    def append_stats_run(self, row: list[Any]) -> None: ...
    # Opening Soon (spec sec 6)
    def read_opening_soon(self) -> list[dict]: return []
    def append_opening_soon(self, entries: list) -> None: ...
    def set_opening_soon_status(self, row_index: int, status: str, notes: str) -> None: ...
    # Paste-in (spec sec 3)
    def read_paste_in(self) -> list[dict]: return []
    def mark_paste_in(self, row_index: int, result: str) -> None: ...
    def read_job_bank(self) -> list[dict]: return []
    # Feedback (why a role was removed / which were pursued)
    def append_feedback(self, rows: list[list[Any]]) -> None: ...
    def read_feedback(self, limit: int = 60) -> list[dict]: return []


# --------------------------------------------------------------------------
class LocalXlsxBackend(SheetBackend):
    mode = "local dry-run (out/sheet_preview.xlsx)"

    def __init__(self, path: Path):
        from openpyxl import Workbook, load_workbook
        self.path = path
        if path.exists():
            self.wb = load_workbook(path)
        else:
            self.wb = Workbook()
            self.wb.remove(self.wb.active)

    def _tab(self, name: str, cols: list[str]):
        if name not in self.wb.sheetnames:
            ws = self.wb.create_sheet(name)
            ws.append(cols)
            from openpyxl.styles import Font
            for cell in ws[1]:
                cell.font = Font(bold=True)
        return self.wb[name]

    def ensure_layout(self, watchlist_seed: list[dict]) -> None:
        self._tab(JOB_BANK, JOB_BANK_COLS)
        self._tab(OPENING_SOON, OPENING_SOON_COLS)
        self._tab(ARCHIVE, ARCHIVE_COLS)
        ws = self._tab(STATS, [])
        if ws.max_row <= 1 and ws.cell(1, 1).value is None:
            ws.append(["CONFIG (editable)", "Value"])
            for key in STATS_CONFIG_KEYS:
                ws.append([key, ""])
            ws.append([])
            ws.append(STATS_RUN_COLS)
        wl = self._tab(WATCHLIST, WATCHLIST_COLS)
        if wl.max_row == 1:
            for c in watchlist_seed:
                wl.append([c.get("name"), c.get("ats"), c.get("slug") or c.get("url", ""), ""])
        self._tab(PASTE_IN, PASTE_IN_COLS)
        self._tab(RUN_LOG, RUN_LOG_COLS)
        self._save()

    def existing_links(self) -> set[str]:
        ws = self._tab(JOB_BANK, JOB_BANK_COLS)
        col = JOB_BANK_COLS.index("Link") + 1
        return {ws.cell(r, col).value for r in range(2, ws.max_row + 1)
                if ws.cell(r, col).value}

    def append_job_bank(self, jobs: list[Job]) -> None:
        from openpyxl.styles import PatternFill
        red = PatternFill("solid", fgColor="FFC7CE")
        ws = self._tab(JOB_BANK, JOB_BANK_COLS)
        today = date.today()
        for job in jobs:
            ws.append(_job_bank_row(job, today.isoformat()))
            if job.deadline and job.deadline <= today + timedelta(days=7):
                ws.cell(ws.max_row, JOB_BANK_COLS.index("Deadline") + 1).fill = red
        self._save()

    def append_archive(self, jobs: list[tuple[Job, str]]) -> None:
        ws = self._tab(ARCHIVE, ARCHIVE_COLS)
        today = date.today().isoformat()
        for job, disposition in jobs:
            ws.append(_archive_row(job, today, disposition))
        self._save()

    def append_run_log(self, row: list[Any]) -> None:
        self._tab(RUN_LOG, RUN_LOG_COLS).append(row)
        self._save()

    def append_stats_run(self, row: list[Any]) -> None:
        self._tab(STATS, []).append(row)
        self._save()

    def read_opening_soon(self) -> list[dict]:
        ws = self._tab(OPENING_SOON, OPENING_SOON_COLS)
        out = []
        for r in range(2, ws.max_row + 1):
            vals = [ws.cell(r, c).value or "" for c in range(1, len(OPENING_SOON_COLS) + 1)]
            if vals[0]:
                out.append({"row": r, **dict(zip(OPENING_SOON_COLS, vals))})
        return out

    def append_opening_soon(self, entries: list) -> None:
        ws = self._tab(OPENING_SOON, OPENING_SOON_COLS)
        for e in entries:
            ws.append([e.company, e.programme, e.expected_open, e.expected_close,
                       e.status, e.link, e.notes])
        self._save()

    def set_opening_soon_status(self, row_index: int, status: str, notes: str) -> None:
        ws = self._tab(OPENING_SOON, OPENING_SOON_COLS)
        ws.cell(row_index, OPENING_SOON_COLS.index("Status") + 1, status)
        if notes:
            ws.cell(row_index, OPENING_SOON_COLS.index("Notes") + 1, notes)
        self._save()

    def read_paste_in(self) -> list[dict]:
        ws = self._tab(PASTE_IN, PASTE_IN_COLS)
        out = []
        for r in range(2, ws.max_row + 1):
            url = ws.cell(r, 1).value
            processed = ws.cell(r, PASTE_IN_COLS.index("Processed") + 1).value
            if url and not processed:
                out.append({"row": r, "url": url,
                            "notes": ws.cell(r, 2).value or ""})
        return out

    def mark_paste_in(self, row_index: int, result: str) -> None:
        ws = self._tab(PASTE_IN, PASTE_IN_COLS)
        ws.cell(row_index, PASTE_IN_COLS.index("Processed") + 1, "yes")
        ws.cell(row_index, PASTE_IN_COLS.index("Result") + 1, result)
        self._save()

    def read_job_bank(self) -> list[dict]:
        ws = self._tab(JOB_BANK, JOB_BANK_COLS)
        out = []
        for r in range(2, ws.max_row + 1):
            vals = [ws.cell(r, c).value or "" for c in range(1, len(JOB_BANK_COLS) + 1)]
            if any(vals):
                out.append({"row": r, **dict(zip(JOB_BANK_COLS, vals))})
        return out


    def append_feedback(self, rows: list[list[Any]]) -> None:
        ws = self._tab(FEEDBACK, FEEDBACK_COLS)
        for row in rows:
            ws.append(row)
        self._save()

    def read_feedback(self, limit: int = 60) -> list[dict]:
        ws = self._tab(FEEDBACK, FEEDBACK_COLS)
        out = []
        for r in range(2, ws.max_row + 1):
            vals = [ws.cell(r, c).value or "" for c in range(1, len(FEEDBACK_COLS) + 1)]
            if any(vals):
                out.append(dict(zip(FEEDBACK_COLS, vals)))
        return out[-limit:]

    def _save(self) -> None:
        self.wb.save(self.path)


# --------------------------------------------------------------------------
class GoogleSheetBackend(SheetBackend):
    mode = "google sheet"

    def __init__(self, sheet_id: str, sa_file: str = "", sa_json: str = ""):
        import gspread
        if sa_file:
            gc = gspread.service_account(filename=sa_file)
        elif sa_json:
            gc = gspread.service_account_from_dict(json.loads(sa_json))
        else:
            raise RuntimeError("SHEET_ID set but no service-account credentials "
                               "(GOOGLE_SERVICE_ACCOUNT_FILE or _JSON)")
        self.sh = gc.open_by_key(sheet_id)

    def _tab(self, name: str, cols: list[str]):
        import gspread
        try:
            ws = self.sh.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = self.sh.add_worksheet(name, rows=200, cols=max(len(cols), 8))
            if cols:
                ws.append_row(cols)
                ws.format("1:1", {"textFormat": {"bold": True}})
        return ws

    def ensure_layout(self, watchlist_seed: list[dict]) -> None:
        self._tab(JOB_BANK, JOB_BANK_COLS)
        self._tab(OPENING_SOON, OPENING_SOON_COLS)
        self._tab(ARCHIVE, ARCHIVE_COLS)
        stats = self._tab(STATS, [])
        if not stats.acell("A1").value:
            stats.update("A1", [["CONFIG (editable)", "Value"]]
                         + [[k, ""] for k in STATS_CONFIG_KEYS]
                         + [[""], STATS_RUN_COLS])
        wl = self._tab(WATCHLIST, WATCHLIST_COLS)
        if len(wl.get_all_values()) <= 1:
            wl.append_rows([[c.get("name"), c.get("ats"),
                             c.get("slug") or c.get("url", ""), ""]
                            for c in watchlist_seed])
        self._tab(PASTE_IN, PASTE_IN_COLS)
        self._tab(RUN_LOG, RUN_LOG_COLS)
        # Deadline highlight + all colour rules/dropdowns live in sheet_format
        # (applied at end of run and via `python -m jobbank format`).

    def existing_links(self) -> set[str]:
        ws = self._tab(JOB_BANK, JOB_BANK_COLS)
        col = JOB_BANK_COLS.index("Link") + 1
        return set(ws.col_values(col)[1:])

    def read_config_overrides(self) -> dict[str, Any]:
        ws = self._tab(STATS, [])
        overrides: dict[str, Any] = {}
        rows = ws.get_values("A2:B10")
        for row in rows:
            if len(row) == 2 and row[0] in STATS_CONFIG_KEYS and row[1] != "":
                key, val = row
                try:
                    overrides[key] = int(val) if key in INT_CONFIG_KEYS else val
                except (TypeError, ValueError):
                    continue   # a mistyped cell must never crash the run
        return overrides

    def write_config_values(self, values: dict[str, Any]) -> None:
        """Publish the settings this run actually used into the CONFIG cells.

        A blank cell means "inherit the config file", which leaves the dashboard
        with nothing to show. Writing the effective value back makes the sheet —
        and Sally's board — state the truth rather than a placeholder. Writing a
        value the user set is a no-op, so this never fights an edit.
        """
        ws = self._tab(STATS, [])
        rows = ws.get_values("A2:B12")
        seen = {r[0]: i + 2 for i, r in enumerate(rows) if r and r[0]}

        # A key added after this sheet was created has no row yet. Insert it
        # above the blank line that separates CONFIG from the run table, so the
        # dashboard can show a real value instead of an empty box.
        missing = [k for k in STATS_CONFIG_KEYS
                   if k in values and k not in seen and k != "cv_scope"]
        if missing:
            at = 2
            for i, r in enumerate(rows):
                head = r[0] if r else ""
                if head == "Run ID" or not head:
                    at = i + 2
                    break
                at = i + 3
            ws.insert_rows([[k, ""] for k in missing], row=at)
            rows = ws.get_values("A2:B12")
            seen = {r[0]: i + 2 for i, r in enumerate(rows) if r and r[0]}

        updates = []
        for key in STATS_CONFIG_KEYS:
            if key not in values or key not in seen:
                continue
            val = "" if values[key] is None else str(values[key])
            cur = ""
            idx = seen[key] - 2
            if idx < len(rows) and len(rows[idx]) > 1:
                cur = str(rows[idx][1])
            if cur != val:
                updates.append({"range": f"B{seen[key]}", "values": [[val]]})
        if updates:
            ws.batch_update(updates)

    def append_watchlist(self, entries: list[dict]) -> None:
        if entries:
            self._tab(WATCHLIST, WATCHLIST_COLS).append_rows(
                [[c.get("name"), c.get("ats"), c.get("slug") or c.get("url", ""), ""]
                 for c in entries])

    def read_watchlist(self) -> list[dict] | None:
        ws = self._tab(WATCHLIST, WATCHLIST_COLS)
        rows = ws.get_all_values()[1:]
        if not rows:
            return None
        out = []
        for row in rows:
            row += [""] * (4 - len(row))
            entry: dict[str, Any] = {"name": row[0], "ats": row[1]}
            if row[2].startswith("http"):
                entry["url"] = row[2]
            else:
                entry["slug"] = row[2]
            out.append(entry)
        return out

    def append_job_bank(self, jobs: list[Job]) -> None:
        if jobs:
            self._tab(JOB_BANK, JOB_BANK_COLS).append_rows(
                [_job_bank_row(j, date.today().isoformat()) for j in jobs],
                value_input_option="USER_ENTERED")

    def append_archive(self, jobs: list[tuple[Job, str]]) -> None:
        if jobs:
            self._tab(ARCHIVE, ARCHIVE_COLS).append_rows(
                [_archive_row(j, date.today().isoformat(), d) for j, d in jobs])

    def append_run_log(self, row: list[Any]) -> None:
        self._tab(RUN_LOG, RUN_LOG_COLS).append_row(row)

    def append_stats_run(self, row: list[Any]) -> None:
        self._tab(STATS, []).append_row(row)

    def read_opening_soon(self) -> list[dict]:
        ws = self._tab(OPENING_SOON, OPENING_SOON_COLS)
        out = []
        for i, row in enumerate(ws.get_all_values()[1:], start=2):
            row += [""] * (len(OPENING_SOON_COLS) - len(row))
            if row[0]:
                out.append({"row": i, **dict(zip(OPENING_SOON_COLS, row))})
        return out

    def append_opening_soon(self, entries: list) -> None:
        if entries:
            self._tab(OPENING_SOON, OPENING_SOON_COLS).append_rows(
                [[e.company, e.programme, e.expected_open, e.expected_close,
                  e.status, e.link, e.notes] for e in entries])

    def set_opening_soon_status(self, row_index: int, status: str, notes: str) -> None:
        ws = self._tab(OPENING_SOON, OPENING_SOON_COLS)
        updates = [{"range": f"{chr(65 + OPENING_SOON_COLS.index('Status'))}{row_index}",
                    "values": [[status]]}]
        if notes:
            updates.append({"range": f"{chr(65 + OPENING_SOON_COLS.index('Notes'))}{row_index}",
                            "values": [[notes]]})
        ws.batch_update(updates)

    def read_paste_in(self) -> list[dict]:
        ws = self._tab(PASTE_IN, PASTE_IN_COLS)
        out = []
        pcol = PASTE_IN_COLS.index("Processed")
        for i, row in enumerate(ws.get_all_values()[1:], start=2):
            row += [""] * (len(PASTE_IN_COLS) - len(row))
            if row[0] and not row[pcol]:
                out.append({"row": i, "url": row[0], "notes": row[1]})
        return out

    def mark_paste_in(self, row_index: int, result: str) -> None:
        ws = self._tab(PASTE_IN, PASTE_IN_COLS)
        ws.batch_update([
            {"range": f"{chr(65 + PASTE_IN_COLS.index('Processed'))}{row_index}",
             "values": [["yes"]]},
            {"range": f"{chr(65 + PASTE_IN_COLS.index('Result'))}{row_index}",
             "values": [[result]]}])

    def read_job_bank(self) -> list[dict]:
        ws = self._tab(JOB_BANK, JOB_BANK_COLS)
        out = []
        for i, row in enumerate(ws.get_all_values()[1:], start=2):
            row += [""] * (len(JOB_BANK_COLS) - len(row))
            if any(row):
                out.append({"row": i, **dict(zip(JOB_BANK_COLS, row))})
        return out

    def append_feedback(self, rows: list[list[Any]]) -> None:
        if not rows:
            return
        self._tab(FEEDBACK, FEEDBACK_COLS).append_rows(rows)

    def read_feedback(self, limit: int = 60) -> list[dict]:
        ws = self._tab(FEEDBACK, FEEDBACK_COLS)
        out = []
        for row in ws.get_all_values()[1:]:
            row += [""] * (len(FEEDBACK_COLS) - len(row))
            if any(c.strip() for c in row):
                out.append(dict(zip(FEEDBACK_COLS, row)))
        return out[-limit:]



def make_backend(settings) -> SheetBackend:
    sheet_id = settings.env("SHEET_ID")
    if sheet_id:
        return GoogleSheetBackend(sheet_id,
                                  sa_file=settings.env("GOOGLE_SERVICE_ACCOUNT_FILE"),
                                  sa_json=settings.env("GOOGLE_SERVICE_ACCOUNT_JSON"))
    return LocalXlsxBackend(settings.out_dir / "sheet_preview.xlsx")
