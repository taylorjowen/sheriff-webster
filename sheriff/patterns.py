"""Wordle feedback patterns and the guesses x candidates pattern table.

A pattern is a base-3 int in 0..242: position i contributes digit*3**i with
G=2, Y=1, B=0. All-green is 242.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import numpy as np

from .config import PACKAGE_DATA

log = logging.getLogger(__name__)

ALL_GREEN = 242
NUM_PATTERNS = 243
_DIGIT = {"B": 0, "Y": 1, "G": 2}
_LETTER = "BYG"


def pattern(guess: str, answer: str) -> int:
    """Standard two-pass Wordle feedback (greens first, then yellows from remaining counts)."""
    guess, answer = guess.lower(), answer.lower()
    res = [0] * 5
    remaining: dict[str, int] = {}
    for i in range(5):
        if guess[i] == answer[i]:
            res[i] = 2
        else:
            remaining[answer[i]] = remaining.get(answer[i], 0) + 1
    for i in range(5):
        if res[i] == 0 and remaining.get(guess[i], 0) > 0:
            res[i] = 1
            remaining[guess[i]] -= 1
    return sum(d * 3**i for i, d in enumerate(res))


def row_to_code(row: str) -> int:
    return sum(_DIGIT[c] * 3**i for i, c in enumerate(row))


def code_to_row(code: int) -> str:
    out = []
    for _ in range(5):
        out.append(_LETTER[code % 3])
        code //= 3
    return "".join(out)


GREENS_OF = np.array([code_to_row(c).count("G") for c in range(NUM_PATTERNS)], dtype=np.int8)
YELLOWS_OF = np.array([code_to_row(c).count("Y") for c in range(NUM_PATTERNS)], dtype=np.int8)


def encode_words(words: list[str]) -> np.ndarray:
    arr = np.frombuffer("".join(words).lower().encode("ascii"), dtype=np.uint8)
    return arr.reshape(len(words), 5) - ord("a")


def pattern_matrix(G: np.ndarray, A: np.ndarray, chunk: int = 512) -> np.ndarray:
    """Vectorized pattern(g, a) for all pairs. G: (n,5), A: (m,5) letter codes."""
    n, m = len(G), len(A)
    out = np.empty((n, m), dtype=np.uint8)
    for s in range(0, n, chunk):
        g = G[s:s + chunk]
        green = g[:, None, :] == A[None, :, :]                 # (c, m, 5)
        notgreen = ~green
        code = np.zeros((len(g), m), dtype=np.int16)
        for i in range(5):
            gi = g[:, i][:, None]                               # (c, 1)
            avail = np.zeros((len(g), m), dtype=np.int8)
            for j in range(5):
                avail += (A[None, :, j] == gi) & notgreen[:, :, j]
            prior = np.zeros((len(g), m), dtype=np.int8)
            for j in range(i):
                prior += (g[:, j] == g[:, i])[:, None] & notgreen[:, :, j]
            digit = np.where(green[:, :, i], 2, (avail > prior).astype(np.int8))
            code += digit.astype(np.int16) * (3**i)
        out[s:s + chunk] = code.astype(np.uint8)
    return out


def load_word_list(name: str) -> list[str]:
    path = PACKAGE_DATA / name
    return [w.strip().lower() for w in path.read_text(encoding="utf-8").split() if len(w.strip()) == 5]


class WordData:
    """Guess dictionary, answer candidates, and their pattern table.

    Candidates are the curated answer list plus any known real answers outside it
    (NYT has added words since the original list). Every candidate is also a valid guess.
    """

    def __init__(self, cache_dir: Path | None = None, extra_answers: list[str] | None = None):
        answers = load_word_list("answers.txt")
        allowed = load_word_list("allowed.txt")
        extra = sorted({w.lower() for w in (extra_answers or [])} - set(answers))
        self.candidates: list[str] = answers + extra
        self.curated = set(answers)
        guess_set = set(allowed) | set(self.candidates)
        self.guesses: list[str] = sorted(guess_set)
        self.guess_idx = {w: i for i, w in enumerate(self.guesses)}
        self.cand_idx = {w: i for i, w in enumerate(self.candidates)}
        self.cand_weights = np.ones(len(self.candidates), dtype=np.float64)
        self.guess_is_curated = np.array([w in self.curated for w in self.guesses])
        self.cand_in_guesses = np.array([self.guess_idx[w] for w in self.candidates])
        self.table = self._load_table(cache_dir)

    def _load_table(self, cache_dir: Path | None) -> np.ndarray:
        h = hashlib.sha1(("|".join(self.guesses) + "#" + "|".join(self.candidates)).encode()).hexdigest()[:16]
        path = None
        if cache_dir is not None:
            path = Path(cache_dir) / f"patterns_{h}.npy"
            if path.exists():
                try:
                    return np.load(path)
                except Exception:  # corrupt cache; rebuild
                    log.warning("pattern table cache unreadable, rebuilding")
        log.info("building pattern table %d x %d", len(self.guesses), len(self.candidates))
        table = pattern_matrix(encode_words(self.guesses), encode_words(self.candidates))
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp.npy")
            np.save(tmp, table)
            tmp.replace(path)
        return table

    def ensure_answer(self, word: str) -> int:
        """Return the candidate index of `word`, adding it (and its column) if missing."""
        word = word.lower()
        if word in self.cand_idx:
            return self.cand_idx[word]
        if word not in self.guess_idx:
            # Add as a guess row too (keeps the table consistent).
            self.guesses.append(word)
            self.guess_idx[word] = len(self.guesses) - 1
            self.guess_is_curated = np.append(self.guess_is_curated, False)
            row = pattern_matrix(encode_words([word]), encode_words(self.candidates))
            self.table = np.vstack([self.table, row])
        col = pattern_matrix(encode_words(self.guesses), encode_words([word]))
        self.table = np.hstack([self.table, col])
        self.candidates.append(word)
        self.cand_idx[word] = len(self.candidates) - 1
        self.cand_weights = np.append(self.cand_weights, 1.0)
        self.cand_in_guesses = np.append(self.cand_in_guesses, self.guess_idx[word])
        return self.cand_idx[word]


_SHARED: WordData | None = None


def shared_word_data(cache_dir: Path | None = None) -> WordData:
    """Process-wide WordData (the table is ~30 MB; share it between runs)."""
    global _SHARED
    if _SHARED is None:
        _SHARED = WordData(cache_dir)
    return _SHARED
