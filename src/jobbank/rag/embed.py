"""Embedders, and the cache that makes a 23.7k-record build resumable.

Two implementations behind one small protocol:

- HashEmbedder: deterministic feature hashing of words and word pairs. No
  network, no model, no randomness (hashlib, never Python's salted hash()), so
  tests and offline builds give identical vectors every time. It only knows
  about shared words, which is exactly why it is useful as a stand-in: anything
  that works with it works for the mechanics, and nothing about its scores may
  be reported as a semantic result.
- GeminiEmbedder: gemini-embedding-2 on Google Cloud's Agent Platform (formerly
  Vertex AI), through the google-genai SDK with vertexai=True, so it is billed
  to the GCP project (free-trial credits) rather than the AI Studio API.

Two facts about gemini-embedding-2 shape GeminiEmbedder, both from the docs
(ai.google.dev/gemini-api/docs/embeddings, checked 2026-10-06):
  - it has NO task_type parameter; the task goes in the text itself, as
    "task: search result | query: ..." for queries and
    "title: ... | text: ..." for documents;
  - several inputs passed to one embed_content call produce ONE aggregated
    embedding, not one each. So this sends one text per request, a few at a
    time, and backs off when rate limited.
"""
from __future__ import annotations

import hashlib
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Protocol, Sequence

import numpy as np

_WORD = re.compile(r"[a-z0-9]+")
# Prices for the cost line in build stats, dated: re-check before quoting one.
# gemini-embedding-2 text, $ per 1M tokens (ai.google.dev/gemini-api/docs/pricing,
# 2026-10-05). ~4 characters per token is the usual rough conversion.
EMBED_PRICE_PER_MTOK = 0.20
CHARS_PER_TOKEN = 4


class Embedder(Protocol):
    name: str            # identifies model AND dims; stored in the index
    dims: int

    def format_doc(self, title: str, body: str) -> str: ...
    def format_query(self, query: str) -> str: ...
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


def _normalise(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class HashEmbedder:
    """Offline, deterministic, word-overlap vectors. For tests and dry builds."""

    def __init__(self, dims: int = 256):
        self.dims = dims
        self.name = f"hash-v1@{dims}"

    def format_doc(self, title: str, body: str) -> str:
        return f"{title}\n{body}"

    def format_query(self, query: str) -> str:
        return query

    def _vec(self, text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
        v = np.zeros(self.dims, dtype=np.float32)
        for f in feats:
            h = hashlib.sha1(f.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "little") % self.dims
            v[idx] += 1.0 if h[4] & 1 else -1.0
        return _normalise(v).astype(np.float32).tolist()

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class GeminiEmbedder:
    """gemini-embedding-2 on the Agent Platform. One text per request."""

    MODEL = "gemini-embedding-2"
    DOC_FORMAT = "title: {title} | text: {body}"
    QUERY_FORMAT = "task: search result | query: {query}"

    def __init__(self, project: str, location: str, dims: int = 768,
                 workers: int = 4, model: str = MODEL, client=None,
                 max_retries: int = 6, sleep: Callable[[float], None] = time.sleep):
        if not project or not location:
            raise ValueError("GeminiEmbedder needs a GCP project and location "
                             "(GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION)")
        self.project, self.location = project, location
        self.dims, self.model, self.workers = dims, model, workers
        self.name = f"{model}@{dims}"
        self._client = client
        self._client_lock = threading.Lock()
        self.max_retries = max_retries
        self._sleep = sleep
        self.calls = 0
        self.chars = 0

    def client(self):
        # Locked: without it the worker threads each build a Client at once, the
        # losers are garbage-collected, and genai.Client.__del__ closes the
        # connection under a request still in flight ("client has been closed").
        with self._client_lock:
            if self._client is None:
                from google import genai   # lazy: the rag extra is optional
                self._client = genai.Client(vertexai=True, project=self.project,
                                            location=self.location)
            return self._client

    def format_doc(self, title: str, body: str) -> str:
        return self.DOC_FORMAT.format(title=title.strip() or "none", body=body)

    def format_query(self, query: str) -> str:
        return self.QUERY_FORMAT.format(query=query)

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        try:
            code = int(code)
        except (TypeError, ValueError):
            return False
        return code == 429 or code >= 500

    def _embed_one(self, text: str) -> list[float]:
        from google.genai import types
        for attempt in range(self.max_retries):
            try:
                resp = self.client().models.embed_content(
                    model=self.model, contents=text,
                    config=types.EmbedContentConfig(output_dimensionality=self.dims))
                self.calls += 1
                self.chars += len(text)
                v = np.asarray(resp.embeddings[0].values, dtype=np.float32)
                # embedding-2 normalises truncated vectors already; doing it again
                # is free and keeps cosine == dot product whatever the model does
                return _normalise(v).tolist()
            except Exception as exc:  # noqa: BLE001 - classified below
                if not self._retryable(exc) or attempt == self.max_retries - 1:
                    raise
                # exponential backoff with jitter: 1, 2, 4, 8 ... s
                self._sleep((2 ** attempt) + random.random())
        raise RuntimeError("unreachable")

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if self.workers <= 1 or len(texts) <= 1:
            return [self._embed_one(t) for t in texts]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(self._embed_one, texts))

    def est_cost_usd(self) -> float:
        return round(self.chars / CHARS_PER_TOKEN / 1e6 * EMBED_PRICE_PER_MTOK, 4)


class EmbeddingCache:
    """Vectors on disk, keyed on (embedder name, exact formatted text).

    A build that dies at record 15,000 resumes at 15,001, and a rebuild after a
    template change re-embeds only what changed. It lives next to the index and
    is never committed.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path))
        self.con.execute("CREATE TABLE IF NOT EXISTS vecs "
                         "(h TEXT PRIMARY KEY, v BLOB NOT NULL)")
        self.con.commit()

    @staticmethod
    def key(embedder_name: str, text: str) -> str:
        return hashlib.sha256(f"{embedder_name}\x00{text}".encode("utf-8")).hexdigest()

    def get_many(self, keys: Sequence[str]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for i in range(0, len(keys), 500):
            chunk = list(keys[i:i + 500])
            q = ",".join("?" * len(chunk))
            for h, blob in self.con.execute(f"SELECT h, v FROM vecs WHERE h IN ({q})", chunk):
                out[h] = np.frombuffer(blob, dtype=np.float32).tolist()
        return out

    def put_many(self, items: dict[str, list[float]]) -> None:
        self.con.executemany(
            "INSERT OR REPLACE INTO vecs (h, v) VALUES (?, ?)",
            [(h, np.asarray(v, dtype=np.float32).tobytes()) for h, v in items.items()])
        self.con.commit()

    def close(self) -> None:
        self.con.close()


def embed_documents(embedder: Embedder, docs: Sequence[tuple[str, str]],
                    cache: EmbeddingCache | None = None, chunk: int = 64,
                    progress: Callable[[str], None] = print) -> list[list[float]]:
    """Vectors for (title, body) pairs, in order, through the cache.

    Uncached texts are embedded in chunks and written back after EACH chunk, so
    progress survives an interruption at any point.
    """
    texts = [embedder.format_doc(t, b) for t, b in docs]
    keys = [EmbeddingCache.key(embedder.name, t) for t in texts]
    have = cache.get_many(keys) if cache else {}
    missing = [i for i, k in enumerate(keys) if k not in have]
    if have:
        progress(f"  [embed] {len(have)} cached, {len(missing)} to embed")
    for start in range(0, len(missing), chunk):
        idx = missing[start:start + chunk]
        vecs = embedder.embed_texts([texts[i] for i in idx])
        fresh = {keys[i]: v for i, v in zip(idx, vecs)}
        have.update(fresh)
        if cache:
            cache.put_many(fresh)
        done = min(start + chunk, len(missing))
        if done == len(missing) or (start // chunk) % 20 == 0:
            progress(f"  [embed] {done}/{len(missing)}")
    return [have[k] for k in keys]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    return float(va @ vb / (na * nb)) if na and nb else 0.0


def embedder_for(name: str, project: str = "", location: str = "") -> Embedder:
    """The embedder an index was built with, rebuilt from its stored name. A
    query MUST be embedded the same way as the documents, so search never picks
    its own embedder."""
    model, _, dims = name.partition("@")
    d = int(dims) if dims.isdigit() else 0
    if model == "hash-v1":
        return HashEmbedder(dims=d or 256)
    if model.startswith("gemini-embedding"):
        return GeminiEmbedder(project=project, location=location, dims=d or 768,
                              model=model, workers=1)
    raise ValueError(f"unknown embedder {name!r}")
