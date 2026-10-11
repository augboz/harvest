"""The retrieval layer, offline: corpus -> embeddings -> LanceDB -> search.

Everything here uses HashEmbedder (deterministic word-overlap vectors, no
network) and a scratch state.db written by the pipeline's own State class, so
the schema under test is the real one. Nothing in this file may be read as a
semantic-quality result: it proves the mechanics, the evaluation measures the
quality.
"""
from __future__ import annotations

import gzip
import json

import pytest

pytest.importorskip("lancedb")

from jobbank.models import Job  # noqa: E402
from jobbank.rag import corpus  # noqa: E402
from jobbank.rag.embed import (EmbeddingCache, GeminiEmbedder, HashEmbedder,  # noqa: E402
                               cosine, embed_documents, embedder_for)
from jobbank.rag.index import build_index, read_meta  # noqa: E402
from jobbank.rag.search import Searcher, _fts_query, _where  # noqa: E402
from jobbank.state import State  # noqa: E402

RECORDS = [
    # company, title, disposition, detail, score
    ("Northwind Robotics", "Perception Software Intern", "bank", "robotics, strong fit", 88),
    ("Contoso Silicon", "Machine Learning Infrastructure Intern", "archive",
     "ml infra, good fit", 70),
    ("Tailspin Toys", "Frontend Web Developer Intern", "archive", "web, weak fit", 52),
    ("Jane Street", "Software Engineering Intern", "filtered",
     "quant: known quant/HF/market-maker firm", None),
    ("Litware", "Graduate Data Engineer", "filtered",
     "type: no internship/placement signal", None),
    ("Fabrikam", "Embedded Firmware Placement", "bank", "embedded, strong fit", 81),
]


@pytest.fixture
def state_path(tmp_path):
    st = State(tmp_path / "state.db")
    for co, title, disp, detail, score in RECORDS:
        url = f"https://{co.split()[0].lower()}.example/jobs/{abs(hash(title)) % 9999}"
        st.record(Job(source="test", company=co, title=title, url=url, score=score),
                  disp, detail)
    st.close()
    return tmp_path / "state.db"


@pytest.fixture
def index(tmp_path, state_path):
    recs = corpus.load_records(state_path)
    path = tmp_path / "rag_index"
    meta = build_index(recs, HashEmbedder(dims=128), path, progress=lambda m: None)
    return path, meta, recs


# ---------------------------------------------------------------- corpus ---

def test_every_seen_record_becomes_one_record(state_path):
    recs = corpus.load_records(state_path)
    assert len(recs) == len(RECORDS)
    assert len({r.id for r in recs}) == len(recs)
    assert all(len(r.id) == corpus.ID_LEN for r in recs)


def test_corpus_never_writes_to_state(state_path):
    before = state_path.read_bytes()
    corpus.load_records(state_path)
    assert state_path.read_bytes() == before


def test_a_missing_state_db_is_an_error_not_an_empty_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        corpus.load_records(tmp_path / "nope.db")
    assert not (tmp_path / "nope.db").exists()


def test_score_reasons_are_content_but_filter_reasons_are_not(state_path):
    """12k records share one filter reason; embedding it would pull them all
    together and let keyword search match the pipeline's own bookkeeping."""
    by = {r.company: r for r in corpus.load_records(state_path)}
    assert "strong fit" in by["Northwind Robotics"].content()
    assert "quant" not in by["Jane Street"].content()
    assert by["Jane Street"].reason.startswith("quant")      # still kept as a field


def test_full_text_joins_on_the_dedupe_key(state_path):
    recs = corpus.load_records(state_path)
    target = next(r for r in recs if r.company == "Contoso Silicon")
    ft = {target.key: {"description": "Build GPU cluster schedulers in Rust.",
                       "location": "Cambridge, UK"}}
    joined = {r.company: r for r in corpus.load_records(state_path, full_text=ft)}
    assert joined["Contoso Silicon"].has_full_text
    assert "GPU cluster" in joined["Contoso Silicon"].content()
    assert "Cambridge" in joined["Contoso Silicon"].content()
    assert not joined["Tailspin Toys"].has_full_text
    cov = corpus.coverage(list(joined.values()))
    assert cov["with_full_text"] == 1 and cov["records"] == len(RECORDS)


def test_long_descriptions_are_capped(state_path):
    recs = corpus.load_records(state_path)
    r = recs[0]
    r.description = "x " * 5000
    assert len(r.content()) < corpus.DESC_CHARS + 200


# ------------------------------------------------------------- embedding ---

def test_hash_embedder_is_deterministic_and_normalised():
    e = HashEmbedder(dims=64)
    a, b = e.embed_texts(["robotics perception"])[0], e.embed_texts(["robotics perception"])[0]
    assert a == b
    assert abs(cosine(a, a) - 1.0) < 1e-5
    assert cosine(a, e.embed_texts(["robotics perception intern"])[0]) > \
        cosine(a, e.embed_texts(["frontend web developer"])[0])


def test_the_cache_makes_a_rebuild_free(tmp_path):
    calls = []

    class Counting(HashEmbedder):
        def embed_texts(self, texts):
            calls.append(len(texts))
            return super().embed_texts(texts)

    e, cache = Counting(dims=32), EmbeddingCache(tmp_path / "c.sqlite")
    docs = [("t1", "alpha beta"), ("t2", "gamma delta")]
    first = embed_documents(e, docs, cache=cache, progress=lambda m: None)
    second = embed_documents(e, docs, cache=cache, progress=lambda m: None)
    cache.close()
    assert first == second
    assert sum(calls) == 2            # the second pass embedded nothing


def test_an_interrupted_build_resumes(tmp_path):
    """Vectors are written after EACH chunk, so a crash at record 15,000 costs
    the chunk in flight, not the 15,000 before it."""
    class Dies(HashEmbedder):
        n = 0

        def embed_texts(self, texts):
            Dies.n += 1
            if Dies.n == 2:
                raise RuntimeError("quota")
            return super().embed_texts(texts)

    cache = EmbeddingCache(tmp_path / "c.sqlite")
    docs = [(f"t{i}", f"word{i}") for i in range(6)]
    with pytest.raises(RuntimeError):
        embed_documents(Dies(dims=16), docs, cache=cache, chunk=3, progress=lambda m: None)
    keys = [EmbeddingCache.key(Dies(dims=16).name, HashEmbedder(dims=16).format_doc(t, b))
            for t, b in docs]
    assert len(cache.get_many(keys)) == 3     # the first chunk survived
    cache.close()


class _FakeResp:
    def __init__(self, n):
        self.embeddings = [type("E", (), {"values": [1.0] + [0.0] * (n - 1)})()]


class _FakeModels:
    def __init__(self, dims, fail_first=0):
        self.dims, self.fail_first, self.seen = dims, fail_first, []

    def embed_content(self, model, contents, config):
        self.seen.append(contents)
        if self.fail_first:
            self.fail_first -= 1
            err = RuntimeError("rate limited")
            err.code = 429
            raise err
        return _FakeResp(self.dims)


def _gemini(models):
    pytest.importorskip("google.genai")
    client = type("C", (), {"models": models})()
    return GeminiEmbedder(project="p", location="global", dims=8, workers=1,
                          client=client, sleep=lambda s: None)


def test_gemini_sends_one_text_per_request():
    """gemini-embedding-2 AGGREGATES several inputs into one vector, so a batch
    must never be passed as a list."""
    m = _FakeModels(8)
    e = _gemini(m)
    out = e.embed_texts(["a", "b", "c"])
    assert len(out) == 3 and all(isinstance(s, str) for s in m.seen)
    assert e.calls == 3


def test_gemini_backs_off_on_429_and_carries_on():
    m = _FakeModels(8, fail_first=2)
    e = _gemini(m)
    assert len(e.embed_texts(["only"])[0]) == 8
    assert len(m.seen) == 3


def test_gemini_formats_carry_the_task_since_there_is_no_task_type():
    e = _gemini(_FakeModels(8))
    assert e.format_query("ml infra") == "task: search result | query: ml infra"
    assert e.format_doc("Perception Intern", "body") == \
        "title: Perception Intern | text: body"
    assert e.format_doc("", "body").startswith("title: none |")


def test_concurrent_workers_share_one_client(monkeypatch):
    """Found on the first live run: unlocked, each worker built its own Client
    and the discarded ones closed the connection under in-flight requests."""
    genai = pytest.importorskip("google.genai")
    import threading
    import time as _time
    made = []

    class SlowClient:
        def __init__(self, **kw):
            _time.sleep(0.05)              # widen the race window
            made.append(self)
            self.models = _FakeModels(8)

    monkeypatch.setattr(genai, "Client", SlowClient)
    e = GeminiEmbedder(project="p", location="global", dims=8, workers=8)
    barrier = threading.Barrier(8)
    orig = e.client

    def racing_client():
        try:
            barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return orig()

    monkeypatch.setattr(e, "client", racing_client)
    assert len(e.embed_texts([str(i) for i in range(16)])) == 16
    assert len(made) == 1


def test_gemini_refuses_to_start_without_a_project():
    with pytest.raises(ValueError):
        GeminiEmbedder(project="", location="")


# ----------------------------------------------------------------- index ---

def test_index_records_how_it_was_built(index):
    path, meta, recs = index
    assert read_meta(path)["embedder"] == "hash-v1@128"
    assert meta["records"] == len(RECORDS)
    assert set(meta["by_disposition"]) == {"bank", "archive", "filtered"}


def test_an_id_collision_is_refused(tmp_path, state_path):
    recs = corpus.load_records(state_path)
    recs[1].key = recs[0].key[:corpus.ID_LEN] + "different"
    with pytest.raises(ValueError, match="collision"):
        build_index(recs, HashEmbedder(dims=16), tmp_path / "x", progress=lambda m: None)


def test_a_query_embedder_that_differs_from_the_index_is_refused(index):
    path, _, _ = index
    with pytest.raises(ValueError, match="built with"):
        Searcher(path, embedder=HashEmbedder(dims=64))


def test_the_searcher_rebuilds_the_right_embedder_from_the_index(index):
    path, _, _ = index
    assert Searcher(path).embedder.name == "hash-v1@128"
    assert embedder_for("hash-v1@128").dims == 128


def test_keyword_search_needs_no_embedding_credentials(tmp_path, state_path):
    """An index built with Gemini, queried where no GCP project is configured:
    the BM25 leg must still answer, and only the vector leg may refuse."""
    recs = corpus.load_records(state_path)
    path = tmp_path / "gem_index"
    build_index(recs, HashEmbedder(dims=16), path, progress=lambda m: None)
    meta_file = path / "meta.json"
    meta = json.loads(meta_file.read_text())
    meta["embedder"] = "gemini-embedding-2@768"          # as if built with Gemini
    meta_file.write_text(json.dumps(meta))
    s = Searcher(path)                                   # no project, no location
    assert s.bm25("robotics")[0]["company"] == "Northwind Robotics"
    with pytest.raises(ValueError, match="project"):
        s.vector("robotics")


# ---------------------------------------------------------------- search ---

def test_bm25_finds_by_keyword(index):
    s = Searcher(index[0])
    hits = s.bm25("robotics perception")
    assert hits and hits[0]["company"] == "Northwind Robotics"
    assert hits[0]["bm25_rank"] == 1


def test_vector_reports_a_raw_cosine(index):
    s = Searcher(index[0])
    hits = s.vector("perception software robotics")
    assert hits[0]["company"] == "Northwind Robotics"
    assert 0.0 < hits[0]["cosine"] <= 1.0
    assert hits == sorted(hits, key=lambda h: h["cosine"], reverse=True)


def test_hybrid_fuses_both_legs_and_keeps_their_ranks(index):
    out = Searcher(index[0]).hybrid("robotics perception intern")
    top = out["hits"][0]
    assert top["company"] == "Northwind Robotics"
    assert top["bm25_rank"] == 1 and top["vector_rank"] == 1
    assert top["rrf"] == pytest.approx(2 / 61, abs=1e-6)
    assert out["top_cosine"] == top["cosine"]
    assert out["hits"] == sorted(out["hits"], key=lambda h: h["rrf"], reverse=True)


def test_a_record_found_by_one_leg_only_still_fuses(index):
    out = Searcher(index[0]).hybrid("firmware", k=10)
    fw = next(h for h in out["hits"] if h["company"] == "Fabrikam")
    assert fw["bm25_rank"] is not None
    assert fw["rrf"] >= 1 / (60 + fw["bm25_rank"])


def test_prefilters_narrow_both_legs(index):
    s = Searcher(index[0])
    for mode in ("bm25", "vector", "hybrid"):
        hits = s.search("intern", mode=mode, disposition="bank")["hits"]
        assert hits and all(h["disposition"] == "bank" for h in hits), mode


def test_hits_carry_ids_and_a_snippet_not_the_vector(index):
    hit = Searcher(index[0]).hybrid("robotics")["hits"][0]
    assert len(hit["id"]) == corpus.ID_LEN
    assert "vector" not in hit and "content" not in hit
    assert "Perception" in hit["snippet"]


def test_query_syntax_cannot_break_the_keyword_leg(index):
    s = Searcher(index[0])
    assert _fts_query('robotics: "intern" (2027) OR AND*') == "robotics intern 2027 OR AND"
    s.bm25('robotics: "intern" (2027)')          # must not raise
    assert s.bm25("!!!") == []


def test_prefilter_sql_comes_only_from_validated_values():
    assert _where("bank", None) == "disposition = 'bank'"
    assert _where(None, True) == "has_full_text = true"
    with pytest.raises(ValueError):
        _where("bank' OR '1'='1", None)


def test_unknown_mode_is_refused(index):
    with pytest.raises(ValueError):
        Searcher(index[0]).search("x", mode="magic")


# ------------------------------------------------------------- snapshot ---

def test_snapshot_round_trips(tmp_path):
    from jobbank.rag import snapshot
    path = tmp_path / "snap.jsonl.gz"
    rows = [{"key": "abc", "description": "Body text", "location": "London"}]
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    assert snapshot.load(path) == {"abc": {"description": "Body text", "location": "London"}}
