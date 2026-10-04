"""Fit the row-level cheat likelihood (eps, beta) from simulated cheater games, and
report honest calibration / false-positive behaviour (§7.7, §11).

    python -m sheriff.tools.fit_model --games 300
"""
from __future__ import annotations

import argparse

import numpy as np

from ..config import Config
from ..patterns import WordData
from ..scoring.engine import Scorer
from ..scoring.posterior import longterm_posterior
from ..sim.bots import POPULAR_OPENERS, Simulator

BETAS = [0.4, 0.6, 0.8, 1.0, 1.2, 1.5, 1.8, 2.2]
EPSILONS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]


def play_games(sim: Simulator, strategy: str, n: int, rng: np.random.Generator):
    words = sim.wd.candidates[:len(sim.wd.curated)]
    out = []
    for _ in range(n):
        answer = words[rng.integers(len(words))]
        rows = sim.play(strategy, answer, rng, skill=float(rng.uniform(0.3, 0.9)),
                        opener=POPULAR_OPENERS[rng.integers(len(POPULAR_OPENERS))])
        out.append((answer, rows))
    return out


def tilts(scorer: Scorer, games, beta: float, samples: int) -> list[np.ndarray]:
    """Per game: per-row charitable tilt ratios at this beta."""
    return [np.array([r.tilt_charitable for r in scorer.score(0, a, rows, samples=samples, beta=beta, eps=1.0).rows])
            for a, rows in games]


def game_llrs(tilt_games: list[np.ndarray], eps: float, cap: float) -> np.ndarray:
    return np.array([np.log(np.clip((1 - eps) + eps * t, 1e-300, cap)).sum() for t in tilt_games])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=200, help="games per strategy")
    ap.add_argument("--samples", type=int, default=64)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    cfg = Config.load()
    cap = float(cfg.get("model.max_row_lr"))
    wd = WordData(cfg.path("cache_dir"))
    scorer = Scorer(wd, cfg)
    sim = Simulator(wd)
    rng = np.random.default_rng(a.seed)

    cheat_games = play_games(sim, "cheat_build_up", a.games, rng) + play_games(sim, "cheat_near_miss", a.games, rng)
    honest_games = {s: play_games(sim, s, a.games, rng)
                    for s in ("honest_narrowing", "honest_elimination", "honest_hybrid")}

    best = None
    for beta in BETAS:
        ct = tilts(scorer, cheat_games, beta, a.samples)
        for eps in EPSILONS:
            ll = game_llrs(ct, eps, cap).sum()
            if best is None or ll > best[0]:
                best = (ll, eps, beta, ct)
    _, eps, beta, ct = best
    print(f"fitted row_epsilon={eps:.2f} row_beta={beta:.2f}")

    cheat_llr = game_llrs(ct, eps, cap)
    print(f"{'cheaters':20s} mean logLR={cheat_llr.mean():+.3f}  median={np.median(cheat_llr):+.3f}")
    honest_llr_all = []
    for style, games in honest_games.items():
        llr = game_llrs(tilts(scorer, games, beta, a.samples), eps, cap)
        honest_llr_all.extend(llr)
        print(f"{style:20s} mean LR={np.mean(np.exp(llr)):.3f}  mean logLR={llr.mean():+.3f}  "
              f"P(daily>50%)={np.mean(llr > np.log(19)):.3f}")
    honest_llr_all = np.array(honest_llr_all)
    prior, rho = cfg.get("model.prior"), cfg.get("model.cheat_rate")
    r2 = np.random.default_rng(1)
    fp = np.mean([longterm_posterior(list(r2.choice(honest_llr_all, 100)), prior, rho) > 0.5 for _ in range(1000)])
    for n_cheat in (5, 10, 20):
        tp = np.mean([longterm_posterior(list(r2.choice(cheat_llr, n_cheat)) + list(r2.choice(honest_llr_all, n_cheat)),
                                         prior, rho) > 0.5 for _ in range(1000)])
        print(f"{n_cheat} cheated + {n_cheat} honest games -> P(bounty) = {tp:.3f}")
    print(f"100 honest games -> P(bounty) = {fp:.4f}")
    print(f"\nAdd to config.yaml:\nmodel:\n  row_epsilon: {eps:.2f}\n  row_beta: {beta:.2f}")


if __name__ == "__main__":
    main()
