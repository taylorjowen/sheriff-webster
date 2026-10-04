"""Sheriff Webster Discord bot: live listeners, slash commands, persistent buttons."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import discord
import yaml
from discord import app_commands

from ..answers import AnswerCache, LiveAnswerProvider
from ..config import Config
from ..game import SheriffGame
from ..identity import LiveIdentityResolver
from ..parsing.text import SHARE_HEADER, classify_wordle_bot_text
from ..patterns import WordData
from ..scoring.engine import Scorer
from ..seams import OutMessage, RealClock, RecapEnvelope
from ..store import IsolationError, Store, open_store
from .discord_io import DiscordSink, envelope_from_message, message_kwargs
from .testmode import TestRunManager

log = logging.getLogger(__name__)

# Settings an admin may change at runtime with `/sheriff config set` (persisted in the live DB).
SETTABLE = {
    "model.prior", "model.cheat_rate", "model.row_epsilon", "model.row_beta", "bounty.threshold",
    "scoring.samples", "image.tolerance", "image.digit_match_threshold", "cusum.threshold",
    "discord.wordle_bot_ids", "discord.ingest_channel_ids", "discord.board_channel_id",
    "discord.sandbox_channel_ids", "discord.admin_user_ids",
}


async def reply(interaction: discord.Interaction, msg: OutMessage | str, ephemeral: bool = True) -> None:
    if isinstance(msg, str):
        msg = OutMessage(content=msg, ephemeral=ephemeral)
    kw = message_kwargs(msg)
    if kw.get("view") is discord.utils.MISSING:
        kw.pop("view")
    if interaction.response.is_done():
        await interaction.followup.send(ephemeral=msg.ephemeral, **kw)
    else:
        await interaction.response.send_message(ephemeral=msg.ephemeral, **kw)


class SheriffBot(discord.Client):
    def __init__(self, cfg: Config):
        intents = discord.Intents.default()
        intents.message_content = True   # privileged: needed to read recap text
        intents.members = True           # privileged: needed to resolve plain-text names
        super().__init__(intents=intents)
        self.cfg = cfg
        self.env = cfg.get("env", "live")
        self.tree = app_commands.CommandTree(self)
        self.tests = TestRunManager(self, cfg)
        self.session: aiohttp.ClientSession | None = None
        self.store: Store | None = None
        self.game: SheriffGame | None = None
        self.resolver: LiveIdentityResolver | None = None
        self.wd: WordData | None = None
        self._edit_seq: dict[int, int] = {}
        build_commands(self)

    # ------------------------------------------------------------ setup
    @property
    def live(self) -> bool:
        return self.env == "live"

    def guild(self) -> discord.Guild | None:
        gid = self.cfg.get("discord.guild_id")
        if gid:
            return self.get_guild(int(gid))
        return self.guilds[0] if self.guilds else None

    def ids(self, key: str) -> set[int]:
        return {int(x) for x in (self.cfg.get(f"discord.{key}") or [])}

    def board_channel_id(self) -> int | None:
        b = self.cfg.get("discord.board_channel_id")
        if b:
            return int(b)
        ingest = self.cfg.get("discord.ingest_channel_ids") or []
        return int(ingest[0]) if ingest else None

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession()
        from ..patterns import shared_word_data
        self.wd = shared_word_data(self.cfg.path("cache_dir"))
        if self.live:
            self.store = open_store(self.cfg.path("live_db"), "live", self.cfg.path("live_db"))
            for k, v in self.store.settings().items():   # runtime overrides from /sheriff config
                self.cfg.set(k, v)
            self.tests.cfg = self.cfg
            cache = AnswerCache(self.cfg.path("answers_db"))
            for w in cache.all().values():
                self.wd.ensure_answer(w)
            self.resolver = LiveIdentityResolver(self.store, self.guild)
            self._build_live_game()
        gid = self.cfg.get("discord.guild_id")
        if gid:
            guild = discord.Object(id=int(gid))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    def _build_live_game(self) -> None:
        bid = self.board_channel_id()
        sink = _LazySink(self, bid)
        self.game = SheriffGame(self.cfg, self.store, LiveAnswerProvider(AnswerCache(self.cfg.path("answers_db"))),
                                RealClock(), sink, self.resolver, Scorer(self.wd, self.cfg), env="live",
                                archive_dir=self.cfg.path("archive_dir"))

    async def close(self) -> None:
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Sheriff Webster riding as %s (env=%s) in %d guild(s)", self.user, self.env, len(self.guilds))
        if self.live and self.store:
            g = self.guild()
            if g:
                now = datetime.now(timezone.utc)
                for m in g.members:
                    self.resolver.record_member(m, now)

    # ------------------------------------------------------------ live listeners
    def _ingest_channel(self, channel_id: int) -> bool:
        return self.live and channel_id in self.ids("ingest_channel_ids") and channel_id not in self.ids("sandbox_channel_ids")

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if self.live and self.resolver:
            self.resolver.record_member(after, datetime.now(timezone.utc))

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user or not self._ingest_channel(message.channel.id):
            return
        await self._ingest_message(message)

    async def _ingest_message(self, message: discord.Message, backfill: bool = False) -> str | None:
        assert self.game and self.session
        if message.author.id in self.ids("wordle_bot_ids"):
            text = message.content or " ".join(filter(None, [e.description for e in message.embeds]))
            kind = classify_wordle_bot_text(text)
            if kind is None:
                return None
            env = await envelope_from_message(message, kind, self.session)
            await self.game.ingest(env, run_cycle=not backfill)
            return kind
        if SHARE_HEADER.search(message.content or ""):
            env = await envelope_from_message(message, "share", self.session, with_images=False)
            await self.game.ingest(env)
            return "share"
        return None

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if not self._ingest_channel(payload.channel_id):
            return
        author = (payload.data.get("author") or {}).get("id")
        if author is None or (int(author) not in self.ids("wordle_bot_ids") and "content" not in payload.data):
            return
        message = payload.message   # the updated message
        if not message.attachments and not message.embeds:
            try:   # partial update payloads may lack attachments; fetch the full message
                message = await self.get_channel(payload.channel_id).fetch_message(payload.message_id)  # type: ignore[union-attr]
            except discord.HTTPException:
                return
        if message.author.id in self.ids("wordle_bot_ids"):
            kind = classify_wordle_bot_text(message.content or "")
            if kind == "recap":
                prior = self.store.recap(str(message.id))
                if prior is None or not prior["parsed_ok"]:   # e.g. the image arrived in a later edit
                    await self.game.ingest(await envelope_from_message(message, "recap", self.session))
                return
            if kind != "playing":
                return
            seq = self._edit_seq[message.id] = self._edit_seq.get(message.id, 0) + 1
            env = await envelope_from_message(message, "playing", self.session, edit_seq=seq)
            await self.game.ingest(env)
        elif SHARE_HEADER.search(message.content or ""):
            await self.game.ingest(await envelope_from_message(message, "share", self.session, with_images=False))

    # ------------------------------------------------------------ buttons
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("sheriff:"):
            return
        parts = cid.split(":")
        envkey, action = parts[1], parts[2]
        arg = parts[3] if len(parts) > 3 else None
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if envkey == "live":
                if not self.game:
                    return await reply(interaction, "Live mode isn't running.")
                row = self.store.player_by_discord(str(interaction.user.id))
                if row is None:
                    if action == "amibounty":
                        from ..content import CLEAN
                        return await reply(interaction, f"✅ {CLEAN}")
                    return await reply(interaction, "You haven't ridden with us yet, stranger.")
                msg = await self.game.handle_component(action, arg, row["player_id"])
            else:
                active = self.tests.runs.get(envkey)
                if active is None:
                    return await reply(interaction, "That test run has ended.")
                pid = active.act_as.get(interaction.user.id)
                if pid is None:
                    return await reply(interaction, "Use `/sheriff test act-as <player>` first.")
                msg = await active.run.game.handle_component(action, arg, pid)
            await reply(interaction, msg)
        except Exception as e:
            log.exception("button %s failed", cid)
            await reply(interaction, f"The Sheriff tripped over his spurs: {e}")

    # ------------------------------------------------------------ helpers for commands
    def is_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.ids("admin_user_ids"):
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and (perms.manage_guild or perms.administrator))

    def set_setting(self, key: str, value) -> None:
        self.cfg.set(key, value)
        if self.store:
            self.store.set_setting(key, value)
        self.tests.cfg = self.cfg
        if key == "discord.board_channel_id" or key == "discord.ingest_channel_ids":
            if self.game:
                self.game.sink = _LazySink(self, self.board_channel_id())

    async def backfill(self, progress) -> dict:
        assert self.game
        counts = {"recap": 0, "playing": 0, "share": 0, "messages": 0}
        for cid in self.ids("ingest_channel_ids"):
            channel = self.get_channel(cid) or await self.fetch_channel(cid)
            async for m in channel.history(limit=None, oldest_first=True):  # type: ignore[union-attr]
                counts["messages"] += 1
                kind = await self._ingest_message(m, backfill=True)
                if kind:
                    counts[kind] += 1
                if counts["messages"] % 500 == 0:
                    await progress(counts)
        await self._finish_bulk()
        return counts

    async def reprocess(self) -> int:
        """Re-ingest every archived message (after parser/glyph/color fixes)."""
        assert self.game
        archive = self.cfg.path("archive_dir")
        metas = []
        for f in archive.glob("*.json"):
            try:
                metas.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
        metas.sort(key=lambda m: (m.get("edited_at") or m["timestamp"]))
        latest: dict[str, dict] = {}
        for m in metas:   # only the final state of each playing message
            latest[m["message_id"] if m["kind"] != "recap" else m["message_id"] + "r"] = m
        n = 0
        old_archive, self.game.archive_dir = self.game.archive_dir, None   # don't re-archive
        try:
            for m in sorted(latest.values(), key=lambda m: m["timestamp"]):
                env = RecapEnvelope(kind=m["kind"], message_id=m["message_id"], channel_id=m["channel_id"],
                                    author_id=m["author_id"], text=m["text"],
                                    timestamp=datetime.fromisoformat(m["timestamp"]),
                                    edited_at=datetime.fromisoformat(m["edited_at"]) if m.get("edited_at") else None,
                                    images=[(archive / name).read_bytes() for name in m.get("images", [])
                                            if (archive / name).exists()])
                await self.game.ingest(env, run_cycle=False)
                n += 1
        finally:
            self.game.archive_dir = old_archive
        await self._finish_bulk()
        return n

    async def _finish_bulk(self) -> None:
        recap_days = [r["day"] for r in self.store.recaps("recap") if r["parsed_ok"] and r["day"] is not None]
        if recap_days:
            await self.game.run_daily_cycle(max(recap_days), post=True)


class _LazySink(DiscordSink):
    """Live sink whose channel may be configured after startup."""

    def __init__(self, client: SheriffBot, channel_id: int | None):
        self.client = client
        self.channel_id = channel_id or 0
        self.env = "live"

    async def _channel(self):
        if not self.channel_id:
            raise RuntimeError("no board channel configured (`/sheriff config here role:board`)")
        if self.channel_id in self.client.ids("sandbox_channel_ids"):
            raise IsolationError("live mode may not post to a sandbox channel")
        return await super()._channel()


# ====================================================================== commands

def build_commands(bot: SheriffBot) -> None:
    sheriff = app_commands.Group(name="sheriff", description="Sheriff Webster, keeper of the Wordle peace")
    config = app_commands.Group(name="config", description="Admin: Sheriff settings", parent=sheriff)
    test = app_commands.Group(name="test", description="Admin: sandbox test runs", parent=sheriff)

    async def admin_only(interaction: discord.Interaction) -> bool:
        if not bot.is_admin(interaction):
            await reply(interaction, "Only the town council (server admins) can do that.")
            return False
        return True

    def live_or_fail():
        if not bot.game:
            raise app_commands.AppCommandError("Live mode isn't running (ENV=test).")

    # -------------------------------------------------------------- live commands
    @sheriff.command(name="backfill", description="Admin: ingest the whole Wordle channel history")
    async def backfill(interaction: discord.Interaction):
        if not await admin_only(interaction):
            return
        live_or_fail()
        await interaction.response.defer(ephemeral=True, thinking=True)

        async def progress(c):
            await interaction.edit_original_response(content=f"Riding through history... {c}")

        counts = await bot.backfill(progress)
        errs = bot.store.count_events("parse_error")
        await interaction.followup.send(f"Backfill done: {counts}. Recap parse errors so far: {errs}. "
                                        "Check `/sheriff status`.", ephemeral=True)

    @sheriff.command(name="reprocess", description="Admin: re-run the parser over all archived messages")
    async def reprocess(interaction: discord.Interaction):
        if not await admin_only(interaction):
            return
        live_or_fail()
        await interaction.response.defer(ephemeral=True, thinking=True)
        from ..parsing.daynum import clear_glyph_cache
        clear_glyph_cache()
        n = await bot.reprocess()
        await interaction.followup.send(f"Reprocessed {n} archived messages.", ephemeral=True)

    @sheriff.command(name="status", description="Admin: ingestion health")
    async def status(interaction: discord.Interaction):
        if not await admin_only(interaction):
            return
        live_or_fail()
        s = bot.store
        recaps = s.recaps("recap")
        ok = sum(1 for r in recaps if r["parsed_ok"])
        lines = [f"Recaps: {ok}/{len(recaps)} parsed · results: {len(s.result_days())} days · "
                 f"pending identities: {len(s.pending())} · fallback scores: {s.count_events('fallback_scorer')}"]
        for e in s.events(limit=200):
            if e["kind"] in ("parse_error", "card_error", "pending_identity", "conflict", "missing_answer",
                             "inconsistent_grid") and len(lines) < 12:
                lines.append(f"- `{e['kind']}` day {e['day']}: {json.dumps(e['detail'])[:150]}")
        await reply(interaction, "\n".join(lines))

    @sheriff.command(name="stats", description="Explainable stats for a rider")
    @app_commands.describe(user="Whose ledger (default: you)", name="Test runs: synthetic player name")
    async def stats(interaction: discord.Interaction, user: discord.Member | None = None, name: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        active = bot.tests.run_for_channel(interaction.channel_id)
        if active:
            p = active.run.player(name) if name else None
            pid = p.pid if p else active.act_as.get(interaction.user.id)
            if pid is None:
                return await reply(interaction, "Name a synthetic player or `/sheriff test act-as` first.")
            return await reply(interaction, await active.run.game.stats(pid))
        live_or_fail()
        target = user or interaction.user
        row = bot.store.player_by_discord(str(target.id))
        if row is None:
            return await reply(interaction, f"No rides on record for {target.display_name}.")
        await reply(interaction, await bot.game.stats(row["player_id"]))

    @sheriff.command(name="explain", description="Row-by-row breakdown of one game")
    @app_commands.describe(day="Wordle number", user="Whose game (default: you)", name="Test runs: synthetic player")
    async def explain(interaction: discord.Interaction, day: int, user: discord.Member | None = None,
                      name: str | None = None):
        active = bot.tests.run_for_channel(interaction.channel_id)
        if active:
            viewer = active.act_as.get(interaction.user.id)
            p = active.run.player(name) if name else None
            target = p.pid if p else viewer
            if target is None:
                return await reply(interaction, "Name a synthetic player or `/sheriff test act-as` first.")
            return await reply(interaction, active.run.game.explain(day, target, viewer, bot.is_admin(interaction)))
        live_or_fail()
        viewer_row = bot.store.player_by_discord(str(interaction.user.id))
        target_user = user or interaction.user
        trow = bot.store.player_by_discord(str(target_user.id))
        if trow is None:
            return await reply(interaction, f"No rides on record for {target_user.display_name}.")
        await reply(interaction, bot.game.explain(day, trow["player_id"], viewer_row["player_id"] if viewer_row else None))

    @sheriff.command(name="link", description="Admin: map a plain-text name to a member")
    async def link(interaction: discord.Interaction, name: str, user: discord.Member):
        if not await admin_only(interaction):
            return
        live_or_fail()
        await interaction.response.defer(ephemeral=True, thinking=True)
        pid = bot.resolver.player_for_user(str(user.id), user.display_name, datetime.now(timezone.utc))
        moved = await bot.game.link(name, pid)
        await reply(interaction, f"Linked **{name}** → {user.mention}. Resolved {moved} pending result(s).")

    # -------------------------------------------------------------- config
    @config.command(name="show", description="Admin: show current settings")
    async def config_show(interaction: discord.Interaction):
        if not await admin_only(interaction):
            return
        shown = {k: bot.cfg.get(k) for k in sorted(SETTABLE)}
        await reply(interaction, "```yaml\n" + yaml.safe_dump(shown, sort_keys=True) + "```")

    @config.command(name="set", description="Admin: set a setting (value is YAML, e.g. 0.05 or [123, 456])")
    async def config_set(interaction: discord.Interaction, key: str, value: str):
        if not await admin_only(interaction):
            return
        if key not in SETTABLE:
            return await reply(interaction, f"Unknown or read-only key. Settable: {', '.join(sorted(SETTABLE))}")
        try:
            parsed = yaml.safe_load(value)
        except yaml.YAMLError as e:
            return await reply(interaction, f"Couldn't parse value: {e}")
        bot.set_setting(key, parsed)
        await reply(interaction, f"`{key}` = `{parsed!r}`")

    @config_set.autocomplete("key")
    async def key_complete(interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=k, value=k) for k in sorted(SETTABLE) if current in k][:25]

    @config.command(name="here", description="Admin: use this channel for a role")
    @app_commands.choices(role=[app_commands.Choice(name=n, value=n) for n in ("ingest", "board", "sandbox")])
    async def config_here(interaction: discord.Interaction, role: app_commands.Choice[str], remove: bool = False):
        if not await admin_only(interaction):
            return
        cid = interaction.channel_id
        if role.value == "board":
            bot.set_setting("discord.board_channel_id", None if remove else cid)
        else:
            key = "discord.ingest_channel_ids" if role.value == "ingest" else "discord.sandbox_channel_ids"
            other = "discord.sandbox_channel_ids" if role.value == "ingest" else "discord.ingest_channel_ids"
            if not remove and cid in bot.ids(other.split(".")[1]):
                return await reply(interaction, "A channel can't be both live (ingest) and sandbox.")
            ids = [i for i in bot.ids(key.split(".")[1]) if i != cid]
            if not remove:
                ids.append(cid)
            bot.set_setting(key, ids)
        await reply(interaction, f"<#{cid}> {'removed from' if remove else 'set as'} **{role.value}**.")

    # -------------------------------------------------------------- test mode
    @test.command(name="start", description="Admin: start a sandbox test run from a scenario")
    @app_commands.describe(scenario="Scenario name in scenarios/ (default: demo)",
                           text_only="Skip image rendering (faster, skips the image parser)")
    async def test_start(interaction: discord.Interaction, scenario: str = "demo", text_only: bool = False):
        if not await admin_only(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            a = await bot.tests.start(interaction.channel_id, scenario, text_only)
        except (IsolationError, ValueError, FileNotFoundError) as e:
            return await reply(interaction, str(e))
        await reply(interaction, f"Run `{a.run.run_id}` started.")

    @test_start.autocomplete("scenario")
    async def scenario_complete(interaction: discord.Interaction, current: str):
        d = bot.cfg.path("scenario_dir")
        names = sorted(p.stem for p in Path(d).glob("*.y*ml"))
        return [app_commands.Choice(name=n, value=n) for n in names if current in n][:25]

    @test.command(name="advance", description="Admin: advance the test clock and run the daily cycle(s)")
    async def test_advance(interaction: discord.Interaction, days: app_commands.Range[int, 1, 365] = 1):
        if not await admin_only(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            done = await bot.tests.advance(interaction.channel_id, days)
        except (IsolationError, ValueError) as e:
            return await reply(interaction, str(e))
        await reply(interaction, f"Advanced through Wordle {done[0]}-{done[-1]}." if done else "Nothing to do.")

    @test.command(name="inject", description="Admin: inject a recap (image and/or text) as the next recap")
    async def test_inject(interaction: discord.Interaction, text: str, image: discord.Attachment | None = None):
        if not await admin_only(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await image.read() if image else None
            res = await bot.tests.inject(interaction.channel_id, text.replace("\\n", "\n"), data)
        except (IsolationError, ValueError) as e:
            return await reply(interaction, str(e))
        await reply(interaction, res)

    @test.command(name="act-as", description="Admin: your button clicks in this run count as a synthetic player")
    async def test_act_as(interaction: discord.Interaction, player: str):
        if not await admin_only(interaction):
            return
        try:
            name = bot.tests.act_as(interaction.channel_id, interaction.user.id, player)
        except ValueError as e:
            return await reply(interaction, str(e))
        await reply(interaction, f"You are now acting as **{name}** in this run.")

    @test_act_as.autocomplete("player")
    async def player_complete(interaction: discord.Interaction, current: str):
        a = bot.tests.run_for_channel(interaction.channel_id)
        names = [p.current_name for p in a.run.scenario.players] if a else []
        return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]

    @test.command(name="status", description="Admin: run id, day, players, posteriors, parse errors")
    async def test_status(interaction: discord.Interaction):
        if not await admin_only(interaction):
            return
        try:
            await reply(interaction, bot.tests.status(interaction.channel_id))
        except ValueError as e:
            await reply(interaction, str(e))

    @test.command(name="end", description="Admin: tear down this channel's test run")
    async def test_end(interaction: discord.Interaction, keep: bool = False, delete_messages: bool = False):
        if not await admin_only(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            rid = await bot.tests.end(interaction.channel_id, keep, delete_messages)
        except ValueError as e:
            return await reply(interaction, str(e))
        await reply(interaction, f"Run `{rid}` ended{' (DB kept)' if keep else ''}.")

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.exception("command failed", exc_info=error)
        msg = str(getattr(error, "original", error))
        try:
            await reply(interaction, f"The Sheriff tripped over his spurs: {msg}")
        except discord.HTTPException:
            pass

    bot.tree.add_command(sheriff)
