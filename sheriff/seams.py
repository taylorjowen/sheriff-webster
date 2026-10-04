"""Interfaces to the outside world (§3 seams). Live and test implementations plug in here.

The parser, scoring engine and game logic only ever talk to these.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .days import datetime_to_day, day_to_datetime


# ---------------------------------------------------------------- envelopes

@dataclass
class RecapEnvelope:
    """One inbound message from any source, live or fake. Goes through the real parser."""
    kind: str                       # "recap" | "playing" | "share"
    message_id: str
    channel_id: str
    author_id: str
    text: str
    timestamp: datetime
    images: list[bytes] = field(default_factory=list)
    edited_at: datetime | None = None
    edit_seq: int = 0
    author_name: str | None = None
    # Text-only shortcut for large simulations (test env only): skip image parsing.
    synthetic_grids: list[list[str]] | None = None
    synthetic_day: int | None = None

    @property
    def effective_time(self) -> datetime:
        return self.edited_at or self.timestamp


class RecapSource(abc.ABC):
    """Pushes envelopes into a handler (the game)."""

    @abc.abstractmethod
    async def start(self, handler: Callable[[RecapEnvelope], Awaitable[Any]]) -> None: ...


# ---------------------------------------------------------------- clock

class Clock(abc.ABC):
    @abc.abstractmethod
    def now(self) -> datetime: ...

    def today_day(self) -> int:
        return datetime_to_day(self.now())


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class TestClock(Clock):
    __test__ = False  # not a pytest class

    def __init__(self, start_day: int):
        self._now = day_to_datetime(start_day, hour=12)

    def now(self) -> datetime:
        return self._now

    def advance_days(self, n: int = 1) -> None:
        self._now += timedelta(days=n)

    def advance_seconds(self, s: float) -> None:
        self._now += timedelta(seconds=s)


# ---------------------------------------------------------------- outbound messages

@dataclass
class Button:
    label: str
    custom_id: str
    style: str = "primary"          # primary | secondary | success | danger
    emoji: str | None = None


@dataclass
class EmbedSpec:
    title: str = ""
    description: str = ""
    color: int = 0x8B5A2B
    fields: list[tuple[str, str, bool]] = field(default_factory=list)
    footer: str | None = None


@dataclass
class OutMessage:
    content: str = ""
    embeds: list[EmbedSpec] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)
    image: bytes | None = None
    image_name: str = "image.png"
    ephemeral: bool = False


class OutputSink(abc.ABC):
    """Where public posts go. Returns opaque message refs for in-place edits."""

    @abc.abstractmethod
    async def post(self, msg: OutMessage) -> str: ...

    @abc.abstractmethod
    async def edit(self, ref: str, msg: OutMessage) -> None: ...

    async def delete(self, ref: str) -> None:  # optional
        return None


# ---------------------------------------------------------------- identity

class Unresolved:
    """Name couldn't be resolved: either no match or several."""

    def __init__(self, reason: str, candidates: list[Any] | None = None):
        self.reason = reason
        self.candidates = candidates or []

    def __repr__(self) -> str:
        return f"Unresolved({self.reason}, {self.candidates})"


class IdentityResolver(abc.ABC):
    @abc.abstractmethod
    async def resolve_mention(self, user_id: str, at: datetime) -> int | Unresolved:
        """`<@id>` mention -> internal player_id."""

    @abc.abstractmethod
    async def resolve_name(self, name: str, at: datetime) -> int | Unresolved:
        """Plain-text name -> internal player_id, or Unresolved. Never guesses."""


# ---------------------------------------------------------------- answers

class AnswerProvider(abc.ABC):
    @abc.abstractmethod
    async def get(self, day: int) -> str | None: ...

    def known_answers(self) -> list[str]:
        return []
