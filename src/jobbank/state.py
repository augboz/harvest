"""SQLite state: seen jobs (dedupe memory), dispositions, run history.

The DB file lives at data/state.db and is committed back to the repo by the
weekly Actions run so reposts never re-enter the pipeline (spec sec 5/8).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rapidfuzz import fuzz, process

from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    dedupe_key  TEXT PRIMARY KEY,
    norm_url    TEXT,
    company     TEXT,
    title       TEXT,
    fuzzy_text  TEXT,
    source      TEXT,
    first_seen  TEXT,
    score       INTEGER,
    disposition TEXT,      -- bank | archive | filtered
    detail      TEXT       -- filter reason / score reason
);
CREATE INDEX IF NOT EXISTS idx_seen_url ON seen_jobs(norm_url);
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    started    TEXT,
    finished   TEXT,
    stats_json TEXT
);
"""


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)
        self._fuzzy_cache: list[str] | None = None

    # ---- dedupe -------------------------------------------------------
    def is_seen_exact(self, job: Job) -> bool:
        cur = self.db.execute(
            "SELECT 1 FROM seen_jobs WHERE dedupe_key=? OR (norm_url<>'' AND norm_url=?)",
            (job.dedupe_key, job.norm_url))
        return cur.fetchone() is not None

    def _fuzzy_corpus(self) -> list[str]:
        if self._fuzzy_cache is None:
            self._fuzzy_cache = [r[0] for r in
                                 self.db.execute("SELECT fuzzy_text FROM seen_jobs")]
        return self._fuzzy_cache

    def is_seen_fuzzy(self, job: Job, threshold: int) -> bool:
        corpus = self._fuzzy_corpus()
        if not corpus:
            return False
        match = process.extractOne(job.fuzzy_text, corpus,
                                   scorer=fuzz.token_sort_ratio,
                                   score_cutoff=threshold)
        return match is not None

    # ---- recording ----------------------------------------------------
    def record(self, job: Job, disposition: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO seen_jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job.dedupe_key, job.norm_url, job.company, job.title, job.fuzzy_text,
             job.source, datetime.now(timezone.utc).isoformat(),
             job.score, disposition, detail))
        if self._fuzzy_cache is not None:
            self._fuzzy_cache.append(job.fuzzy_text)

    def banked_since(self, days: int = 7) -> int:
        """How many roles were added to the Job Bank in the last `days`.
        Lets the cap stay genuinely WEEKLY even when the pipeline runs daily."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        row = self.db.execute(
            "SELECT COUNT(*) FROM seen_jobs WHERE disposition='bank' AND first_seen >= ?",
            (cutoff,)).fetchone()
        return row[0] if row else 0

    def last_run_started(self) -> str | None:
        """ISO start time of the most recent completed run, or None."""
        row = self.db.execute("SELECT MAX(started) FROM runs").fetchone()
        return row[0] if row and row[0] else None

    def record_run(self, run_id: str, started: str, finished: str, stats_json: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
                        (run_id, started, finished, stats_json))

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()
