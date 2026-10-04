"""Scenarios (§9.3) and the test-mode day loop (§9.4).

A ScenarioRun owns one isolated environment: its own store, clock, synthetic players,
answers and sink. Each day it simulates plays, renders fake Wordle-app messages, and
pushes them through the *real* ingestion path, which triggers the normal daily cycle.
"""
from __future__ import annotations

import secrets
import zlib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from ..answers import AnswerCache, TestAnswerProvider
from ..config import Config
from ..game import SheriffGame
from ..identity import SyntheticIdentityResolver
from ..patterns import WordData, shared_word_data
from ..scoring.engine import Scorer
from ..seams import OutputSink, RecapEnvelope, TestClock
from ..store import open_store
from .bots import CHEATS, HONEST, POPULAR_OPENERS, Simulator
from .recorder import HeadlessSink
from .renderer import (CardSpec, playing_text, recap_text, render_playing_image, render_recap_image)


@dataclass
class PlayerSpec:
    name: str
    strategy: str = "honest_hybrid"
    skill: float = 0.6
    cheat_rate: float = 1.0
    start_cheating_day: int | None = None
    rename_on_day: int | None = None
    new_name: str | None = None
    post_rate: float = 1.0
    opener: str | None = None
    # runtime
    pid: int = 0
    mention_id: str = ""
    current_name: str = ""


@dataclass
class Scenario:
    name: str
    seed: int = 0
    start_day: int = 1900
    days: int = 30
    answers: Any = "real"
    players: list[PlayerSpec] = field(default_factory=list)
    expect: list[dict] = field(default_factory=list)
    plain_name_rate: float = 0.3
    playing_messages: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "Scenario":
        players = [PlayerSpec(**p) for p in d.get("players", [])]
        for p in players:
            if p.strategy not in HONEST + CHEATS:
                raise ValueError(f"unknown strategy {p.strategy!r} for {p.name}")
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k != "players"}
        return cls(players=players, **known)

    @classmethod
    def load(cls, path: str | Path) -> "Scenario":
        return cls.from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def find_scenario(cfg: Config, name: str) -> Path:
    p = Path(name)
    if p.exists():
        return p
    d = cfg.path("scenario_dir")
    for cand in (d / name, d / f"{name}.yaml", d / f"{name}.yml"):
        if cand.exists():
            return cand
    raise FileNotFoundError(f"scenario {name!r} not found in {d}")


class ScenarioRun:
    def __init__(self, cfg: Config, scenario: Scenario, sink: OutputSink | None = None,
                 store_path: str | Path = ":memory:", mode: str = "image", run_id: str | None = None,
                 wd: WordData | None = None):
        self.cfg = cfg
        self.scenario = scenario
        self.mode = mode
        self.run_id = run_id or secrets.token_hex(3)
        self.store_path = store_path
        self.store = open_store(store_path, env="test", live_db_path=cfg.path("live_db"))
        self.store.set_meta("env", "test")
        self.store.set_meta("run_id", self.run_id)
        self.store.set_meta("scenario", scenario.name)
        self.clock = TestClock(scenario.start_day)
        self.sink = sink or HeadlessSink()
        self.resolver = SyntheticIdentityResolver(self.store)
        self.wd = wd or shared_word_data(cfg.path("cache_dir"))
        cache = AnswerCache(cfg.path("answers_db"), read_only=True)
        explicit = {}
        if isinstance(scenario.answers, list):
            explicit = {scenario.start_day + i: w.lower() for i, w in enumerate(scenario.answers)}
        self.answers = TestAnswerProvider(cache if scenario.answers == "real" or explicit else cache, explicit,
                                          fallback_words=self.wd.candidates[:len(self.wd.curated)],
                                          seed=scenario.seed)
        self.scorer = Scorer(self.wd, cfg)
        self.game = SheriffGame(cfg, self.store, self.answers, self.clock, self.sink, self.resolver, self.scorer,
                                env="test", run_id=self.run_id, archive_dir=None, seed=scenario.seed)
        self.sim = Simulator(self.wd)
        self.history: dict[str, dict[int, float]] = {}
        self.msg_counter = 0
        now = self.clock.now()
        for i, p in enumerate(scenario.players):
            p.mention_id = str(i + 1)          # placeholder ids; real snowflakes are ~1e17+
            p.current_name = p.name
            p.pid = self.resolver.add_player(p.name, p.mention_id, now)
            self.history[p.name] = {}
            if p.opener is None:
                p.opener = POPULAR_OPENERS[zlib.crc32(f"{scenario.seed}:{p.name}".encode()) % len(POPULAR_OPENERS)]

    def _rng(self, *parts: Any) -> np.random.Generator:
        return np.random.default_rng(zlib.crc32(":".join(map(str, (self.scenario.seed,) + parts)).encode()))

    def _next_id(self) -> str:
        self.msg_counter += 1
        return f"{self.run_id}-{self.msg_counter}"

    @property
    def day(self) -> int:
        return self.clock.today_day()

    def player(self, name: str) -> PlayerSpec | None:
        key = name.casefold()
        return next((p for p in self.scenario.players if key in (p.name.casefold(), p.current_name.casefold())), None)

    def play_day(self, day: int) -> list[CardSpec]:
        answer = self.answers.get_sync(day)
        cards = []
        for i, p in enumerate(self.scenario.players):
            rng = self._rng(p.name, day)
            if p.rename_on_day is not None and day >= p.rename_on_day and p.current_name == p.name:
                p.current_name = p.new_name or f"{p.name} the Second"
                self.resolver.rename(p.pid, p.current_name)
            if rng.random() >= p.post_rate:
                continue
            strategy = p.strategy
            if strategy in CHEATS:
                cheating = (p.start_cheating_day is None or day >= p.start_cheating_day) and rng.random() < p.cheat_rate
                if not cheating:
                    strategy = "honest_hybrid"
            rows = self.sim.play(strategy, answer, rng, skill=p.skill, opener=p.opener)
            outcome = str(len(rows)) if rows[-1] == "GGGGG" else "X"
            plain = rng.random() < self.scenario.plain_name_rate
            cards.append(CardSpec(p.current_name, None if plain else p.mention_id, outcome, rows, avatar=i))
        return cards

    def recap_envelope(self, day: int, cards: list[CardSpec]) -> RecapEnvelope:
        text = recap_text(cards, streak=day - self.scenario.start_day + 1)
        env = RecapEnvelope(kind="recap", message_id=self._next_id(), channel_id="sandbox", author_id="wordle-bot",
                            text=text, timestamp=self.clock.now())
        if self.mode == "text":
            from .renderer import order_cards
            env.synthetic_grids = [c.rows for c in order_cards(cards)]
            env.synthetic_day = day
        else:
            env.images = [render_recap_image(day, cards, self.cfg)]
        return env

    async def _emit_playing(self, day: int, cards: list[CardSpec]) -> None:
        for c in cards:
            mid = self._next_id()
            base = self.clock.now()
            for k in range(1, len(c.rows) + 1):
                partial = CardSpec(c.name, c.mention, c.outcome, c.rows[:k], c.avatar)
                env = RecapEnvelope(kind="playing", message_id=mid, channel_id="sandbox", author_id="wordle-bot",
                                    text=playing_text(c.name), timestamp=base,
                                    edited_at=base + timedelta(seconds=30 * k), edit_seq=k)
                if self.mode == "text":
                    env.synthetic_grids = [partial.rows]
                    env.synthetic_day = day
                else:
                    env.images = [render_playing_image(day, partial, self.cfg)]
                await self.game.ingest(env)

    async def advance(self, days: int = 1, post: bool = True, before_ingest=None) -> list[int]:
        """Simulate `days` days. `before_ingest(day, envelope)` may observe each fake recap."""
        done = []
        for _ in range(days):
            day = self.day
            cards = self.play_day(day)
            if self.scenario.playing_messages and cards:
                await self._emit_playing(day, cards)
            self.clock.advance_days(1)   # the recap arrives the next morning
            if cards:
                env = self.recap_envelope(day, cards)
                if before_ingest is not None:
                    await before_ingest(day, env)
                await self.game.ingest(env, post=post)
            self._record(day)
            done.append(day)
        return done

    async def inject(self, text: str, image: bytes | None) -> Any:
        env = RecapEnvelope(kind="recap", message_id=self._next_id(), channel_id="sandbox", author_id="wordle-bot",
                            text=text, timestamp=self.clock.now(), images=[image] if image else [])
        return await self.game.ingest(env)

    def _record(self, day: int) -> None:
        for p in self.scenario.players:
            self.history[p.name][day] = self.store.state(p.pid)["longterm_posterior"] or float(self.cfg.get("model.prior"))

    async def run_all(self) -> None:
        remaining = self.scenario.days - (self.day - self.scenario.start_day)
        if remaining > 0:
            await self.advance(remaining)

    def check_expectations(self) -> list[tuple[bool, str]]:
        out = []
        for e in self.scenario.expect:
            name = e["player"]
            hist = self.history.get(name, {})
            if not hist:
                out.append((False, f"{name}: no history"))
                continue
            by = e.get("by_day")
            days = sorted(d for d in hist if by is None or d <= by)
            if not days:
                out.append((False, f"{name}: no data by day {by}"))
                continue
            val = hist[days[-1]]
            if "longterm_posterior_gt" in e:
                ok = val > e["longterm_posterior_gt"]
                out.append((ok, f"{name}: posterior {val:.3f} > {e['longterm_posterior_gt']} at day {days[-1]}"))
            if "longterm_posterior_lt" in e:
                ok = val < e["longterm_posterior_lt"]
                out.append((ok, f"{name}: posterior {val:.3f} < {e['longterm_posterior_lt']} at day {days[-1]}"))
        return out

    def status_lines(self) -> list[str]:
        lines = [f"run `{self.run_id}` · scenario `{self.scenario.name}` · day {self.day}"]
        for p in self.scenario.players:
            st = self.store.state(p.pid)
            b = f" · bounty ${st['bounty_amount']:,} as {st['alias']}" if st["bounty_amount"] else ""
            lines.append(f"- {p.current_name} ({p.strategy}): {st['longterm_posterior'] or 0:.1%}{b}")
        errs = self.store.count_events("parse_error") + self.store.count_events("card_error")
        pend = len(self.store.pending())
        lines.append(f"parse errors: {errs} · pending identities: {pend} · "
                     f"fallback scores: {self.store.count_events('fallback_scorer')}")
        return lines

    def close(self, keep: bool = False) -> None:
        self.store.close()
        if not keep and str(self.store_path) != ":memory:":
            for suffix in ("", "-wal", "-shm"):
                Path(str(self.store_path) + suffix).unlink(missing_ok=True)
