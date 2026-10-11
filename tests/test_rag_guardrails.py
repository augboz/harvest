"""The agent's guardrails, offline. These are the rules the README claims, so
each one is pinned by a test that would fail if the rule were quietly weakened.
"""
from __future__ import annotations

import pytest

from jobbank.rag import guardrails as g

A, B, C = "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"


# -------------------------------------------------------- least privilege ---

@pytest.mark.parametrize("tool", ["run_pipeline", "health_check", "job_bank",
                                  "watchlist", "doctor", "anything_else"])
def test_only_read_tools_are_allowed(tool):
    ok, _, why = g.check_tool_call(tool, {})
    assert not ok and tool in why


def test_profile_is_pinned_whatever_the_model_asks_for():
    ok, args, _ = g.check_tool_call("semantic_search", {"query": "x", "profile": "other"})
    assert ok and args["profile"] == "default"


def test_result_sizes_are_capped():
    _, args, _ = g.check_tool_call("semantic_search", {"query": "x", "k": 5000})
    assert args["k"] == g.MAX_K
    _, args, _ = g.check_tool_call("search_jobs", {"query": "x", "limit": "lots"})
    assert args["limit"] == 10


# ---------------------------------------------------------- cite or strip ---

def test_ids_are_collected_from_both_search_tools():
    ids, top = g.ids_from_tool_response(
        "semantic_search", {"hits": [{"id": A}, {"id": B}], "top_cosine": 0.71})
    assert ids == {A, B} and top == pytest.approx(0.71)
    ids, top = g.ids_from_tool_response("search_jobs", {"jobs": [{"id": C}]})
    assert ids == {C} and top is None


def test_a_wrapped_mcp_result_is_unwrapped():
    ids, _ = g.ids_from_tool_response("semantic_search",
                                      {"structuredContent": {"hits": [{"id": A}]}})
    assert ids == {A}


def test_fabricated_citations_are_stripped_and_counted():
    cc = g.check_citations(f"Two fit: [{A}] and [{C}].", known_ids={A, B})
    assert cc.valid == [A] and cc.invalid == [C]
    assert f"[{C}]" not in cc.text and f"[{A}]" in cc.text


def test_citations_are_counted_once_each():
    cc = g.check_citations(f"[{A}] then again [{A}]", known_ids={A})
    assert cc.valid == [A]


# -------------------------------------------------------------- finalise ---

def test_a_grounded_answer_passes_untouched():
    v = g.finalise(f"Best match: [{A}].", {A}, top_cosines=[0.8], threshold=0.5)
    assert not v.refused and not v.downgraded and v.text == f"Best match: [{A}]."


def test_weak_retrieval_and_nothing_cited_is_a_refusal():
    v = g.finalise("Probably the robotics one.", {A}, top_cosines=[0.31], threshold=0.5)
    assert v.refused and v.text == g.REFUSAL


def test_weak_retrieval_but_a_verified_citation_is_not_refused():
    """A low cosine with a real citation means the record was found some other
    way (e.g. an exact search_jobs hit); refusing would throw away a true answer."""
    v = g.finalise(f"It's [{A}].", {A}, top_cosines=[0.31], threshold=0.5)
    assert not v.refused


def test_an_answer_that_cites_only_fabrications_is_refused_when_retrieval_was_weak():
    v = g.finalise(f"It's [{C}].", {A}, top_cosines=[0.2], threshold=0.5)
    assert v.refused and v.citations.invalid == [C]


def test_records_returned_but_none_cited_is_flagged_not_refused():
    v = g.finalise("There were several.", {A, B}, top_cosines=[0.9], threshold=0.5)
    assert v.downgraded and not v.refused and v.text.endswith(g.UNCITED_NOTE)


def test_no_retrieval_at_all_is_left_alone():
    """A status question uses no search tool, so there is nothing to cite."""
    v = g.finalise("The last run banked 3 records.", set(), top_cosines=[], threshold=0.5)
    assert not v.refused and not v.downgraded


def test_an_uncalibrated_threshold_never_refuses_on_confidence_alone():
    v = g.finalise("A guess.", {A}, top_cosines=[0.01], threshold=0.0)
    assert not v.refused


# ------------------------------------------------------------ calibration ---

def test_calibration_separates_the_two_groups_and_reports_error_rates():
    out = g.calibrate_threshold(answerable=[0.72, 0.68, 0.81, 0.66],
                                unanswerable=[0.41, 0.52, 0.47])
    assert 0.52 < out["threshold"] <= 0.66
    assert out["false_refusal_rate"] == 0 and out["false_answer_rate"] == 0


def test_calibration_with_overlap_reports_the_cost():
    out = g.calibrate_threshold(answerable=[0.6, 0.5, 0.7], unanswerable=[0.55, 0.3])
    assert out["false_refusal_rate"] + out["false_answer_rate"] > 0


def test_calibration_needs_both_groups():
    with pytest.raises(ValueError):
        g.calibrate_threshold([0.5], [])
