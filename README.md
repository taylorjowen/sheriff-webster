# Sheriff Webster 🤠

A wild-west Discord bot that reads your server's Wordle results, estimates how likely each
player is to be cheating, and turns it into a game: a daily opt-in **Wanted Today** board
and anonymized long-term **bounties**. Design: [`sheriff-webster-plan.md`](sheriff-webster-plan.md).

## Setup

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows; use .venv/bin/pip elsewhere
cp .env.example .env                               # put your bot token in DISCORD_TOKEN
cp config.example.yaml config.yaml                 # optional
python -m sheriff.tools.answers fetch --from 1700  # fill the answer cache from NYT
python -m sheriff                                  # run the bot
```

The first run builds a ~30 MB pattern table in `data/cache/` (a few seconds).

## Deploying with Docker

On any Linux server with Docker (e.g. a small VPS; 512 MB RAM is plenty):

```bash
curl -fsSL https://get.docker.com | sh                 # if Docker isn't installed
git clone https://github.com/taylorjowen/sheriff-webster.git && cd sheriff-webster
# copy .env and config.yaml from your PC (scp), or create them from the .example files
docker compose run --rm sheriff python -m sheriff.tools.answers fetch --from 1700
docker compose up -d --build
docker compose logs -f                                  # look for "riding as ..."
```

All state lives in `./data` on the host (live DB, archive, answer cache, pattern table,
harvested digit templates); back that folder up. Tools run inside the container:

```bash
docker compose exec sheriff python -m sheriff.tools.calibrate data/archive/<id>_0.png
docker compose exec sheriff python -m sheriff.tools.harvest_glyphs data/archive/<id>_0.png 1924
```

Update with `git pull && docker compose up -d --build`. Run only one copy of the bot per token.

## How the scoring works (short version)

An honest player picks each guess without knowing the answer, so given what their own
feedback has told them, every remaining candidate is equally likely. For each row the bot
asks how much "green progress" the outcome showed compared with what that player's guess
and knowledge could be expected to produce. This test doesn't care about strategy, so
narrowing, elimination and hybrid players are all judged fairly. We only see colors, not
words, so guesses are Monte Carlo sampled from the words consistent with each row. The
**charitable** reading (most favorable to the player) drives anything accusatory.

Per-row likelihood ratios multiply into a daily cheat probability (prior 5%). The long-term
probability treats a "cheater" as someone who cheats some fraction of games. For honest
play its likelihood ratio is a supermartingale, so false bounties are provably rare. In
simulation: 0 of 1000 honest 100-game histories got a bounty, and ~90% of players who
cheated in 20 of 40 games did. Days you don't play never count.

Details are in `sheriff/scoring/engine.py` and `sheriff/scoring/posterior.py`. Refit the
model with `python -m sheriff.tools.fit_model`.

## Commands

| Command | Who | What |
|---|---|---|
| `/sheriff stats [user]` | anyone | Explainable stats: average, trap escapes vs expected, row-1 greens vs baseline, play style, opener consistency, changepoints |
| `/sheriff explain <day> [user]` | anyone | Row-by-row breakdown (your own, or someone who opted in that day) |
| `/sheriff backfill` | admin | Ingest the whole channel history (idempotent) |
| `/sheriff reprocess` | admin | Re-parse every archived message (after calibrating colors or harvesting digits) |
| `/sheriff status` | admin | Parse health, pending identities, conflicts |
| `/sheriff link <name> <@user>` | admin | Map a plain-text name to a member |
| `/sheriff config show / set / here` | admin | Settings; `here role:ingest\|board\|sandbox` assigns the current channel |
| `/sheriff test start / advance / inject / act-as / status / end` | admin, sandbox only | Interactive test runs |

Buttons: **👀 View the Wanted list** (opt in and see the board), **🔍 Am I the bounty?**
(private), **🤠 Turn yourself in** (only shown privately to the outlaw).

## Testing

```bash
.venv/Scripts/python -m pytest                                   # unit + scenario tests
.venv/Scripts/python -m sheriff.sim demo --report                # headless scenario run
.venv/Scripts/python -m sheriff.sim honest_only_long --text-only # false-positive check
```

Scenarios live in `scenarios/*.yaml` (format in plan §9.3). Test runs use their own DB,
synthetic players, a controllable clock, and only sandbox channels. They can't touch live data.

## Calibrating against real images

The image parser was developed against rendered look-alikes, because there were no real
fixtures yet. After the first `/sheriff backfill` (raw inputs go to `data/archive/`):

1. `python -m sheriff.tools.calibrate data/archive/<id>_0.png --overlay check.png` prints the
   dominant colors and what the parser sees. Put the real tile colors in `config.yaml`
   under `image.colors`.
2. `python -m sheriff.tools.harvest_glyphs data/archive/<id>_0.png <wordle number>` (saved to `data/glyphs/`) teaches
   the day-number reader the real font. Do this for one recap image and one "was playing" image.
3. `/sheriff reprocess`, then `/sheriff status`.

## Tools

- `python -m sheriff.tools.answers fetch|import|show`: answer cache
- `python -m sheriff.tools.calibrate <image>`: color calibration and parser debugging
- `python -m sheriff.tools.harvest_glyphs <image> <day>`: digit templates for the title
- `python -m sheriff.tools.fit_model`: refit the cheat likelihood from simulated bots
