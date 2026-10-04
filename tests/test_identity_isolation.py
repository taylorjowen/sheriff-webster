import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sheriff.answers import AnswerCache
from sheriff.identity import LiveIdentityResolver, SyntheticIdentityResolver
from sheriff.seams import Unresolved
from sheriff.sim.scenario import Scenario, ScenarioRun
from sheriff.store import IsolationError, open_store

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def member(uid, name, nick=None, global_name=None):
    return SimpleNamespace(id=uid, name=name, nick=nick, global_name=global_name, display_name=nick or global_name or name)


def test_live_resolver_mentions_names_and_ambiguity(cfg):
    store = open_store(cfg.path("live_db"), "live", cfg.path("live_db"))
    guild = SimpleNamespace(members=[member(1, "steve", nick="Fire Emblem: Scuba Steve"), member(2, "pat"),
                                     member(3, "twin1", nick="Twin"), member(4, "twin2", nick="Twin")])
    guild.get_member = lambda uid: next((m for m in guild.members if m.id == uid), None)
    r = LiveIdentityResolver(store, lambda: guild)
    pid = asyncio.run(r.resolve_mention("1", NOW))
    assert asyncio.run(r.resolve_name("Fire Emblem: Scuba Steve", NOW)) == pid
    # display-name change: the old name still resolves through name_history, same player
    guild.members[0].nick = "Fire Emblem: Fortune's Weeb"
    guild.members[0].display_name = guild.members[0].nick
    assert asyncio.run(r.resolve_name("Fire Emblem: Fortune's Weeb", NOW)) == pid
    assert asyncio.run(r.resolve_name("Fire Emblem: Scuba Steve", NOW)) == pid
    amb = asyncio.run(r.resolve_name("Twin", NOW))
    assert isinstance(amb, Unresolved) and amb.reason == "ambiguous"
    assert isinstance(asyncio.run(r.resolve_name("Nobody", NOW)), Unresolved)
    # admin link wins
    tw = r.player_for_user("4", "Twin", NOW)
    r.link_name("Twin", tw, NOW)
    assert asyncio.run(r.resolve_name("Twin", NOW)) == tw


def test_synthetic_resolver_never_resolves_real_ids(cfg, tmp_path):
    store = open_store(tmp_path / "t.db", "test", cfg.path("live_db"))
    r = SyntheticIdentityResolver(store)
    pid = r.add_player("Test Tex", "1", NOW)
    assert asyncio.run(r.resolve_mention("1", NOW)) == pid
    assert isinstance(asyncio.run(r.resolve_mention("123456789012345678", NOW)), Unresolved)
    r.rename(pid, "Tex Two")
    assert asyncio.run(r.resolve_name("Test Tex", NOW)) == pid
    assert asyncio.run(r.resolve_name("Tex Two", NOW)) == pid


def test_test_env_cannot_open_live_db(cfg):
    with pytest.raises(IsolationError):
        open_store(cfg.path("live_db"), "test", cfg.path("live_db"))


def test_answer_cache_read_only(tmp_path):
    AnswerCache(tmp_path / "a.db").put(1, "crane")
    ro = AnswerCache(tmp_path / "a.db", read_only=True)
    assert ro.get(1) == "crane"
    with pytest.raises(PermissionError):
        ro.put(2, "slate")


def test_discord_sink_refuses_live_channels():
    from sheriff.bot.discord_io import DiscordSink
    with pytest.raises(IsolationError):
        DiscordSink(client=None, channel_id=111, env="test", sandbox_ids={222})
    with pytest.raises(IsolationError):
        DiscordSink(client=None, channel_id=222, env="live", sandbox_ids={222})
    DiscordSink(client=None, channel_id=222, env="test", sandbox_ids={222})


def test_teardown_removes_db(cfg, tmp_path):
    sc = Scenario.from_dict({"name": "t", "start_day": 1880, "days": 2,
                             "players": [{"name": "A", "strategy": "honest_hybrid"}]})
    db = tmp_path / "runs" / "x.db"
    run = ScenarioRun(cfg, sc, store_path=db, mode="text")
    asyncio.run(run.run_all())
    assert db.exists()
    run.close()
    assert not db.exists()
    run2 = ScenarioRun(cfg, Scenario.from_dict({"name": "t", "start_day": 1880, "days": 1, "players": []}),
                       store_path=db, mode="text")
    run2.close(keep=True)
    assert db.exists()


def test_test_runs_post_with_prefix_and_placeholder_mentions(cfg):
    sc = Scenario.from_dict({"name": "t", "start_day": 1880, "days": 3, "plain_name_rate": 0.5,
                             "players": [{"name": "A"}, {"name": "B"}]})
    run = ScenarioRun(cfg, sc, mode="text", run_id="abc123")
    asyncio.run(run.run_all())
    posts = run.sink.posts()
    assert posts and all(p.content.startswith("[TEST RUN abc123]") for p in posts)
    for p in posts:
        for b in p.buttons:
            assert b.custom_id.startswith("sheriff:abc123:")
    for pl in sc.players:
        assert int(pl.mention_id) < 1000   # never a real snowflake
