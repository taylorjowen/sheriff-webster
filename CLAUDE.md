# CLAUDE.md

Sheriff Webster: a wild-west Discord bot that ingests Wordle-app recaps, estimates per-player
cheat probabilities, and runs a daily opt-in "Wanted Today" board plus anonymized bounties.
The design spec is `sheriff-webster-plan.md` (section numbers like §7.3 in code comments refer to it).

## Commands

Windows dev box: the venv lives at `.venv/Scripts/python` (use `.venv/bin/python` on Linux).

```bash
.venv/Scripts/python -m pytest -q                       # full suite (~15s)
.venv/Scripts/python -m sheriff.sim demo --report       # headless scenario run (scenarios/*.yaml)
.venv/Scripts/python -m sheriff.sim <name> --text-only  # faster: skips image render/parse
.venv/Scripts/python -m sheriff.tools.fit_model         # refit model.row_epsilon / row_beta
.venv/Scripts/python -m sheriff                         # run the bot (needs DISCORD_TOKEN in .env)
```

## Architecture

Everything that touches the outside world sits behind a seam in `sheriff/seams.py`
(`RecapEnvelope`, `Clock`, `OutputSink`, `IdentityResolver`, `AnswerProvider`). Parser, scoring
and game logic are identical in live and test mode; only the seam implementations differ.

- `parsing/`: `image.py` (tile detection by color, grid grouping), `daynum.py` (title digits by
  template matching), `text.py` (recap/playing/share text), `__init__.py` (strict validation).
- `scoring/engine.py`: Monte Carlo calibration scoring. Row LR = (1-eps) + eps*e^(beta*greens)/Z;
  E[LR]=1 for honest play. "Charitable" (min over sampled guesses) drives accusations.
- `scoring/posterior.py`: daily posterior, long-term mixture posterior, CUSUM, bounty amounts.
- `game.py`: `SheriffGame`, the ingestion pipeline, daily cycle, boards, buttons, stats/explain.
- `sim/`: bots (honest/cheater simulators), renderer (fake Wordle-app images), scenario driver,
  headless sink. `bot/`: discord.py client, Discord sink, interactive test-run manager.
- `store.py`: SQLite, one DB per environment.

## Rules that must hold

- **Test/live isolation (plan §9.1):** test runs never open the live DB (`open_store` raises
  `IsolationError`), never post outside sandbox channels (`DiscordSink`), never resolve real
  users, and use placeholder mention ids < 1000. Keep these checks when changing related code.
- **Never feed uncertain data to the scorer:** parse failures reject the recap/card, they don't guess.
- **Bounties are anonymous:** nothing public (stats included) may reveal who has a bounty or
  their long-term posterior.
- Missing days never count against a player. Ambiguous names go to `pending_identity`, never guessed.
- `WordData` is shared and grows via `ensure_answer`. Read `wd.cand_in_guesses` / `wd.table`
  fresh rather than caching them.

## Data and files

- `sheriff/data/`: word lists and built-in (rendered-font) digit templates. Committed.
- `data/` (git-ignored): `live.db`, `archive/` (raw recap inputs), `answers.db`,
  `cache/` (pattern table .npy), `glyphs/` (templates harvested from real images), `test_runs/`.
- `.env` (token) and `config.yaml` are git-ignored; see the `.example` files.
- The image parser is calibrated against rendered look-alikes only; real-image calibration is
  done with `sheriff.tools.calibrate` + `sheriff.tools.harvest_glyphs`, then `/sheriff reprocess`.

## Deployment

Docker Compose on a VPS (README "Deploying with Docker"). Server address and handy commands
are in `deploy-notes.local.md` (git-ignored; don't commit server details).
