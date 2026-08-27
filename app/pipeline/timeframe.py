"""Resolve fuzzy time expressions ("last quarter") into explicit date bounds."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class TimeWindow:
    start: date
    end: date  # inclusive
    label: str

    def sql(self, column: str) -> str:
        return f"{column} BETWEEN DATE '{self.start.isoformat()}' AND DATE '{self.end.isoformat()}'"


def _quarter_of(d: date) -> int:
    return (d.month - 1) // 3 + 1


def quarter_bounds(year: int, q: int) -> tuple[date, date]:
    from datetime import timedelta

    start_month = 3 * (q - 1) + 1
    start = date(year, start_month, 1)
    end_month = start_month + 2
    if end_month == 12:
        return start, date(year, 12, 31)
    return start, date(year, end_month + 1, 1) - timedelta(days=1)


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    from datetime import timedelta

    start = date(year, month, 1)
    nxt = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, nxt - timedelta(days=1)


def resolve_time_range(expr: str | None, today: date | None = None) -> TimeWindow | None:
    """Map natural-language time expressions to a concrete inclusive window."""
    if not expr:
        return None
    from datetime import timedelta

    today = today or date.today()
    e = expr.lower().strip()

    m = re.search(r"q([1-4])\s*(?:of\s*)?(\d{4})", e)
    if m:
        q, y = int(m.group(1)), int(m.group(2))
        s, en = quarter_bounds(y, q)
        return TimeWindow(s, en, f"Q{q} {y}")

    if "last quarter" in e or "previous quarter" in e:
        q, y = _quarter_of(today), today.year
        q -= 1
        if q == 0:
            q, y = 4, y - 1
        s, en = quarter_bounds(y, q)
        return TimeWindow(s, en, f"Q{q} {y} (last quarter)")

    if "this quarter" in e or "current quarter" in e:
        q, y = _quarter_of(today), today.year
        s, en = quarter_bounds(y, q)
        return TimeWindow(s, min(en, today), f"Q{q} {y} (quarter to date)")

    if "last month" in e or "previous month" in e:
        y, mo = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        s, en = _month_bounds(y, mo)
        return TimeWindow(s, en, s.strftime("%B %Y"))

    if "this month" in e:
        s, en = _month_bounds(today.year, today.month)
        return TimeWindow(s, min(en, today), s.strftime("%B %Y (month to date)"))

    if "last year" in e or "previous year" in e:
        y = today.year - 1
        return TimeWindow(date(y, 1, 1), date(y, 12, 31), str(y))

    if "this year" in e or "ytd" in e or "year to date" in e:
        return TimeWindow(date(today.year, 1, 1), today, f"{today.year} year to date")

    m = re.search(r"last (\d+)\s*(day|week|month|quarter|year)s?", e)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"day": 1, "week": 7, "month": 30, "quarter": 91, "year": 365}[unit] * n
        return TimeWindow(today - timedelta(days=days), today, f"last {n} {unit}s")

    m = re.search(r"\b(?:in\s+)?(\d{4})\b", e)
    if m:
        y = int(m.group(1))
        if 2000 <= y <= today.year + 1:
            return TimeWindow(date(y, 1, 1), date(y, 12, 31), str(y))

    if "last week" in e:
        return TimeWindow(today - timedelta(days=7), today, "last 7 days")

    return None
