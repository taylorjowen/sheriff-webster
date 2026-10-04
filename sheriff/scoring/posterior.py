"""From likelihood ratios to probabilities (§7.7) and changepoints (§7.8).

Daily:     P(cheated this game) = prior*LR / (prior*LR + 1 - prior)
Long-term: H1 "cheater" cheats each game independently with rate rho; H0 never cheats.
           L1/L0 = prod_g (rho*LR_g + 1 - rho).
Under honest play E[LR_g] <= 1 (charitable scores are conservative), so the long-term
ratio is a nonnegative supermartingale: by Ville's inequality an honest player's
posterior ever exceeding 50% has probability <= prior/(1-prior), and in practice far less.
"""
from __future__ import annotations

import math


def _sigmoid_from_logodds(lo: float) -> float:
    if lo >= 0:
        return 1.0 / (1.0 + math.exp(-lo))
    e = math.exp(lo)
    return e / (1.0 + e)


def daily_posterior(log_lr: float, prior: float) -> float:
    return _sigmoid_from_logodds(math.log(prior / (1 - prior)) + log_lr)


def longterm_log_ratio(log_lrs: list[float], rho: float) -> float:
    total = 0.0
    for llr in log_lrs:
        # log(rho*e^llr + 1 - rho), computed stably
        if llr > 0:
            total += llr + math.log(rho + (1 - rho) * math.exp(-llr))
        else:
            total += math.log(rho * math.exp(llr) + (1 - rho))
    return total


def longterm_posterior(log_lrs: list[float], prior: float, rho: float) -> float:
    return _sigmoid_from_logodds(math.log(prior / (1 - prior)) + longterm_log_ratio(log_lrs, rho))


def cusum(log_lrs: list[float], days: list[int], threshold: float, drift: float = 0.0) -> dict | None:
    """One-sided CUSUM on per-game log-LR. Returns the onset of the first alarm, if any."""
    s, start = 0.0, None
    for llr, day in zip(log_lrs, days):
        prev = s
        s = max(0.0, s + llr - drift)
        if prev == 0.0 and s > 0.0:
            start = day
        if s == 0.0:
            start = None
        if s >= threshold:
            return {"onset_day": start if start is not None else day, "alarm_day": day, "statistic": s}
    return None


def bounty_amount(p: float) -> int:
    """Log-scaled bounty: 50% -> $50, 99% -> $10,000, rounded to $5/$50/$500 steps."""
    t = min(1.0, max(0.0, (p - 0.50) / 0.49))
    amount = 50 * (10000 / 50) ** t
    step = 5 if amount < 100 else 50 if amount < 1000 else 500
    return int(max(step, round(amount / step) * step))
