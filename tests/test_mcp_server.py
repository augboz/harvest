"""MCP server: tools answer from a scratch profile tree, in-memory transport.

Network/spend tools (health_check, run_pipeline) are wired but not exercised
here — they are the pipeline's own code paths, already covered elsewhere; these
tests prove the MCP surface reads state truthfully and rejects bad input.
"""
import asyncio
import json

import pytest

fastmcp = pytest.importorskip("fastmcp")

from jobbank import mcp_server            # noqa: E402
from jobbank.models import Job            # noqa: E402
from jobbank.state import State           # noqa: E402

CONFIG = """
profile: aug
weekly_cap: 10
min_score: 60
priority_score: 80
freshness_days: 45
scorer:
  model: claude-haiku-4-5-20251001
"""
WATCHLIST = """
companies:
  - company: McLaren
    ats: greenhouse
    slug: mclaren
"""


@pytest.fixture
def scratch_root(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "config" / "watchlist.yaml").write_text(WATCHLIST, encoding="utf-8")
    st = State(tmp_path / "data" / "state.db")
    st.record(Job(source="test", company="Acme Robotics", title="Software Intern 2027",
                  url="https://acme.example/jobs/123", score=71), "bank", "scored 71")
    st.record(Job(source="test", company="HedgeCo", title="Quant Trading Intern",
                  url="https://hedge.example/jobs/9"), "filtered", "quant/trading")
    st.record_run("20260901-0700-abc123", "2026-09-01T07:00:00+00:00",
                  "2026-09-01T07:04:00+00:00", json.dumps({"fetched": 40, "banked": 1}))
    st.close()
    monkeypatch.setattr(mcp_server, "ROOT", tmp_path)
    # The repo .env (loaded via os.environ by any earlier Settings) must not
    # drag these tests onto the live sheet.
    monkeypatch.delenv("SHEET_ID", raising=False)
    return tmp_path


def call(tool: str, **args):
    async def go():
        async with fastmcp.Client(mcp_server.mcp) as c:
            return (await c.call_tool(tool, args)).data
    return asyncio.run(go())


def test_status_reads_state(scratch_root):
    out = call("status")
    assert out["banked_last_7_days"] == 1
    assert out["totals_by_disposition"] == {"bank": 1, "filtered": 1}
    assert out["weekly_cap"] == 10
    assert out["last_run"]["run_id"] == "20260901-0700-abc123"
    assert out["last_run"]["stats"]["banked"] == 1
    assert {"reason": "quant/trading", "jobs": 1} in out["top_discard_reasons_14d"]


def test_search_finds_filtered_with_reason(scratch_root):
    out = call("search_jobs", query="hedge")
    assert out["matches"] == 1
    job = out["jobs"][0]
    assert job["disposition"] == "filtered"
    assert job["detail"] == "quant/trading"
    assert call("search_jobs", query="intern", disposition="bank")["matches"] == 1


def test_search_rejects_bad_disposition(scratch_root):
    with pytest.raises(Exception, match="disposition"):
        call("search_jobs", query="x", disposition="banked")


def test_run_history(scratch_root):
    runs = call("run_history")["runs"]
    assert len(runs) == 1 and runs[0]["stats"] == {"fetched": 40, "banked": 1}


def test_doctor_booleans_only(scratch_root):
    out = call("doctor")
    assert set(out["configured"]) >= {"adzuna", "anthropic_llm_scoring", "google_sheet"}
    assert all(isinstance(v, bool) for v in out["configured"].values())
    assert out["config"]["weekly_cap"] == 10


def test_watchlist_seed(scratch_root):
    out = call("watchlist")
    assert out["companies"][0]["company"] == "McLaren"


def test_job_bank_falls_back_to_local(scratch_root):
    out = call("job_bank")
    assert "local" in out["backend"]
    assert out["rows"] == []


def test_unknown_profile_names_available(scratch_root):
    with pytest.raises(Exception, match="unknown profile"):
        call("status", profile="nobody")
