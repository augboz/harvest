# Job Bank dashboard — one-time setup (~3 minutes)

A real web page served from the spreadsheet itself. Once deployed you get a
permanent URL that always shows current data, works on your phone, and needs no
hosting or maintenance. It only *reads* the sheet, so it can't disturb the pipeline.

## Deploy

1. Open the Job Bank spreadsheet → **Extensions ▸ Apps Script**.
2. Delete whatever is in `Code.gs`, paste in the whole of
   [`dashboard/Code.gs`](Code.gs), and save (Ctrl+S).
3. Click **Deploy ▸ New deployment** → gear icon ▸ **Web app**.
   - *Description*: `Job Bank dashboard`
   - *Execute as*: **Me**
   - *Who has access*: **Only myself** (keeps it private — you'll be signed in)
4. **Deploy** → approve the permissions prompt (it's your own script reading your
   own sheet) → copy the **Web app URL**.

Bookmark that URL / add it to your phone home screen. Refresh to see the latest.

## What's on it

- Link to the spreadsheet, top right
- Numbers: open roles · top picks · closing soon · not open yet
- A note listing anything closing within seven days, each linked
- One card per role: score, category, company, Top pick marker, the role as a direct
  link, location · duration · deadline countdown, why it scored what it did
- **Status control on every card** — Not applied → Applied → OA → Interview → Offer
  → Rejected. Writes straight to the sheet; the pipeline never overwrites Status.
- **A heart on every card** pins that role to the *first rows of the Job Bank tab* —
  the row is physically moved to row 2, and hearted roles lead the list on the page
  too, above anything scoring higher. Click it again to unheart: the row drops to
  just under the roles still hearted (where it originally sat is recorded nowhere,
  so nothing pretends to restore it).
- **"Not for me"** dismisses a role and pulls the highest-scoring archived role into
  the freed slot. Undo is offered for a few seconds afterwards.
- **Cards / List switch** above the tabs, and on More roles. List view is one role per row — initials
  for a logo, the role, company, location, age and score — for scanning a long
  bank rather than reading six cards. Clicking a row expands it into the full
  card, so nothing is reachable only from one view. One choice covers both
  pages and is remembered.
- Category filters, and the Not-open-yet table with live rows highlighted

## Actions write to the sheet

`setStatus`, `toggleFavourite`, `dismissJob` and `undismissJob` are the only writes.
`toggleFavourite` writes a **Favourite** column (added to the tab on first use) and
reorders rows by rotating values rather than deleting and re-inserting, so the
Status dropdown and colour rules stay attached to their cells. The column is
declared in `JOB_BANK_COLS` (`src/jobbank/sheets.py`) because `ensure_schema()`
clears any Job Bank column the pipeline does not know about. Dismissing sets
Status to **Not interested** (which is why that value was added to the sheet's Status
dropdown) and the row stays in the sheet, so nothing is destroyed and the pipeline's
dedupe still knows about it.

## Updating it later

Paste the new `Code.gs` over the old one, then **Deploy ▸ Manage deployments ▸
edit (pencil) ▸ Version: New version ▸ Deploy**. The URL stays the same.

## Note on links

Every link in the Job Bank is verified before the row is written (`src/jobbank/
linkcheck.py`): a posting that returns 404/410 is dropped rather than shown to you,
and `/apply` form URLs are normalised to the posting page. Aggregator sources
(e.g. Fluidjobs) link to their own posting page — that's the deepest link they
expose, since their outbound `/apply` redirect is disallowed by their robots.txt.
