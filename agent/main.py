"""The agent as an HTTP service: ADK's FastAPI app, for Cloud Run.

Cloud Run puts IAM in front of it (deployed without --allow-unauthenticated),
so a request needs an identity token: `gcloud run services proxy` locally, or
`Authorization: Bearer $(gcloud auth print-identity-token)`.

Sessions are in memory and per instance. auto_create_session lets /run create
the session it is handed, so a caller never depends on landing on the instance
that created it. max_llm_calls bounds the tool-calling loop per answer: a
runaway loop costs at most that many model calls.
"""
from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from google.adk.cli.fast_api import get_fast_api_app

AGENTS_DIR = str(Path(__file__).resolve().parent)

app = get_fast_api_app(
    agents_dir=AGENTS_DIR,
    web=os.environ.get("SERVE_WEB_UI") == "1",
    auto_create_session=True,
    trace_to_cloud=os.environ.get("TRACE_TO_CLOUD") == "1",
    max_llm_calls=int(os.environ.get("MAX_LLM_CALLS", "8")),
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
