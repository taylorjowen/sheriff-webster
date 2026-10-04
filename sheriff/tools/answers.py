"""Answer cache maintenance.

    python -m sheriff.tools.answers fetch --from 1700 --to 1935   # fill from the NYT endpoint
    python -m sheriff.tools.answers import answers.csv            # lines of day,word or YYYY-MM-DD,word
    python -m sheriff.tools.answers show 1924
"""
from __future__ import annotations

import argparse
import asyncio

import aiohttp

from ..answers import AnswerCache, fetch_nyt
from ..config import Config
from ..days import date_to_day
from datetime import date


async def fetch_range(cache: AnswerCache, start: int, end: int) -> int:
    n = 0
    async with aiohttp.ClientSession() as session:
        for day in range(start, end + 1):
            if cache.get(day):
                continue
            word = await fetch_nyt(day, session)
            if word:
                cache.put(day, word)
                n += 1
            await asyncio.sleep(0.15)
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--from", dest="start", type=int, default=None)
    f.add_argument("--to", dest="end", type=int, default=None)
    i = sub.add_parser("import")
    i.add_argument("path")
    s = sub.add_parser("show")
    s.add_argument("day", type=int)
    a = ap.parse_args()
    cfg = Config.load()
    cache = AnswerCache(cfg.path("answers_db"))
    if a.cmd == "fetch":
        today = date_to_day(date.today())
        end = a.end if a.end is not None else today
        start = a.start if a.start is not None else end - 120
        print(f"fetched {asyncio.run(fetch_range(cache, start, end))} new answers for days {start}-{end}")
    elif a.cmd == "import":
        print(f"imported {cache.import_file(a.path)} answers")
    else:
        print(cache.get(a.day))


if __name__ == "__main__":
    main()
