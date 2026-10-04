"""Honest and cheater player simulators (§7.7). Used to fit L_cheat, measure false
positives, and drive test scenarios.

Honest bots only ever look at their own feedback. Cheater bots peek at the answer.
"""
from __future__ import annotations

import numpy as np

from ..patterns import ALL_GREEN, GREENS_OF, NUM_PATTERNS, WordData, code_to_row

POPULAR_OPENERS = ["crane", "slate", "adieu", "stare", "raise", "audio", "roate", "trace", "soare",
                   "crate", "arise", "irate", "later", "salet", "least", "train", "house", "steam",
                   "ocean", "plant", "tears", "heart", "about", "pious", "media"]

HONEST = ("honest_narrowing", "honest_elimination", "honest_hybrid")
CHEATS = ("cheat_build_up", "cheat_near_miss")


class Simulator:
    def __init__(self, wd: WordData):
        self.wd = wd

    @property
    def cand_guess_idx(self) -> np.ndarray:
        return self.wd.cand_in_guesses   # grows when new real answers are added

    # ------------------------------------------------------------ helpers
    def _entropy_pick(self, pool: np.ndarray, mask: np.ndarray, rng: np.random.Generator, skill: float) -> int:
        cand = np.flatnonzero(mask)
        P = self.wd.table[np.ix_(pool, cand)].astype(np.int64)
        offs = (np.arange(len(pool)) * NUM_PATTERNS)[:, None]
        hist = np.bincount((P + offs).ravel(), minlength=len(pool) * NUM_PATTERNS).reshape(len(pool), NUM_PATTERNS)
        p = hist / len(cand)
        with np.errstate(divide="ignore", invalid="ignore"):
            ent = -(np.where(p > 0, p * np.log(p), 0.0)).sum(1)
        # a small bonus for guesses that could themselves be the answer
        in_cand = np.isin(pool, self.cand_guess_idx[cand])
        ent = ent + in_cand * (1.0 / len(cand))
        order = np.argsort(-ent)
        if rng.random() < skill:
            return int(pool[order[0]])
        return int(pool[order[rng.integers(0, min(6, len(order)))]])

    def _narrow(self, mask: np.ndarray, rng: np.random.Generator, skill: float) -> int:
        cand = np.flatnonzero(mask)
        if len(cand) <= 2 or rng.random() > skill:
            return int(self.cand_guess_idx[rng.choice(cand)])
        pool = self.cand_guess_idx[rng.choice(cand, size=min(40, len(cand)), replace=False)]
        return self._entropy_pick(pool, mask, rng, skill)

    def _eliminate(self, mask: np.ndarray, rng: np.random.Generator, skill: float) -> int:
        cand = np.flatnonzero(mask)
        pool = np.concatenate([rng.integers(0, len(self.wd.guesses), size=50),
                               self.cand_guess_idx[rng.choice(cand, size=min(15, len(cand)), replace=False)]])
        return self._entropy_pick(np.unique(pool), mask, rng, skill)

    def _opener(self, opener: str | None, rng: np.random.Generator, loyalty: float = 0.85) -> int:
        if opener and rng.random() < loyalty:
            return self.wd.guess_idx[opener]
        return int(self.cand_guess_idx[rng.integers(0, len(self.wd.curated))])

    def _honest_choice(self, style: str, row: int, mask: np.ndarray, rng, skill: float) -> int:
        k = int(mask.sum())
        if k <= 2:
            return self._narrow(mask, rng, 1.0)
        if style == "honest_elimination" and row <= 3:
            return self._eliminate(mask, rng, skill)
        if style == "honest_hybrid" and row <= 3 and k > 6:
            return self._eliminate(mask, rng, skill)
        return self._narrow(mask, rng, skill)

    # ------------------------------------------------------------ games
    def play_honest(self, answer: str, style: str, rng: np.random.Generator, skill: float = 0.6,
                    opener: str | None = None) -> list[str]:
        a = self.wd.ensure_answer(answer)
        mask = np.ones(len(self.wd.candidates), dtype=bool)
        rows: list[str] = []
        for r in range(6):
            g = self._opener(opener, rng) if r == 0 else self._honest_choice(style, r, mask, rng, skill)
            p = int(self.wd.table[g, a])
            rows.append(code_to_row(p))
            if p == ALL_GREEN:
                break
            mask &= self.wd.table[g] == p
        return rows

    def play_build_up(self, answer: str, rng: np.random.Generator) -> list[str]:
        """Opens with answer-like words and climbs toward the answer."""
        a = self.wd.ensure_answer(answer)
        col = self.wd.table[:, a]
        greens = GREENS_OF[col]
        n = int(rng.choice([2, 3, 4], p=[0.2, 0.5, 0.3]))
        target = int(rng.choice([1, 2, 3], p=[0.3, 0.45, 0.25]))
        rows = []
        for _ in range(n - 1):
            options = np.flatnonzero((greens == target) & (col != ALL_GREEN))
            if options.size == 0:
                options = np.flatnonzero((greens >= target - 1) & (col != ALL_GREEN))
            g = int(rng.choice(options))
            rows.append(code_to_row(int(col[g])))
            target = min(4, target + int(rng.choice([0, 1], p=[0.3, 0.7])))
        rows.append("GGGGG")
        return rows

    def play_near_miss(self, answer: str, rng: np.random.Generator, skill: float = 0.6,
                       opener: str | None = None) -> list[str]:
        """Plays roughly honestly, then jumps to the answer from a large candidate set."""
        a = self.wd.ensure_answer(answer)
        mask = np.ones(len(self.wd.candidates), dtype=bool)
        rows: list[str] = []
        for r in range(6):
            k = int(mask.sum())
            last_greens = rows[-1].count("G") if rows else 0
            jump_p = 0.0 if r == 0 else (0.85 if last_greens >= 3 else 0.5)
            if r >= 3:
                jump_p = 1.0
            if r > 0 and k >= 3 and rng.random() < jump_p:
                rows.append("GGGGG")
                break
            g = self._opener(opener, rng) if r == 0 else self._honest_choice("honest_hybrid", r, mask, rng, skill)
            p = int(self.wd.table[g, a])
            rows.append(code_to_row(p))
            if p == ALL_GREEN:
                break
            mask &= self.wd.table[g] == p
        return rows

    def play(self, strategy: str, answer: str, rng: np.random.Generator, skill: float = 0.6,
             opener: str | None = None) -> list[str]:
        if strategy in HONEST:
            return self.play_honest(answer, strategy, rng, skill, opener)
        if strategy == "cheat_build_up":
            return self.play_build_up(answer, rng)
        if strategy == "cheat_near_miss":
            return self.play_near_miss(answer, rng, skill, opener)
        raise ValueError(f"unknown strategy {strategy!r}")
