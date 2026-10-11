"""Per-answer telemetry: latency, tokens (thinking included), cost, tools.

One JSON line per agent answer on stdout. On Cloud Run, a stdout line that is
a JSON object with "severity" and "message" becomes a structured log entry in
Cloud Logging, so every number in the evaluation's latency/cost table can be
re-derived from the logs, not just from the eval script's own timer.

Prices are constants WITH A DATE, like the scorer's: they are an estimate the
day they are written and must be checked against the Cloud Billing SKU report
before a cost is quoted anywhere. Gemini 3.x models think by default, and
thinking tokens are billed as output, so they are counted separately and
priced as output.
"""
from __future__ import annotations

import json
import sys

# $ per 1M tokens (input, output). ai.google.dev/gemini-api/docs/pricing,
# checked 2026-10-05. VERIFY against the Billing SKU report before quoting.
PRICES_PER_MTOK = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.8-flash": (0.75, 3.75),
}
PRICES_CHECKED = "2026-10-05"


def cost_usd(model: str, prompt_tokens: int, output_tokens: int,
             thought_tokens: int) -> float | None:
    p = PRICES_PER_MTOK.get(model)
    if p is None:
        return None
    return round((prompt_tokens * p[0] + (output_tokens + thought_tokens) * p[1]) / 1e6, 6)


def emit(record: dict, stream=None) -> None:
    """One structured line. stderr locally would be fine too, but Cloud Run
    parses JSON on stdout, and the agent's stdout is NOT an MCP channel (the
    MCP server is a separate child process with its own pipes)."""
    line = json.dumps({"severity": "INFO", "message": "agent_turn", **record},
                      ensure_ascii=False)
    print(line, file=stream or sys.stdout, flush=True)
