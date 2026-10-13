"""High-level parsing of envelopes into validated results (strict, §5.2 step 8)."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..days import datetime_to_day
from ..seams import RecapEnvelope
from .image import ParsedImage, parse_image
from .text import NameToken, parse_playing_text, parse_recap_text


@dataclass
class PlayerCard:
    token: NameToken
    outcome: str                  # "1".."6" | "X"
    rows: list[str] | None = None
    error: str | None = None

    @property
    def guesses(self) -> int | None:
        return None if self.outcome == "X" else int(self.outcome)

    @property
    def grid(self) -> str | None:
        return "|".join(self.rows) if self.rows else None


@dataclass
class ParsedRecap:
    day: int | None
    cards: list[PlayerCard] = field(default_factory=list)
    error: str | None = None


@dataclass
class ParsedPlaying:
    name: str | None
    day: int | None
    rows: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def finished(self) -> bool:
        return bool(self.rows) and (self.rows[-1] == "GGGGG" or len(self.rows) == 6)

    @property
    def outcome(self) -> str:
        if self.rows and self.rows[-1] == "GGGGG":
            return str(len(self.rows))
        return "X"


def validate_rows(rows: list[str], outcome: str) -> str | None:
    expected = 6 if outcome == "X" else int(outcome)
    if len(rows) != expected:
        return f"{len(rows)} filled rows but outcome {outcome}/6"
    wins = [i for i, r in enumerate(rows) if r == "GGGGG"]
    if outcome == "X":
        if wins:
            return "X/6 card has an all-green row"
    elif wins != [len(rows) - 1]:
        return "winning card doesn't end with its only all-green row"
    return None


def _implied_outcome(rows: list[str]) -> str | None:
    if rows and rows[-1] == "GGGGG":
        return str(len(rows))
    return "X" if len(rows) == 6 else None


def _align_missing_cards(entries: list[tuple[NameToken, str]],
                         grids: list[tuple[list[str] | None, str | None]]):
    """Pair grids with names when the image has fewer cards than the text has names.

    The Wordle app sometimes leaves a listed player's card out of the image. Each grid's outcome
    is known from the grid itself, so grids are matched to the text's outcome groups: a group with
    as many grids as names pairs up as usual, a group with no grids loses only its cards, and a
    group with some but not all of its grids is ambiguous (we can't tell whose card is missing),
    so none of its players get a grid. Returns None when the image can't be reconciled at all.
    """
    if len(grids) > len(entries):
        return None
    implied = []
    for rows, _ in grids:
        outcome = _implied_outcome(rows) if rows else None
        if outcome is None:
            return None
        implied.append(outcome)
    order = list(dict.fromkeys(o for _, o in entries))
    if any(o not in order for o in implied):
        return None
    ranks = [order.index(o) for o in implied]
    if ranks != sorted(ranks):   # cards must appear in the same order as the text
        return None
    aligned: list[tuple[list[str] | None, str | None]] = []
    for _, outcome in entries:
        names = sum(1 for _, o in entries if o == outcome)
        have = [rows for (rows, _), o in zip(grids, implied) if o == outcome]
        if len(have) > names:
            return None
        if len(have) == names:
            seen = sum(1 for _, o in entries[:len(aligned)] if o == outcome)
            aligned.append((have[seen], None))
        elif not have:
            aligned.append((None, "no card in recap image"))
        else:
            aligned.append((None, f"only {len(have)} of {names} {outcome}/6 cards in recap image"))
    return aligned


def _image_for(env: RecapEnvelope, cfg: Config, expected_day: int) -> ParsedImage | None:
    for data in env.images:
        try:
            parsed = parse_image(data, cfg, expected_day)
        except Exception as e:  # corrupt image etc.
            return ParsedImage([], None, f"image error: {e}", 0.0, error=f"image error: {e}")
        if parsed.grids:
            return parsed
    return None


def parse_recap(env: RecapEnvelope, cfg: Config, allow_synthetic: bool = False) -> ParsedRecap:
    text = parse_recap_text(env.text)
    if text is None:
        return ParsedRecap(None, error="no outcome lines in recap text")
    entries = text.entries
    expected_day = datetime_to_day(env.timestamp)

    if env.synthetic_grids is not None:
        if not allow_synthetic:
            return ParsedRecap(None, error="synthetic grids are only allowed in test environments")
        day = env.synthetic_day
        grids: list[tuple[list[str] | None, str | None]] = [(g, None) for g in env.synthetic_grids]
    else:
        img = _image_for(env, cfg, expected_day)
        if img is None:
            return ParsedRecap(None, error="no parseable image")
        if img.error:
            return ParsedRecap(None, error=img.error)
        if img.day is None:
            return ParsedRecap(None, error=f"day number unreadable: {img.day_error}")
        day = img.day
        grids = [(g.rows if g.ok else None, g.error) for g in img.grids]

    if day is None:
        return ParsedRecap(None, error="no day number")
    if len(grids) != len(entries):
        aligned = _align_missing_cards(entries, grids)
        if aligned is None:
            return ParsedRecap(day, error=f"{len(grids)} grids but {len(entries)} names in text")
        grids = aligned

    cards = []
    for (token, outcome), (rows, gerr) in zip(entries, grids):
        card = PlayerCard(token, outcome)
        if rows is None:
            card.error = gerr or "grid unreadable"
        else:
            err = validate_rows(rows, outcome)
            if err:
                card.error = err
            else:
                card.rows = rows
        cards.append(card)
    return ParsedRecap(day, cards)


def parse_playing(env: RecapEnvelope, cfg: Config, allow_synthetic: bool = False) -> ParsedPlaying:
    name = parse_playing_text(env.text)
    if name is None:
        return ParsedPlaying(None, None, error="not a 'was playing' message")
    expected_day = datetime_to_day(env.timestamp)
    if env.synthetic_grids is not None:
        if not allow_synthetic:
            return ParsedPlaying(name, None, error="synthetic grids are only allowed in test environments")
        rows = env.synthetic_grids[0] if env.synthetic_grids else []
        return ParsedPlaying(name, env.synthetic_day, rows)
    img = _image_for(env, cfg, expected_day)
    if img is None or img.error:
        return ParsedPlaying(name, None, error=(img.error if img else "no parseable image"))
    if img.day is None:
        return ParsedPlaying(name, None, error=f"day number unreadable: {img.day_error}")
    if len(img.grids) != 1:
        return ParsedPlaying(name, img.day, error=f"expected 1 grid, found {len(img.grids)}")
    g = img.grids[0]
    if not g.ok:
        return ParsedPlaying(name, img.day, error=g.error)
    rows = g.rows
    wins = [i for i, r in enumerate(rows) if r == "GGGGG"]
    if wins and wins != [len(rows) - 1]:
        return ParsedPlaying(name, img.day, error="all-green row before the end")
    return ParsedPlaying(name, img.day, rows)
