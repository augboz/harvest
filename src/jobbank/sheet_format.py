"""Make the Job Bank tab scannable: coloured dropdowns + conditional colour
rules + a score gradient. Idempotent — clears its own rules first so re-running
never stacks duplicates. Pure Google Sheets API, needs no LLM key.

Applied automatically at the end of every run and via `python -m jobbank format`.
"""
from __future__ import annotations

import re

from .sheets import (ARCHIVE, ARCHIVE_COLS, HIDDEN_COLS, JOB_BANK, JOB_BANK_COLS,
                     UTILITY_TABS)

C = {c: i for i, c in enumerate(JOB_BANK_COLS)}


def _rgb(hex_str: str) -> dict:
    h = hex_str.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255,
            "blue": int(h[4:6], 16) / 255}


DARK = _rgb("#1A1A1A")

# value -> background colour
CATEGORY_COLOURS = {
    "AI/ML": "#E1BEE7", "SWE": "#BBDEFB", "MusicTech": "#F8BBD0",
    "SportsData": "#B2DFDB", "Motorsport": "#FFAB91", "Space": "#C5CAE9",
    "BigTech": "#C8E6C9", "Other": "#E0E0E0",
}
STATUS_VALUES = ["Not applied", "Applied", "OA", "Interview", "Offer", "Rejected",
                 "Not interested"]   # dashboard writes this on dismiss
STATUS_COLOURS = {
    "Not applied": "#ECEFF1", "Applied": "#BBDEFB", "OA": "#FFE082",
    "Interview": "#FFCC80", "Offer": "#A5D6A7", "Rejected": "#EF9A9A",
    "Not interested": "#ECEFF1",
}
# location keyword -> colour (first match wins; EU/UK green, remote blue, US amber)
LOCATION_RULES = [
    # locations are normalised to "City, Country" (see enrich.normalize_location),
    # so matching the country suffix is enough. UK/EU green, remote blue, US amber.
    ("Remote", "#B3E5FC"),
    ("UK", "#C8E6C9"), ("France", "#C8E6C9"), ("Germany", "#C8E6C9"),
    ("Netherlands", "#C8E6C9"), ("Spain", "#C8E6C9"), ("Italy", "#C8E6C9"),
    ("Ireland", "#C8E6C9"), ("Belgium", "#C8E6C9"), ("Portugal", "#C8E6C9"),
    ("Poland", "#C8E6C9"), ("Sweden", "#C8E6C9"), ("Switzerland", "#C8E6C9"),
    ("Austria", "#C8E6C9"), ("Denmark", "#C8E6C9"), ("Serbia", "#C8E6C9"),
    ("USA", "#FFE0B2"), ("Canada", "#FFE0B2"),
]


MAX_ROWS = 2000  # bound dropdown ranges; Sheets rejects validation on unbounded columns


def _col_range(sid: int, col: int, start_row: int = 1, end_row: int | None = None) -> dict:
    r = {"sheetId": sid, "startRowIndex": start_row,
         "startColumnIndex": col, "endColumnIndex": col + 1}
    if end_row is not None:
        r["endRowIndex"] = end_row
    return r


def _dropdown(sid: int, col: int, values: list[str]) -> dict:
    return {"setDataValidation": {
        "range": _col_range(sid, col, end_row=MAX_ROWS),
        "rule": {"condition": {"type": "ONE_OF_LIST",
                               "values": [{"userEnteredValue": v} for v in values]},
                 "showCustomUi": True, "strict": False}}}


def _text_eq_rule(sid: int, col: int, value: str, bg: str, bold: bool = False) -> dict:
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [_col_range(sid, col)],
        "booleanRule": {
            "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": value}]},
            "format": {"backgroundColor": _rgb(bg),
                       "textFormat": {"foregroundColor": DARK, "bold": bold}}}}}}


def _text_contains_rule(sid: int, col: int, value: str, bg: str) -> dict:
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [_col_range(sid, col)],
        "booleanRule": {
            "condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": value}]},
            "format": {"backgroundColor": _rgb(bg)}}}}}


def _score_gradient(sid: int) -> dict:
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [_col_range(sid, C["Score"])],
        "gradientRule": {
            "minpoint": {"color": _rgb("#F8696B"), "type": "NUMBER", "value": "55"},
            "midpoint": {"color": _rgb("#FFEB84"), "type": "NUMBER", "value": "72"},
            "maxpoint": {"color": _rgb("#63BE7B"), "type": "NUMBER", "value": "90"}}}}}


DATE_COLS = ["Deadline", "Opens"]


def _date_format(sid: int, col: int) -> dict:
    """Display dates European style (dd/mm/yyyy). Values are written as ISO so
    Sheets parses them into real dates regardless of locale; this only changes
    how they render."""
    return {"repeatCell": {
        "range": _col_range(sid, col, end_row=MAX_ROWS),
        "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}}},
        "fields": "userEnteredFormat.numberFormat"}}


def _deadline_rule(sid: int) -> dict:
    """Red when the deadline is under 7 days away. Compares real date values, so
    it is locale-proof (the old DATEVALUE(text) form broke on dd/mm input)."""
    col_letter = _a1_col(C["Deadline"])
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [_col_range(sid, C["Deadline"])],
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA", "values": [{
                "userEnteredValue":
                    f'=AND(${col_letter}2<>"", ISNUMBER(${col_letter}2), '
                    f'${col_letter}2-TODAY()<7, ${col_letter}2-TODAY()>=0)'}]},
            "format": {"backgroundColor": _rgb("#EF9A9A"),
                       "textFormat": {"bold": True}}}}}}


def _hide_columns(sid: int) -> list[dict]:
    reqs = []
    for name in HIDDEN_COLS:
        if name in C:
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS",
                          "startIndex": C[name], "endIndex": C[name] + 1},
                "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
    return reqs


def _a1_col(idx: int) -> str:
    """0-based column index -> A1 letter(s) (handles beyond Z)."""
    s, idx = "", idx
    while True:
        s = chr(65 + idx % 26) + s
        idx = idx // 26 - 1
        if idx < 0:
            return s


def hide_utility_tabs(sh) -> int:
    """Hide the machinery tabs so only Job Bank + Opening Soon (and any tab this
    pipeline didn't create, e.g. a Gemini canvas) show in the tab bar. The tabs
    are still written to — hidden, not removed."""
    meta = sh.fetch_sheet_metadata()
    reqs = []
    for s_ in meta["sheets"]:
        p = s_["properties"]
        if p["title"] in UTILITY_TABS and not p.get("hidden"):
            reqs.append({"updateSheetProperties": {
                "properties": {"sheetId": p["sheetId"], "hidden": True},
                "fields": "hidden"}})
    if reqs:
        sh.batch_update({"requests": reqs})
        print(f"  [sheet] hid {len(reqs)} utility tab(s)")
    return len(reqs)


# A disposition always starts with one of these; used to tell a 12-column legacy
# archive row from a migrated 13-column one, since the two are otherwise identical
# in shape and a mis-detection would shift real data into the wrong column.
_DISPOSITION_RE = re.compile(
    r"^\s*(score\b|paste-in\b|dead link\b|removed\b|promoted\b)", re.I)

# Archive layout before the Description column was added (ARCHIVE_COLS gained it,
# but nothing ever migrated the tab, so old rows still sit in this shape).
ARCHIVE_LEGACY_COLS = [c for c in ARCHIVE_COLS if c != "Description"]


def ensure_archive_schema(sh) -> int:
    """Migrate the Archive tab to ARCHIVE_COLS.

    `Description` was added to ARCHIVE_COLS but the tab was never migrated, so the
    header stayed 12-wide while newer rows were appended 13-wide. The result: every
    column from `Description` on is mislabelled, the dashboard reads blank
    descriptions, and the disposition guard reads the wrong column.

    Old and new rows are interleaved by date, so this cannot be a header rewrite —
    each row is classified individually by where its disposition sits, and legacy
    rows get an empty Description spliced in at the right index. Idempotent: a row
    already in the new shape is left alone, so re-running is a no-op.

    Returns the number of rows migrated.
    """
    try:
        ws = sh.worksheet(ARCHIVE)
    except Exception:  # noqa: BLE001 — tab may not exist yet on a fresh sheet
        return 0
    vals = ws.get_all_values()
    if not vals or not any(vals):
        ws.update(values=[ARCHIVE_COLS], range_name="A1")
        return 0

    n_new, n_legacy = len(ARCHIVE_COLS), len(ARCHIVE_LEGACY_COLS)
    desc_at = ARCHIVE_COLS.index("Description")
    header_ok = vals[0][:n_new] == ARCHIVE_COLS

    out, migrated = [], 0
    for row in vals[1:]:
        if not any(c.strip() for c in row):
            continue
        row = list(row) + [""] * (n_new - len(row))
        # New-shape rows carry the disposition in the last column. Legacy rows have
        # it one to the left with a trailing blank — those are the ones to splice.
        if not _DISPOSITION_RE.search(row[n_new - 1] or "") \
                and _DISPOSITION_RE.search(row[n_legacy - 1] or ""):
            row = row[:desc_at] + [""] + row[desc_at:n_legacy]
            migrated += 1
        out.append(row[:n_new])

    if not migrated and header_ok:
        return 0
    ws.update(values=[ARCHIVE_COLS] + out, range_name="A1",
              value_input_option="USER_ENTERED")
    return migrated


def ensure_schema(sh) -> None:
    """Delete any native Table on Job Bank (it forces 'typed columns' and blocks
    dropdowns) and reorder existing rows to the current JOB_BANK_COLS, preserving
    every value by column name (Status/Notes included); columns dropped from
    JOB_BANK_COLS are removed from the sheet."""
    migrated = ensure_archive_schema(sh)
    if migrated:
        print(f"  [sheet] migrated {migrated} legacy Archive row(s) to add Description")

    meta = sh.fetch_sheet_metadata()
    del_reqs = []
    for s in meta["sheets"]:
        if s["properties"]["title"] == JOB_BANK:
            for t in s.get("tables", []):
                del_reqs.append({"deleteTable": {"tableId": t["tableId"]}})
    if del_reqs:
        sh.batch_update({"requests": del_reqs})
        print(f"  [sheet] removed {len(del_reqs)} native table(s) from Job Bank")

    ws = sh.worksheet(JOB_BANK)
    vals = ws.get_all_values()
    if not vals or not any(vals):
        ws.update(values=[JOB_BANK_COLS], range_name="A1")
        return
    header = vals[0]
    if header == JOB_BANK_COLS:
        return  # already in the right order
    data_rows = [r for r in vals[1:] if any(r)]
    remapped = [[dict(zip(header, row)).get(col, "") for col in JOB_BANK_COLS]
                for row in data_rows]
    # Overwrite in place (NO clear-then-write: that leaves a window where a crash
    # loses every row). Same column count in/out, so the block fully overwrites.
    new_block = [JOB_BANK_COLS] + remapped
    ws.update(values=new_block, range_name="A1", value_input_option="USER_ENTERED")
    # clear only trailing rows that the new (shorter) block no longer covers
    if len(new_block) < len(vals):
        empty = [[""] * len(JOB_BANK_COLS) for _ in range(len(vals) - len(new_block))]
        ws.update(values=empty, range_name=f"A{len(new_block) + 1}")
    # ...and trailing COLUMNS left behind when a column is dropped from the schema
    extra = len(header) - len(JOB_BANK_COLS)
    if extra > 0:
        first, last = _a1_col(len(JOB_BANK_COLS)), _a1_col(len(header) - 1)
        ws.batch_clear([f"{first}1:{last}{max(len(vals), 2)}"])
        print(f"  [sheet] cleared {extra} stale column(s) after schema change")
    print(f"  [sheet] reordered {len(remapped)} Job Bank rows to new column order")


def apply_job_bank_formatting(sh) -> None:
    """sh is a gspread Spreadsheet. Colours are applied first (always); native
    dropdowns are attempted separately so a validation quirk can't lose them."""
    meta = sh.fetch_sheet_metadata()
    sid = existing = row_count = None
    for s in meta["sheets"]:
        if s["properties"]["title"] == JOB_BANK:
            sid = s["properties"]["sheetId"]
            existing = len(s.get("conditionalFormats", []))
            row_count = s["properties"]["gridProperties"]["rowCount"]
            break
    if sid is None:
        return

    # --- Batch A: colours + gradient + freeze (the main ask) ---------------
    colour: list[dict] = []
    for _ in range(existing or 0):
        colour.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}})
    colour.append(_deadline_rule(sid))
    colour.append(_text_eq_rule(sid, C["Priority"], "PRIORITY", "#FFD54F", bold=True))
    for value, bg in CATEGORY_COLOURS.items():
        colour.append(_text_eq_rule(sid, C["Category"], value, bg))
    for value, bg in STATUS_COLOURS.items():
        colour.append(_text_eq_rule(sid, C["Status"], value, bg, bold=(value in ("Offer", "Interview"))))
    for value, bg in LOCATION_RULES:
        colour.append(_text_contains_rule(sid, C["Location"], value, bg))
    colour.append(_score_gradient(sid))
    for _name in DATE_COLS:
        if _name in C:
            colour.append(_date_format(sid, C[_name]))
    colour.extend(_hide_columns(sid))
    colour.append({"updateSheetProperties": {
        "properties": {"sheetId": sid,
                       "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 6}},
        "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}})
    sh.batch_update({"requests": colour})

    # --- Batch B: native dropdowns (the table is gone, so all 4 work) -------
    end_row = min(MAX_ROWS, row_count or MAX_ROWS)
    dropdown_cols = [(C["Category"], list(CATEGORY_COLOURS)),
                     (C["Priority"], ["PRIORITY"]),
                     (C["Status"], STATUS_VALUES)]
    # Wipe every existing validation on the sheet first. Reordering columns
    # leaves old dropdowns sitting on whatever column now occupies that index
    # (Status's list ended up on Opens, rejecting valid dates as "not an item on
    # the specified list"). fields="dataValidation" touches only validation.
    drop: list[dict] = [{"updateCells": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": end_row,
                  "startColumnIndex": 0, "endColumnIndex": len(JOB_BANK_COLS) + 6},
        "fields": "dataValidation"}}]
    for col, values in dropdown_cols:
        drop.append({"setDataValidation": {
            "range": _col_range(sid, col, end_row=end_row),
            "rule": {"condition": {"type": "ONE_OF_LIST",
                                   "values": [{"userEnteredValue": v} for v in values]},
                     "showCustomUi": True, "strict": False}}})
    try:
        sh.batch_update({"requests": drop})
    except Exception as exc:  # noqa: BLE001
        print(f"  [sheet] native dropdowns skipped ({exc}); colour rules still applied")


PLACEHOLDER_REASONS = ("pulled in from the archive", "promoted from the archive")
# A row scored while the LLM was unreachable is exactly as unreviewed as one
# promoted from the archive, and its reason cell says so in as many words. Left
# alone it keeps a cheap keyword score forever — Auto Trader's "Early Careers
# Advisor" sat at 79 (the LLM scores it 28) after the 2026-08-25 credit outage.
# Matched anywhere in the string, not just at the start: an urgent deadline
# prefixes the reason.
UNREVIEWED_MARK = "heuristic score"


def complete_bank_rows(sh, settings) -> int:
    """Every Job Bank row must carry real data. A role promoted from the archive
    arrives with a plumbing note where its reasoning belongs and no deadline, so
    those rows get their description fetched and are scored properly — exactly as
    if they had come through the pipeline. One LLM call per incomplete row
    (fractions of a penny); rows that are already complete are skipped.
    """
    import re as _re
    from .models import Job
    from .pastein import _fetch_html
    # MUST be the profile's own scorer. Hardcoding one profile's scorer re-scored
    # every row of another profile's bank against the wrong rubric — those roles
    # came back at 1-35, which then read as the pipeline banking junk when in
    # fact it was this pass overwriting good scores.
    from .profile_impl import scorer_for
    score_jobs = scorer_for(settings).score_jobs
    from .enrich import enrich_timing

    ws = sh.worksheet(JOB_BANK)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return 0
    header = rows[0]
    idx = {c: i for i, c in enumerate(header)}
    if any(c not in idx for c in ("Score reason", "Company", "Role", "Link")):
        return 0

    todo = []
    for r, row in enumerate(rows[1:], start=2):
        if not any(row):
            continue
        row = row + [""] * (len(header) - len(row))
        reason = (row[idx["Score reason"]] or "").strip().lower()
        desc = (row[idx["Description"]] or "").strip() if "Description" in idx else ""
        placeholder = (not reason
                       or any(reason.startswith(p) for p in PLACEHOLDER_REASONS)
                       or UNREVIEWED_MARK in reason)
        if desc and not placeholder:
            continue
        job = Job(source=row[idx["Source"]] if "Source" in idx else "",
                  company=row[idx["Company"]], title=row[idx["Role"]],
                  url=row[idx["Link"]],
                  location=row[idx["Location"]] if "Location" in idx else "")
        html = _fetch_html(job.url) or ""
        if html:
            job.description = _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", " ", html))[:6000]
        todo.append((r, job))

    if not todo:
        return 0
    print(f"  [sheet] filling in {len(todo)} row(s) missing a description or reason")
    score_jobs([j for _, j in todo], settings)

    updates = []
    for r, job in todo:
        enrich_timing(job)
        reason = job.score_reason + (
            f" | Attainability: {job.attainability_note}" if job.attainability_note else "")
        for col, val in (("Score", job.score), ("Category", job.category),
                         ("Score reason", reason), ("Duration", job.duration),
                         ("Description", job.summary),
                         ("Deadline", job.deadline.isoformat() if job.deadline else "")):
            if col in idx and val not in (None, ""):
                updates.append({"range": f"{_a1_col(idx[col])}{r}", "values": [[val]]})
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    return len(todo)


def repair_bank_rows(sh) -> int:
    """Normalise any Job Bank row that didn't come through the pipeline's own
    enrichment — e.g. a role the dashboard promoted out of the archive, where the
    stored Location may predate normalisation and Duration is blank. Only ever
    fills/normalises those two columns; human columns are untouched."""
    from .enrich import extract_duration, normalize_location
    ws = sh.worksheet(JOB_BANK)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return 0
    header = rows[0]
    try:
        loc_i, dur_i, role_i = (header.index("Location"), header.index("Duration"),
                                header.index("Role"))
    except ValueError:
        return 0
    updates, fixed = [], 0
    for r, row in enumerate(rows[1:], start=2):
        if not any(row):
            continue
        row = row + [""] * (len(header) - len(row))
        loc, dur, role = row[loc_i], row[dur_i], row[role_i]
        new_loc = normalize_location(loc) if loc else loc
        new_dur = dur or extract_duration(role)
        if new_loc != loc:
            updates.append({"range": f"{_a1_col(loc_i)}{r}", "values": [[new_loc]]})
        if new_dur != dur:
            updates.append({"range": f"{_a1_col(dur_i)}{r}", "values": [[new_dur]]})
        if new_loc != loc or new_dur != dur:
            fixed += 1
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    return fixed


def backfill_timing(sh) -> int:
    """Fill a blank Duration on existing Job Bank rows from the Role text.
    Writes only that one column, addressed by name — an earlier version wrote to a
    hardcoded H:I range, which after the column reorder pointed at Deadline/Opens."""
    from .enrich import extract_duration
    ws = sh.worksheet(JOB_BANK)
    rows = ws.get_all_values()
    if len(rows) <= 1 or "Duration" not in C:
        return 0
    dur_col, role_col = C["Duration"], C["Role"]
    letter = _a1_col(dur_col)
    updates, filled = [], 0
    for r, row in enumerate(rows[1:], start=2):
        if not any(row):
            continue
        role = row[role_col] if len(row) > role_col else ""
        current = row[dur_col] if len(row) > dur_col else ""
        if current:
            continue
        dur = extract_duration(role)
        if dur:
            updates.append({"range": f"{letter}{r}", "values": [[dur]]})
            filled += 1
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    return filled
