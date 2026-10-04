"""SQLite storage. One DB file per environment (live, or one test run).

Isolation (§9.1): `open_store` refuses the live DB path for any non-live env.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS players(
    player_id INTEGER PRIMARY KEY,
    discord_user_id TEXT UNIQUE,
    display_name TEXT NOT NULL,
    is_synthetic INTEGER NOT NULL DEFAULT 0,
    joined_at TEXT
);
CREATE TABLE IF NOT EXISTS recaps(
    message_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    day INTEGER,
    raw_text TEXT,
    image_path TEXT,
    parsed_ok INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    received_at TEXT,
    PRIMARY KEY(message_id)
);
CREATE TABLE IF NOT EXISTS playing_edits(
    message_id TEXT NOT NULL,
    edited_at TEXT NOT NULL,
    filled_rows INTEGER,
    grid TEXT,
    PRIMARY KEY(message_id, edited_at)
);
CREATE TABLE IF NOT EXISTS name_history(
    discord_user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    first_seen TEXT,
    last_seen TEXT,
    PRIMARY KEY(discord_user_id, name, kind)
);
CREATE TABLE IF NOT EXISTS pending_identity(
    id INTEGER PRIMARY KEY,
    raw_name TEXT NOT NULL,
    day INTEGER NOT NULL,
    source_message_id TEXT,
    source TEXT,
    grid TEXT,
    outcome TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS results(
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    day INTEGER NOT NULL,
    guesses INTEGER,
    hard_mode INTEGER,
    grid TEXT,
    source TEXT NOT NULL,
    source_message_id TEXT,
    posted_at TEXT,
    UNIQUE(player_id, day)
);
CREATE TABLE IF NOT EXISTS day_scores(
    player_id INTEGER NOT NULL,
    day INTEGER NOT NULL,
    surprisal_charitable REAL,
    surprisal_typical REAL,
    posterior_day REAL,
    features TEXT,
    PRIMARY KEY(player_id, day)
);
CREATE TABLE IF NOT EXISTS player_state(
    player_id INTEGER PRIMARY KEY,
    longterm_posterior REAL,
    baseline_day INTEGER,
    alias TEXT,
    bounty_amount INTEGER
);
CREATE TABLE IF NOT EXISTS confessions(
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL,
    day INTEGER,
    posterior_at_confession REAL,
    punishment TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS wanted_today(
    day INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    opted_in_at TEXT,
    PRIMARY KEY(day, player_id)
);
CREATE TABLE IF NOT EXISTS bot_messages(
    kind TEXT NOT NULL,
    day INTEGER NOT NULL DEFAULT -1,
    channel_id TEXT,
    message_id TEXT,
    PRIMARY KEY(kind, day)
);
CREATE TABLE IF NOT EXISTS run_meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    day INTEGER,
    detail TEXT,
    created_at TEXT
);
"""


class IsolationError(RuntimeError):
    """A test environment tried to touch live resources."""


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


@dataclass
class Result:
    id: int
    player_id: int
    day: int
    guesses: int | None       # None = X (failed)
    hard_mode: bool | None
    grid: str | None          # "BYBBG|GGBYG|GGGGG"; None = guess-count only
    source: str
    source_message_id: str | None
    posted_at: str | None

    @property
    def rows(self) -> list[str]:
        return self.grid.split("|") if self.grid else []

    @property
    def outcome(self) -> str:
        return "X" if self.guesses is None else str(self.guesses)


def open_store(path: str | Path, env: str, live_db_path: str | Path) -> "Store":
    """Store factory. Refuses the live DB for non-live environments."""
    if str(path) != ":memory:":
        if env != "live" and Path(path).resolve() == Path(live_db_path).resolve():
            raise IsolationError(f"env={env!r} may not open the live database {live_db_path}")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return Store(path, env)


class Store:
    def __init__(self, path: str | Path, env: str):
        self.path = str(path)
        self.env = env
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _x(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self.db.execute(sql, tuple(args))
        self.db.commit()
        return cur

    def _q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, tuple(args)).fetchall()

    # ------------------------------------------------------------ players
    def create_player(self, display_name: str, discord_user_id: str | None = None,
                      synthetic: bool = False, joined_at: datetime | None = None) -> int:
        cur = self._x("INSERT INTO players(discord_user_id, display_name, is_synthetic, joined_at) VALUES(?,?,?,?)",
                      (discord_user_id, display_name, int(synthetic), _iso(joined_at)))
        return cur.lastrowid

    def player_by_discord(self, uid: str) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM players WHERE discord_user_id=?", (str(uid),))
        return rows[0] if rows else None

    def player(self, pid: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM players WHERE player_id=?", (pid,))
        return rows[0] if rows else None

    def players(self) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM players ORDER BY player_id")

    def player_by_name(self, name: str) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM players WHERE display_name=? COLLATE NOCASE", (name,))

    def set_display_name(self, pid: int, name: str) -> None:
        self._x("UPDATE players SET display_name=? WHERE player_id=?", (name, pid))

    # ------------------------------------------------------------ names
    def record_name(self, uid: str, name: str, kind: str, at: datetime) -> None:
        if not name:
            return
        ts = _iso(at)
        self._x("""INSERT INTO name_history(discord_user_id, name, kind, first_seen, last_seen) VALUES(?,?,?,?,?)
                   ON CONFLICT(discord_user_id, name, kind) DO UPDATE SET
                     first_seen=MIN(first_seen, excluded.first_seen),
                     last_seen=MAX(last_seen, excluded.last_seen)""",
                (str(uid), name, kind, ts, ts))

    def name_matches(self, name: str) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM name_history WHERE name=? COLLATE NOCASE", (name,))

    # ------------------------------------------------------------ raw messages
    def upsert_recap(self, message_id: str, kind: str, day: int | None, raw_text: str,
                     image_path: str | None, parsed_ok: bool, error: str | None, received_at: datetime) -> None:
        self._x("""INSERT INTO recaps(message_id, kind, day, raw_text, image_path, parsed_ok, error, received_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(message_id) DO UPDATE SET kind=excluded.kind, day=excluded.day,
                     raw_text=excluded.raw_text, image_path=excluded.image_path,
                     parsed_ok=excluded.parsed_ok, error=excluded.error, received_at=excluded.received_at""",
                (str(message_id), kind, day, raw_text, image_path, int(parsed_ok), error, _iso(received_at)))

    def recap(self, message_id: str) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM recaps WHERE message_id=?", (str(message_id),))
        return rows[0] if rows else None

    def recaps(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return self._q("SELECT * FROM recaps WHERE kind=? ORDER BY received_at", (kind,))
        return self._q("SELECT * FROM recaps ORDER BY received_at")

    def add_playing_edit(self, message_id: str, edited_at: datetime, filled_rows: int, grid: str) -> None:
        self._x("INSERT OR REPLACE INTO playing_edits(message_id, edited_at, filled_rows, grid) VALUES(?,?,?,?)",
                (str(message_id), _iso(edited_at), filled_rows, grid))

    def playing_edits(self, message_id: str) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM playing_edits WHERE message_id=? ORDER BY edited_at", (str(message_id),))

    # ------------------------------------------------------------ pending identity
    def add_pending(self, raw_name: str, day: int, source_message_id: str, source: str,
                    grid: str | None, outcome: str, at: datetime) -> int:
        existing = self._q("SELECT id FROM pending_identity WHERE raw_name=? AND day=? AND source=?",
                           (raw_name, day, source))
        if existing:
            self._x("UPDATE pending_identity SET grid=?, outcome=?, source_message_id=? WHERE id=?",
                    (grid, outcome, str(source_message_id), existing[0]["id"]))
            return existing[0]["id"]
        return self._x("""INSERT INTO pending_identity(raw_name, day, source_message_id, source, grid, outcome, created_at)
                          VALUES(?,?,?,?,?,?,?)""",
                       (raw_name, day, str(source_message_id), source, grid, outcome, _iso(at))).lastrowid

    def pending(self) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM pending_identity ORDER BY day, id")

    def delete_pending(self, pid: int) -> None:
        self._x("DELETE FROM pending_identity WHERE id=?", (pid,))

    # ------------------------------------------------------------ results
    @staticmethod
    def _result(row: sqlite3.Row) -> Result:
        return Result(row["id"], row["player_id"], row["day"], row["guesses"],
                      None if row["hard_mode"] is None else bool(row["hard_mode"]),
                      row["grid"], row["source"], row["source_message_id"], row["posted_at"])

    def result(self, pid: int, day: int) -> Result | None:
        rows = self._q("SELECT * FROM results WHERE player_id=? AND day=?", (pid, day))
        return self._result(rows[0]) if rows else None

    def put_result(self, pid: int, day: int, guesses: int | None, hard_mode: bool | None, grid: str | None,
                   source: str, source_message_id: str | None, posted_at: datetime | None) -> None:
        """Insert or replace; invalidates the day's score so it gets recomputed."""
        self._x("""INSERT INTO results(player_id, day, guesses, hard_mode, grid, source, source_message_id, posted_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(player_id, day) DO UPDATE SET guesses=excluded.guesses, hard_mode=excluded.hard_mode,
                     grid=excluded.grid, source=excluded.source, source_message_id=excluded.source_message_id,
                     posted_at=excluded.posted_at""",
                (pid, day, guesses, None if hard_mode is None else int(hard_mode), grid, source,
                 None if source_message_id is None else str(source_message_id), _iso(posted_at)))
        self._x("DELETE FROM day_scores WHERE player_id=? AND day=?", (pid, day))

    def results_for_day(self, day: int) -> list[Result]:
        return [self._result(r) for r in self._q("SELECT * FROM results WHERE day=? ORDER BY player_id", (day,))]

    def results_for_player(self, pid: int, since_day: int | None = None) -> list[Result]:
        if since_day is None:
            rows = self._q("SELECT * FROM results WHERE player_id=? ORDER BY day", (pid,))
        else:
            rows = self._q("SELECT * FROM results WHERE player_id=? AND day>? ORDER BY day", (pid, since_day))
        return [self._result(r) for r in rows]

    def unscored_results(self, up_to_day: int | None = None) -> list[Result]:
        sql = """SELECT r.* FROM results r LEFT JOIN day_scores s ON s.player_id=r.player_id AND s.day=r.day
                 WHERE s.player_id IS NULL"""
        args: tuple = ()
        if up_to_day is not None:
            sql += " AND r.day<=?"
            args = (up_to_day,)
        return [self._result(r) for r in self._q(sql + " ORDER BY r.day, r.player_id", args)]

    def result_days(self) -> list[int]:
        return [r[0] for r in self._q("SELECT DISTINCT day FROM results ORDER BY day")]

    # ------------------------------------------------------------ scores
    def put_day_score(self, pid: int, day: int, s_char: float, s_typ: float, post: float, features: dict) -> None:
        self._x("""INSERT OR REPLACE INTO day_scores(player_id, day, surprisal_charitable, surprisal_typical,
                   posterior_day, features) VALUES(?,?,?,?,?,?)""",
                (pid, day, s_char, s_typ, post, json.dumps(features)))

    def day_score(self, pid: int, day: int) -> dict | None:
        rows = self._q("SELECT * FROM day_scores WHERE player_id=? AND day=?", (pid, day))
        return self._score(rows[0]) if rows else None

    @staticmethod
    def _score(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["features"] = json.loads(d["features"] or "{}")
        return d

    def day_scores_for_day(self, day: int) -> list[dict]:
        return [self._score(r) for r in self._q("SELECT * FROM day_scores WHERE day=? ORDER BY player_id", (day,))]

    def day_scores_for_player(self, pid: int, since_day: int | None = None) -> list[dict]:
        if since_day is None:
            rows = self._q("SELECT * FROM day_scores WHERE player_id=? ORDER BY day", (pid,))
        else:
            rows = self._q("SELECT * FROM day_scores WHERE player_id=? AND day>? ORDER BY day", (pid, since_day))
        return [self._score(r) for r in rows]

    # ------------------------------------------------------------ player state
    def state(self, pid: int) -> dict:
        rows = self._q("SELECT * FROM player_state WHERE player_id=?", (pid,))
        if rows:
            return dict(rows[0])
        return {"player_id": pid, "longterm_posterior": None, "baseline_day": None, "alias": None, "bounty_amount": None}

    def put_state(self, pid: int, **fields: Any) -> None:
        cur = self.state(pid)
        cur.update(fields)
        self._x("""INSERT OR REPLACE INTO player_state(player_id, longterm_posterior, baseline_day, alias, bounty_amount)
                   VALUES(?,?,?,?,?)""",
                (pid, cur["longterm_posterior"], cur["baseline_day"], cur["alias"], cur["bounty_amount"]))

    def active_bounties(self) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM player_state WHERE bounty_amount IS NOT NULL ORDER BY bounty_amount DESC, player_id")]

    # ------------------------------------------------------------ confessions / wanted
    def add_confession(self, pid: int, day: int, posterior: float, punishment: str, at: datetime) -> None:
        self._x("INSERT INTO confessions(player_id, day, posterior_at_confession, punishment, created_at) VALUES(?,?,?,?,?)",
                (pid, day, posterior, punishment, _iso(at)))

    def confessions(self) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM confessions ORDER BY id")

    def opt_in(self, day: int, pid: int, at: datetime) -> bool:
        cur = self._x("INSERT OR IGNORE INTO wanted_today(day, player_id, opted_in_at) VALUES(?,?,?)", (day, pid, _iso(at)))
        return cur.rowcount > 0

    def opted_in(self, day: int) -> list[int]:
        return [r[0] for r in self._q("SELECT player_id FROM wanted_today WHERE day=? ORDER BY opted_in_at, player_id", (day,))]

    # ------------------------------------------------------------ bot messages / meta / events
    def bot_message(self, kind: str, day: int = -1) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM bot_messages WHERE kind=? AND day=?", (kind, day))
        return rows[0] if rows else None

    def set_bot_message(self, kind: str, day: int, channel_id: str | None, message_id: str) -> None:
        self._x("INSERT OR REPLACE INTO bot_messages(kind, day, channel_id, message_id) VALUES(?,?,?,?)",
                (kind, day, channel_id, message_id))

    def bot_messages(self) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM bot_messages")

    def set_meta(self, key: str, value: Any) -> None:
        self._x("INSERT OR REPLACE INTO run_meta(key, value) VALUES(?,?)", (key, json.dumps(value)))

    def meta(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM run_meta WHERE key=?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def set_setting(self, key: str, value: Any) -> None:
        self._x("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, json.dumps(value)))

    def settings(self) -> dict[str, Any]:
        return {r[0]: json.loads(r[1]) for r in self._q("SELECT key, value FROM settings")}

    def log_event(self, kind: str, day: int | None, detail: Any, at: datetime | None = None) -> None:
        self._x("INSERT INTO events(kind, day, detail, created_at) VALUES(?,?,?,?)",
                (kind, day, json.dumps(detail, default=str), _iso(at or datetime.now(timezone.utc))))

    def events(self, kind: str | None = None, limit: int = 50) -> list[dict]:
        if kind:
            rows = self._q("SELECT * FROM events WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))
        else:
            rows = self._q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out

    def count_events(self, kind: str) -> int:
        return self._q("SELECT COUNT(*) FROM events WHERE kind=?", (kind,))[0][0]
