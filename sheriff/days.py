"""Wordle day number <-> calendar date. Day 0 = 2021-06-19."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

EPOCH = date(2021, 6, 19)


def day_to_date(day: int) -> date:
    return EPOCH + timedelta(days=day)


def date_to_day(d: date) -> int:
    return (d - EPOCH).days


def datetime_to_day(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return date_to_day(dt.astimezone(timezone.utc).date())


def day_to_datetime(day: int, hour: int = 12) -> datetime:
    d = day_to_date(day)
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)
