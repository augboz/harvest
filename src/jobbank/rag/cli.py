"""python -m jobbank rag {snapshot,build,search,stats,eval} [--root PROFILE_DIR]

  snapshot   fetch-only pass; saves full posting text (no scoring, no writes)
  build      embed every state.db record (+ snapshot text) into the index
  search     query the index: hybrid (default), bm25 or vector
  stats      how the current index was built
  eval       targets | pool | label | score  (see rag/evaluate.py; data in
             private/eval/, written by YOU before any system is run)

`build --fake` uses the offline HashEmbedder: free, instant, and good for
checking the mechanics and the BM25 leg, but its vector scores mean nothing
semantically. Without --fake it needs Google Cloud credentials (ADC, from
`gcloud auth application-default login`) plus GOOGLE_CLOUD_PROJECT and
GOOGLE_CLOUD_LOCATION (default "global": gemini-embedding-2 and the 3.x
text models are served only from the global endpoint, not from regions).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _settings(root: str | None):
    from ..settings import Settings
    return Settings(root=Path(root) if root else None)


def _gcp() -> tuple[str, str]:
    return (os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
            os.environ.get("GOOGLE_CLOUD_LOCATION", "global"))


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="jobbank rag")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("snapshot", "build", "search", "stats", "eval"):
        sp = sub.add_parser(name)
        sp.add_argument("--root", help="profile directory (default: repo root)")
        if name == "eval":
            sp.add_argument("step", choices=("targets", "pool", "label", "score"))
            sp.add_argument("--dir", default="private/eval",
                            help="where queries.yaml and results live")
            sp.add_argument("-n", type=int, default=10, help="targets to sample")
        if name == "snapshot":
            sp.add_argument("--source", action="append",
                            help="only these fetchers (repeatable)")
        if name == "build":
            sp.add_argument("--fake", action="store_true",
                            help="offline HashEmbedder instead of Gemini")
            sp.add_argument("--dims", type=int, default=768)
            sp.add_argument("--workers", type=int, default=4,
                            help="concurrent embedding requests (Gemini)")
            sp.add_argument("--limit", type=int, help="only the N newest records")
        if name == "search":
            sp.add_argument("query")
            sp.add_argument("--mode", default="hybrid", choices=("hybrid", "bm25", "vector"))
            sp.add_argument("-k", type=int, default=10)
            sp.add_argument("--disposition", choices=("bank", "archive", "filtered"))
            sp.add_argument("--json", action="store_true", help="raw JSON output")
    args = p.parse_args(argv)
    settings = _settings(args.root)

    from .index import index_path, read_meta
    path = index_path(settings.root)

    if args.cmd == "snapshot":
        from .snapshot import capture
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        out = capture(settings, path / "snapshots" / f"{stamp}.jsonl.gz", only=args.source)
        print(f"\nsnapshot: {out['with_text']} of {out['fetched']} fetched postings carry "
              f"text -> {out['path']}")
        for src, n in sorted(out["by_source"].items(), key=lambda x: -x[1]):
            print(f"   {n:6}  {src}")
        if out["failures"]:
            print(f"  failed sources: {', '.join(sorted(out['failures']))}")
        return 0

    if args.cmd == "build":
        from .corpus import coverage, load_records
        from .embed import GeminiEmbedder, HashEmbedder
        from .index import build_index
        from .snapshot import load_all
        full = load_all(path / "snapshots")
        records = load_records(settings.state_path, full_text=full, limit=args.limit)
        cov = coverage(records)
        print(f"corpus: {cov['records']} records, {cov['with_full_text']} with full text "
              f"({cov['full_text_pct']}%)")
        if args.fake:
            emb = HashEmbedder(dims=min(args.dims, 256))
        else:
            project, location = _gcp()
            emb = GeminiEmbedder(project=project, location=location,
                                 dims=args.dims, workers=args.workers)
        meta = build_index(records, emb, path)
        print(json.dumps({k: meta[k] for k in ("embedder", "records", "with_full_text",
                                               "full_text_pct", "built_at")}, indent=2))
        if "est_embed_cost_usd_this_build" in meta:
            print(f"embedding calls this build: {meta['embed_calls_this_build']}, "
                  f"est cost ${meta['est_embed_cost_usd_this_build']}")
        return 0

    if args.cmd == "stats":
        print(json.dumps(read_meta(path), indent=2))
        return 0

    if args.cmd == "eval":
        return _eval(args, settings, path)

    if args.cmd == "search":
        from .search import Searcher
        project, location = _gcp()
        out = Searcher(path, project=project, location=location).search(
            args.query, mode=args.mode, k=args.k, disposition=args.disposition)
        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0
        print(f"{out['mode']}  top cosine: {out.get('top_cosine')}")
        for i, h in enumerate(out["hits"], start=1):
            ranks = []
            if h.get("bm25_rank"):
                ranks.append(f"bm25 #{h['bm25_rank']}")
            if h.get("vector_rank"):
                ranks.append(f"vec #{h['vector_rank']} cos {h.get('cosine')}")
            print(f"{i:2}. [{h['id']}] {h['company']} | {h['title']}  "
                  f"({h['disposition']}{', full text' if h['has_full_text'] else ''})"
                  f"  {' · '.join(ranks)}")
        return 0
    return 1


def _eval(args, settings, path: Path) -> int:
    from . import evaluate as ev
    edir = Path(args.dir)
    qfile = edir / "queries.yaml"

    if args.step == "targets":
        # known-item targets, seeded and stratified; write a query for each
        # WITHOUT running any search first
        from .snapshot import load_all
        full = set(load_all(path / "snapshots"))
        for t in ev.sample_targets(settings.state_path, n=args.n, full_text_keys=full):
            print(f"- {{id: kNN, type: known_lexical, target: {t['target']}, text: \"\"}}"
                  f"   # {t['company']} | {t['title']} ({t['disposition']}"
                  f"{', full text' if t['full_text'] else ''})")
        return 0

    queries = ev.load_queries(qfile)
    sha = ev.file_sha256(qfile)
    runs_file, judg_file = edir / "runs.json", edir / "judgments.json"

    if args.step == "pool":
        from .search import Searcher
        project, location = _gcp()
        s = Searcher(path, project=project, location=location)
        runs = {sysname: {} for sysname in ev.SYSTEMS}
        for q in queries:
            for sysname in ev.SYSTEMS:
                runs[sysname][q["id"]] = ev.run_system(sysname, s, settings.state_path,
                                                       q["text"], ev.POOL_DEPTH)
        ev.save_json(runs_file, {"queries_sha256": sha, "index": s.meta, "runs": runs})
        need = sum(len(ev.pool(runs, q["id"])) for q in queries if q["type"] == "topical")
        print(f"ran {len(ev.SYSTEMS)} systems x {len(queries)} queries -> {runs_file}")
        print(f"{need} topical pool items to judge: python -m jobbank rag eval label")
        return 0

    saved = json.loads(runs_file.read_text(encoding="utf-8"))
    if saved["queries_sha256"] != sha:
        print("queries.yaml changed since the runs were pooled: re-run `eval pool`. "
              "(A changed query set is a new experiment, not a revision.)")
        return 1
    runs = saved["runs"]
    judged = json.loads(judg_file.read_text(encoding="utf-8")) if judg_file.exists() else {}

    from .search import Searcher
    s = Searcher(path)
    rows = {r["id"]: r for r in s.tbl.to_arrow().select(
        ["id", "company", "title", "content", "has_full_text"]).to_pylist()}

    if args.step == "label":
        for q in queries:
            if q["type"] != "topical":
                continue
            print(f"\n=== {q['id']}: {q['text']}")
            items = [{"id": d, "company": rows[d]["company"], "title": rows[d]["title"],
                      "snippet": rows[d]["content"]} for d in ev.pool(runs, q["id"])
                     if d in rows]
            done = judged.setdefault(q["id"], {})
            before = len(done)
            ev.label_pool(items, done)
            ev.save_json(judg_file, judged)
            if len(done) < len(items):
                print(f"stopped at {len(done)}/{len(items)}; run again to resume")
                return 0
            print(f"  {len(done) - before} judged")
        print("all pools judged: python -m jobbank rag eval score")
        return 0

    if args.step == "score":
        qrels: dict[str, dict[str, int]] = {}
        for q in queries:
            if q["type"].startswith("known"):
                qrels[q["id"]] = ev.known_item_qrels(q["target"], rows)
            elif q["type"] == "topical":
                qrels[q["id"]] = judged.get(q["id"], {})
        full_ids = {d for d, r in rows.items() if r["has_full_text"]}
        out = ev.score(queries, runs, qrels, full_ids)
        ev.save_json(edir / "results.json", {"queries_sha256": sha,
                                             "index_built_at": saved["index"].get("built_at"),
                                             "embedder": saved["index"].get("embedder"),
                                             **out})
        _print_table(out["table"])
        return 0
    return 1


def _print_table(table: dict) -> None:
    for group, g in table.items():
        print(f"\n{group}  (n={g['n']})")
        print(f"  {'system':8} {'recall@5':>18} {'mrr@10':>18} {'ndcg@10':>18}")
        for sysname, m in g["systems"].items():
            def cell(name):
                x = m[name]
                return f"{x['mean']:.3f} [{x['ci'][0]:.2f},{x['ci'][1]:.2f}]"
            print(f"  {sysname:8} {cell('recall@5'):>18} {cell('mrr@10'):>18} "
                  f"{cell('ndcg@10'):>18}")
        if "hybrid_minus_bm25_recall" in g:
            d = g["hybrid_minus_bm25_recall"]
            print(f"  hybrid - bm25 recall@5: {d['mean']:+.3f} "
                  f"[{d['ci'][0]:+.2f},{d['ci'][1]:+.2f}]")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
