"""MCP: the semantic_search tool, record ids, and read-only mode.

Same in-memory transport as test_mcp_server.py. The index is built with the
offline HashEmbedder, so nothing here touches the network.
"""
from __future__ import annotations

import asyncio
import importlib
import json

import pytest

fastmcp = pytest.importorskip("fastmcp")
pytest.importorskip("lancedb")

from jobbank import mcp_server                       # noqa: E402
from jobbank.models import Job                       # noqa: E402
from jobbank.rag import corpus                       # noqa: E402
from jobbank.rag.embed import HashEmbedder           # noqa: E402
from jobbank.rag.index import build_index, index_path  # noqa: E402
from jobbank.state import State                      # noqa: E402

CONFIG = "profile: default\nweekly_cap: 10\nmin_score: 60\npriority_score: 80\n" \
         "freshness_days: 45\nscorer:\n  model: claude-haiku-4-5-20251001\n"
WATCHLIST = "companies: []\n"


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "config" / "watchlist.yaml").write_text(WATCHLIST, encoding="utf-8")
    st = State(tmp_path / "data" / "state.db")
    st.record(Job(source="t", company="Northwind Robotics", title="Perception Software Intern",
                  url="https://northwind.example/jobs/1", score=88), "bank", "robotics fit")
    st.record(Job(source="t", company="Contoso", title="Frontend Web Intern",
                  url="https://contoso.example/jobs/2", score=55), "archive", "web, weak fit")
    st.record(Job(source="t", company="HedgeCo", title="Quant Trading Intern",
                  url="https://hedge.example/jobs/9"), "filtered", "quant/trading")
    st.close()
    monkeypatch.setattr(mcp_server, "ROOT", tmp_path)
    for k in ("SHEET_ID", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _build(root):
    recs = corpus.load_records(root / "data" / "state.db")
    build_index(recs, HashEmbedder(dims=64), index_path(root), progress=lambda m: None)


def call(tool, **args):
    async def go():
        async with fastmcp.Client(mcp_server.mcp) as c:
            return (await c.call_tool(tool, args)).data
    return asyncio.run(go())


def tool_names():
    async def go():
        async with fastmcp.Client(mcp_server.mcp) as c:
            return {t.name for t in await c.list_tools()}
    return asyncio.run(go())


def test_semantic_search_finds_by_meaning_and_cites_ids(root):
    _build(root)
    out = call("semantic_search", query="robotics perception")
    top = out["hits"][0]
    assert top["company"] == "Northwind Robotics"
    assert len(top["id"]) == corpus.ID_LEN
    assert out["mode"] == "hybrid" and out["degraded"] == ""
    assert out["top_cosine"] is not None


def test_both_search_tools_report_the_same_id_for_a_record(root):
    _build(root)
    sem = call("semantic_search", query="robotics perception")["hits"][0]["id"]
    sub = call("search_jobs", query="Northwind")["jobs"][0]["id"]
    assert sem == sub


def test_disposition_prefilter(root):
    _build(root)
    hits = call("semantic_search", query="intern", disposition="filtered")["hits"]
    assert hits and all(h["disposition"] == "filtered" for h in hits)


def test_no_index_is_a_clear_answer_not_a_crash(root):
    out = call("semantic_search", query="anything")
    assert out["hits"] == [] and "rag build" in out["error"]


def test_without_embedding_credentials_it_degrades_to_keywords_and_says_so(root):
    _build(root)
    meta = index_path(root) / "meta.json"
    m = json.loads(meta.read_text())
    m["embedder"] = "gemini-embedding-2@768"            # built with Gemini, no GCP here
    meta.write_text(json.dumps(m))
    out = call("semantic_search", query="robotics")
    assert out["mode"] == "bm25"
    assert "keyword-only" in out["degraded"]
    assert out["hits"][0]["company"] == "Northwind Robotics"


def test_bad_arguments_are_refused(root):
    _build(root)
    with pytest.raises(Exception):
        call("semantic_search", query="x", disposition="banked")
    with pytest.raises(Exception):
        call("semantic_search", query="x", mode="magic")


@pytest.fixture
def readonly(monkeypatch):
    """Re-import the server with the flag set, and put it back afterwards so
    the rest of the suite sees the normal tool set."""
    monkeypatch.setenv("JOBBANK_MCP_READONLY", "1")
    importlib.reload(mcp_server)
    yield
    monkeypatch.delenv("JOBBANK_MCP_READONLY")
    importlib.reload(mcp_server)


def test_readonly_mode_does_not_register_the_spending_tools(readonly, root):
    names = tool_names()
    assert "run_pipeline" not in names and "health_check" not in names
    assert {"semantic_search", "search_jobs", "status", "run_history"} <= names
    # not merely hidden from the list: unreachable by name
    with pytest.raises(Exception):
        call("run_pipeline")


def test_normal_mode_still_has_every_tool():
    names = tool_names()
    assert {"run_pipeline", "health_check", "semantic_search"} <= names
