# harvest

A public snapshot of the job-search pipeline I run privately. The code and
tests are real, but my search state, profile and scheduled runs live in the
private repo.

Every morning it pulls internship and placement openings from 15+ sources
(GitHub boards, Adzuna, Reed, Greenhouse/Lever watchlists, Gradcracker,
Welcome to the Jungle and more), dedupes them, applies hard filters, scores
what survives with an LLM for fit and attainability, and appends a curated
shortlist to a Google Sheets job bank. Once a week it emails a digest.

```
sources -> dedupe -> filters -> LLM scoring -> volume gate -> sheet -> digest
```

Some choices I care about:

- Dedupe is SQLite state plus fuzzy matching, so the same role posted on
  five boards surfaces once.
- Filters and per-company rules are YAML config, not code.
- Missing credentials never kill a run. No API key means a clearly labelled
  heuristic scorer, a missing source key means a logged skip.
- A rolling weekly cap keeps the shortlist around 10 roles, and watched
  companies get a lower bar so their roles are not lost to it.
- Status and Notes columns in the sheet belong to the human. The pipeline
  only ever appends rows.
- One dead job board cannot crash a run, but zero jobs across all sources
  turns CI red on purpose.

There is also an MCP server (`python -m jobbank mcp`) so a Claude Code
session can query the bank directly: why was this role filtered, how full
is this week's cap, and so on.

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
config/        search windows, company rules, watchlist seed
tests/         pytest suite plus dashboard tests
dashboard/     Apps Script dashboard for the sheet
```
