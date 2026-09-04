#!/usr/bin/env bash
# Dashboard tests. The Apps Script files are plain ES5 in one big string, so they
# can be run in Node against fake Spreadsheet and DOM objects.
#
#   units.js  — server functions against tidy fixtures
#   render.js — renders the real page HTML for the DOM test to load
#   dom.js    — runs that page in jsdom: does the script RUN, do the tabs work,
#               does a status change move the card? (parse-only checks missed
#               all of that)
#   realdata.js — optional: server functions against a dump of the live sheet,
#               written by tests/dashboard/dump_real.py
set -euo pipefail
cd "$(dirname "$0")"
[ -d node_modules ] || npm install --silent jsdom
# units.js reports its own result and then exits. For a while that exit sat in
# the MIDDLE of the file: ~180 lines of checks after it never ran, the suite
# still said "all checks passed", and two assertions rotted out of date and one
# real bug shipped while nobody was looking. Nothing else would catch this, so:
last_exit=$(grep -n "process.exit" units.js | tail -1 | cut -d: -f1)
last_code=$(grep -n "[^[:space:]]" units.js | tail -1 | cut -d: -f1)
if [ "$last_exit" != "$last_code" ]; then
  echo "units.js: process.exit is on line $last_exit but the file has code to"        "line $last_code — everything after the exit is dead and never runs." >&2
  exit 1
fi

for f in ../../dashboard; do
  echo "=== $(basename "$f")"
  node units.js "$f/Code.gs" | tail -1
  node render.js "$f/Code.gs" /tmp/page.html >/dev/null
  node dom.js /tmp/page.html | tail -1
  node removecheck.js /tmp/page.html | tail -1
done
