"""The job bank as MCP tools, built on FastMCP (github.com/PrefectHQ/fastmcp).

`python -m jobbank mcp` serves stdio; `.mcp.json` registers it with Claude Code,
so a chat session can ask the pipeline questions ("why was the Rolls-Royce role
filtered?", "how full is this week's cap?") or drive it (health probe, full run)
without leaving the conversation.

Two rules keep this safe:
- On stdio transport stdout IS the protocol, and the pipeline prints. Every
  tool that calls pipeline code runs it under redirect_stdout and returns the
  captured log instead of letting it hit the wire.
- Tools report whether a credential is configured, never its value.

Every tool takes `profile`: the default profile (repo root) or a directory
name under profiles/.

Read-only mode (JOBBANK_MCP_READONLY=1): the two tools that spend money and
write (health_check, run_pipeline) are not REGISTERED at all, so a client
cannot reach them even by name. This is what a deployed agent runs: filtering
tools on the client side alone would leave them one misconfiguration away.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

from fastmcp import FastMCP

from .settings import REPO_ROOT, Settings

# Tests point this at a scratch tree; everything resolves through it.
ROOT = REPO_ROOT
READONLY = os.environ.get("JOBBANK_MCP_READONLY", "") == "1"

mcp = FastMCP("jobbank")

_DISPOSITIONS = ("bank", "archive", "filtered")
_LOG_TAIL = 12_000  # chars of captured pipeline output returned to the caller
# A record's id as every tool reports it, and as an agent must cite it:
# dedupe_key[:12], the same as jobbank.rag.corpus.ID_LEN.
_ID_LEN = 12


def _settings(profile: str) -> Settings:
    if profile in ("", "default"):
        return Settings(root=ROOT)
    root = ROOT / "profiles" / profile
    if not (root / "config" / "config.yaml").exists():
        have = sorted(p.name for p in (ROOT / "profiles").glob("*/config/config.yaml"))
        raise ValueError(f"unknown profile {profile!r}; available: default, {', '.join(have)}")
    return Settings(root=root)


def _db(settings: Settings) -> sqlite3.Connection:
    """Read-only connection: a query must never create an empty state DB."""
    path = settings.state_path
    if not path.exists():
        raise ValueError(f"no state DB at {path} — the pipeline has not run for this profile")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _tail(buf: io.StringIO) -> str:
    text = buf.getvalue()
    return text[-_LOG_TAIL:] if len(text) > _LOG_TAIL else text


# ---- read tools -----------------------------------------------------------

@mcp.tool
def status(profile: str = "default") -> dict:
    """Where the pipeline stands: last run, weekly-cap usage, disposition
    totals, top discard reasons of the last 14 days, and the effective config.
    Reads local state only — no network, no spend."""
    s = _settings(profile)
    # Cutoffs computed in Python, isoformat-vs-isoformat, exactly like
    # State.banked_since — sqlite's datetime('now') writes a different string
    # shape and lexicographic comparison drifts on boundary days.
    def _ago(days: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _db(s) as db:
        counts = {r["disposition"]: r["n"] for r in db.execute(
            "SELECT disposition, COUNT(*) AS n FROM seen_jobs GROUP BY disposition")}
        banked_week = db.execute(
            "SELECT COUNT(*) FROM seen_jobs WHERE disposition='bank' AND first_seen >= ?",
            (_ago(7),)).fetchone()[0]
        reasons = [{"reason": r["detail"], "jobs": r["n"]} for r in db.execute(
            "SELECT detail, COUNT(*) AS n FROM seen_jobs "
            "WHERE disposition='filtered' AND first_seen >= ? "
            "GROUP BY detail ORDER BY n DESC LIMIT 8", (_ago(14),))]
        last = db.execute(
            "SELECT run_id, started, finished, stats_json FROM runs "
            "ORDER BY started DESC LIMIT 1").fetchone()
    last_run = None
    if last:
        last_run = {"run_id": last["run_id"], "started": last["started"],
                    "finished": last["finished"]}
        try:
            last_run["stats"] = json.loads(last["stats_json"] or "{}")
        except json.JSONDecodeError:
            last_run["stats"] = {"raw": last["stats_json"]}
    return {
        "profile": s.profile,
        "last_run": last_run,
        "banked_last_7_days": banked_week,
        "weekly_cap": s.weekly_cap,
        "totals_by_disposition": counts,
        "top_discard_reasons_14d": reasons,
        "config": {"min_score": s.min_score, "priority_score": s.priority_score,
                   "freshness_days": s.freshness_days, "bank_ceiling": s.bank_ceiling,
                   "watchlist_companies": len(s.watchlist)},
    }


@mcp.tool
def search_jobs(query: str, disposition: str | None = None, limit: int = 20,
                profile: str = "default") -> dict:
    """Search everything the pipeline has ever seen (bank, archive AND
    filtered-out), by company or title substring. Each hit carries `detail` —
    the filter reason or score reason — so this answers "did we ever see X, and
    what happened to it?". Local state only."""
    if disposition is not None and disposition not in _DISPOSITIONS:
        raise ValueError(f"disposition must be one of {_DISPOSITIONS}")
    s = _settings(profile)
    like = f"%{query.strip()}%"
    # `id` (dedupe_key[:12]) is the same id semantic_search returns, so an
    # answer citing records can be checked against whichever tool found them
    sql = (f"SELECT substr(dedupe_key, 1, {_ID_LEN}) AS id, company, title, source, "
           "norm_url, first_seen, score, disposition, detail FROM seen_jobs "
           "WHERE (company LIKE ? OR title LIKE ?)")
    args: list = [like, like]
    if disposition:
        sql += " AND disposition = ?"
        args.append(disposition)
    sql += " ORDER BY first_seen DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    with _db(s) as db:
        rows = [dict(r) for r in db.execute(sql, args)]
    return {"profile": s.profile, "query": query, "matches": len(rows), "jobs": rows}


@mcp.tool
def semantic_search(query: str, mode: str = "hybrid", k: int = 10,
                    disposition: str | None = None, profile: str = "default") -> dict:
    """Search every record ever seen by MEANING, not just by substring: hybrid
    (default) fuses keyword BM25 with vector similarity; "bm25" or "vector" run
    one leg. Each hit carries its `id` (cite it), a snippet, its rank in each
    leg and its vector `cosine`. `top_cosine` is how close the BEST match is at
    all: a low value means nothing in the records really answers the query.
    Needs a built index (python -m jobbank rag build). Local only, no writes."""
    if disposition is not None and disposition not in _DISPOSITIONS:
        raise ValueError(f"disposition must be one of {_DISPOSITIONS}")
    if mode not in ("hybrid", "bm25", "vector"):
        raise ValueError("mode must be hybrid, bm25 or vector")
    s = _settings(profile)
    try:
        from .rag.index import index_path
        from .rag.search import Searcher
    except ImportError:
        return {"profile": s.profile, "error": "retrieval layer not installed "
                "(pip install -e .[rag])", "hits": []}
    k = max(1, min(int(k), 50))
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            searcher = Searcher(index_path(s.root),
                                project=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
                                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"))
        except FileNotFoundError:
            return {"profile": s.profile, "hits": [],
                    "error": "no index for this profile: run python -m jobbank rag build"}
        degraded = ""
        try:
            out = searcher.search(query, mode=mode, k=k, disposition=disposition)
        except ValueError as exc:
            if mode == "bm25":
                raise
            # the embedding service is not configured here: answer on keywords
            # and SAY so, rather than failing the whole call
            degraded = f"vector leg unavailable ({exc}); keyword-only results"
            out = searcher.search(query, mode="bm25", k=k, disposition=disposition)
    for h in out["hits"]:
        r = str(h.get("reason") or "")
        h["reason"] = r if len(r) <= 200 else r[:200] + "…"
    return {"profile": s.profile, "query": query, "mode": out["mode"],
            "degraded": degraded, "top_cosine": out.get("top_cosine"),
            "index_built_at": searcher.meta.get("built_at"),
            "hits": out["hits"]}


@mcp.tool
def job_bank(open_only: bool = True, limit: int = 50, profile: str = "default") -> dict:
    """The Job Bank as it stands — live Google Sheet when SHEET_ID is
    configured, otherwise the local out/sheet_preview.xlsx dry-run. `open_only`
    keeps rows still awaiting action (Status not applied/oa/interview/offer/
    rejected). Descriptions are truncated; use the Link to read a posting."""
    s = _settings(profile)
    from .sheets import make_backend
    buf = io.StringIO()
    with redirect_stdout(buf):
        backend = make_backend(s)
        rows = backend.read_job_bank()
    if open_only:
        from .pipeline import _is_open
        rows = [r for r in rows if _is_open(r)]
    total = len(rows)
    rows = rows[: max(1, min(int(limit), 200))]
    for r in rows:
        for col in ("Description", "Score reason"):
            v = str(r.get(col) or "")
            if len(v) > 200:
                r[col] = v[:200] + "…"
    return {"profile": s.profile, "backend": backend.mode, "open_only": open_only,
            "total": total, "returned": len(rows), "rows": rows}


@mcp.tool
def run_history(limit: int = 10, profile: str = "default") -> dict:
    """Recent pipeline runs, newest first, each with its parsed stats
    (fetched/deduped/filtered/banked counts). Local state only."""
    s = _settings(profile)
    with _db(s) as db:
        rows = db.execute(
            "SELECT run_id, started, finished, stats_json FROM runs "
            "ORDER BY started DESC LIMIT ?",
            (max(1, min(int(limit), 100)),)).fetchall()
    runs = []
    for r in rows:
        entry = {"run_id": r["run_id"], "started": r["started"], "finished": r["finished"]}
        try:
            entry["stats"] = json.loads(r["stats_json"] or "{}")
        except json.JSONDecodeError:
            entry["stats"] = {"raw": r["stats_json"]}
        runs.append(entry)
    return {"profile": s.profile, "runs": runs}


@mcp.tool
def watchlist(profile: str = "default") -> dict:
    """The watched-company seed from config/watchlist.yaml. NOTE: after the
    first run the sheet's Watchlist tab is the source of truth; this file may
    lag behind edits made there."""
    s = _settings(profile)
    return {"profile": s.profile, "companies": s.watchlist,
            "note": "seed config — the sheet's Watchlist tab wins after first run"}


@mcp.tool
def doctor(profile: str = "default") -> dict:
    """Which credentials are configured (true/false only, mirrors
    `python -m jobbank doctor`) and what degrades when one is missing.
    No network, no secret values."""
    s = _settings(profile)
    checks = {
        "adzuna": bool(s.env("ADZUNA_APP_ID") and s.env("ADZUNA_APP_KEY")),
        "reed": bool(s.env("REED_API_KEY")),
        "anthropic_llm_scoring": bool(s.env("ANTHROPIC_API_KEY")),
        "google_sheet": bool(s.env("SHEET_ID")),
        "google_service_account": bool(s.env("GOOGLE_SERVICE_ACCOUNT_FILE")
                                       or s.env("GOOGLE_SERVICE_ACCOUNT_JSON")),
        "digest_email": bool(s.env("RESEND_API_KEY") or s.env("SMTP_HOST")),
    }
    return {"profile": s.profile, "configured": checks,
            "missing_means": "feature degrades to skip / heuristic / local xlsx dry-run",
            "config": {"weekly_cap": s.weekly_cap, "min_score": s.min_score,
                       "priority_score": s.priority_score,
                       "freshness_days": s.freshness_days,
                       "watchlist_companies": len(s.watchlist)}}


# ---- action tools ---------------------------------------------------------
# Registered at the bottom of the module, and only when READONLY is off.

def health_check(sources: list[str] | None = None, profile: str = "default") -> dict:
    """Probe every fetcher (or just `sources`) plus one tiny LLM ping — the
    fetch phase only: no scoring spend beyond the ping, no sheet writes.
    Hits the network and can take a couple of minutes. A source with zero jobs
    is reported but is NOT a failure; a raised source is."""
    s = _settings(profile)
    from .fetchers import run_all
    from .__main__ import _llm_health
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = run_all(s, only=sources)
        llm_ok, llm_detail = _llm_health(s)
    empty = sorted(n for n, c in result.per_source.items()
                   if c == 0 and n not in result.failures)
    return {"profile": s.profile,
            "jobs_seen": sum(result.per_source.values()),
            "per_source": result.per_source,
            "failures": result.failures,
            "empty_but_not_failed": empty,
            "llm_scoring": {"ok": llm_ok, "detail": llm_detail},
            "log": _tail(buf)}


def run_pipeline(sources: list[str] | None = None, digest: bool = False,
                 profile: str = "default") -> dict:
    """Execute the full pipeline: fetch -> dedupe -> hard filters -> LLM score
    -> volume gate -> sheet (or local xlsx) -> state commit. Spends LLM credit,
    appends to the sheet, and can take several minutes. The digest email is OFF
    by default for interactive runs — pass digest=true to send it. Restrict to
    `sources` for a partial/test run."""
    s = _settings(profile)
    from .pipeline import run
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = run(s, only_sources=sources, skip_digest=not digest)
    return {"profile": s.profile, "exit_code": code,
            "ok": code == 0, "log": _tail(buf)}


if not READONLY:
    mcp.tool(health_check)
    mcp.tool(run_pipeline)


if __name__ == "__main__":
    mcp.run()
