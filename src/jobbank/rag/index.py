"""Build the LanceDB table: one row per record, a vector, and a BM25 index.

Layout under <profile root>/rag_index/ (gitignored, never committed):
  lance/             the LanceDB database (table "records")
  meta.json          how it was built: embedder, formats, counts, timestamp
  embed_cache.sqlite vectors by (embedder, exact text), so rebuilds are cheap

No ANN index, on purpose: at ~25k rows an exact scan over 768-d vectors takes
milliseconds, and exact search has no recall loss to explain.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

from .corpus import Record, coverage
from .embed import EmbeddingCache, Embedder, embed_documents

INDEX_DIRNAME = "rag_index"
TABLE = "records"
_LANCE = "lance"
_META = "meta.json"
_CACHE = "embed_cache.sqlite"


def index_path(root: Path) -> Path:
    return Path(root) / INDEX_DIRNAME


def read_meta(path: Path) -> dict:
    f = Path(path) / _META
    if not f.exists():
        raise FileNotFoundError(f"no index at {path} (run: python -m jobbank rag build)")
    return json.loads(f.read_text(encoding="utf-8"))


def build_index(records: Sequence[Record], embedder: Embedder, path: Path,
                progress: Callable[[str], None] = print) -> dict:
    import lancedb
    import pyarrow as pa
    from lancedb.index import FTS

    path = Path(path)
    ids = [r.id for r in records]
    if len(set(ids)) != len(ids):
        seen, dup = set(), None
        for i in ids:
            if i in seen:
                dup = i
                break
            seen.add(i)
        raise ValueError(f"record id collision on {dup!r}: lengthen corpus.ID_LEN")

    progress(f"  [index] embedding {len(records)} records with {embedder.name}")
    cache = EmbeddingCache(path / _CACHE)
    try:
        vecs = embed_documents(embedder, [(r.title, r.content()) for r in records],
                               cache=cache, progress=progress)
    finally:
        cache.close()

    schema = pa.schema([
        ("id", pa.string()), ("key", pa.string()),
        ("company", pa.string()), ("title", pa.string()),
        ("source", pa.string()), ("url", pa.string()),
        ("first_seen", pa.string()), ("disposition", pa.string()),
        ("score", pa.int32()), ("reason", pa.string()),
        ("location", pa.string()), ("has_full_text", pa.bool_()),
        ("content", pa.string()),
        ("vector", pa.list_(pa.float32(), embedder.dims)),
    ])
    table = pa.table({
        "id": ids, "key": [r.key for r in records],
        "company": [r.company for r in records], "title": [r.title for r in records],
        "source": [r.source for r in records], "url": [r.url for r in records],
        "first_seen": [r.first_seen for r in records],
        "disposition": [r.disposition for r in records],
        "score": [r.score for r in records], "reason": [r.reason for r in records],
        "location": [r.location for r in records],
        "has_full_text": [r.has_full_text for r in records],
        "content": [r.content() for r in records],
        "vector": vecs,
    }, schema=schema)

    db = lancedb.connect(str(path / _LANCE))
    tbl = db.create_table(TABLE, data=table, mode="overwrite")
    # BM25 over exactly the text that was embedded, so the two legs compete on
    # the same evidence. Defaults: lowercase, English stemming, stop words out.
    tbl.create_index("content", config=FTS())
    # "overwrite" keeps every earlier version on disk; drop them, since the
    # directory is baked into the deploy image and nothing reads old versions.
    tbl.optimize(cleanup_older_than=timedelta(0), delete_unverified=True)

    cov = coverage(records)
    meta = {
        "embedder": embedder.name, "dims": embedder.dims,
        "doc_format": getattr(embedder, "DOC_FORMAT", "plain"),
        "query_format": getattr(embedder, "QUERY_FORMAT", "plain"),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **cov,
    }
    if hasattr(embedder, "est_cost_usd"):
        meta["embed_calls_this_build"] = embedder.calls
        meta["est_embed_cost_usd_this_build"] = embedder.est_cost_usd()
    (path / _META).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    progress(f"  [index] {cov['records']} rows, {cov['with_full_text']} with full text "
             f"({cov['full_text_pct']}%) -> {path}")
    return meta
