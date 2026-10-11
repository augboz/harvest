"""Records to index, read from the pipeline's own state.

state.db's `seen_jobs` holds every record the pipeline has ever seen, but only
short text: company, title, and a one-line `detail` (the hard-filter reason, or
Claude's few-word score reason). Full posting text is NOT persisted (dropped on
purpose 2026-09-15), so it comes from elsewhere when it exists at all: a
fetch-only snapshot (see snapshot.py) keyed on the same dedupe_key.

What goes into the searchable text is a deliberate choice:

- Title, organisation, location and full text when there is any.
- Claude's score reason for SCORED records (bank / archive): it is a short
  assessment of the record itself.
- NOT the hard-filter reason. 12k records share "type: no internship/placement
  signal"; embedding it would pull thousands of unrelated records together and
  let a keyword search match on the pipeline's own bookkeeping. It is kept as a
  plain field for display.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

# A record's id as an agent cites it. 12 hex chars of the sha1 dedupe_key is 48
# bits: a collision among ~25k records is about 1 in a billion, and build_index
# checks anyway.
ID_LEN = 12
# How much full text goes into the searchable content. Enough for a posting's
# substance; the embedding model reads up to 8k tokens but a long tail of
# benefits boilerplate only dilutes the vector.
DESC_CHARS = 2000
_SCORED = ("bank", "archive")


@dataclass
class Record:
    key: str                 # full dedupe_key
    company: str
    title: str
    source: str
    url: str
    first_seen: str
    disposition: str         # bank | archive | filtered
    score: int | None
    reason: str              # filter reason or score reason, verbatim
    location: str = ""
    description: str = ""

    @property
    def id(self) -> str:
        return self.key[:ID_LEN]

    @property
    def has_full_text(self) -> bool:
        return bool(self.description.strip())

    def content(self) -> str:
        """The text that is BOTH embedded and BM25-indexed, so the two search
        legs are compared on exactly the same evidence."""
        parts = [self.title, self.company]
        if self.location:
            parts.append(self.location)
        if self.disposition in _SCORED and self.reason:
            parts.append(self.reason)
        if self.description:
            parts.append(self.description[:DESC_CHARS])
        return "\n".join(p.strip() for p in parts if p and p.strip())


def _connect_ro(path: Path) -> sqlite3.Connection:
    """Read-only, exactly like mcp_server._db: building an index must never
    create or touch the pipeline's state."""
    if not path.exists():
        raise FileNotFoundError(f"no state DB at {path}")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_records(state_path: Path, full_text: dict[str, dict] | None = None,
                 limit: int | None = None) -> list[Record]:
    """Every seen record, newest first, with full text joined in where the
    snapshot has it (`full_text` maps dedupe_key -> {description, location})."""
    full_text = full_text or {}
    sql = ("SELECT dedupe_key, company, title, source, norm_url, first_seen, "
           "score, disposition, detail FROM seen_jobs ORDER BY first_seen DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    con = _connect_ro(state_path)
    try:
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        extra = full_text.get(r["dedupe_key"]) or {}
        out.append(Record(
            key=r["dedupe_key"], company=r["company"] or "", title=r["title"] or "",
            source=r["source"] or "", url=r["norm_url"] or "",
            first_seen=r["first_seen"] or "", disposition=r["disposition"] or "",
            score=r["score"], reason=r["detail"] or "",
            location=str(extra.get("location") or ""),
            description=str(extra.get("description") or "")))
    return out


def coverage(records: list[Record]) -> dict:
    """The numbers every claim about this index rests on."""
    by_disp: dict[str, int] = {}
    full_by_disp: dict[str, int] = {}
    for r in records:
        by_disp[r.disposition] = by_disp.get(r.disposition, 0) + 1
        if r.has_full_text:
            full_by_disp[r.disposition] = full_by_disp.get(r.disposition, 0) + 1
    n_full = sum(full_by_disp.values())
    return {"records": len(records), "with_full_text": n_full,
            "full_text_pct": round(100 * n_full / len(records), 1) if records else 0.0,
            "by_disposition": by_disp, "full_text_by_disposition": full_by_disp}
