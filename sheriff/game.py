"""Sheriff Webster game logic: ingestion pipeline, daily cycle, boards and interactions.

Identical in live and test mode; only the seams differ.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import content
from .config import Config
from .days import datetime_to_day
from .parsing import parse_playing, parse_recap
from .parsing.text import NameToken, ShareParseError, parse_share_text
from .patterns import GREENS_OF, WordData, row_to_code
from .scoring.engine import GameScore, InconsistentGrid, Scorer, explain_flavor
from .scoring.posterior import bounty_amount, cusum, daily_posterior, longterm_posterior
from .seams import (AnswerProvider, Button, Clock, EmbedSpec, IdentityResolver, OutMessage, OutputSink,
                    RecapEnvelope, Unresolved)
from .store import Store

log = logging.getLogger(__name__)

EMOJI = {"G": "\U0001F7E9", "Y": "\U0001F7E8", "B": "⬛"}
WANTED_COLOR = 0x8B5A2B
CLEAN_COLOR = 0x2E7D32


def pct(p: float | None) -> str:
    return "?" if p is None else f"{round(p * 100):d}%"


def emoji_row(row: str) -> str:
    return "".join(EMOJI[c] for c in row)


@dataclass
class IngestResult:
    kind: str
    ok: bool
    day: int | None = None
    detail: str = ""


class SheriffGame:
    def __init__(self, cfg: Config, store: Store, answers: AnswerProvider, clock: Clock, sink: OutputSink,
                 resolver: IdentityResolver, scorer: Scorer, env: str = "live", run_id: str | None = None,
                 archive_dir: Path | None = None, seed: int | None = None):
        self.cfg = cfg
        self.store = store
        self.answers = answers
        self.clock = clock
        self.sink = sink
        self.resolver = resolver
        self.scorer = scorer
        self.env = env
        self.run_id = run_id
        self.archive_dir = archive_dir
        self.rng = random.Random(seed)
        self.lock = asyncio.Lock()
        self._row1_baseline: float | None = None

    # ------------------------------------------------------------ helpers
    @property
    def is_test(self) -> bool:
        return self.env != "live"

    @property
    def cid(self) -> str:
        """custom_id prefix; embeds the run id so interactions route to the right environment."""
        return f"sheriff:{self.run_id or 'live'}"

    @property
    def prefix(self) -> str:
        return f"[TEST RUN {self.run_id}] " if self.is_test else ""

    def _msg(self, msg: OutMessage) -> OutMessage:
        if self.prefix and not msg.ephemeral:
            msg.content = self.prefix + (msg.content or "")
        return msg

    def name_of(self, pid: int) -> str:
        row = self.store.player(pid)
        return row["display_name"] if row else f"player {pid}"

    def _archive(self, env: RecapEnvelope) -> str | None:
        if self.archive_dir is None:
            return None
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        stem = str(env.message_id)
        if env.edited_at is not None:
            stem += "_" + env.edited_at.strftime("%Y%m%dT%H%M%S%f")
        meta = {"kind": env.kind, "message_id": env.message_id, "channel_id": env.channel_id,
                "author_id": env.author_id, "author_name": env.author_name, "text": env.text,
                "timestamp": env.timestamp.isoformat(),
                "edited_at": env.edited_at.isoformat() if env.edited_at else None, "edit_seq": env.edit_seq,
                "images": []}
        for i, data in enumerate(env.images):
            p = self.archive_dir / f"{stem}_{i}.png"
            p.write_bytes(data)
            meta["images"].append(p.name)
        (self.archive_dir / f"{stem}.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return str(self.archive_dir / (meta["images"][0] if meta["images"] else f"{stem}.json"))

    async def _resolve(self, token: NameToken, at) -> int | Unresolved:
        if token.kind == "mention":
            return await self.resolver.resolve_mention(token.value, at)
        return await self.resolver.resolve_name(token.value, at)

    # ------------------------------------------------------------ ingestion
    async def ingest(self, env: RecapEnvelope, run_cycle: bool = True, post: bool = True) -> IngestResult:
        async with self.lock:
            if env.kind == "recap":
                res = await self._ingest_recap(env)
                if res.day is not None and run_cycle:
                    await self._daily_cycle(res.day, post=post)
                return res
            if env.kind == "playing":
                return await self._ingest_playing(env)
            if env.kind == "share":
                return await self._ingest_share(env)
            return IngestResult(env.kind, False, detail="unknown kind")

    async def _ingest_recap(self, env: RecapEnvelope) -> IngestResult:
        image_path = self._archive(env)
        parsed = parse_recap(env, self.cfg, allow_synthetic=self.is_test)
        at = env.timestamp
        if parsed.error:
            self.store.upsert_recap(env.message_id, "recap", parsed.day, env.text, image_path, False, parsed.error, at)
            self.store.log_event("parse_error", parsed.day, {"message_id": env.message_id, "error": parsed.error})
            log.warning("recap %s rejected: %s", env.message_id, parsed.error)
            return IngestResult("recap", False, parsed.day, parsed.error)
        day = parsed.day
        problems = []
        for card in parsed.cards:
            pid = await self._resolve(card.token, at)
            if isinstance(pid, Unresolved):
                self.store.add_pending(str(card.token), day, env.message_id, "recap", card.grid, card.outcome, at)
                self.store.log_event("pending_identity", day, {"name": str(card.token), "reason": pid.reason})
                problems.append(f"{card.token}: {pid.reason}")
                continue
            existing = self.store.result(pid, day)
            if card.rows:
                if existing and existing.grid and existing.grid != card.grid:
                    self.store.log_event("conflict", day, {"player_id": pid, "kept": "recap_image",
                                                           "recap": card.grid, "other": existing.grid,
                                                           "other_source": existing.source})
                if not existing or existing.grid != card.grid or existing.source != "recap_image":
                    hard = existing.hard_mode if existing and existing.grid == card.grid else None
                    self.store.put_result(pid, day, card.guesses, hard, card.grid, "recap_image", env.message_id, at)
            else:
                self.store.log_event("card_error", day, {"player_id": pid, "error": card.error})
                problems.append(f"{card.token}: {card.error}")
                if existing and existing.grid and existing.outcome == card.outcome:
                    self.store.log_event("fallback_source", day, {"player_id": pid, "source": existing.source})
                elif not existing or existing.grid is None:
                    self.store.put_result(pid, day, card.guesses, None, None, "recap_text", env.message_id, at)
                    self.store.log_event("fallback_scorer", day, {"player_id": pid})
        self.store.upsert_recap(env.message_id, "recap", day, env.text, image_path, True,
                                "; ".join(problems) or None, at)
        return IngestResult("recap", True, day, "; ".join(problems))

    async def _ingest_playing(self, env: RecapEnvelope) -> IngestResult:
        image_path = self._archive(env)
        parsed = parse_playing(env, self.cfg, allow_synthetic=self.is_test)
        self.store.upsert_recap(env.message_id, "playing", parsed.day, env.text, image_path,
                                parsed.error is None, parsed.error, env.timestamp)
        if parsed.error or parsed.day is None:
            return IngestResult("playing", False, parsed.day, parsed.error or "no day")
        grid = "|".join(parsed.rows)
        self.store.add_playing_edit(env.message_id, env.effective_time, len(parsed.rows), grid)
        if not parsed.finished:
            return IngestResult("playing", True, parsed.day, "in progress")
        pid = await self.resolver.resolve_name(parsed.name, env.timestamp)
        if isinstance(pid, Unresolved):
            self.store.add_pending(parsed.name, parsed.day, env.message_id, "playing", grid, parsed.outcome,
                                   env.timestamp)
            return IngestResult("playing", True, parsed.day, f"pending identity: {pid.reason}")
        existing = self.store.result(pid, parsed.day)
        guesses = None if parsed.outcome == "X" else int(parsed.outcome)
        if existing is None:
            self.store.put_result(pid, parsed.day, guesses, None, grid, "playing_image", env.message_id, env.timestamp)
        elif existing.grid != grid:
            if existing.source == "playing_image" and existing.source_message_id == str(env.message_id):
                self.store.put_result(pid, parsed.day, guesses, None, grid, "playing_image", env.message_id,
                                      env.timestamp)
            else:
                self.store.log_event("conflict", parsed.day, {"player_id": pid, "kept": existing.source,
                                                              "kept_grid": existing.grid, "playing": grid})
        return IngestResult("playing", True, parsed.day)

    async def _ingest_share(self, env: RecapEnvelope) -> IngestResult:
        try:
            share = parse_share_text(env.text)
        except ShareParseError as e:
            self.store.log_event("share_malformed", None, {"message_id": env.message_id, "error": str(e)})
            return IngestResult("share", False, detail=str(e))
        if share is None:
            return IngestResult("share", False, detail="no share")
        if abs(share.day - datetime_to_day(env.timestamp)) > 1:
            return IngestResult("share", False, share.day, "day out of range for post date")
        pid = await self.resolver.resolve_mention(env.author_id, env.timestamp)
        if isinstance(pid, Unresolved):
            return IngestResult("share", False, share.day, pid.reason)
        existing = self.store.result(pid, share.day)
        if existing is None or (existing.source == "share_text" and existing.source_message_id == str(env.message_id)):
            self.store.put_result(pid, share.day, share.guesses, share.hard_mode, share.grid, "share_text",
                                  env.message_id, env.timestamp)
        elif existing.grid == share.grid:
            if existing.hard_mode is None:
                self.store.put_result(pid, share.day, existing.guesses, share.hard_mode, existing.grid,
                                      existing.source, existing.source_message_id, None)
        elif existing.source == "share_text":
            pass  # repost of a different share: keep the first valid one
        else:
            self.store.log_event("conflict", share.day, {"player_id": pid, "kept": existing.source,
                                                         "kept_grid": existing.grid, "share": share.grid})
        return IngestResult("share", True, share.day)

    # ------------------------------------------------------------ identity retries
    async def retry_pending(self) -> int:
        moved = 0
        for p in self.store.pending():
            token = NameToken("mention", p["raw_name"][2:-1]) if p["raw_name"].startswith("<@") \
                else NameToken("name", p["raw_name"].lstrip("@"))
            pid = await self._resolve(token, self.clock.now())
            if isinstance(pid, Unresolved):
                continue
            existing = self.store.result(pid, p["day"])
            outcome = p["outcome"]
            guesses = None if outcome == "X" else int(outcome)
            src = {"recap": "recap_image", "playing": "playing_image"}.get(p["source"], p["source"])
            if p["grid"] is None:
                src = "recap_text"
            if existing is None or (src == "recap_image" and existing.source != "recap_image" and p["grid"]):
                self.store.put_result(pid, p["day"], guesses, None, p["grid"], src, p["source_message_id"], None)
            self.store.delete_pending(p["id"])
            self.store.log_event("pending_resolved", p["day"], {"name": p["raw_name"], "player_id": pid})
            moved += 1
        return moved

    async def link(self, name: str, pid: int) -> int:
        """Admin: map a plain-text name to a player, then retry pending results."""
        async with self.lock:
            self.resolver.link_name(name, pid, self.clock.now())  # type: ignore[attr-defined]
            moved = await self.retry_pending()
            if moved:
                await self._score_pending()
                self._update_longterm()
            return moved

    # ------------------------------------------------------------ scoring
    async def _score_pending(self, up_to_day: int | None = None) -> int:
        n = 0
        prior = float(self.cfg.get("model.prior"))
        for r in self.store.unscored_results(up_to_day):
            answer = await self.answers.get(r.day)
            if not answer:
                self.store.log_event("missing_answer", r.day, {})
                continue
            gs: GameScore
            if r.grid:
                self.scorer.wd.ensure_answer(answer)  # mutate shared tables on this thread only
                try:
                    gs = await asyncio.to_thread(self.scorer.score, r.day, answer, r.rows, r.hard_mode)
                except InconsistentGrid as e:
                    self.store.log_event("inconsistent_grid", r.day, {"player_id": r.player_id, "error": str(e)})
                    gs = self.scorer.score_fallback(r.day, r.guesses)
            else:
                gs = self.scorer.score_fallback(r.day, r.guesses)
            feats = gs.to_features()
            feats["source"] = r.source
            feats["flavor"] = explain_flavor(feats)
            self.store.put_day_score(r.player_id, r.day, gs.surprisal_charitable, gs.surprisal_typical,
                                     daily_posterior(gs.log_lr_charitable, prior), feats)
            n += 1
        return n

    def _update_longterm(self) -> None:
        prior = float(self.cfg.get("model.prior"))
        rho = float(self.cfg.get("model.cheat_rate"))
        threshold = float(self.cfg.get("bounty.threshold"))
        taken = {s["alias"] for s in self.store.active_bounties() if s["alias"]}
        for p in self.store.players():
            pid = p["player_id"]
            st = self.store.state(pid)
            scores = self.store.day_scores_for_player(pid, st["baseline_day"])
            llrs = [s["features"].get("log_lr_charitable", 0.0) for s in scores]
            post = longterm_posterior(llrs, prior, rho) if llrs else prior
            fields: dict[str, Any] = {"longterm_posterior": post}
            if post > threshold:
                alias = st["alias"]
                if not alias or (alias in taken and st["bounty_amount"] is None):
                    alias = content.make_alias(self.rng, taken)
                taken.add(alias)
                fields.update(alias=alias, bounty_amount=bounty_amount(post))
            else:
                fields["bounty_amount"] = None
            self.store.put_state(pid, **fields)

    # ------------------------------------------------------------ daily cycle
    async def run_daily_cycle(self, day: int, post: bool = True) -> None:
        async with self.lock:
            await self._daily_cycle(day, post)

    async def _daily_cycle(self, day: int, post: bool = True) -> None:
        await self.retry_pending()
        await self._score_pending(up_to_day=day)
        self._update_longterm()
        last = self.store.meta("last_cycle_day")
        if last is None or day >= last:
            self.store.set_meta("last_cycle_day", day)
        if post:
            await self._post_wanted(day)
            await self._update_bounty_board()

    # ------------------------------------------------------------ Wanted Today
    def _wanted_public(self, day: int) -> OutMessage:
        players = self.store.results_for_day(day)
        opted = self.store.opted_in(day)
        lines = []
        for pid in opted:
            s = self.store.day_score(pid, day)
            if s:
                lines.append(f"**{self.name_of(pid)}** - {pct(s['posterior_day'])} · {s['features'].get('flavor', '')}")
        desc = "\n".join(lines) if lines else "_Nobody's brave enough to look yet._"
        embed = EmbedSpec(
            title=f"\U0001F4DC WANTED TODAY · Wordle No. {day}",
            description=desc,
            color=WANTED_COLOR,
            footer=f"{len(players)} riders on the trail · {len(opted)} opted in. "
                   "Viewing the list puts your own odds on this board.",
        )
        return self._msg(OutMessage(embeds=[embed], buttons=[
            Button("View the Wanted list", f"{self.cid}:optin:{day}", "danger", "\U0001F440")]))

    async def _post_wanted(self, day: int) -> None:
        msg = self._wanted_public(day)
        existing = self.store.bot_message("wanted", day)
        if existing:
            await self.sink.edit(existing["message_id"], msg)
        else:
            ref = await self.sink.post(msg)
            self.store.set_bot_message("wanted", day, None, ref)

    async def opt_in(self, day: int, pid: int) -> OutMessage:
        async with self.lock:
            mine = self.store.day_score(pid, day)
            if mine is None:
                return OutMessage(f"You didn't ride on Wordle No. {day}, partner. No score, no peeking.",
                                  ephemeral=True)
            new = self.store.opt_in(day, pid, self.clock.now())
            if new:
                ref = self.store.bot_message("wanted", day)
                if ref:
                    await self.sink.edit(ref["message_id"], self._wanted_public(day))
            scores = sorted(self.store.day_scores_for_day(day), key=lambda s: -s["posterior_day"])
            opted = set(self.store.opted_in(day))
            lines = []
            for s in scores:
                mark = "" if s["player_id"] in opted else " _(hiding)_"
                lines.append(f"**{self.name_of(s['player_id'])}**{mark} - {pct(s['posterior_day'])} · "
                             f"{s['features'].get('flavor', '')}")
            return OutMessage(content=f"_{content.OPT_IN}_", ephemeral=True, embeds=[EmbedSpec(
                title=f"The full Wanted list · Wordle No. {day}", description="\n".join(lines), color=WANTED_COLOR,
                footer="Daily cheat probabilities. One game is noisy evidence; don't hang anyone over it.")])

    # ------------------------------------------------------------ Bounty Board
    def _bounty_public(self) -> OutMessage:
        bounties = self.store.active_bounties()
        embeds = []
        for b in bounties[:9]:
            embeds.append(EmbedSpec(
                title="★ W A N T E D ★",
                description=f"## {b['alias']}\nFor crimes against the dictionary.\n**DEAD OR ALIVE**",
                color=WANTED_COLOR,
                fields=[("Reward", f"**${b['bounty_amount']:,}**", True)],
            ))
        extra = len(bounties) - len(embeds)
        text = "\U0001F335 **BOUNTY BOARD**"
        if extra > 0:
            text += f"\n...and {extra} more outlaws at large."
        if not bounties:
            embeds.append(EmbedSpec(title="The town is quiet", description="No bounties posted. For now.",
                                    color=CLEAN_COLOR))
        return self._msg(OutMessage(content=text, embeds=embeds, buttons=[
            Button("Am I the bounty?", f"{self.cid}:amibounty", "secondary", "\U0001F50D")]))

    async def _update_bounty_board(self) -> None:
        msg = self._bounty_public()
        existing = self.store.bot_message("bounty", -1)
        # A new or removed bounty reposts the board at the bottom of the channel so it gets seen;
        # amount changes alone just edit in place (no spam).
        signature = sorted(b["alias"] for b in self.store.active_bounties())
        if existing and signature != self.store.meta("bounty_signature"):
            await self.sink.delete(existing["message_id"])
            existing = None
        self.store.set_meta("bounty_signature", signature)
        if existing:
            try:
                await self.sink.edit(existing["message_id"], msg)
                return
            except Exception as e:  # deleted message etc.: repost
                log.warning("bounty board edit failed (%s); reposting", e)
        ref = await self.sink.post(msg)
        self.store.set_bot_message("bounty", -1, None, ref)

    async def update_bounty_board(self) -> None:
        async with self.lock:
            await self._update_bounty_board()

    def bounty_check(self, pid: int) -> OutMessage:
        st = self.store.state(pid)
        if st["bounty_amount"] is None:
            return OutMessage(content=f"✅ {content.CLEAN}", ephemeral=True)
        return OutMessage(
            content=(f"\U0001F920 Well, well. **{st['alias']}**... that's you, partner.\n"
                     f"Reward on your head: **${st['bounty_amount']:,}**. "
                     f"Long-term cheat probability: **{pct(st['longterm_posterior'])}**.\n"
                     "Nobody else knows. Yet."),
            ephemeral=True,
            buttons=[Button("Turn yourself in", f"{self.cid}:confess", "danger", "\U0001F920")])

    def confess_prompt(self, pid: int) -> OutMessage:
        st = self.store.state(pid)
        if st["bounty_amount"] is None:
            return OutMessage(content="There's no bounty on you, partner.", ephemeral=True)
        return OutMessage(
            content=("Are you sure? The whole town will hear your name, your odds, and your sentence. "
                     "In exchange: a clean slate."),
            ephemeral=True,
            buttons=[Button("Yes, I confess", f"{self.cid}:confess_yes", "danger"),
                     Button("Never mind", f"{self.cid}:confess_no", "secondary")])

    async def confess(self, pid: int) -> OutMessage:
        async with self.lock:
            st = self.store.state(pid)
            if st["bounty_amount"] is None:
                return OutMessage(content="There's no bounty on you anymore.", ephemeral=True)
            punishment = content.pick_punishment(self.rng)
            post = st["longterm_posterior"]
            today = self.clock.today_day()
            await self.sink.post(self._msg(OutMessage(
                content=(f"\U0001F920 **CONFESSION!** {self.name_of(pid)} has turned themselves in "
                         f"to Sheriff Webster.\nLong-term cheat probability: **{pct(post)}**\n"
                         f"Sentence: **{punishment}**.\n_{content.CONFESSION}_"))))
            taken = {s["alias"] for s in self.store.active_bounties() if s["player_id"] != pid}
            self.store.put_state(pid, baseline_day=today, longterm_posterior=float(self.cfg.get("model.prior")),
                                 bounty_amount=None, alias=content.make_alias(self.rng, taken, avoid=st["alias"]))
            self.store.add_confession(pid, today, post, punishment, self.clock.now())
            await self._update_bounty_board()
            return OutMessage(content="The Sheriff keeps the ledger, but your slate is clean. Ride straight.",
                              ephemeral=True)

    async def handle_component(self, action: str, arg: str | None, pid: int | None) -> OutMessage:
        if pid is None:
            return OutMessage(content="I don't know who you are, stranger.", ephemeral=True)
        if action == "optin" and arg:
            return await self.opt_in(int(arg), pid)
        if action == "amibounty":
            return self.bounty_check(pid)
        if action == "confess":
            return self.confess_prompt(pid)
        if action == "confess_yes":
            return await self.confess(pid)
        if action == "confess_no":
            return OutMessage(content="Suit yourself. The Sheriff is patient.", ephemeral=True)
        return OutMessage(content="That button's gone stale.", ephemeral=True)

    # ------------------------------------------------------------ stats / explain
    def row1_baseline(self) -> float:
        if self._row1_baseline is None:
            wd = self.scorer.wd
            idx = np.flatnonzero(wd.guess_is_curated)
            self._row1_baseline = float(GREENS_OF[wd.table[idx]].mean())
        return self._row1_baseline

    async def opener_consistency(self, results) -> dict:
        wd: WordData = self.scorer.wd
        cols, pats = [], []
        for r in results:
            if not r.grid:
                continue
            ans = await self.answers.get(r.day)
            if not ans:
                continue
            cols.append(wd.ensure_answer(ans))
            pats.append(row_to_code(r.rows[0]))
        if not cols:
            return {"games": 0}
        M = wd.table[:, cols] == np.array(pats, dtype=np.uint8)[None, :]
        full = np.flatnonzero(M.all(1))
        if full.size:
            curated = [wd.guesses[i] for i in full if wd.guess_is_curated[i]] or [wd.guesses[i] for i in full]
            return {"games": len(cols), "openers_needed": 1, "examples": curated[:3], "n_single": int(full.size)}
        covered = np.zeros(len(cols), dtype=bool)
        picks = []
        while not covered.all() and len(picks) < 12:
            gain = (M & ~covered[None, :]).sum(1)
            best = int(gain.argmax())
            if gain[best] == 0:
                break
            picks.append(wd.guesses[best])
            covered |= M[best]
        return {"games": len(cols), "openers_needed": len(picks), "examples": picks[:3]}

    async def stats(self, pid: int) -> OutMessage:
        results = self.store.results_for_player(pid)
        scores = self.store.day_scores_for_player(pid)
        name = self.name_of(pid)
        if not results:
            return OutMessage(content=f"No rides on record for {name}.", ephemeral=True)
        wins = [r.guesses for r in results if r.guesses is not None]
        fails = sum(1 for r in results if r.guesses is None)
        first, last = results[0].day, results[-1].day
        dist = {str(k): sum(1 for g in wins if g == k) for k in range(1, 7)}
        traps = [t for s in scores for t in s["features"].get("traps", [])]
        escaped = sum(1 for t in traps if t["escaped"])
        expected = sum(t["expected"] for t in traps)
        grid_scores = [s for s in scores if not s["features"].get("fallback")]
        row1 = [s["features"].get("row1_greens", 0) for s in grid_scores]
        nonfinal = [r for s in grid_scores for r in s["features"].get("rows", []) if r["row"] != "GGGGG"]
        elim_rate = (sum(1 for r in nonfinal if r["kind"] == "E") / len(nonfinal)) if nonfinal else 0.0
        switch = [s["features"]["last_e_row"] for s in grid_scores if s["features"].get("last_e_row")]
        oc = await self.opener_consistency(results)
        fallback_n = sum(1 for s in scores if s["features"].get("fallback"))
        llrs = [s["features"].get("log_lr_typical", 0.0) for s in scores]
        cp = cusum(llrs, [s["day"] for s in scores], float(self.cfg.get("cusum.threshold")),
                   float(self.cfg.get("cusum.drift")))

        fields = [
            ("Games", f"{len(results)} (Wordle {first}-{last})", True),
            ("Average", f"{np.mean(wins):.2f} guesses" if wins else "-", True),
            ("Posting rate", f"{len(results) / (last - first + 1):.0%}", True),
            ("Distribution", " ".join(f"{k}:{v}" for k, v in dist.items()) + f" X:{fails}", False),
            ("Trap escapes", f"escaped {escaped} of {len(traps)} traps; typical is ~{expected:.1f}", False),
            ("Row-1 greens", f"{np.mean(row1):.2f} per game vs ~{self.row1_baseline():.2f} for any opener"
             if row1 else "-", False),
            ("Play style", f"{elim_rate:.0%} elimination rows"
             + (f"; usually switches to narrowing after guess {int(np.median(switch))}" if switch else ""), False),
        ]
        if oc.get("games"):
            if oc["openers_needed"] == 1:
                fields.append(("Opener", f"every opener fits one word (e.g. {', '.join(w.upper() for w in oc['examples'])})", False))
            else:
                fields.append(("Opener", f"needs at least ~{oc['openers_needed']} different openers "
                                         f"(e.g. {', '.join(w.upper() for w in oc['examples'])})", False))
        if cp:
            fields.append(("Changepoint", f"luck turned suspiciously around Wordle {cp['onset_day']}", False))
        if fallback_n:
            fields.append(("Unreadable grids", f"{fallback_n} games judged on guess count only", False))
        return OutMessage(ephemeral=True, embeds=[EmbedSpec(title=f"\U0001F4CA Sheriff's ledger: {name}",
                                                            fields=fields, color=WANTED_COLOR)])

    def explain(self, day: int, target: int, viewer: int | None, viewer_is_admin: bool = False) -> OutMessage:
        if target != viewer and not viewer_is_admin and target not in self.store.opted_in(day):
            return OutMessage(content="That rider's business is their own unless they've opted in that day.",
                              ephemeral=True)
        s = self.store.day_score(target, day)
        if s is None:
            return OutMessage(content=f"No scored game for {self.name_of(target)} on Wordle No. {day}.",
                              ephemeral=True)
        f = s["features"]
        lines = []
        if f.get("fallback"):
            lines.append("⚠ Grid unreadable: judged on guess count alone (weak evidence).")
        for r in f.get("rows", []):
            lines.append(f"`{r['index'] + 1}` {emoji_row(r['row'])} {r['kind']} · "
                         f"{r['k_before']:.0f}→{r['k_after']:.0f} left · "
                         f"P={r['p_obs_charitable']:.1%} · suspicion ×{r['lr_charitable']:.2f}")
        lines.append(f"\nDaily cheat probability: **{pct(s['posterior_day'])}** · _{f.get('flavor', '')}_")
        lines.append("N = narrowing row, E = elimination row. Suspicion is the charitable likelihood "
                     "ratio: >1 leans cheat, <1 leans honest.")
        return OutMessage(ephemeral=True, embeds=[EmbedSpec(
            title=f"\U0001F50E {self.name_of(target)} · Wordle No. {day}", description="\n".join(lines),
            color=WANTED_COLOR)])
