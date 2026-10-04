"""Interactive test mode (§9.5): isolated scenario runs bound to sandbox channels."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field

import discord

from ..config import Config
from ..seams import OutMessage
from ..sim.scenario import Scenario, ScenarioRun, find_scenario
from ..store import IsolationError
from .discord_io import DiscordSink


@dataclass
class ActiveRun:
    run: ScenarioRun
    channel_id: int
    act_as: dict[int, int] = field(default_factory=dict)   # tester discord id -> synthetic player id


class TestRunManager:
    __test__ = False

    def __init__(self, client: discord.Client, cfg: Config):
        self.client = client
        self.cfg = cfg
        self.runs: dict[str, ActiveRun] = {}
        self.by_channel: dict[int, str] = {}

    def sandbox_ids(self) -> set[int]:
        return {int(x) for x in self.cfg.get("discord.sandbox_channel_ids") or []}

    def require_sandbox(self, channel_id: int) -> None:
        if channel_id not in self.sandbox_ids():
            raise IsolationError("test commands only work in sandbox channels "
                                 "(`/sheriff config here role:sandbox` to make this one)")

    def run_for_channel(self, channel_id: int) -> ActiveRun | None:
        rid = self.by_channel.get(channel_id)
        return self.runs.get(rid) if rid else None

    async def start(self, channel_id: int, scenario_name: str, text_only: bool) -> ActiveRun:
        self.require_sandbox(channel_id)
        if channel_id in self.by_channel:
            raise ValueError(f"run `{self.by_channel[channel_id]}` is already active here; `/sheriff test end` first")
        scenario = Scenario.load(find_scenario(self.cfg, scenario_name))
        run_id = secrets.token_hex(3)
        sink = DiscordSink(self.client, channel_id, env="test", sandbox_ids=self.sandbox_ids())
        db = self.cfg.path("test_dir") / f"{run_id}.db"
        run = ScenarioRun(self.cfg.with_overrides({"env": "test"}), scenario, sink=sink, store_path=db,
                          mode="text" if text_only else "image", run_id=run_id)
        active = ActiveRun(run, channel_id)
        self.runs[run_id] = active
        self.by_channel[channel_id] = run_id
        names = ", ".join(f"**{p.name}** ({p.strategy})" for p in scenario.players)
        await sink.post(OutMessage(content=f"[TEST RUN {run_id}] \U0001F9EA Started scenario `{scenario.name}` "
                                           f"at Wordle {scenario.start_day}. Players: {names}.\n"
                                           "Use `/sheriff test act-as` to click buttons as one of them."))
        return active

    async def advance(self, channel_id: int, days: int) -> list[int]:
        a = self._get(channel_id)

        async def show(day: int, env) -> None:
            if days <= 3 and env.images:   # echo the fake recap so testers can see what was parsed
                await a.run.sink.post(OutMessage(content=f"[TEST RUN {a.run.run_id}] Fake recap:\n{env.text}",
                                                 image=env.images[0], image_name=f"wordle_{day}.png"))

        return await a.run.advance(days, before_ingest=show)

    async def inject(self, channel_id: int, text: str, image: bytes | None) -> str:
        a = self._get(channel_id)
        res = await a.run.inject(text, image)
        return f"{'ok' if res.ok else 'rejected'}: day {res.day} {res.detail}"

    def act_as(self, channel_id: int, tester_id: int, name: str) -> str:
        a = self._get(channel_id)
        p = a.run.player(name)
        if p is None:
            raise ValueError(f"no player named {name!r} in this run")
        a.act_as[tester_id] = p.pid
        return p.current_name

    def player_for(self, run_id: str, tester_id: int) -> int | None:
        a = self.runs.get(run_id)
        return a.act_as.get(tester_id) if a else None

    def status(self, channel_id: int) -> str:
        return "\n".join(self._get(channel_id).run.status_lines())

    async def end(self, channel_id: int, keep: bool, delete_messages: bool) -> str:
        a = self._get(channel_id)
        rid = a.run.run_id
        if delete_messages:
            for row in a.run.store.bot_messages():
                await a.run.sink.delete(row["message_id"])
        a.run.close(keep=keep)
        del self.runs[rid]
        del self.by_channel[channel_id]
        return rid

    def _get(self, channel_id: int) -> ActiveRun:
        a = self.run_for_channel(channel_id)
        if a is None:
            raise ValueError("no active test run in this channel; `/sheriff test start` first")
        return a
