import math

import numpy as np
import pytest

from sheriff.scoring.engine import InconsistentGrid, Scorer
from sheriff.scoring.posterior import bounty_amount, cusum, daily_posterior, longterm_posterior
from sheriff.sim.bots import POPULAR_OPENERS, Simulator


def _games(wd, strategy, n, seed):
    sim = Simulator(wd)
    rng = np.random.default_rng(seed)
    words = wd.candidates[:len(wd.curated)]
    out = []
    for _ in range(n):
        a = words[rng.integers(len(words))]
        out.append((a, sim.play(strategy, a, rng, skill=0.6, opener=POPULAR_OPENERS[rng.integers(10)])))
    return out


def test_honest_is_calibrated_and_cheaters_stand_out(cfg, wd):
    """E[LR] <= ~1 for every honest style (strategy-agnostic); cheaters score clearly higher."""
    scorer = Scorer(wd, cfg)
    means = {}
    for strat in ("honest_narrowing", "honest_elimination", "honest_hybrid", "cheat_build_up", "cheat_near_miss"):
        llrs = [scorer.score(0, a, rows, samples=32).log_lr_charitable for a, rows in _games(wd, strat, 40, 5)]
        means[strat] = (np.mean(np.exp(llrs)), np.mean(llrs))
    for s in ("honest_narrowing", "honest_elimination", "honest_hybrid"):
        assert means[s][0] < 1.1, means
        assert means[s][1] < 0, means
    for s in ("cheat_build_up", "cheat_near_miss"):
        assert means[s][1] > 0.3, means


def test_row_features(cfg, wd):
    scorer = Scorer(wd, cfg)
    gs = scorer.score(0, "night", ["BGGGG", "BGGGG", "GGGGG"], samples=32)
    f = gs.to_features()
    assert f["row1_greens"] == 4
    assert f["traps"] and f["traps"][0]["escaped"] is False
    assert f["traps"][1]["escaped"] is True
    assert all(r["kind"] == "N" for r in f["rows"])
    gs2 = scorer.score(0, "night", ["BGGGG", "BBBBB", "GGGGG"], samples=32)
    assert gs2.rows[1].kind == "E"


def test_inconsistent_grid_raises(cfg, wd):
    with pytest.raises(InconsistentGrid):
        # 'night' has no repeated letters, so YYYYY-with-four-yellows + green... impossible pattern
        Scorer(wd, cfg).score(0, "night", ["GGGGY", "GGGGG"], samples=8)


def test_fallback_is_weak(cfg, wd):
    s = Scorer(wd, cfg).score_fallback(0, 2)
    assert s.fallback and 0 < s.log_lr_charitable < 1.0


def test_posteriors():
    assert daily_posterior(0.0, 0.05) == pytest.approx(0.05)
    assert daily_posterior(math.log(19), 0.05) == pytest.approx(0.5)
    assert longterm_posterior([], 0.05, 0.5) == pytest.approx(0.05)
    assert longterm_posterior([-2.0] * 50, 0.05, 0.5) < 0.05
    assert longterm_posterior([3.0] * 10, 0.05, 0.5) > 0.9


def test_bounty_amounts():
    assert bounty_amount(0.50) == 50
    assert bounty_amount(0.99) == 10000
    assert bounty_amount(1.0) == 10000
    for p in np.linspace(0.5, 0.99, 30):
        a = bounty_amount(p)
        assert a % (5 if a < 100 else 50 if a < 1000 else 500) == 0


def test_cusum_flags_onset():
    llrs = [-0.5] * 20 + [1.5] * 6
    days = list(range(100, 126))
    cp = cusum(llrs, days, threshold=4.0)
    assert cp and cp["onset_day"] == 120
    assert cusum([-0.5] * 30, list(range(30)), 4.0) is None
