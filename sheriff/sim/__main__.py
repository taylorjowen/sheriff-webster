"""Headless scenario runner (§9.6).

    python -m sheriff.sim scenarios/build_up_cheater_vs_honest.yaml [--text-only] [--report]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from ..config import Config
from .scenario import Scenario, ScenarioRun, find_scenario


async def run(args) -> int:
    overrides = {"env": "test"}
    if args.samples:
        overrides["scoring"] = {"samples": args.samples}
    cfg = Config.load(args.config, overrides=overrides)
    scenario = Scenario.load(find_scenario(cfg, args.scenario))
    run = ScenarioRun(cfg, scenario, mode="text" if args.text_only else "image",
                      store_path=args.keep or ":memory:")
    t0 = time.time()
    await run.run_all()
    elapsed = time.time() - t0
    print(f"scenario {scenario.name}: {scenario.days} days, {len(scenario.players)} players, {elapsed:.1f}s")
    for line in run.status_lines():
        print(line)
    if args.report:
        print("\nlong-term posterior by day:")
        days = sorted(next(iter(run.history.values())).keys())
        step = max(1, len(days) // 15)
        print("day   " + " ".join(f"{p.name[:12]:>12s}" for p in scenario.players))
        for d in days[::step] + ([days[-1]] if days[-1] not in days[::step] else []):
            print(f"{d:<5d} " + " ".join(f"{run.history[p.name][d]:12.3f}" for p in scenario.players))
        posts = run.sink.posts() if hasattr(run.sink, "posts") else []
        print(f"\n{len(posts)} posts recorded; confessions: {len(run.store.confessions())}")
    results = run.check_expectations()
    failed = 0
    if results:
        print("\nexpectations:")
        for ok, msg in results:
            print(("  PASS " if ok else "  FAIL ") + msg)
            failed += not ok
    run.close(keep=bool(args.keep))
    return 1 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scenario")
    ap.add_argument("--text-only", action="store_true", help="skip image rendering (faster)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--samples", type=int, help="Monte Carlo samples per game")
    ap.add_argument("--keep", metavar="DB_PATH", help="write the run DB here and keep it")
    ap.add_argument("--config")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
