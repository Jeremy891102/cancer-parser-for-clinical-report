"""Partial diagnosis dates retain their documented precision."""

import calendar
import datetime as dt


def date_bounds(value):
    parts = list(map(int, value.split("-")))
    year = parts[0]
    if len(parts) == 1:
        return dt.date(year, 1, 1), dt.date(year, 12, 31)
    month = parts[1]
    if len(parts) == 2:
        return dt.date(year, month, 1), dt.date(
            year, month, calendar.monthrange(year, month)[1]
        )
    date = dt.date(*parts)
    return date, date
