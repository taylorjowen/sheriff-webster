import asyncio
from datetime import timedelta

import pytest

from sheriff.days import day_to_datetime
from sheriff.seams import RecapEnvelope
from sheriff.sim.renderer import CardSpec, recap_text
from sheriff.sim.scenario import Scenario, ScenarioRun, find_scenario


def make_run(cfg, mode="text", **overrides):
    d = {"name": "unit", "seed": 1, "start_day": 1880, "days": 20,
         "players": [{"name": "Honest Abe", "strategy": "honest_hybrid"},
                     {"name": "Sneaky Pete", "strategy": "cheat_near_miss", "cheat_rate": 1.0},
                     {"name": "Flaky Fran", "strategy": "honest_narrowing", "post_rate": 0.5}]}
    d.update(overrides)
    return ScenarioRun(cfg, Scenario.from_dict(d), mode=mode, run_id="unit01")


def test_full_loop_with_real_image_parser(cfg):
    run = make_run(cfg, mode="image", days=6, playing_messages=True)
    asyncio.run(run.run_all())
    assert run.store.count_events("parse_error") == 0
    assert run.store.count_events("card_error") == 0
    assert run.store.count_events("conflict") == 0
    sources = {r.source for d in range(1880, 1886) for r in run.store.results_for_day(d)}
    assert sources == {"recap_image"}
    assert run.store.playing_edits(f"{run.run_id}-1")   # edit sequence captured


def test_wanted_opt_in_flow(cfg):
    run = make_run(cfg, days=2)
    asyncio.run(run.run_all())
    g, s = run.game, run.store
    day = 1881
    abe = run.player("Honest Abe").pid
    pete = run.player("Sneaky Pete").pid
    wanted_ref = s.bot_message("wanted", day)["message_id"]
    before = run.sink.messages[wanted_ref].embeds[0].description
    assert "Nobody" in before
    reply = asyncio.run(g.handle_component("optin", str(day), abe))
    assert reply.ephemeral and "Sneaky Pete" in reply.embeds[0].description   # sees the full board
    public = run.sink.messages[wanted_ref].embeds[0].description
    assert "Honest Abe" in public and "Sneaky Pete" not in public            # only opted-in on the public board
    assert s.opted_in(day) == [abe]
    asyncio.run(g.handle_component("optin", str(day), abe))                    # idempotent
    assert s.opted_in(day) == [abe]
    # explain permissions: Pete can see Abe (opted in); Abe can't see Pete
    assert g.explain(day, abe, pete).embeds
    assert not g.explain(day, pete, abe).embeds


def test_opt_in_requires_a_score(cfg):
    run = make_run(cfg, days=1, players=[{"name": "A"}, {"name": "B", "post_rate": 0.0}])
    asyncio.run(run.run_all())
    reply = asyncio.run(run.game.handle_component("optin", "1880", run.player("B").pid))
    assert "No score" in reply.content


def test_bounty_check_and_confession(cfg):
    run = make_run(cfg, days=20)
    asyncio.run(run.run_all())
    g, s = run.game, run.store
    pete, abe = run.player("Sneaky Pete").pid, run.player("Honest Abe").pid
    st = s.state(pete)
    assert st["bounty_amount"] and st["alias"]
    board = run.sink.messages[s.bot_message("bounty")["message_id"]]
    assert st["alias"] in board.embeds[0].description
    assert "Sneaky Pete" not in str(board)                       # anonymized
    assert "clean" in asyncio.run(g.handle_component("amibounty", None, abe)).content
    mine = asyncio.run(g.handle_component("amibounty", None, pete))
    assert st["alias"] in mine.content and mine.buttons[0].custom_id.endswith(":confess")
    confirm = asyncio.run(g.handle_component("confess", None, pete))
    assert any(b.custom_id.endswith(":confess_yes") for b in confirm.buttons)
    n_results = len(s.results_for_player(pete))
    asyncio.run(g.handle_component("confess_yes", None, pete))
    st2 = s.state(pete)
    assert st2["bounty_amount"] is None and st2["alias"] != st["alias"]
    assert st2["baseline_day"] == run.clock.today_day()
    assert len(s.results_for_player(pete)) == n_results          # the Sheriff keeps the ledger
    assert any("CONFESSION" in p.content and "Sneaky Pete" in p.content for p in run.sink.posts())
    assert s.confessions()
    # clean slate: next cycle recomputes from the prior
    asyncio.run(run.advance(1))
    assert s.state(pete)["longterm_posterior"] < 0.5


def test_missing_days_never_count(cfg):
    run = make_run(cfg, days=10, players=[{"name": "Flaky Fran", "post_rate": 0.4}])
    asyncio.run(run.run_all())
    pid = run.player("Flaky Fran").pid
    played = len(run.store.results_for_player(pid))
    assert len(run.store.day_scores_for_player(pid)) == played < 10


def test_rename_keeps_one_player(cfg):
    run = make_run(cfg, days=6, plain_name_rate=1.0,
                   players=[{"name": "Fran", "rename_on_day": 1883, "new_name": "Fran the Fair"}])
    asyncio.run(run.run_all())
    assert len(run.store.players()) == 1
    assert len(run.store.results_for_player(run.player("Fran").pid)) == 6


def test_ambiguous_name_goes_pending_then_link(cfg):
    run = make_run(cfg, days=0, players=[{"name": "Twin"}, {"name": "Other"}])
    # rename "Other" to "Twin" too: plain "@Twin" is now ambiguous
    other = run.player("Other").pid
    run.resolver.rename(other, "Twin")
    cards = [CardSpec("Twin", None, "2", ["BYBBG", "GGGGG"])]
    env = RecapEnvelope("recap", "x1", "sandbox", "bot", recap_text(cards), run.clock.now() + timedelta(days=1),
                        synthetic_grids=[["BYBBG", "GGGGG"]], synthetic_day=1880)
    asyncio.run(run.game.ingest(env))
    assert run.store.pending() and not run.store.results_for_day(1880)
    moved = asyncio.run(run.game.link("Twin", other))
    assert moved == 1 and not run.store.pending()
    assert run.store.result(other, 1880)


def test_share_text_dedupe_and_conflict(cfg):
    run = make_run(cfg, days=0, players=[{"name": "A"}])
    g, s = run.game, run.store
    pid = run.player("A").pid
    G, K = "\U0001F7E9", "⬛"
    share = f"Wordle 1880 2/6*\n{K}{G}{K}{K}{K}\n{G * 5}"
    ts = day_to_datetime(1880)
    env = RecapEnvelope("share", "s1", "c", "1", share, ts)
    asyncio.run(g.ingest(env))
    first = s.result(pid, 1880)
    assert first and first.hard_mode and first.source == "share_text"
    repost = RecapEnvelope("share", "s2", "c", "1", f"Wordle 1880 3/6\n{K * 5}\n{K * 5}\n{G * 5}", ts)
    asyncio.run(g.ingest(repost))
    assert s.result(pid, 1880).grid == first.grid                 # keep the first valid post
    malformed = RecapEnvelope("share", "s3", "c", "1", f"Wordle 1880 3/6\n{G * 5}", ts)
    assert not asyncio.run(g.ingest(malformed)).ok


@pytest.mark.parametrize("name", ["build_up_cheater_vs_honest", "near_miss_cheater", "honest_only_long"])
def test_scenario_expectations(cfg, name):
    run = ScenarioRun(cfg, Scenario.load(find_scenario(cfg, name)), mode="text")
    asyncio.run(run.run_all())
    results = run.check_expectations()
    assert results and all(ok for ok, _ in results), results
