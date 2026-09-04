"""Dump the live sheet's tabs so realdata.js can run the dashboard's server
functions against actual rows rather than tidy fixtures — the tidy ones would
never have caught a row with a missing column or an odd date."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from jobbank.settings import Settings          # noqa: E402
from jobbank.sheets import make_backend        # noqa: E402

b = make_backend(Settings())
out = {}
for tab in ("Job Bank", "Archive", "Opening Soon", "Watchlist"):
    vals = b.sh.worksheet(tab).get_all_values()
    if tab == "Archive":            # the dashboard only ever reads the tail
        vals = [vals[0]] + vals[-800:]
    out[tab] = vals
dest = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/real.json")
dest.write_text(json.dumps(out), encoding="utf-8")
print({k: len(v) for k, v in out.items()}, "->", dest)
