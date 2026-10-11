"""Retrieval evaluation: honest numbers for search over the records.

Design (from the approved plan; every choice is there to keep the headline
number from being tuned in embeddings' favour):

- Queries are PRE-REGISTERED: written before any system is run, and the file's
  sha256 is recorded in every results file. Edit the queries and the hash
  changes, so a re-tuned query set cannot pass for the original.
- Four systems: the existing `search_jobs` (one SQL LIKE substring, exactly as
  the MCP tool runs it: the product baseline), BM25 (the scientific baseline
  every headline compares against), vector only, and hybrid RRF.
- Known-item queries have their relevant set fixed by construction: the target
  record plus its near-duplicates (same organisation, title ratio >= 90).
- Topical queries are POOLED: the top 10 of every system, judged blind and
  shuffled (no system names) on 0/1/2. Unjudged records count as not relevant,
  so recall is pool-relative and reported as such.
- Every metric is reported overall, by query type, and split by whether the
  relevant records have full text or metadata only.
- Paired bootstrap 95% CIs over queries; a difference inside the CI is not a
  difference.

The query set and labels live in private/eval/ (they reveal what the records
are about); only aggregate tables are meant for public text.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
from pathlib import Path
from typing import Callable, Iterable, Sequence

QUERY_TYPES = ("known_lexical", "known_paraphrase", "topical", "unanswerable")
SYSTEMS = ("like", "bm25", "vector", "hybrid")
POOL_DEPTH = 10
NEAR_DUP_RATIO = 90


# ------------------------------------------------------------------ metrics --
# All pure: ranked ids + relevance grades in, a number out. Unit-tested on
# hand-computed cases in tests/test_rag_eval.py.

def recall_at(ranked: Sequence[str], rel: dict[str, int], k: int) -> float:
    relevant = {d for d, g in rel.items() if g > 0}
    if not relevant:
        return 0.0
    return len(relevant & set(ranked[:k])) / len(relevant)


def precision_at(ranked: Sequence[str], rel: dict[str, int], k: int) -> float:
    return sum(1 for d in ranked[:k] if rel.get(d, 0) > 0) / k


def mrr_at(ranked: Sequence[str], rel: dict[str, int], k: int) -> float:
    for i, d in enumerate(ranked[:k], start=1):
        if rel.get(d, 0) > 0:
            return 1.0 / i
    return 0.0


def ndcg_at(ranked: Sequence[str], rel: dict[str, int], k: int) -> float:
    def dcg(grades: Iterable[int]) -> float:
        return sum((2 ** g - 1) / math.log2(i + 1) for i, g in enumerate(grades, start=1))
    ideal = dcg(sorted((g for g in rel.values() if g > 0), reverse=True)[:k])
    if ideal == 0:
        return 0.0
    return dcg(rel.get(d, 0) for d in ranked[:k]) / ideal


def bootstrap_ci(values: Sequence[float], n: int = 2000, seed: int = 0,
                 alpha: float = 0.05) -> tuple[float, float, float]:
    """Mean and percentile CI over queries."""
    if not values:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(values, k=len(values))) / len(values) for _ in range(n))
    lo, hi = means[int(alpha / 2 * n)], means[int((1 - alpha / 2) * n) - 1]
    return (sum(values) / len(values), lo, hi)


def paired_diff_ci(a: Sequence[float], b: Sequence[float], n: int = 2000,
                   seed: int = 0) -> tuple[float, float, float]:
    """CI of mean(a - b), resampling QUERIES (so the pairing is kept)."""
    if len(a) != len(b):
        raise ValueError("paired samples must be the same length")
    return bootstrap_ci([x - y for x, y in zip(a, b)], n=n, seed=seed)


# ------------------------------------------------------------------ queries --

def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_queries(path: Path) -> list[dict]:
    import yaml
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    qs = data.get("queries") or []
    seen = set()
    for q in qs:
        if q.get("type") not in QUERY_TYPES:
            raise ValueError(f"query {q.get('id')}: type must be one of {QUERY_TYPES}")
        if not str(q.get("text") or "").strip():
            raise ValueError(f"query {q.get('id')}: empty text")
        if q["id"] in seen:
            raise ValueError(f"duplicate query id {q['id']}")
        seen.add(q["id"])
        if q["type"].startswith("known") and not q.get("target"):
            raise ValueError(f"query {q['id']}: known-item queries need a target id")
    return qs


def sample_targets(state_path: Path, n: int = 10, seed: int = 2026,
                   full_text_keys: set[str] | None = None) -> list[dict]:
    """Seeded, stratified known-item targets: spread across dispositions and
    across full-text vs metadata-only, so no single stratum carries the result.
    You write a query for each WITHOUT looking at any search output."""
    full_text_keys = full_text_keys or set()
    con = sqlite3.connect(f"file:{Path(state_path).as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT dedupe_key, company, title, disposition FROM seen_jobs").fetchall()
    finally:
        con.close()
    strata: dict[tuple[str, bool], list] = {}
    for key, co, title, disp in rows:
        strata.setdefault((disp, key in full_text_keys), []).append((key, co, title, disp))
    rng = random.Random(seed)
    order = sorted(strata)                      # deterministic stratum order
    picked: list[dict] = []
    while len(picked) < n and any(strata[s] for s in order):
        for s in order:
            if strata[s] and len(picked) < n:
                key, co, title, disp = strata[s].pop(rng.randrange(len(strata[s])))
                picked.append({"target": key[:12], "company": co, "title": title,
                               "disposition": disp, "full_text": s[1]})
    return picked


# ----------------------------------------------------------------- systems --

def run_like(state_path: Path, query: str, k: int) -> list[str]:
    """The existing search_jobs tool, verbatim: ONE substring over company or
    title. Multi-word natural-language queries mostly return nothing, which is
    the point of having it as the product baseline."""
    con = sqlite3.connect(f"file:{Path(state_path).as_posix()}?mode=ro", uri=True)
    try:
        like = f"%{query.strip()}%"
        rows = con.execute(
            "SELECT substr(dedupe_key, 1, 12) FROM seen_jobs "
            "WHERE (company LIKE ? OR title LIKE ?) ORDER BY first_seen DESC LIMIT ?",
            (like, like, k)).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def run_system(system: str, searcher, state_path: Path, query: str,
               k: int) -> list[str]:
    if system == "like":
        return run_like(state_path, query, k)
    if system == "bm25":
        return [h["id"] for h in searcher.bm25(query, k)]
    if system == "vector":
        return [h["id"] for h in searcher.vector(query, k)]
    if system == "hybrid":
        return [h["id"] for h in searcher.hybrid(query, k)["hits"]]
    raise ValueError(system)


# ------------------------------------------------------------------- qrels --

def known_item_qrels(target: str, records: dict[str, dict]) -> dict[str, int]:
    """The target, plus records that are the same advert in all but name: same
    organisation and a title ratio of at least NEAR_DUP_RATIO."""
    from rapidfuzz import fuzz
    t = records.get(target)
    if t is None:
        raise ValueError(f"target {target} is not in the index")
    rel = {target: 2}
    for rid, r in records.items():
        if rid != target and r["company"].lower() == t["company"].lower() and \
                fuzz.token_sort_ratio(r["title"], t["title"]) >= NEAR_DUP_RATIO:
            rel[rid] = 2
    return rel


def pool(runs: dict[str, dict[str, list[str]]], qid: str, depth: int = POOL_DEPTH,
         seed: int = 0) -> list[str]:
    """Union of every system's top `depth` for one query, SHUFFLED with a fixed
    seed so the order a judge sees says nothing about which system found what."""
    ids = sorted({d for sys_runs in runs.values() for d in sys_runs.get(qid, [])[:depth]})
    random.Random(f"{seed}:{qid}").shuffle(ids)
    return ids


def label_pool(items: list[dict], judged: dict[str, int],
               ask: Callable[[str], str] = input,
               show: Callable[[str], None] = print) -> dict[str, int]:
    """Blind 0/1/2 judging, resumable: already-judged ids are skipped, and the
    caller saves after every answer. `items` carry display fields only, never a
    system name or rank. 'q' stops early."""
    for it in items:
        if it["id"] in judged:
            continue
        show(f"\n[{it['id']}] {it['company']} | {it['title']}\n    {it.get('snippet', '')[:240]}")
        while True:
            a = ask("  relevant? 0 no / 1 partly / 2 yes / q quit > ").strip().lower()
            if a == "q":
                return judged
            if a in ("0", "1", "2"):
                judged[it["id"]] = int(a)
                break
    return judged


# ------------------------------------------------------------------ scoring --

def score(queries: list[dict], runs: dict[str, dict[str, list[str]]],
          qrels: dict[str, dict[str, int]], full_text_ids: set[str],
          k_recall: int = 5) -> dict:
    """Per system: metrics per query, then means with CIs, overall / by type /
    by text depth of the relevant records. Unanswerable queries are scored by
    the refusal check in the agent eval, not here."""
    per: dict[str, dict[str, dict[str, float]]] = {s: {} for s in runs}
    for q in queries:
        if q["type"] == "unanswerable":
            continue
        rel = qrels.get(q["id"], {})
        for system, by_q in runs.items():
            ranked = by_q.get(q["id"], [])
            per[system][q["id"]] = {
                f"recall@{k_recall}": recall_at(ranked, rel, k_recall),
                "recall@10": recall_at(ranked, rel, 10),
                "mrr@10": mrr_at(ranked, rel, 10),
                "ndcg@10": ndcg_at(ranked, rel, 10),
                "p@5": precision_at(ranked, rel, 5),
            }

    def depth_of(q) -> str:
        rel = [d for d, g in qrels.get(q["id"], {}).items() if g > 0]
        if not rel:
            return "none"
        n_full = sum(1 for d in rel if d in full_text_ids)
        return "full_text" if n_full * 2 >= len(rel) else "metadata_only"

    groups: dict[str, list[dict]] = {"all": [q for q in queries if q["type"] != "unanswerable"]}
    for t in QUERY_TYPES[:3]:
        groups[t] = [q for q in groups["all"] if q["type"] == t]
    for d in ("full_text", "metadata_only"):
        groups[d] = [q for q in groups["all"] if depth_of(q) == d]

    table: dict[str, dict] = {}
    for g, qs in groups.items():
        if not qs:
            continue
        table[g] = {"n": len(qs), "systems": {}}
        for system in runs:
            metrics = {}
            for m in per[system][qs[0]["id"]]:
                vals = [per[system][q["id"]][m] for q in qs]
                mean, lo, hi = bootstrap_ci(vals)
                metrics[m] = {"mean": round(mean, 4), "ci": [round(lo, 4), round(hi, 4)]}
            table[g]["systems"][system] = metrics
        if "hybrid" in runs and "bm25" in runs:
            a = [per["hybrid"][q["id"]][f"recall@{k_recall}"] for q in qs]
            b = [per["bm25"][q["id"]][f"recall@{k_recall}"] for q in qs]
            d, lo, hi = paired_diff_ci(a, b)
            table[g]["hybrid_minus_bm25_recall"] = {"mean": round(d, 4),
                                                    "ci": [round(lo, 4), round(hi, 4)]}
    return {"per_query": per, "table": table}


def save_json(path: Path, obj) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
