"""Calibration scoring (§7).

An honest player picks each guess without knowing the answer, so from their point of
view the answer is uniform over the candidates still consistent with their feedback.
For each row we compute where the observed outcome falls in the distribution of
outcomes their guess *could* have produced: the randomized probability integral
transform (PIT) of the outcome, ordering outcomes by how much they shrink the
candidate set (smaller = luckier). For honest play the PIT is uniform no matter
the strategy; cheating pushes it toward 0.

We only see colors, not words, so the guesses are unknown: Monte Carlo samples guess
sequences from the words consistent with each row (§7.3).
  charitable = for each row, the sampled guess most favorable to the player (accusations)
  typical    = average across samples (rankings, stats)

Row likelihood ratio (cheat vs honest). A cheater's outcomes are tilted toward
progress their knowledge can't explain: P_cheat(q) = (1-eps)*P(q) + eps*P(q)*e^(beta*greens(q))/Z,
with Z = sum_q P(q)*e^(beta*greens(q)) under the player's own guess and knowledge. So
  LR = (1-eps) + eps*e^(beta*greens(obs))/Z,    and E_honest[LR] = 1 exactly.
Opening with 3 greens, escaping traps, and solving from a large candidate set all
score high; greens the knowledge state already guaranteed don't.
"""
from __future__ import annotations

import math
import zlib
from dataclasses import asdict, dataclass, field

import numpy as np

from ..config import Config
from ..patterns import GREENS_OF, NUM_PATTERNS, YELLOWS_OF, WordData, row_to_code


class InconsistentGrid(ValueError):
    """A row pattern no valid word can produce against the answer (bad parse or wrong day)."""


def tilt_ratio(hist: np.ndarray, total: np.ndarray, p: int, beta: float) -> np.ndarray:
    """e^(beta*greens(obs)) / E[e^(beta*greens)] per sample; hist is (S, 243) outcome weights."""
    eg = np.exp(beta * GREENS_OF.astype(np.float64))
    Z = (hist * eg[None, :]).sum(1) / total
    return eg[p] / Z


def row_lr(tilt: np.ndarray, eps: float, cap: float) -> np.ndarray:
    return np.clip((1 - eps) + eps * tilt, 0.0, cap)


@dataclass
class RowScore:
    index: int
    row: str
    greens: int
    yellows: int
    kind: str                 # "N" narrowing | "E" eliminating
    k_before: float           # median remaining candidates before this row
    k_after: float
    p_obs_charitable: float   # max over samples of P(observed pattern)
    p_obs_typical: float      # geometric mean over samples
    pit_mid_charitable: float # max over samples of the mid-PIT
    lr_charitable: float      # min over samples
    loglr_typical: float      # mean over samples
    tilt_charitable: float    # min over samples of the raw tilt ratio (eps-free, for refitting)


@dataclass
class GameScore:
    day: int
    guesses: int | None
    solved: bool
    rows: list[RowScore] = field(default_factory=list)
    log_lr_charitable: float = 0.0
    log_lr_typical: float = 0.0
    surprisal_charitable: float = 0.0
    surprisal_typical: float = 0.0
    fallback: bool = False
    features: dict = field(default_factory=dict)

    def to_features(self) -> dict:
        d = dict(self.features)
        d["rows"] = [asdict(r) for r in self.rows]
        d["log_lr_charitable"] = self.log_lr_charitable
        d["log_lr_typical"] = self.log_lr_typical
        d["fallback"] = self.fallback
        d["guesses"] = self.guesses
        d["solved"] = self.solved
        return d


class Scorer:
    def __init__(self, wd: WordData, cfg: Config):
        self.wd = wd
        self.cfg = cfg

    @property
    def eps(self) -> float:
        return float(self.cfg.get("model.row_epsilon"))

    @property
    def beta(self) -> float:
        return float(self.cfg.get("model.row_beta"))

    def score(self, day: int, answer: str, rows: list[str], hard_mode: bool | None = None,
              samples: int | None = None, seed: int | None = None, eps: float | None = None,
              beta: float | None = None) -> GameScore:
        wd = self.wd
        S = int(samples or self.cfg.get("scoring.samples"))
        eps = self.eps if eps is None else eps
        beta = self.beta if beta is None else beta
        cap = float(self.cfg.get("model.max_row_lr"))
        if seed is None:
            seed = zlib.crc32(f"{day}:{answer}:{'|'.join(rows)}".encode())
        rng = np.random.default_rng(seed)
        a = wd.ensure_answer(answer)
        col = wd.table[:, a]
        weights = wd.cand_weights
        gw = np.where(wd.guess_is_curated, float(self.cfg.get("scoring.guess_weight_answer")),
                      float(self.cfg.get("scoring.guess_weight_other")))

        mask = np.ones((S, len(wd.candidates)), dtype=bool)
        offsets = (np.arange(S) * NUM_PATTERNS)[:, None]
        greens_seen = [False] * 5
        out_rows: list[RowScore] = []
        log_lr_c = log_lr_t = s_char = s_typ = 0.0
        sample_loglik = np.zeros(S)

        for i, row in enumerate(rows):
            p = row_to_code(row)
            feasible = np.flatnonzero(col == p)
            if feasible.size == 0:
                raise InconsistentGrid(f"row {i + 1} ({row}) is impossible for answer {answer!r}")
            pw = gw[feasible]
            g = rng.choice(feasible, size=S, p=pw / pw.sum())
            P = wd.table[g]                                         # (S, C)
            wmask = mask * weights[None, :]
            hist = np.bincount((P.astype(np.int64) + offsets).ravel(), weights=wmask.ravel(),
                               minlength=S * NUM_PATTERNS).reshape(S, NUM_PATTERNS)
            total = hist.sum(1)
            obs = hist[:, p]
            lo = (hist * (hist < obs[:, None])).sum(1) / total
            eq = (hist * (hist == obs[:, None])).sum(1) / total
            hi = lo + eq
            pobs = obs / total
            k_before = mask.sum(1)
            mask &= (P == p)
            k_after = mask.sum(1)
            tilt = tilt_ratio(hist, total, p, beta)
            lr = row_lr(tilt, eps, cap)
            loglr = np.log(np.maximum(lr, 1e-300))
            sample_loglik += -np.log(pobs)

            kind = "N"
            for c in range(5):
                if greens_seen[c] and row[c] != "G":
                    kind = "E"
            for c in range(5):
                if row[c] == "G":
                    greens_seen[c] = True

            rs = RowScore(
                index=i, row=row, greens=int(GREENS_OF[p]), yellows=int(YELLOWS_OF[p]), kind=kind,
                k_before=float(np.median(k_before)), k_after=float(np.median(k_after)),
                p_obs_charitable=float(pobs.max()), p_obs_typical=float(np.exp(np.log(pobs).mean())),
                pit_mid_charitable=float(((lo + hi) / 2).max()),
                lr_charitable=float(lr.min()), loglr_typical=float(loglr.mean()),
                tilt_charitable=float(tilt.min()),
            )
            out_rows.append(rs)
            log_lr_c += math.log(max(rs.lr_charitable, 1e-300))
            log_lr_t += rs.loglr_typical
            s_char += -math.log(rs.p_obs_charitable)

        s_typ = float(sample_loglik.mean())
        solved = bool(rows) and rows[-1] == "GGGGG"
        gs = GameScore(day=day, guesses=len(rows) if solved else None, solved=solved, rows=out_rows,
                       log_lr_charitable=log_lr_c, log_lr_typical=log_lr_t,
                       surprisal_charitable=s_char, surprisal_typical=s_typ)
        gs.features = self._features(gs, hard_mode)
        return gs

    def _features(self, gs: GameScore, hard_mode: bool | None) -> dict:
        cfg = self.cfg
        rows = gs.rows
        f: dict = {}
        if rows:
            f["row1_greens"] = rows[0].greens
            f["row1_yellows"] = rows[0].yellows
            f["row1_pit"] = rows[0].pit_mid_charitable
        traps = []
        for i, r in enumerate(rows[:-1]):
            if r.row == "GGGGG":
                continue
            if cfg.get("scoring.trap_min_greens") <= r.greens <= 4 and r.k_after >= cfg.get("scoring.trap_min_k"):
                escaped = rows[i + 1].row == "GGGGG"
                traps.append({"row": i + 1, "k": r.k_after, "escaped": escaped,
                              "expected": 1.0 / max(r.k_after, 1.0)})
        f["traps"] = traps
        f["out_of_nowhere"] = [
            {"row": r.index + 1, "k": r.k_before}
            for r in rows if r.row == "GGGGG" and r.k_before >= cfg.get("scoring.out_of_nowhere_k")
        ]
        f["fake_elimination"] = sum(1 for r in rows if r.kind == "E" and r.pit_mid_charitable < cfg.get("scoring.fake_elim_p"))
        f["elimination_rows"] = sum(1 for r in rows if r.kind == "E")
        f["first_e_row"] = next((r.index + 1 for r in rows if r.kind == "E"), None)
        f["last_e_row"] = max((r.index + 1 for r in rows if r.kind == "E"), default=None)
        f["green_velocity"] = [rows[0].greens] + [max(0, b.greens - a.greens) for a, b in zip(rows, rows[1:])] if rows else []
        f["hard_mode"] = hard_mode
        f["hard_mode_violation"] = bool(hard_mode) and f["elimination_rows"] > 0
        return f

    def score_fallback(self, day: int, guesses: int | None) -> GameScore:
        """Guess-count-only scorer (§7.6). Weak evidence; heavily down-weighted."""
        key = "X" if guesses is None else str(guesses)
        ph = float(self.cfg.get("model.honest_guess_dist")[key])
        pc = float(self.cfg.get("model.cheat_guess_dist")[key])
        w = float(self.cfg.get("model.fallback_weight"))
        llr = w * math.log(pc / ph)
        return GameScore(day=day, guesses=guesses, solved=guesses is not None, log_lr_charitable=llr,
                         log_lr_typical=llr, surprisal_charitable=-math.log(ph), surprisal_typical=-math.log(ph),
                         fallback=True, features={"traps": [], "out_of_nowhere": [], "fallback": True})


def explain_flavor(features: dict) -> str:
    """One-line flavor explanation of the most notable thing in a game."""
    if features.get("fallback"):
        return "grid unreadable; judged on guess count alone"
    oon = features.get("out_of_nowhere") or []
    if oon:
        o = oon[0]
        return f"solved out of nowhere from ~{o['k']:.0f} possibilities on guess {o['row'] + 0}"
    escaped = [t for t in features.get("traps", []) if t["escaped"]]
    if escaped:
        t = escaped[0]
        return f"escaped a {t['k']:.0f}-way trap on guess {t['row'] + 1}"
    g1 = features.get("row1_greens", 0)
    if g1 >= 3:
        return f"opened with {g1} greens, fancy that"
    rows = features.get("rows") or []
    if rows:
        worst = max(rows, key=lambda r: r["lr_charitable"])
        if worst["lr_charitable"] > 2.5:
            return f"guess {worst['index'] + 1} lit up suspiciously ({worst['row']})"
    traps = features.get("traps", [])
    if traps:
        t = traps[0]
        return f"stuck in a {t['k']:.0f}-way trap like an honest cowpoke"
    if features.get("solved") is False:
        return "rode six guesses and came up empty"
    n = features.get("guesses")
    return f"an honest-looking {n}-guess ride" if n else "nothing to see here"
