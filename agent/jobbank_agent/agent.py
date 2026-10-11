"""An ADK agent that answers questions over the pipeline's records.

It reuses the pipeline's own MCP server rather than reimplementing anything:
the server runs as a stdio child process in read-only mode, and the agent sees
four of its tools (semantic_search, search_jobs, status, run_history). ADK runs
the tool-calling loop: the model decides to call a tool, reads the result, and
either calls another or answers. That is ReAct-style iteration, but there is no
explicit ReAct planner here.

The guardrails live in jobbank.rag.guardrails as pure, tested functions; the
four callbacks below are thin adapters that call them and keep per-answer
bookkeeping in ADK's `temp:` session state (scoped to one answer).

Configuration, all from the environment:
  GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION   the GCP project (free trial);
                                                location "global" (the 3.x models
                                                are not served from regions)
  GOOGLE_GENAI_USE_VERTEXAI=TRUE                route Gemini through the Agent
                                                Platform, billed to the project
  JOBBANK_AGENT_MODEL       default gemini-3.5-flash-lite
  JOBBANK_REFUSAL_COSINE    calibrated threshold; 0.0 (never refuses on
                            confidence alone) until the eval has calibrated it

Defined synchronously at import, as ADK requires for deployment.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from jobbank.rag import guardrails as g
from jobbank.rag import telemetry

MODEL = os.environ.get("JOBBANK_AGENT_MODEL", "gemini-3.5-flash-lite")
REFUSAL_COSINE = float(os.environ.get("JOBBANK_REFUSAL_COSINE", "0.0") or 0.0)
REPO_ROOT = Path(__file__).resolve().parents[2]

# The MCP stdio client does NOT pass the parent's environment through to the
# child, so whatever the server needs must be listed. Credentials for the
# embedding call travel as ADC paths and project ids; nothing pipeline-secret
# (Anthropic key, sheet id, mail) is passed, and the read tools never use them.
_PASS_THROUGH = ("PATH", "SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "HOME",
                 "USERPROFILE", "TEMP", "TMP", "PYTHONPATH",
                 "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
                 "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GENAI_USE_VERTEXAI",
                 "GOOGLE_GENAI_USE_ENTERPRISE")


def child_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASS_THROUGH if k in os.environ}
    env["JOBBANK_MCP_READONLY"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


INSTRUCTION = """\
You answer questions about a collection of records gathered by a data pipeline
from many sources. Each record has an organisation, a title, a source, a
disposition (bank = kept, archive = scored but not kept, filtered = rejected by
a hard rule) and sometimes a score and a short reason.

How to work:
- For "find" or "which records" questions, call semantic_search first. Use
  search_jobs when the user names an organisation or an exact title.
- Use status or run_history for questions about the pipeline's runs.
- Read the tool results, then answer. Call another tool if the first did not
  answer the question; do not call the same tool with the same arguments twice.

Rules:
- Cite every record you mention by its id in square brackets, e.g. [3f9a0c21b7de].
  Only cite ids that a tool returned in this conversation turn. Never invent one.
- If the results do not support an answer, say so plainly instead of guessing.
- Be brief: a few sentences or a short list.
"""


# ------------------------------------------------------------- callbacks ---

def _state(ctx):
    st = ctx.state
    if "temp:turn" not in st:
        st["temp:turn"] = True
        st["temp:ids"] = []
        st["temp:cos"] = []
        st["temp:tools"] = []
        st["temp:usage"] = [0, 0, 0, 0]       # calls, prompt, output, thoughts
        st["temp:t0"] = time.perf_counter()
    return st


def before_tool(tool, args, tool_context):
    ok, safe, why = g.check_tool_call(tool.name, args)
    st = _state(tool_context)
    st["temp:tools"] = list(st["temp:tools"]) + [tool.name]
    if not ok:
        return {"error": why}                 # returned as the tool's result
    args.clear()
    args.update(safe)                         # the call proceeds with safe args
    return None


def after_tool(tool, args, tool_context, tool_response):
    ids, top = g.ids_from_tool_response(tool.name, tool_response)
    st = _state(tool_context)
    if ids:
        st["temp:ids"] = sorted(set(st["temp:ids"]) | ids)
    if top is not None:
        st["temp:cos"] = list(st["temp:cos"]) + [top]
    return None


def _final_text(resp: LlmResponse) -> str | None:
    parts = (resp.content.parts if resp.content else None) or []
    if any(getattr(p, "function_call", None) for p in parts):
        return None                           # an intermediate tool-calling step
    text = "".join(p.text for p in parts if getattr(p, "text", None)
                   and not getattr(p, "thought", False))
    return text or None


def after_model(callback_context, llm_response: LlmResponse):
    st = _state(callback_context)
    u = llm_response.usage_metadata
    calls, p_tok, o_tok, t_tok = st["temp:usage"]
    st["temp:usage"] = [calls + 1,
                        p_tok + int(getattr(u, "prompt_token_count", 0) or 0),
                        o_tok + int(getattr(u, "candidates_token_count", 0) or 0),
                        t_tok + int(getattr(u, "thoughts_token_count", 0) or 0)]
    text = _final_text(llm_response)
    if text is None:
        return None
    v = g.finalise(text, set(st["temp:ids"]), list(st["temp:cos"]), REFUSAL_COSINE)
    calls, p_tok, o_tok, t_tok = st["temp:usage"]
    telemetry.emit({
        "model": MODEL,
        "latency_s": round(time.perf_counter() - st["temp:t0"], 3),
        "model_calls": calls, "prompt_tokens": p_tok, "output_tokens": o_tok,
        "thought_tokens": t_tok,
        "cost_usd": telemetry.cost_usd(MODEL, p_tok, o_tok, t_tok),
        "prices_checked": telemetry.PRICES_CHECKED,
        "tools": list(st["temp:tools"]),
        "top_cosine": max(st["temp:cos"]) if st["temp:cos"] else None,
        "cited_valid": v.citations.valid, "cited_invalid": v.citations.invalid,
        "refused": v.refused, "downgraded": v.downgraded,
        "refusal_threshold": REFUSAL_COSINE,
    })
    if v.text == text:
        return None
    replaced = llm_response.model_copy(deep=True)
    replaced.content = types.Content(role="model", parts=[types.Part(text=v.text)])
    return replaced


# ---------------------------------------------------------------- agent ---

toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable, args=["-m", "jobbank", "mcp"],
            env=child_env(), cwd=str(REPO_ROOT)),
        timeout=60),
    tool_filter=list(g.READ_TOOLS),
)

root_agent = LlmAgent(
    name="records_agent",
    model=MODEL,
    description="Answers questions over the pipeline's records, citing record ids.",
    instruction=INSTRUCTION,
    tools=[toolset],
    before_tool_callback=before_tool,
    after_tool_callback=after_tool,
    after_model_callback=after_model,
    generate_content_config=types.GenerateContentConfig(temperature=0.2),
)
