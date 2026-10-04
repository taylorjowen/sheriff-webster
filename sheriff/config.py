"""Configuration: built-in defaults deep-merged with an optional YAML file.

Every tunable model parameter lives here so it can be changed without code edits.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DATA = Path(__file__).resolve().parent / "data"

DEFAULTS: dict[str, Any] = {
    "env": "live",
    "paths": {
        "data_dir": "data",
        "live_db": "data/live.db",
        "answers_db": "data/answers.db",
        "archive_dir": "data/archive",
        "cache_dir": "data/cache",
        "test_dir": "data/test_runs",
        "scenario_dir": "scenarios",
        "glyph_dir": str(PACKAGE_DATA / "glyphs"),
    },
    "discord": {
        # Astrocade / Wordle app bot user id. Verify against your server.
        "wordle_bot_ids": [1211781489931452447],
        "guild_id": None,             # set for instant slash-command sync
        "ingest_channel_ids": [],     # channels where the Wordle app posts
        "board_channel_id": None,     # where Sheriff posts; defaults to first ingest channel
        "sandbox_channel_ids": [],    # test-mode-only channels
        "admin_user_ids": [],         # in addition to members with Manage Server
        "enable_test_commands": True,
    },
    "image": {
        # Reference colors (RGB). Calibrate from real fixtures with
        # `python -m sheriff.tools.calibrate <image>`.
        "colors": {
            "green": [83, 141, 78],
            "yellow": [181, 159, 59],
            "gray": [58, 58, 60],
            "empty": [44, 44, 46],
        },
        "tolerance": 22.0,        # max RGB distance from a reference color
        "min_margin": 6.0,        # nearest vs second-nearest distance margin for a confident tile
        "min_tile_px": 6,
        "text_min_luma": 150,     # title text is light on a dark panel
        "text_max_chroma": 60,
        "digit_match_threshold": 0.72,
    },
    "scoring": {
        "samples": 200,               # Monte Carlo guess sequences per game
        "guess_weight_answer": 1.0,   # sampling weight for curated-answer words
        "guess_weight_other": 0.35,   # sampling weight for other valid guesses
        "trap_min_greens": 3,
        "trap_min_k": 4,
        "out_of_nowhere_k": 10,
        "fake_elim_p": 0.05,
    },
    "model": {
        "prior": 0.05,           # P(a given game / player is cheating)
        "cheat_rate": 0.5,       # per-game cheat rate under the "cheater" hypothesis (long-term)
        # Row-level cheat likelihood: LR = (1-eps) + eps * e^(beta*greens)/Z (see scoring/engine.py).
        # Fitted by `python -m sheriff.tools.fit_model`.
        "row_epsilon": 0.6,
        "row_beta": 1.2,
        "max_row_lr": 50.0,
        "fallback_weight": 0.25,  # exponent applied to guess-count-only likelihood ratios
        "honest_guess_dist": {"1": 0.004, "2": 0.06, "3": 0.32, "4": 0.37, "5": 0.18, "6": 0.056, "X": 0.01},
        "cheat_guess_dist": {"1": 0.03, "2": 0.30, "3": 0.42, "4": 0.18, "5": 0.05, "6": 0.015, "X": 0.005},
        "timing_enabled": False,
    },
    "bounty": {"threshold": 0.5},
    "cusum": {"threshold": 4.0, "drift": 0.0},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, data: dict | None = None, root: Path = PROJECT_ROOT):
        self.data = _deep_merge(DEFAULTS, data or {})
        self.root = root

    @classmethod
    def load(cls, path: str | os.PathLike | None = None, overrides: dict | None = None) -> "Config":
        path = path or os.environ.get("SHERIFF_CONFIG") or (PROJECT_ROOT / "config.yaml")
        data: dict = {}
        p = Path(path)
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        data = _deep_merge(data, overrides or {})
        if os.environ.get("ENV"):
            data["env"] = os.environ["ENV"]
        return cls(data)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        cur = self.data
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value

    def path(self, key: str) -> Path:
        p = Path(self.get(f"paths.{key}"))
        return p if p.is_absolute() else (self.root / p)

    def with_overrides(self, overrides: dict) -> "Config":
        return Config(_deep_merge(self.data, overrides), self.root)
