"""Parsing helpers for the scan time window."""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Tuple

from .errors import GitfieldError

_DURATION_RE = re.compile(
    r"^(\d+)\s*(y|yr|yrs|year|years|mo|mon|month|months|w|wk|week|weeks|d|day|days|h|hr|hour|hours)$",
    re.IGNORECASE,
)
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _shift_months(value: datetime, months: int) -> datetime:
    total_months = value.year * 12 + (value.month - 1) - months
    year, month_index = divmod(total_months, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _parse_absolute(spec: str, *, end_of_day: bool) -> datetime:
    text = spec.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        if _DATE_ONLY_RE.match(text):
            parsed_date = date.fromisoformat(text)
            if end_of_day:
                parsed = datetime.combine(parsed_date, time(23, 59, 59, 999999))
            else:
                parsed = datetime.combine(parsed_date, time.min)
        else:
            parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise GitfieldError(
            f"invalid date {spec!r}; use YYYY-MM-DD, an ISO-8601 timestamp, "
            "or a relative duration such as 2y, 18mo, or 730d"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_since(spec: str, now: Optional[datetime] = None) -> datetime:
    """Parse a --since value.

    Durations are relative to *now* and support years, months, weeks, days and
    hours.  Absolute values without a timezone are interpreted as UTC.
    """

    current = now or utc_now()
    text = spec.strip()
    match = _DURATION_RE.match(text)
    if not match:
        return _parse_absolute(text, end_of_day=False)

    amount = int(match.group(1))
    unit = match.group(2).lower()
    if amount < 0:
        raise GitfieldError("--since duration must be positive")
    if unit.startswith("y"):
        return _shift_months(current, amount * 12)
    if unit.startswith("mo"):
        return _shift_months(current, amount)
    if unit.startswith("w"):
        return current - timedelta(weeks=amount)
    if unit.startswith("d"):
        return current - timedelta(days=amount)
    return current - timedelta(hours=amount)


def parse_until(spec: Optional[str], now: Optional[datetime] = None) -> datetime:
    current = now or utc_now()
    if spec is None or spec.strip().lower() == "now":
        return current
    if _DURATION_RE.match(spec.strip()):
        raise GitfieldError(
            "--until requires an absolute date such as 2026-01-31; "
            "relative durations are only supported by --since"
        )
    return _parse_absolute(spec, end_of_day=True)


def parse_window(
    since: str = "2y",
    until: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Tuple[datetime, datetime]:
    current = now or utc_now()
    start = parse_since(since, current)
    end = parse_until(until, current)
    if start > end:
        raise GitfieldError(
            f"the scan window is empty: since {start.isoformat()} is after "
            f"until {end.isoformat()}"
        )
    return start, end


def git_date(value: datetime) -> str:
    """A timezone-explicit format accepted by git's date parser."""

    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def iso_date(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def now_iso() -> str:
    return iso_date(utc_now())
