"""Offline smoke test of the live Discord ingestion path with fake message objects."""
import asyncio
from types import SimpleNamespace

from sheriff.answers import TestAnswerProvider
from sheriff.bot.discord_io import envelope_from_message
from sheriff.days import day_to_datetime
from sheriff.game import SheriffGame
from sheriff.identity import LiveIdentityResolver
from sheriff.parsing.text import classify_wordle_bot_text
from sheriff.scoring.engine import Scorer
from sheriff.seams import TestClock
from sheriff.sim.recorder import HeadlessSink
from sheriff.sim.renderer import CardSpec, recap_text, render_recap_image
from sheriff.store import open_store


class FakeAttachment:
    def __init__(self, data):
        self.data, self.content_type, self.filename = data, "image/png", "recap.png"

    async def read(self):
        return self.data


def fake_message(mid, text, author_id, images, created):
    return SimpleNamespace(id=mid, content=text, embeds=[], attachments=[FakeAttachment(i) for i in images],
                           channel=SimpleNamespace(id=10), author=SimpleNamespace(id=author_id, display_name="Wordle"),
                           created_at=created, edited_at=None)


def test_live_recap_message_end_to_end(cfg, wd):
    store = open_store(cfg.path("live_db"), "live", cfg.path("live_db"))
    members = [SimpleNamespace(id=111, name="abe", nick=None, global_name="Honest Abe", display_name="Honest Abe"),
               SimpleNamespace(id=222, name="pat", nick="Plain Pat", global_name=None, display_name="Plain Pat")]
    guild = SimpleNamespace(members=members, get_member=lambda uid: next((m for m in members if m.id == uid), None))
    sink = HeadlessSink()
    game = SheriffGame(cfg, store, TestAnswerProvider(None, {1900: "crane"}), TestClock(1901), sink,
                       LiveIdentityResolver(store, lambda: guild), Scorer(wd, cfg), env="live",
                       archive_dir=cfg.path("archive_dir"))
    cards = [CardSpec("Honest Abe", "111", "3", ["BBBBB", "GGBBB", "GGGGG"]),
             CardSpec("Plain Pat", None, "X", ["BBBBB", "BBBBY", "BBYBB", "GBBBB", "BBBBB", "BYBBB"])]
    text = recap_text(cards)
    img = render_recap_image(1900, cards, cfg)
    msg = fake_message(5555, text, 1211781489931452447, [img], day_to_datetime(1901, hour=9))
    kind = classify_wordle_bot_text(msg.content)
    assert kind == "recap"
    env = asyncio.run(envelope_from_message(msg, kind, session=None))
    res = asyncio.run(game.ingest(env))
    assert res.ok, res.detail
    # crane vs crane-patterns are validated against the answer during scoring
    results = store.results_for_day(1900)
    assert len(results) == 2
    assert {store.player(r.player_id)["discord_user_id"] for r in results} == {"111", "222"}
    assert len(store.day_scores_for_day(1900)) + store.count_events("inconsistent_grid") >= 1
    assert sink.posts()                                               # Wanted Today + Bounty Board
    assert list(cfg.path("archive_dir").glob("5555*.json"))           # raw input archived
