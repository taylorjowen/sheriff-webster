"""Answer table: NYT endpoint + a local cache kept forever (§5.4).

The cache is a separate SQLite DB shared by every environment. Test runs open it
read-only (§9.1).
"""
from __future__ import annotations

import logging
import random
import sqlite3
from pathlib import Path

import aiohttp

from .days import date_to_day, day_to_date
from .seams import AnswerProvider

log = logging.getLogger(__name__)

NYT_URL = "https://www.nytimes.com/svc/wordle/v2/{date}.json"


class AnswerCache:
    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self.db: sqlite3.Connection | None = None
        if read_only:
            if self.path.exists():
                self.db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(self.path), check_same_thread=False)
            self.db.execute("CREATE TABLE IF NOT EXISTS answers(day INTEGER PRIMARY KEY, word TEXT NOT NULL)")
            self.db.commit()

    def get(self, day: int) -> str | None:
        if self.db is None:
            return None
        row = self.db.execute("SELECT word FROM answers WHERE day=?", (day,)).fetchone()
        return row[0] if row else None

    def put(self, day: int, word: str) -> None:
        if self.read_only or self.db is None:
            raise PermissionError("answer cache is read-only in this environment")
        self.db.execute("INSERT OR REPLACE INTO answers(day, word) VALUES(?,?)", (day, word.lower()))
        self.db.commit()

    def all(self) -> dict[int, str]:
        if self.db is None:
            return {}
        return {d: w for d, w in self.db.execute("SELECT day, word FROM answers")}

    def import_file(self, path: str | Path) -> int:
        """Lines of `day,word` or `YYYY-MM-DD,word`."""
        from datetime import date
        n = 0
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, word = [p.strip() for p in line.replace("\t", ",").split(",")[:2]]
            day = date_to_day(date.fromisoformat(key)) if "-" in key else int(key)
            self.put(day, word)
            n += 1
        return n


async def fetch_nyt(day: int, session: aiohttp.ClientSession | None = None) -> str | None:
    url = NYT_URL.format(date=day_to_date(day).isoformat())
    own = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                log.warning("NYT answer fetch %s -> HTTP %s", url, resp.status)
                return None
            data = await resp.json(content_type=None)
            word = (data.get("solution") or "").strip().lower()
            return word if len(word) == 5 else None
    except Exception as e:  # network errors are expected occasionally
        log.warning("NYT answer fetch failed for day %s: %s", day, e)
        return None
    finally:
        if own:
            await session.close()


class LiveAnswerProvider(AnswerProvider):
    def __init__(self, cache: AnswerCache):
        self.cache = cache

    async def get(self, day: int) -> str | None:
        word = self.cache.get(day)
        if word:
            return word
        word = await fetch_nyt(day)
        if word:
            self.cache.put(day, word)
        return word

    def known_answers(self) -> list[str]:
        return list(self.cache.all().values())


class TestAnswerProvider(AnswerProvider):
    """Scenario answers: explicit list, else the read-only cache, else seeded random words."""
    __test__ = False

    def __init__(self, cache: AnswerCache | None, explicit: dict[int, str] | None = None,
                 fallback_words: list[str] | None = None, seed: int = 0):
        self.cache = cache
        self.explicit = explicit or {}
        self.fallback_words = fallback_words or []
        self.seed = seed

    async def get(self, day: int) -> str | None:
        return self.get_sync(day)

    def get_sync(self, day: int) -> str | None:
        if day in self.explicit:
            return self.explicit[day]
        if self.cache is not None:
            w = self.cache.get(day)
            if w:
                return w
        if self.fallback_words:
            return random.Random(f"{self.seed}:{day}").choice(self.fallback_words)
        return None

    def known_answers(self) -> list[str]:
        return list(self.explicit.values())
