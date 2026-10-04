# Sheriff Webster — Wordle Cheat Detection Bot

Planning document for implementation. This describes **what** to build and **why**; implementation details are left to judgment unless specified.

---

## 1. Overview

Sheriff Webster is a wild-west-themed Discord bot for a private friend-group server. It reads everyone's Wordle results, estimates how likely each player is to be cheating, and turns that into a game:

- **Daily:** an opt-in "Wanted Today" board showing per-day cheat probabilities. You can only see the board by adding your own score to it (a prisoner's-dilemma-style mechanic).
- **Long-term:** anonymized bounties for players whose overall cheat probability exceeds 50%. Only the outlaw can discover it's them, and they can turn themselves in for a public confession, a silly punishment, and a clean slate.

The tone is playful. The math should still be honest, explainable, and fair to the accused.

---

## 2. Goals and non-goals

**Goals**
- A strategy-agnostic cheat score that doesn't penalize legitimate play styles: narrowing, elimination/"scenic route", or a hybrid of the two.
- Specific detection of the two known cheating strategies:
  1. **Build-up / sibling words:** opening with words suspiciously close to the answer and climbing toward it.
  2. **Near-miss then jump:** hovering just short of the answer, then solving "out of nowhere".
- Explainable output ("escaped 9 of 10 traps; typical is ~3"), not just a number.
- Fully within Discord ToS: a proper bot account, never self-botting.
- Everyone in the server who plays is tracked. **There is no opt-out** (house rule). The only opt-in is viewing the daily Wanted list (§8.1).

**Non-goals**
- Certainty. This is a probabilistic party game, not a court.
- Supporting multiple servers at scale (design for one server, < 100 servers max).

---

## 3. Stack

- Python 3.11+, `discord.py` 2.x
- SQLite (via `sqlite3` or SQLAlchemy). A single file DB is fine.
- `numpy` for the pattern table and scoring
- `Pillow` for recap image parsing and fake recap rendering
- `aiohttp` for fetching attachments and the NYT answer endpoint
- `PyYAML` for test scenarios
- `pytest` for tests

### Architecture principle: seams for testability
Everything that touches the outside world sits behind an interface, so test mode (§9) can swap it out:

| Seam | Live implementation | Test implementation |
|---|---|---|
| `RecapSource` | Discord `on_message` from the Wordle app bot | Injected fake recaps (rendered or replayed) |
| `IdentityResolver` | Maps recap mentions to real guild members | Maps to run-scoped ephemeral fake players |
| `Store` | Live SQLite DB | Fresh per-run SQLite DB (temp file or `:memory:`) |
| `Clock` | Real time | Controllable clock; days advance on command |
| `OutputSink` | Live channel(s) | Sandbox channel, or a headless recorder for assertions |
| `AnswerProvider` | NYT endpoint + local cache | Same cache (read-only), or scenario-defined answers |

The parser, scoring engine, and game logic must be identical in both modes. Only the seams differ.

### Discord setup
- Bot application created in the Discord Developer Portal.
- Enable the **Message Content** privileged intent (no verification needed under 100 servers).
- Permissions: Read Messages, Read Message History, Send Messages, Embed Links, Use Application Commands.
- Use only a bot token. **Never** use a user token (self-botting violates Discord ToS).

---

## 4. Daily flow (trigger)

The Wordle integration in the server posts a **daily recap** of the previous day's play. Sheriff Webster's daily cycle is triggered by that recap:

1. `on_message` detects a message authored by the Wordle app's bot user that matches the recap format. Make the user ID configurable. The community bot Astrocade defaults to `1211781489931452447`; verify this against the server.
2. The bot finalizes ingestion for that Wordle day (all shares that will count are in).
3. It runs scoring for that day and updates long-term posteriors.
4. It posts the **Wanted Today** message (with the opt-in button) and updates the **Bounty Board**.

Posting on this fixed cadence, rather than immediately after each share, also reduces anonymity leaks from timing.

### What the Wordle app posts (confirmed from real messages)

**Daily recap**
- **Text:** "Your group is on a N day streak! 🔥 Here are yesterday's results:", then one line per outcome, e.g. `👑 3/6: @A @B`, `4/6: @C @D`, `X/6: @E`. Several players can share a line.
- **Image:** a dark panel titled "Wordle No. 1924" with one card per player, laid out horizontally. Each card shows an avatar above a 5×6 tile grid (green / yellow / gray, with unfilled rows as empty dark tiles).
- **Card order** is exactly the text order: lines top to bottom, and within a tied line, the order the names appear.
- **Failures:** a player who made 6 guesses and missed appears as `X/6` with 6 filled rows and no all-green row. A player who **didn't guess at all doesn't appear** in the text or the image.
- **The day number only appears in the image title.** Don't infer it from the post time. The "yesterday's results" wording and the post time don't reliably match the puzzle date (observed: recaps whose "Wordle No." is the same calendar day they were posted).
- **Mentions aren't always pills.** Most names are real `<@id>` mentions, but some appear as plain text "@Name" (observed on an `X/6` line). Identity resolution must handle both (§5.5).

**"X was playing" message**
- **Text:** "<Display Name> was playing", with the name as plain text, not a mention.
- **Image:** "Wordle No. 1925" title, then a single card in a **different layout**: avatar on the left, a larger 5×6 grid on the right.
- **Behavior:** the message is **edited live as the player plays**, so it always shows the current board, and its final state is the finished game.

The grids in these images carry the full per-row pattern data. **Image parsing is the primary ingestion path** (§5.2).

---

## 5. Data ingestion

### 5.1 Sources
1. **Daily recap (primary).** Text (outcomes + mentions, with user IDs where available) plus the results image (grids). This is the authoritative daily record and the trigger for the daily cycle.
2. **"X was playing" messages (co-primary / cross-check).** One per player per day, holding that player's own board. Uses:
   - Cross-check the recap grid for that player. If they disagree, log the conflict and prefer the recap.
   - A fallback when a recap card fails to parse or a recap name can't be resolved.
   - Optional live per-guess timing (§7.5).
3. **Pasted NYT share text (tertiary).** If anyone pastes classic emoji shares, parse them too (§5.3).

Mechanics:
- **Live:** `on_message` and `on_message_edit` in configured channel(s), filtered to the Wordle app bot for sources 1–2.
- **Backfill:** admin command `/sheriff backfill` walks `channel.history(limit=None)` and ingests all historical recaps and the final state of each "was playing" message (idempotent). Backfill only sees the final edit, never intermediate states. This is also how the image fixture corpus gets built (§11).
- **Archive raw inputs:** save every recap's and playing message's raw text and image bytes to disk (keyed by message ID, plus edit timestamp for playing messages). This keeps reprocessing possible after parser fixes, and turns real messages into test fixtures.

### 5.2 Image parser (both layouts)
The images are synthetic renders with flat colors and fixed layouts. The tile detection is layout-agnostic, so one parser handles both the recap (many cards, grid under avatar) and the playing message (one card, grid beside avatar).

1. **Fetch** the image from the message's attachment or embed URL.
2. **Classify pixels** by nearest reference color: tile green, tile yellow, tile gray, empty tile, or background/other. Calibrate the reference colors from real fixtures and keep them in config, with a tolerance.
3. **Find tiles:** connected components of tile-colored pixels. Keep components that are roughly square and roughly the modal tile size. This automatically ignores avatars, text, and borders. Tile size differs between the two layouts, so compute it per image.
4. **Group into grids:** cluster tile centers by x into columns. Consecutive groups of 5 evenly spaced columns form a grid, and larger x-gaps separate cards. Within each grid, cluster by y into 6 rows. Expect N grids for a recap and exactly 1 for a playing message.
5. **Read each grid:** a 6×5 grid of `G`/`Y`/`B`/empty. Filled rows = the guess count.
6. **Read the day number** from the "Wordle No. N" title:
   - Crop the title region (the text above the grids, centered).
   - Read the digits by template matching against digit glyphs harvested from real fixtures. The font is fixed. Tesseract is an acceptable fallback.
   - Sanity check: N must be within ±1 of the date-derived day number for the message timestamp. Otherwise reject.
7. **Map grids to players (recap):** grid order = text order (lines top to bottom, names left to right). Parse names in order from the raw message content: `<@id>` tokens and plain-text "@Name" tokens. Resolve through `IdentityResolver` (§5.5).
8. **Validate (strict):**
   - Grid count must equal the number of names in the text.
   - Each player's filled-row count must equal their text outcome (6 for `X/6`).
   - The last filled row must be all green on a win. `X/6` must have no all-green row.
   - Every tile must classify with confidence.
   - The day number must be read successfully.

   On a recap-wide failure (count mismatch, unreadable day), store nothing from the recap. On a single-player failure, skip only that player and try their playing-message fallback. Always log and keep the raw image. **Never feed uncertain data to the scorer.**
9. **Scale-independence:** derive all thresholds from detected tile size, not absolute pixels.

Unknowns to handle as they appear: large groups (does the recap cap cards or wrap rows?), high-contrast mode, and layout changes by Discord/NYT. Each new variant becomes a fixture.

### 5.3 NYT share text parser (tertiary source)
- Header regex: `Wordle ([\d,]+) ([1-6X])/6(\*?)`
  - Strip commas from the day number.
  - `X` = failed.
  - `*` = hard mode.
- Grid rows follow the header. Map emoji to `G`/`Y`/`B`:
  - Green: 🟩, and 🟧 (high contrast)
  - Yellow: 🟨, and 🟦 (high contrast)
  - Gray: ⬛ (dark mode), ⬜ (light mode)
- Validate: the row count matches the guess count (6 rows for X), and the final row is all-green for wins.
- Handle: edits (re-parse and upsert), duplicates or reposts (one result per user per day, keeping the first valid post), and malformed shares (log and skip).
- If an image-parsed and a text-parsed grid exist for the same player-day, they must agree. If they don't, log the conflict and prefer the recap.

Note: neither image shows hard mode. Image-sourced results have `hard_mode = NULL` (unknown).

### 5.4 Reference data
- **Answer table:** fetch from the unofficial NYT endpoint `https://www.nytimes.com/svc/wordle/v2/{YYYY}-{MM}-{DD}.json`, using its `solution` field. Map Wordle day number ↔ date (day 0 = 2021-06-19). The endpoint is undocumented, so **cache every answer locally forever** and fall back to a manually importable list if it breaks.
- **Valid guess list:** the full allowed-guess dictionary (~13k words).
- **Answer-candidate list:** the curated answer list (~2.3k words). Used to weight candidates, since real players know answers are common words. Ignoring this would make honest players look suspicious.
- Optional: word-frequency weights for guess plausibility.

### 5.5 Identity resolution
**Players change display names** (observed: the same avatar appearing as "Fire Emblem: Scuba Steve" in one recap and "Fire Emblem: Fortune's Weeb" in a playing message days later). Display names are never identity.

- **Identity = Discord user ID.** Every `players` row is keyed to a `discord_user_id` in live mode.
- **`<@id>` mentions** resolve directly.
- **Plain-text "@Name" and "<Name> was playing"** resolve by matching against current guild members' display names, nicknames, and usernames at the message's timestamp. Also check a `name_history` table the bot maintains from `on_member_update` and from every resolved mention.
- **Ambiguous or unmatched names:** don't guess. Hold the result in a `pending_identity` state and retry when a later message resolves the name (e.g. a recap mention that pairs that name with an ID). Admins can also map names with `/sheriff link <name> <@user>`.
- **Cross-linking:**
  - A playing message and a recap card for the same day with identical grids are almost certainly the same player. Use this to confirm a name → ID mapping, never as the sole basis for one.
  - Optional: compare avatar crops as a tiebreaker. Avatars change too, so treat this as weak evidence.
- In test mode, the same interface resolves against run-scoped synthetic players only (§9.1).

---

## 6. Data model (sketch)

Each environment (live, or one test run) gets its **own SQLite database file**, so the schema has no environment column and test data can never mix with live data (§9). The answer cache lives in a separate shared, read-only DB.

```
players(player_id PK, discord_user_id NULL, display_name, is_synthetic BOOL, joined_at)
answers(day PK, word)                      -- shared answer-cache DB
recaps(message_id PK, kind /*recap|playing*/, day, raw_text, image_path, parsed_ok BOOL,
       error TEXT, received_at)
playing_edits(message_id, edited_at, filled_rows, grid TEXT)  -- live-captured intermediate states
name_history(discord_user_id, name, kind /*display|nick|username*/, first_seen, last_seen)
pending_identity(id PK, raw_name, day, source_message_id, grid TEXT, outcome TEXT, created_at)
results(id PK, player_id, day, guesses INT NULL /*NULL=X*/, hard_mode BOOL NULL,
        grid TEXT /*e.g. "BYBBG|GGBYG|GGGGG"*/, source /*recap_image|playing_image|share_text*/,
        source_message_id, posted_at, UNIQUE(player_id, day))
day_scores(player_id, day, surprisal_charitable, surprisal_typical,
           posterior_day, features JSON, PRIMARY KEY(player_id, day))
player_state(player_id PK, longterm_posterior, baseline_day /*for clean slate*/,
             alias TEXT NULL, bounty_amount INT NULL)
confessions(id PK, player_id, day, posterior_at_confession, punishment, created_at)
wanted_today(day, player_id, opted_in_at, PRIMARY KEY(day, player_id))
bot_messages(kind, day, channel_id, message_id)  -- for in-place edits
run_meta(key PK, value)                           -- env name, run id, scenario, created_at
```

`player_id` is internal. Live players have `discord_user_id` set. Synthetic test players have `is_synthetic = 1` and no real Discord ID.

---

## 7. Scoring engine

### 7.1 Pattern function
- `pattern(guess, answer) -> base-3 int (0..242)` using the standard two-pass algorithm (greens first, then yellows from remaining letter counts). **Duplicate letters must be correct.** Unit-test this heavily.
- Precompute a `guesses × answers` pattern table as `uint8` (~13k × 2.3k ≈ 30 MB) and cache it to disk.

### 7.2 Core idea: calibration
An honest player chooses each guess **without knowing the answer**. From their point of view, the answer is (weighted-)uniform over the candidates still consistent with their feedback. So for row *i*:

```
P(pattern_i | guess_i, knowledge) =
    weight(candidates producing pattern_i) / weight(all remaining candidates)
```

This probability depends only on the guess and the player's knowledge state. **The player's strategy doesn't matter**, so narrowing, elimination, and hybrid play are all judged fairly. Cheating breaks calibration: patterns correlate with the answer beyond what the player's knowledge allows.

Per-row surprisal = `-log P`. The final all-green row is just a pattern with probability ≈ `1/k`, where `k` is the number of remaining candidates.

### 7.3 Unknown guesses
We only see colors, not words. For each row, the set of possible guesses = valid words that produce the observed pattern against the known answer. The knowledge state depends on the whole guess sequence, so:
- **Monte Carlo:** sample N guess sequences (e.g. N = 200), drawing each row's guess from its feasible set (optionally weighted by word frequency). For each sequence, compute the candidate set and row probabilities.
- **Charitable score:** for each row, use the guess most favorable to the player (the max probability across samples). This gives an upper bound on plausibility. **Use this for anything accusatory** (bounties).
- **Typical score:** average across samples. Use this for rankings and stats.

### 7.4 Row classification (for profiling and features)
Label each row:
- **Narrowing (N):** some feasible guess is consistent with all prior feedback (it would be hard-mode-legal).
- **Eliminating (E):** no feasible guess is consistent with prior feedback (for example, a known green position goes non-green).

Hard-mode games (`*`) should have only N rows. Flag violations as data errors, not cheating.

### 7.5 Targeted features
Computed per game and stored in `day_scores.features`:

- **Row-1 greens / yellows.** An honest opener cannot depend on the answer, so this is the cleanest test of build-up cheating. Compare against the baseline distribution for random or fixed openers.
- **Opener consistency.** Is there a single word that explains all of a player's row-1 patterns (across the history)? If none exists, which minimum set of openers is needed, and are those openers unusually answer-like?
- **Green velocity.** Greens gained per row relative to what the knowledge state allowed (supports build-up detection).
- **Trap escapes.** A trap is a state where the candidates share 3–4 green positions with k ≥ 4 remaining (e.g. _IGHT). Count entries vs escapes on the next row, and compare to the expected escape rate (~1/k per guess).
- **Out-of-nowhere solves.** A solve following a row whose remaining candidate count was large (score ≈ 1/k).
- **Fake elimination.** E rows that light up greens/yellows far above the chance rate for answer-independent guesses.
- **Low variance.** The guess-count distribution is unnaturally tight (no 5s/6s over many games).
- **Missing days: never scored.** A day a player didn't play contributes nothing to any score or posterior, by design decision. The posterior is computed only over games actually played.
- **Per-guess timing (optional, live-only).** Each edit of a "was playing" message reveals one more filled row, and the edit timestamps give the time between guesses. Possible signals:
  - A long pause before an improbable solve.
  - Near-instant solves from a large candidate set.

  Caveats:
  - Timing exists only for games the bot watched live. Backfill can't recover it.
  - Verify that every guess actually triggers an edit.
  - Honest players pause for all sorts of reasons.

  Keep this as a separate, low-weight feature, disabled by default until it's validated on real data.

### 7.6 Last resort: guess-count-only scorer
Use this only for an individual player-day where every grid source failed validation (e.g. a recap layout the image parser doesn't understand yet). Score from the guess count alone, against a benchmark distribution (a strong solver bot averages ~3.4 guesses). This is much weaker evidence:
- Down-weight it heavily in the long-term posterior.
- Mark it in `/sheriff explain`.
- Count how often it happens. A spike means the image parser needs fixing, not that players changed.

### 7.7 From surprisal to probability (posterior)
Surprisal isn't a percentage. Convert it with Bayes:

```
posterior = prior × L_cheat / (prior × L_cheat + (1 − prior) × L_honest)
```

- **Prior:** configurable, default ~5% of games cheated.
- **L_honest:** from the calibration model above.
- **L_cheat:** build simulated cheater bots and fit feature likelihoods from their output:
  - *Build-up bot:* opens with words sharing many letters/positions with the answer and climbs toward it.
  - *Near-miss bot:* plays roughly honestly, then jumps to the answer from a large candidate set.
  - Optionally, a mixture of the two.
- **Honest simulation:** build honest bots with noise (narrowing, elimination, hybrid; human-level skill) to calibrate L_honest and to measure the false-positive rate.

Outputs:
- **Daily posterior:** a single game. Noisy by nature, which is fine for the daily game.
- **Long-term posterior:** combines all of a player's games since their `baseline_day`. Stable. Drives bounties.

All model parameters live in a config file so they're tunable.

### 7.8 Changepoint detection (optional, later)
Run CUSUM on per-game surprisal per player to catch someone who started cheating partway through. Surface the result in stats/explanations.

---

## 8. Bot features

### 8.1 Wanted Today (daily, opt-in)
- Posted after the recap. The public message shows only who has opted in so far, not their scores. Hidden scores are the point of the mechanic.
- Button **"👀 View the Wanted list"**:
  - On click, the clicker's daily probability is **added to the public board** (the bot edits the message in place), then the full board is shown to them.
  - Opting in is permanent for that day.
- Show probabilities as whole percentages, with a one-line flavor explanation per player (e.g. "escaped a 6-way trap on guess 3").

### 8.2 Bounty Board (long-term, anonymized)
- A bounty is posted for any player with a long-term posterior > 50%.
- Each bounty shows: alias, amount, a WANTED-poster-styled embed. **No identifying info.**
- **Alias:** generated from the lists in §10, sticky per person while the bounty is active, re-rolled after a confession.
- **Amount:** log-scaled. 50% → $50, 99% → $10,000. Round to a nice value:
  ```
  t = (p − 0.50) / 0.49            # clamp to [0, 1]
  amount = 50 × (10000 / 50) ** t  # round to $5 / $50 / $500 steps by magnitude
  ```
- Update on the daily cadence only (reduces timing leaks).

### 8.3 "Am I the bounty?"
- Button on the Bounty Board.
- Respond with an **ephemeral** message (visible only to the clicker):
  - Not a bounty: a flavored "You're clean, partner."
  - A bounty: reveal which alias is theirs, and include a **"🤠 Turn yourself in"** button. This button only ever appears in that ephemeral message.

### 8.4 Turning yourself in
- On confirm (add an "Are you sure?" step), the bot posts publicly:
  - Display name, their long-term cheat probability, and a randomly chosen punishment from §10.
- Then:
  - Remove their bounty and re-roll their alias.
  - Set `baseline_day` to today. Their long-term posterior restarts from the prior.
  - **Keep the raw results** (needed for debugging and model tuning). "The Sheriff keeps the ledger."
  - Log the event in `confessions`.

### 8.5 Slash commands
- `/sheriff backfill` (admin): ingest channel history.
- `/sheriff stats [user]`: explainable stats such as average guesses, posting rate, trap escapes vs expected, row-1 green rate vs baseline, and play-style fingerprint (elimination rate, opener consistency, switch point).
- `/sheriff explain <day> [user]`: row-by-row breakdown of a game's probabilities. The user can only view their own, unless that user has opted in that day.
- `/sheriff config` (admin): prior, channels, Wordle-bot user ID, sandbox channels, thresholds.
- `/sheriff link <name> <@user>` (admin): resolve a pending or ambiguous name to a user (§5.5).
- `/sheriff test ...` (admin, sandbox only): see §9.5.

### 8.6 Persistence
- Register buttons as **persistent views** with stable `custom_id`s so they keep working after restarts.
- Store message IDs in `bot_messages` so boards are edited in place, not re-spammed.

---

## 9. Test mode

The live loop runs once a day, so iterating against real data is far too slow. Test mode runs the **exact same** parser, scoring engine, and game logic against fake recaps in a fully isolated environment, with as many simulated days per minute as you like.

### 9.1 Isolation guarantees (hard requirements)
- **Separate storage.** Each test run gets a fresh SQLite DB, either a temp file or `:memory:` (headless). The live DB path is never opened in test mode. Enforce this in code: the `Store` factory refuses the live path when `env != live`.
- **Ephemeral players.** Each run creates its own synthetic players (`is_synthetic = 1`, internal IDs only, run-scoped names like "Test Tex"). The test `IdentityResolver` resolves only within the run and **never** looks up or touches real guild members. Fake recap text uses plain names or run-scoped placeholders, never real `<@id>` mentions, so no real user is ever pinged.
- **No live side effects.**
  - Test runs post only to configured sandbox channel(s) or to the headless recorder.
  - The `OutputSink` refuses live channels when `env != live`.
  - Every test message is prefixed `[TEST RUN <run_id>]`.
  - Live-mode ingestion ignores sandbox channels entirely, and test runs ignore live channels.
- **Shared read-only data.** Test runs may read the shared answer cache and word lists but never write to them.
- **Teardown.** Ending a run deletes its DB and, optionally, its sandbox messages. An optional `--keep` flag preserves the DB for debugging.
- **Concurrency.** Multiple runs may exist, keyed by `run_id`, with at most one active run per sandbox channel. Button `custom_id`s embed the run ID so interactions route to the right environment.

### 9.2 Fake recap sources
All fake messages go through the same `RecapEnvelope` (kind + text + image bytes + timestamp + author + edit sequence) that live messages produce, and from there through the real parser. This covers both recaps and "was playing" messages, including their edit sequences. Three ways to get them:

1. **Replay:** feed archived real recaps (§5.1) from the fixture corpus. Players are remapped to synthetic players, so real identities stay out of the run.
2. **Rendered:** a `RecapRenderer` generates text and images that **mimic the real layouts**:
   - **Recap:** dark panel, "Wordle No. N" title, cards in a horizontal row, placeholder avatar above each 5×6 grid, matching colors and proportions. Text lines grouped by outcome with ties in card order, `X/6` lines, and absent non-players. Some names are rendered as plain "@Name" text to exercise fallback resolution.
   - **Playing message:** "<Name> was playing" text plus a single card with the avatar left and the grid right, emitted as a sequence of edits (one per guess) with scenario-controlled timestamps.
   - **Day number:** rendered in the title, using the real font glyphs harvested from fixtures if possible. Calibrate the renderer against real fixtures. A good check: the parser must round-trip rendered images perfectly.
3. **Manual inject:** upload an image or paste text via a slash command, for quick one-off cases.

A **text-only shortcut** may skip image rendering for speed in large simulations, but at least one test path must exercise the real image parser end to end.

### 9.3 Scenarios
Scenarios are YAML files describing a simulated history:

```yaml
name: build_up_cheater_vs_honest
seed: 42
start_day: 1900
days: 60
answers: real            # use the answer cache; or list explicit words
players:
  - name: Honest Abe
    strategy: honest_narrowing      # honest_narrowing | honest_elimination | honest_hybrid
    skill: 0.7
  - name: Sneaky Pete
    strategy: cheat_build_up        # cheat_build_up | cheat_near_miss
    cheat_rate: 0.5                 # fraction of days cheated
    start_cheating_day: 1930        # for changepoint testing
  - name: Flaky Fran
    strategy: honest_hybrid
    rename_on_day: 1920              # display-name change (identity testing)
    post_rate: 0.6                  # skips days; must not change her scores
expect:                             # optional assertions (headless)
  - player: Sneaky Pete
    longterm_posterior_gt: 0.5
    by_day: 1950
  - player: Honest Abe
    longterm_posterior_lt: 0.2
  - player: Flaky Fran
    longterm_posterior_lt: 0.2
```

Player strategies reuse the honest and cheater simulators from §7.7. `seed` makes every run reproducible.

### 9.4 Controllable clock and the day loop
- The test `Clock` starts at `start_day` and advances only on command (or automatically in headless runs).
- Each advance: generate that day's plays from the scenario → render the recap → push it through `RecapSource` → the **normal recap trigger** fires the full daily cycle (scoring, Wanted Today, Bounty Board).
- Time-dependent logic (aliases, clean-slate baselines, CUSUM) must read only from `Clock`, never from wall time.

### 9.5 Interactive test mode (in Discord)
For testing buttons, ephemerals, and the overall feel, run in a sandbox channel (ideally a separate test server):

- `/sheriff test start [scenario]`: create a run, its DB, and its synthetic players, and post a run header.
- `/sheriff test inject`: upload a recap image and/or paste recap text as the next recap.
- `/sheriff test advance [days]`: advance the clock and run the daily cycle(s).
- `/sheriff test act-as <fake player>`: subsequent button clicks by *you* in this run are treated as that synthetic player. This is the only way to test "Am I the bounty?" and confessions from different perspectives. The mapping is per tester, per run.
- `/sheriff test status`: show the run ID, current day, players, posteriors, and parse errors.
- `/sheriff test end [--keep]`: tear down the run.

All test commands are admin-only and work only in sandbox channels.

### 9.6 Headless mode (no Discord)
A CLI harness, `python -m sheriff.sim scenarios/foo.yaml`, runs a scenario end to end with the headless `OutputSink`. That sink records every would-be post, embed, and button as structured objects. It:
- Runs in seconds, so it's suitable for pytest and CI.
- Evaluates the scenario's `expect` assertions.
- Optionally dumps a report: per-player posteriors over time, bounties posted, and parse failures.

This is the main loop for tuning the model. Use interactive mode for UX.

### 9.7 Bot startup modes
- `ENV=live` (default in production): live seams only.
- `ENV=test`: test seams only. The bot will not connect its live listeners.
- A single process can also run live with test commands enabled. In that case the per-run isolation from §9.1 still applies, and live and test code paths share no mutable state.

---

## 10. Content

### Alias format
`[First] "[Nickname]" [Last]`, or `[Nickname] [Last]`. For example: Cornelius "Two-Guess" Pettibone.

**First names:** Jebediah, Cornelius, Ezekiel, Clementine, Hezekiah, Prudence, Thaddeus, Mabel, Obadiah, Winifred, Silas, Delphine, Augustus, Temperance, Bartholomew, Henrietta, Amos, Lavinia, Ephraim, Rosalind

**Nicknames:** Two-Guess, Greenhorn, Lucky Streak, Yellowbelly, Six-Shooter, Dead-Eye, Copperhead, Snake Oil, Quickdraw, Five-Letter, Hornswoggle, No-Gray, Sidewinder, Tin Star, Long Odds, Card Sharp, Trap Dodger, Dusty

**Last names:** McGraw, Buckley, Whitlock, Pettibone, Crenshaw, Hollister, Blackwood, Tumbleweed, Fairweather, Haverford, Calloway, Thistlewood, Redfern, Cartwright, Ashby, Munroe

### Punishments
- Sentenced to muck the stables for a fortnight
- Run out of town on a rail
- Must play hard mode until the next full moon
- Tarred, feathered, and forced to open with "QAJAQ"
- Banished to the Dodge City saloon to sweep floors
- Sheriff confiscates their horse and their vowels
- Must buy a round for the whole saloon
- Shall ride backwards on a mule through Main Street
- Chained to the hitching post with only consonants for company
- Ordered to guess "XYLYL" first thing every morning for a week

### Flavor text (examples)
- Clean result: "You're clean, partner. For now."
- Opt-in: "Takes a brave soul to look at the Wanted list."
- Confession: "Honesty's a rare thing in these parts."

---

## 11. Testing and validation

- **Pattern function:** exhaustive duplicate-letter cases (e.g. guess SPEED vs ABIDE, LLAMA vs HELLO).
- **Image parser:**
  - Golden tests on the archived real corpus, covering both layouts and `X/6` cards. Each fixture has a hand-verified expected result.
  - Round-trip tests on rendered recaps: render → parse → identical grids.
  - Perturbation tests: rescaled images, JPEG-ish noise, extra/missing cards. These must either parse correctly or fail validation. They must **never** produce wrong data silently.
- **Day-number reading:** golden tests on real titles for both layouts, plus rejection of out-of-range values.
- **Identity resolution:**
  - Mention pills, plain-text names, and playing-message names.
  - A mid-history display-name change must keep one player.
  - Ambiguous names must land in `pending_identity`, never get guessed.
- **Text parser:** fixtures for light/dark/high-contrast, hard mode, X/6, edits, commas in day numbers, malformed input.
- **Calibration sanity check:** simulated honest bots should produce surprisal consistent with the model (well-calibrated p-values, roughly uniform).
- **False-positive rate:** headless scenarios with only honest players over 100+ days. Long-term posteriors above 50% should be rare. Tune the prior and model until this holds.
- **Detection power:** scenarios with each cheater type should cross 50% within a reasonable number of days. Changepoint scenarios should flag the onset.
- **Isolation tests:** assert that a test run cannot open the live DB, cannot post to a live channel, cannot resolve a real user, and leaves nothing behind after teardown.
- **Bot interactions:** in interactive test mode, test the opt-in board, ephemeral bounty check, `act-as` confessions, persistent views after a restart, and in-place edits.

---

## 12. Milestones

1. **Recon and fixtures:** backfill and archive all real recap messages and images. Hand-label a handful of them as golden fixtures. Calibrate tile colors.
2. **Seams and test harness skeleton:** define the interfaces (§3), per-run stores, and the headless sink. Build this early so everything after it is testable.
3. **Ingestion:** image parser for both layouts (golden + round-trip tests), day-number reader, recap text parser, card → player mapping, identity resolution and name history, playing-message capture (including edit sequences), NYT answer fetch/cache, share-text parser.
4. **Engine core:** pattern table, candidate filtering, Monte Carlo calibration scoring (charitable and typical).
5. **Simulators and models:** honest and cheater bots, feature extraction, posterior, headless scenario validation (§11).
6. **Recap renderer and full headless day loop.**
7. **Daily loop in Discord:** recap trigger, Wanted Today board with opt-in.
8. **Bounties:** aliases, scaling, "Am I the bounty?", confessions, clean slate.
9. **Interactive test mode:** sandbox commands, `act-as`.
10. **Polish:** `/sheriff stats` and `explain`, flavor text, CUSUM changepoints.

---

## 13. Open questions

- How do large groups render in the recap: is there a card cap, wrapping onto a second row, or multiple images?
- Does every guess trigger an edit of the "was playing" message? This determines whether per-guess timing is usable.
- Why do some recap names render as plain text instead of mention pills (left the server, nickname quirk, something else)?
- Default prior: tune after seeing real data.

### Resolved
- `X/6` appears only for players who made all 6 guesses. Non-players are absent from the recap.
- "Was playing" messages show the board, updated live as the player plays.
- Recap card order = text order, including within ties.
- Missing days never count against players.
- No opt-out from tracking.
