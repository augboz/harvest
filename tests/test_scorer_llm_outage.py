"""A dead Anthropic API must not look like a successful LLM run.

On 2026-08-25 every scoring batch came back 400 "Your credit balance is too low",
on every profile, and the run still reported `scored 65 jobs via llm (est $0.0)`
and exited 0. Worse, `llm_scored` was set from `llm_candidate` — true for every
SHORTLISTED job whether or not the LLM ever ran — so unreviewed heuristic scores
became bank-eligible, which is exactly what score_jobs' docstring promises can
never happen. An HR role reached the bank that way.
"""
from __future__ import annotations

import anthropic
import httpx
import pytest

from jobbank.models import Job
from jobbank import scorer
from jobbank.volume import volume_gate


def _job(title, company="Some Co"):
    return Job(source="test", company=company, title=title,
               url=f"https://jobs.test/{title.replace(' ', '-')}",
               location="London, UK", description="A role.")


def _settings():
    class S:
        watchlist = []
        cfg = {"scorer": {"model": "claude-haiku-4-5-20251001", "batch_size": 2,
                          "llm_top_n": 50, "max_jobs_per_run": 400,
                          "description_chars": 400}}

        def env(self, key):
            return "sk-ant-test" if key == "ANTHROPIC_API_KEY" else ""
    return S()


def _billing_error():
    """The exact shape the SDK raises for a spent credit balance."""
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req, json={
        "type": "error",
        "error": {"type": "invalid_request_error",
                  "message": "Your credit balance is too low to access the "
                             "Anthropic API. Please go to Plans & Billing to "
                             "upgrade or purchase credits."}})
    return anthropic.BadRequestError(
        "Your credit balance is too low to access the Anthropic API.",
        response=resp, body=None)


@pytest.fixture
def dead_api(monkeypatch):
    calls = {"n": 0}

    class _Messages:
        def create(self, **kw):
            calls["n"] += 1
            raise _billing_error()

    class _Client:
        def __init__(self, **kw):
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    monkeypatch.setattr(scorer, "digest_for", lambda s: "", raising=False)
    return calls


def test_a_spent_balance_leaves_nothing_bank_eligible(dead_api, monkeypatch):
    monkeypatch.setattr("jobbank.feedback.digest_for", lambda s: "")
    jobs = [_job(f"Software Engineer Intern {i}") for i in range(4)]
    meta = scorer.score_jobs(jobs, _settings())

    assert meta["mode"] == "heuristic (LLM unavailable)", \
        "a run where the LLM never answered must not report mode 'llm'"
    assert meta["llm_scored"] == 0
    assert "credit balance" in meta["llm_error"]
    assert all(j.extra["llm_scored"] is False for j in jobs), \
        "unreviewed jobs must not be bank-eligible"

    # and the gate must actually act on that
    gate = volume_gate(jobs, slots=10, min_score=0, priority_score=90,
                       headroom=10)
    assert gate.bank == [], "nothing the LLM never saw may enter the bank"


def test_it_stops_asking_once_the_answer_is_known(dead_api, monkeypatch):
    """Billing/key/model 400s repeat identically — retrying every batch twice
    against a dead account wasted a minute and buried the real message."""
    monkeypatch.setattr("jobbank.feedback.digest_for", lambda s: "")
    scorer.score_jobs([_job(f"SWE Intern {i}") for i in range(10)], _settings())
    assert dead_api["n"] == 1, \
        f"expected to stop after the first 400, made {dead_api['n']} calls"


def test_the_sheet_says_why_not_the_wrong_why(dead_api, monkeypatch):
    """The cell used to read 'HEURISTIC score (no LLM key) (LLM batch failed)' —
    two contradictory reasons, neither of them the actual one."""
    monkeypatch.setattr("jobbank.feedback.digest_for", lambda s: "")
    jobs = [_job("Software Engineer Intern")]
    scorer.score_jobs(jobs, _settings())
    reason = jobs[0].score_reason
    assert "credit balance" in reason, reason
    assert "no LLM key" not in reason, "a key WAS present; don't blame the key"


def test_no_key_still_says_no_key():
    """The genuine no-key path must keep its own honest message."""
    class S:
        watchlist = []
        cfg = {"scorer": {"model": "m", "batch_size": 2, "llm_top_n": 50,
                          "max_jobs_per_run": 400, "description_chars": 400}}

        def env(self, key):
            return ""
    jobs = [_job("Software Engineer Intern")]
    meta = scorer.score_jobs(jobs, S())
    assert meta["mode"] == "heuristic"
    assert "(no LLM key)" in jobs[0].score_reason


# --------------------------------------------------------- repairing a sheet ---
def test_rows_scored_during_an_outage_are_treated_as_unreviewed():
    """complete_bank_rows re-scores rows whose reason is a placeholder. A row
    scored while the LLM was down is exactly as unreviewed as one promoted from
    the archive, so it has to qualify — otherwise the cheap keyword score stays
    on the sheet forever (Auto Trader sat at 79; the LLM scores it 28)."""
    from jobbank.sheet_format import PLACEHOLDER_REASONS, UNREVIEWED_MARK

    def is_placeholder(reason: str) -> bool:
        reason = reason.strip().lower()
        return (not reason
                or any(reason.startswith(p) for p in PLACEHOLDER_REASONS)
                or UNREVIEWED_MARK in reason)

    assert is_placeholder("HEURISTIC score (no LLM key): interest=26 timing=20")
    assert is_placeholder(
        "HEURISTIC score (LLM unavailable: Anthropic credit balance too low): x=1")
    # the urgent prefix is why this cannot be a startswith check
    assert is_placeholder("URGENT deadline. HEURISTIC score (no LLM key): interest=9")
    assert is_placeholder("promoted from the archive")
    assert is_placeholder("")
    # a real LLM verdict must be left alone
    assert not is_placeholder(
        "Top-tier AI/ML watchlist company, Paris location (EU work rights), "
        "summer 2027 internship fits penultimate year.")
