"""The evaluation's metrics, checked against hand-computed values.

A retrieval number on a CV is only as good as the arithmetic behind it, so
every metric is pinned here on cases small enough to work out by hand.
"""
from __future__ import annotations

import pytest

from jobbank.rag import evaluate as ev

REL = {"a": 2, "c": 1}          # two relevant records, graded


def test_recall():
    assert ev.recall_at(["a", "b", "c"], REL, 1) == 0.5
    assert ev.recall_at(["a", "b", "c"], REL, 3) == 1.0
    assert ev.recall_at(["x", "y"], REL, 5) == 0.0
    assert ev.recall_at(["a"], {}, 5) == 0.0          # nothing relevant: 0, not a crash


def test_precision():
    assert ev.precision_at(["a", "b", "c", "d", "e"], REL, 5) == pytest.approx(0.4)


def test_mrr():
    assert ev.mrr_at(["b", "c", "a"], REL, 10) == pytest.approx(1 / 2)
    assert ev.mrr_at(["b", "d"], REL, 10) == 0.0
    assert ev.mrr_at(["b", "d", "a"], REL, 2) == 0.0  # found, but past k


def test_ndcg_by_hand():
    # ranked [c, a]: DCG = (2^1-1)/log2(2) + (2^2-1)/log2(3) = 1 + 3/1.58496
    # ideal  [a, c]: IDCG = 3/1 + 1/log2(3)
    import math
    dcg = 1 + 3 / math.log2(3)
    idcg = 3 + 1 / math.log2(3)
    assert ev.ndcg_at(["c", "a"], REL, 10) == pytest.approx(dcg / idcg)
    assert ev.ndcg_at(["a", "c"], REL, 10) == pytest.approx(1.0)


def test_bootstrap_is_seeded_and_brackets_the_mean():
    vals = [0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.0]
    m1 = ev.bootstrap_ci(vals)
    assert m1 == ev.bootstrap_ci(vals)                # same seed, same answer
    mean, lo, hi = m1
    assert mean == pytest.approx(5 / 8) and lo <= mean <= hi


def test_paired_difference_keeps_the_pairing():
    a, b = [1.0, 1.0, 0.0, 1.0], [0.0, 1.0, 0.0, 0.0]
    mean, lo, hi = ev.paired_diff_ci(a, b)
    assert mean == pytest.approx(0.5) and lo <= mean <= hi
    with pytest.raises(ValueError):
        ev.paired_diff_ci([1.0], [1.0, 0.0])


def test_pool_is_the_union_shuffled_deterministically():
    runs = {"bm25": {"q1": ["a", "b"]}, "vector": {"q1": ["b", "c"]}}
    p = ev.pool(runs, "q1")
    assert sorted(p) == ["a", "b", "c"]
    assert p == ev.pool(runs, "q1")                   # reproducible


def test_the_labeller_is_blind_and_resumable():
    items = [{"id": "a", "company": "X", "title": "T1"},
             {"id": "b", "company": "Y", "title": "T2"},
             {"id": "c", "company": "Z", "title": "T3"}]
    shown = []
    answers = iter(["2", "maybe", "0", "q"])         # an invalid answer is re-asked
    judged = ev.label_pool(items, {}, ask=lambda _p: next(answers), show=shown.append)
    assert judged == {"a": 2, "b": 0}
    assert not any("bm25" in s or "vector" in s for s in shown)   # no system names
    answers2 = iter(["1"])
    judged = ev.label_pool(items, judged, ask=lambda _p: next(answers2), show=lambda s: None)
    assert judged == {"a": 2, "b": 0, "c": 1}        # resumed at c


def test_query_file_validation(tmp_path):
    p = tmp_path / "q.yaml"
    p.write_text("queries:\n  - {id: k1, type: known_lexical, text: robotics intern}\n")
    with pytest.raises(ValueError, match="target"):
        ev.load_queries(p)
    p.write_text("queries:\n  - {id: t1, type: vibes, text: x}\n")
    with pytest.raises(ValueError, match="type"):
        ev.load_queries(p)


def test_score_reports_splits_and_the_headline_difference():
    queries = [{"id": "k1", "type": "known_lexical"},
               {"id": "t1", "type": "topical"},
               {"id": "u1", "type": "unanswerable"}]
    qrels = {"k1": {"a": 2}, "t1": {"b": 2, "c": 1}}
    runs = {"bm25": {"k1": ["a"], "t1": ["x"]},
            "hybrid": {"k1": ["a"], "t1": ["b", "c"]}}
    out = ev.score(queries, runs, qrels, full_text_ids={"b", "c"})
    t = out["table"]
    assert t["all"]["n"] == 2                          # unanswerable is not scored here
    assert t["all"]["systems"]["hybrid"]["recall@5"]["mean"] == 1.0
    assert t["all"]["systems"]["bm25"]["recall@5"]["mean"] == 0.5
    assert t["full_text"]["n"] == 1 and t["metadata_only"]["n"] == 1
    assert t["all"]["hybrid_minus_bm25_recall"]["mean"] == 0.5


def test_known_item_relevance_includes_near_duplicates():
    pytest.importorskip("rapidfuzz")
    records = {"t": {"company": "Arm", "title": "Hardware Engineering Intern 2027"},
               "d": {"company": "ARM", "title": "Hardware Engineering Intern (2027)"},
               "o": {"company": "Arm", "title": "Marketing Intern"}}
    assert ev.known_item_qrels("t", records) == {"t": 2, "d": 2}
