# Built ONLY from the context deploy/stage.sh assembles (out/deploy_ctx), never
# from the repo root: the root holds .env and service_account.json.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so a code-only change reuses the cached layer.
COPY pyproject.toml ./
COPY src ./src
# Editable install so jobbank.settings.REPO_ROOT (parents[2]) resolves to /app.
RUN pip install -e ".[agent]"

COPY agent ./agent
COPY config ./config
COPY data ./data
COPY rag_index ./rag_index

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

# Gemini and embeddings go through the Agent Platform (formerly Vertex AI) and
# authenticate as the service's runtime service account. No API keys.
ENV GOOGLE_GENAI_USE_VERTEXAI=TRUE \
    JOBBANK_MCP_READONLY=1

CMD ["python", "agent/main.py"]
