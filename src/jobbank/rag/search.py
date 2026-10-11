"""BM25, vector and hybrid search over the index.

Hybrid is fused HERE with reciprocal rank fusion rather than by LanceDB's own
hybrid query, which returns only a fused `_relevance_score`. Doing it ourselves
keeps two things the rest of the system needs:

  - each hit's rank in BOTH legs, which the evaluation reports;
  - the raw cosine similarity of the vector leg's best hit. RRF scores are
    rank-based and uncalibrated (the top hit scores ~1/61 whatever the query),
    so a "nothing relevant here" refusal can only be decided on the cosine.

RRF: score(d) = sum over legs of 1 / (k + rank(d)), k = 60 (Cormack et al.,
2009), with each leg searched to `depth` before fusing.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .embed import Embedder, embedder_for
from .index import TABLE, _LANCE, read_meta

DISPOSITIONS = ("bank", "archive", "filtered")
RRF_K = 60
_FTS_WORD = re.compile(r"[A-Za-z0-9]+")
_RETURN = ("id", "company", "title", "source", "url", "first_seen", "disposition",
           "score", "location", "has_full_text", "reason")
SNIPPET_CHARS = 300


def _fts_query(query: str) -> str:
    """The FTS parser treats quotes, colons, brackets and the like as syntax; a
    user's natural-language query should only ever be words."""
    return " ".join(_FTS_WORD.findall(query))


def _where(disposition: str | None, full_text: bool | None) -> str | None:
    """Prefilter SQL from validated parameters only, never from caller text."""
    parts = []
    if disposition is not None:
        if disposition not in DISPOSITIONS:
            raise ValueError(f"disposition must be one of {DISPOSITIONS}")
        parts.append(f"disposition = '{disposition}'")
    if full_text is not None:
        parts.append(f"has_full_text = {'true' if full_text else 'false'}")
    return " AND ".join(parts) or None


def _hit(row: dict) -> dict[str, Any]:
    h = {k: row.get(k) for k in _RETURN}
    content = str(row.get("content") or "")
    h["snippet"] = content[:SNIPPET_CHARS] + ("…" if len(content) > SNIPPET_CHARS else "")
    return h


class Searcher:
    """The embedder is built LAZILY, on the first vector query: the BM25 leg
    needs no credentials, so keyword search keeps working (and a caller can
    degrade to it) when the embedding service is unavailable."""

    def __init__(self, path: Path, embedder: Embedder | None = None,
                 project: str = "", location: str = ""):
        import lancedb
        self.path = Path(path)
        self.meta = read_meta(self.path)
        self._project, self._location = project, location
        self._embedder = embedder
        if embedder is not None:
            self._check(embedder)
        self.tbl = lancedb.connect(str(self.path / _LANCE)).open_table(TABLE)

    def _check(self, embedder: Embedder) -> None:
        if embedder.name != self.meta["embedder"]:
            raise ValueError(f"index was built with {self.meta['embedder']}, "
                             f"not {embedder.name}: queries must be embedded "
                             f"the same way as the records")

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            e = embedder_for(self.meta["embedder"], self._project, self._location)
            self._check(e)
            self._embedder = e
        return self._embedder

    # ---- the two legs ------------------------------------------------------

    def bm25(self, query: str, k: int = 10, disposition: str | None = None,
             full_text: bool | None = None) -> list[dict]:
        q = _fts_query(query)
        if not q:
            return []
        s = self.tbl.search(q, query_type="fts")
        w = _where(disposition, full_text)
        if w:
            s = s.where(w, prefilter=True)
        rows = s.limit(k).to_list()
        out = []
        for rank, r in enumerate(rows, start=1):
            h = _hit(r)
            h["bm25_rank"], h["bm25_score"] = rank, float(r.get("_score") or 0.0)
            out.append(h)
        return out

    def vector(self, query: str, k: int = 10, disposition: str | None = None,
               full_text: bool | None = None) -> list[dict]:
        qv = self.embedder.embed_texts([self.embedder.format_query(query)])[0]
        s = self.tbl.search(qv, vector_column_name="vector").metric("cosine")
        w = _where(disposition, full_text)
        if w:
            s = s.where(w, prefilter=True)
        rows = s.limit(k).to_list()
        out = []
        for rank, r in enumerate(rows, start=1):
            h = _hit(r)
            # LanceDB's cosine metric reports DISTANCE = 1 - similarity
            h["vector_rank"], h["cosine"] = rank, round(1.0 - float(r["_distance"]), 4)
            out.append(h)
        return out

    # ---- fusion -----------------------------------------------------------

    def hybrid(self, query: str, k: int = 10, disposition: str | None = None,
               full_text: bool | None = None, depth: int = 50,
               rrf_k: int = RRF_K) -> dict[str, Any]:
        b = self.bm25(query, depth, disposition, full_text)
        v = self.vector(query, depth, disposition, full_text)
        fused: dict[str, dict] = {}
        for leg, rank_key in ((b, "bm25_rank"), (v, "vector_rank")):
            for h in leg:
                cur = fused.setdefault(h["id"], {**h, "bm25_rank": None,
                                                 "vector_rank": None, "cosine": None,
                                                 "rrf": 0.0})
                cur[rank_key] = h[rank_key]
                if "cosine" in h:
                    cur["cosine"] = h["cosine"]
                cur["rrf"] += 1.0 / (rrf_k + h[rank_key])
        hits = sorted(fused.values(), key=lambda h: h["rrf"], reverse=True)[:k]
        for h in hits:
            h["rrf"] = round(h["rrf"], 6)
            h.pop("bm25_score", None)
        return {"hits": hits,
                # the guardrail's input: how similar the CLOSEST record is at all
                "top_cosine": v[0]["cosine"] if v else None,
                "legs": {"bm25": len(b), "vector": len(v)}}

    def search(self, query: str, mode: str = "hybrid", k: int = 10,
               disposition: str | None = None,
               full_text: bool | None = None) -> dict[str, Any]:
        if mode == "hybrid":
            return {"mode": mode, **self.hybrid(query, k, disposition, full_text)}
        if mode == "bm25":
            return {"mode": mode, "hits": self.bm25(query, k, disposition, full_text),
                    "top_cosine": None}
        if mode == "vector":
            hits = self.vector(query, k, disposition, full_text)
            return {"mode": mode, "hits": hits,
                    "top_cosine": hits[0]["cosine"] if hits else None}
        raise ValueError("mode must be hybrid, bm25 or vector")
