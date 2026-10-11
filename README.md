# harvest

A public snapshot of a data pipeline I run privately. The code and tests are
real; my state, configuration and scheduled runs live in a private repo.

Every morning it pulls records from 19 sources (REST APIs, GitHub-hosted
datasets, per-organisation watchlists and headless-browser scraping), dedupes
them, applies hard filters, scores what survives with an LLM, and appends a
curated shortlist to a Google Sheets workbook. Once a week it emails a digest.

```
sources -> dedupe -> filters -> LLM scoring -> volume gate -> sheet -> digest
```

Some choices I care about:

- Scoring is two-stage: a free heuristic pre-ranks everything, and only the
  top 50 go to the LLM, which keeps a run to a few cents.
- Dedupe is SQLite state plus fuzzy matching, so the same record arriving
  from five sources surfaces once.
- Filters and per-entity rules are YAML config, not code.
- Missing credentials never kill a run. No API key means a clearly labelled
  heuristic scorer; a missing source key means a logged skip.
- A rolling weekly cap keeps the shortlist to around 10 items, and
  watchlisted entities get a lower bar so they are not lost to it.
- Status and Notes columns in the sheet belong to the human. The pipeline
  only ever appends rows.
- One dead source cannot crash a run, but zero records across all sources
  turns CI red on purpose.

There is also an MCP server built on FastMCP (`python -m jobbank mcp`), so an
AI agent such as a Claude Code session can query the results directly: why
was this record filtered, how full is this week's cap, and so on.

## Retrieval and an agent on Google Cloud

The records are also searchable by meaning (`python -m jobbank rag`, needs
the `[rag]` extra):

- Every record is embedded with `gemini-embedding-2` and stored in LanceDB
  with a BM25 full-text index. Hybrid search fuses the two legs with
  reciprocal rank fusion; the best vector cosine is reported alongside, so a
  caller can tell when nothing really matches.
- It is exposed as a `semantic_search` tool on the same MCP server.
- `rag eval` is an evaluation harness written before any system was run:
  pre-registered queries, four systems (SQL substring, BM25, vector, hybrid),
  blind pooled judging, recall@k / MRR / nDCG and paired bootstrap CIs.
  The query set itself stays private because it reveals the records.

  First run (2026-10-11, 23.7k records, `gemini-embedding-2` at 768 dims):
  36 answerable queries (12 known-item lexical, 12 known-item paraphrase,
  12 topical; 248 pooled results judged 0/1/2 blind by an LLM judge).

  | system | recall@5 | MRR@10 | nDCG@10 |
  |---|---|---|---|
  | SQL substring (old tool) | 0.03 | 0.03 | 0.03 |
  | BM25 | 0.52 | 0.68 | 0.60 [0.48, 0.73] |
  | vector | 0.68 | 0.85 | 0.83 [0.74, 0.91] |
  | hybrid (RRF, k=60) | 0.59 | 0.78 | 0.73 [0.63, 0.83] |

  Vector beats BM25 most on paraphrased queries (recall@5 0.92 vs 0.33);
  BM25 still wins exact-title lookups (1.00 vs 0.83). Equal-weight RRF lands
  between the two rather than above both, so the fusion is the next thing to
  fix, measured against a new query set rather than this one.

`agent/` is an ADK agent on Gemini that answers questions through that MCP
server, run in read-only mode so the tools that spend money or write are not
even registered. Its guardrails (`jobbank/rag/guardrails.py`, pure and
unit-tested) pin the profile, cap result sizes, strip any cited record id that
no tool returned, and can refuse when retrieval confidence is low. Every
answer logs latency, tokens, cost and citation validity as structured JSON,
and traces go to Cloud Trace.

`deploy/` ships it to Cloud Run: the build context is assembled from an
allowlist with a secret scan, the service is private (identity token
required) and runs as a service account that can call models and write logs,
nothing else.

## Run it

```
pip install -e .[dev]
python -m jobbank doctor      # which credentials are configured
python -m jobbank run
pytest
```

Without a SHEET_ID it writes the same tabs to a local xlsx. See
`.env.example` for the credentials it can use.

## Layout

```
src/jobbank/   fetchers, filters, scorer, state, sheets, digest, MCP server
src/jobbank/rag/  embeddings, LanceDB index, hybrid search, eval, guardrails
agent/         ADK agent over the MCP server
deploy/        Cloud Run packaging
config/        search windows, filter rules, watchlist seed
tests/         pytest suite plus dashboard tests
dashboard/     Apps Script dashboard for the sheet
```
