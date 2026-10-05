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
config/        search windows, filter rules, watchlist seed
tests/         pytest suite plus dashboard tests
dashboard/     Apps Script dashboard for the sheet
```
