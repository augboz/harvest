"""The ADK agent's wiring and callbacks, offline.

The callbacks are driven directly with a fake context (a dict-backed `state`)
and real ADK/GenAI response objects, so this checks the adapters the guardrail
tests cannot: that a denied tool really becomes an error result, that safe
arguments really replace the model's, that a fabricated citation really leaves
the final text, and that every answer emits one telemetry line. No model is
called and no MCP child is started.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("google.adk")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.genai import types  # noqa: E402

from jobbank_agent import agent as a  # noqa: E402

A, B, X = "aaaaaaaaaaaa", "bbbbbbbbbbbb", "ffffffffffff"


class Ctx:
    def __init__(self):
        self.state = {}


class Tool:
    def __init__(self, name):
        self.name = name


def _resp(text=None, call=None, usage=(100, 20, 30)):
    parts = []
    if text is not None:
        parts.append(types.Part(text=text))
    if call is not None:
        parts.append(types.Part(function_call=types.FunctionCall(name=call, args={})))
    return LlmResponse(
        content=types.Content(role="model", parts=parts),
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=usage[0], candidates_token_count=usage[1],
            thoughts_token_count=usage[2]))


# ------------------------------------------------------------------ wiring ---

def test_the_mcp_child_runs_read_only_without_pipeline_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("SHEET_ID", "sheet-secret")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    env = a.child_env()
    assert env["JOBBANK_MCP_READONLY"] == "1"
    assert env["GOOGLE_CLOUD_PROJECT"] == "proj"
    assert "ANTHROPIC_API_KEY" not in env and "SHEET_ID" not in env


def test_the_agent_only_sees_read_tools():
    assert a.toolset.tool_filter == list(a.g.READ_TOOLS)
    assert a.root_agent.before_tool_callback is a.before_tool
    assert a.root_agent.after_model_callback is a.after_model


# ------------------------------------------------------------------- tools ---

def test_a_denied_tool_becomes_an_error_result_not_a_call():
    ctx = Ctx()
    out = a.before_tool(Tool("run_pipeline"), {}, ctx)
    assert "not available" in out["error"]


def test_safe_arguments_replace_the_models():
    ctx, args = Ctx(), {"query": "x", "k": 999, "profile": "other"}
    assert a.before_tool(Tool("semantic_search"), args, ctx) is None
    assert args["profile"] == "default" and args["k"] == a.g.MAX_K


def test_tool_results_feed_the_citation_check():
    ctx = Ctx()
    a.after_tool(Tool("semantic_search"), {}, ctx,
                 {"hits": [{"id": A}, {"id": B}], "top_cosine": 0.66})
    assert set(ctx.state["temp:ids"]) == {A, B}
    assert ctx.state["temp:cos"] == [0.66]


# ------------------------------------------------------------------ answers --

def test_an_intermediate_tool_calling_step_is_left_alone(capsys):
    ctx = Ctx()
    assert a.after_model(ctx, _resp(call="semantic_search")) is None
    assert capsys.readouterr().out == ""                 # no telemetry yet
    assert ctx.state["temp:usage"][0] == 1               # but its tokens counted


def test_a_fabricated_citation_is_stripped_and_logged(capsys):
    ctx = Ctx()
    a.after_tool(Tool("semantic_search"), {}, ctx, {"hits": [{"id": A}], "top_cosine": 0.8})
    out = a.after_model(ctx, _resp(text=f"Two matches: [{A}] and [{X}]."))
    assert out is not None
    final = out.content.parts[0].text
    assert f"[{A}]" in final and f"[{X}]" not in final
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["message"] == "agent_turn"
    assert line["cited_valid"] == [A] and line["cited_invalid"] == [X]
    assert line["thought_tokens"] == 30 and line["cost_usd"] is not None


def test_a_grounded_answer_is_returned_unchanged(capsys):
    ctx = Ctx()
    a.after_tool(Tool("search_jobs"), {}, ctx, {"jobs": [{"id": A}]})
    assert a.after_model(ctx, _resp(text=f"Found it: [{A}].")) is None
    assert json.loads(capsys.readouterr().out)["refused"] is False


def test_weak_retrieval_with_nothing_cited_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(a, "REFUSAL_COSINE", 0.5)
    ctx = Ctx()
    a.after_tool(Tool("semantic_search"), {}, ctx, {"hits": [{"id": A}], "top_cosine": 0.2})
    out = a.after_model(ctx, _resp(text="Probably something about robots."))
    assert out.content.parts[0].text == a.g.REFUSAL
    assert json.loads(capsys.readouterr().out)["refused"] is True


def test_tokens_accumulate_across_every_model_call_in_one_answer(capsys):
    ctx = Ctx()
    a.after_model(ctx, _resp(call="semantic_search", usage=(500, 10, 40)))
    a.after_tool(Tool("semantic_search"), {}, ctx, {"hits": [{"id": A}], "top_cosine": 0.9})
    a.after_model(ctx, _resp(text=f"[{A}]", usage=(900, 30, 60)))
    line = json.loads(capsys.readouterr().out)
    assert line["model_calls"] == 2
    assert (line["prompt_tokens"], line["output_tokens"], line["thought_tokens"]) == \
        (1400, 40, 100)
