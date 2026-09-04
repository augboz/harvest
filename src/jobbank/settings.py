"""Config + env loading. Local YAML gives defaults; when a Google Sheet is
connected, the Stats-tab config cells override the four user-editable values
(weekly cap, min score, digest email) — spec sec 9.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


class Settings:
    def __init__(self, root: Path | None = None):
        self.root = root or REPO_ROOT
        _load_dotenv(self.root / ".env")
        with open(self.root / "config" / "config.yaml", encoding="utf-8") as f:
            self.cfg: dict[str, Any] = yaml.safe_load(f)
        with open(self.root / "config" / "watchlist.yaml", encoding="utf-8") as f:
            self.watchlist: list[dict[str, Any]] = yaml.safe_load(f)["companies"]

    # editable-in-sheet values (sheet overrides applied by pipeline once read)
    @property
    def weekly_cap(self) -> int: return int(self.cfg["weekly_cap"])
    @property
    def min_score(self) -> int: return int(self.cfg["min_score"])
    @property
    def priority_score(self) -> int: return int(self.cfg["priority_score"])
    @property
    def bank_ceiling(self) -> int: return int(self.cfg.get("bank_ceiling", 10))
    @property
    def max_per_company(self) -> int: return int(self.cfg.get("max_per_company", 0))
    @property
    def bank_overflow(self) -> int: return int(self.cfg.get("bank_overflow", 0))

    @property
    def watchlist_min_relief(self) -> int:
        # How far below min_score a WATCHLIST company's role may score and
        # still bank. 0 = watchlist companies play by the same rules.
        return int(self.cfg.get("watchlist_min_relief", 0))

    @property
    def admit_all_qualifying(self) -> bool:
        return bool(self.cfg.get("admit_all_qualifying", False))
    @property
    def freshness_days(self) -> int: return int(self.cfg["freshness_days"])
    @property
    def digest_to(self) -> str: return self.cfg.get("digest_to", "")
    @property
    def profile(self) -> str: return self.cfg.get("profile", "default")

    def apply_sheet_overrides(self, overrides: dict[str, Any]) -> None:
        for key in ("weekly_cap", "min_score", "digest_to", "run_every_days"):
            if overrides.get(key) not in (None, ""):
                self.cfg[key] = overrides[key]
        if overrides.get("llm_top_n") not in (None, ""):
            self.cfg.setdefault("scorer", {})["llm_top_n"] = overrides["llm_top_n"]

    @property
    def run_every_days(self) -> int:
        try:
            return max(1, int(self.cfg.get("run_every_days", 1)))
        except (TypeError, ValueError):
            return 1

    # env-backed secrets
    def env(self, key: str, default: str = "") -> str:
        return os.environ.get(key, default)

    @property
    def state_path(self) -> Path: return self.root / "data" / "state.db"
    @property
    def out_dir(self) -> Path:
        d = self.root / "out"
        d.mkdir(exist_ok=True)
        return d
