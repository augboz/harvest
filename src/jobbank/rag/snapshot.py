"""Capture full posting text without changing the pipeline.

The pipeline drops descriptions after each run on purpose, so historical
records carry only ~60-150 characters of text. But many sources (the ATS APIs,
jobspy) return up to 8,000 characters per posting, and most of what they list
TODAY is still in state.db from earlier runs. One fetch-only pass recovers
full text for every record still live, keyed on the same dedupe_key state.db
uses.

Fetch only: no filtering, no scoring, no LLM spend, no sheet, no state writes.
Sources that return no description simply contribute nothing. What this can
claim is exactly what it measures: full text for N records, captured on date D.

Snapshots are written under the (gitignored) index directory, since they hold
the text of third-party postings.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path


def capture(settings, out_path: Path, only: list[str] | None = None) -> dict:
    from ..enrich import enrich_location
    from ..fetchers import run_all

    result = run_all(settings, only=only)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    kept = 0
    by_source: dict[str, int] = {}
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        for job in result.jobs:
            try:
                enrich_location(job)
            except Exception:  # noqa: BLE001 - cosmetic, never lose a row for it
                pass
            if not (job.description or "").strip():
                continue
            f.write(json.dumps({
                "key": job.dedupe_key, "source": job.source,
                "company": job.company, "title": job.title,
                "location": job.location, "description": job.description,
                "fetched_at": fetched_at}, ensure_ascii=False) + "\n")
            kept += 1
            by_source[job.source.split(":")[0]] = by_source.get(job.source.split(":")[0], 0) + 1
    return {"fetched": len(result.jobs), "with_text": kept, "by_source": by_source,
            "failures": result.failures, "path": str(out_path)}


def load(path: Path) -> dict[str, dict]:
    """dedupe_key -> {description, location} from one snapshot file."""
    out: dict[str, dict] = {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            out[r["key"]] = {"description": r.get("description", ""),
                             "location": r.get("location", "")}
    return out


def load_all(directory: Path) -> dict[str, dict]:
    """Every snapshot in a directory, oldest first, so a later capture of the
    same record wins."""
    merged: dict[str, dict] = {}
    d = Path(directory)
    if not d.exists():
        return merged
    for p in sorted(d.glob("*.jsonl.gz")):
        merged.update(load(p))
    return merged
