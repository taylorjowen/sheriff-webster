"""Text parsers: Wordle-app recap text, "X was playing" text, and pasted NYT share text."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

OUTCOME_LINE = re.compile(r"(?:^|\s)([1-6Xx])/6\s*:\s*(.*)$")
MENTION = re.compile(r"<@!?(\d+)>")
PLAYING = re.compile(r"^\s*(.+?)\s+was playing\s*$", re.IGNORECASE | re.DOTALL)
SHARE_HEADER = re.compile(r"Wordle\s+([\d,.\s]+?)\s+([1-6X])/6(\*?)", re.IGNORECASE)

EMOJI_MAP = {
    "\U0001F7E9": "G",  # green square
    "\U0001F7E7": "G",  # orange square (high contrast)
    "\U0001F7E8": "Y",  # yellow square
    "\U0001F7E6": "Y",  # blue square (high contrast)
    "⬛": "B",      # black large square (dark mode)
    "⬜": "B",      # white large square (light mode)
}


@dataclass
class NameToken:
    """A player reference in recap text, in card order."""
    kind: str          # "mention" | "name"
    value: str         # user id or plain name

    def __str__(self) -> str:
        return f"<@{self.value}>" if self.kind == "mention" else f"@{self.value}"


@dataclass
class RecapLine:
    outcome: str                        # "1".."6" or "X"
    names: list[NameToken] = field(default_factory=list)


@dataclass
class RecapText:
    lines: list[RecapLine]

    @property
    def entries(self) -> list[tuple[NameToken, str]]:
        """(name, outcome) in card order: lines top to bottom, names left to right."""
        return [(n, line.outcome) for line in self.lines for n in line.names]


def _split_names(segment: str) -> list[NameToken]:
    out: list[NameToken] = []
    pos = 0
    for m in MENTION.finditer(segment):
        out.extend(_plain_names(segment[pos:m.start()]))
        out.append(NameToken("mention", m.group(1)))
        pos = m.end()
    out.extend(_plain_names(segment[pos:]))
    return out


def _plain_names(text: str) -> list[NameToken]:
    # "@Name One @Name Two" -> ["Name One", "Name Two"]. Names may contain spaces.
    parts = re.split(r"(?:^|\s)@", " " + text)
    names = []
    for p in parts[1:]:
        name = p.strip().strip(",")
        if name:
            names.append(NameToken("name", name))
    return names


def parse_recap_text(text: str) -> RecapText | None:
    lines: list[RecapLine] = []
    for raw in text.splitlines():
        m = OUTCOME_LINE.search(raw)
        if not m:
            continue
        outcome = m.group(1).upper()
        names = _split_names(m.group(2))
        if names:
            lines.append(RecapLine(outcome, names))
    return RecapText(lines) if lines else None


def parse_playing_text(text: str) -> str | None:
    m = PLAYING.match(text.strip())
    return m.group(1).strip() if m else None


def classify_wordle_bot_text(text: str) -> str | None:
    if parse_playing_text(text):
        return "playing"
    if parse_recap_text(text):
        return "recap"
    return None


@dataclass
class ShareResult:
    day: int
    guesses: int | None    # None = X
    hard_mode: bool
    rows: list[str]

    @property
    def grid(self) -> str:
        return "|".join(self.rows)


class ShareParseError(ValueError):
    pass


def parse_share_text(text: str) -> ShareResult | None:
    """Returns None if there's no share header; raises ShareParseError if malformed."""
    m = SHARE_HEADER.search(text)
    if not m:
        return None
    day_str = re.sub(r"[,.\s]", "", m.group(1))
    if not day_str.isdigit():
        raise ShareParseError(f"bad day number {m.group(1)!r}")
    day = int(day_str)
    guesses = None if m.group(2).upper() == "X" else int(m.group(2))
    hard = m.group(3) == "*"
    rows: list[str] = []
    for line in text[m.end():].splitlines():
        cells = [EMOJI_MAP[ch] for ch in line if ch in EMOJI_MAP]
        if not cells:
            if rows:
                break
            continue
        if len(cells) != 5:
            raise ShareParseError(f"row with {len(cells)} tiles")
        rows.append("".join(cells))
    expected = 6 if guesses is None else guesses
    if len(rows) != expected:
        raise ShareParseError(f"{len(rows)} rows for outcome {m.group(2)}")
    if guesses is not None and rows[-1] != "GGGGG":
        raise ShareParseError("winning share does not end all-green")
    if any(r == "GGGGG" for r in (rows if guesses is None else rows[:-1])):
        raise ShareParseError("all-green row before the end")
    return ShareResult(day, guesses, hard, rows)
