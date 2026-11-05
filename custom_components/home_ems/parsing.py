"""Turn the many shapes of price / forecast data into ``(start, end, value)``.

Home Assistant price integrations all publish their data differently:
Nord Pool (``raw_today``: start/end/value), ENTSO-e (``prices``: time/price),
EnergyZero and Nord Pool services (``prices``: timestamp/price or start/price),
Frank Energie (from/till/price), Octopus-style rate lists as used for Predbat
(valid_from/valid_to/value_inc_vat), JSON web APIs, and so on. This module finds
lists of timestamped values anywhere in a nested structure.

Pure Python, no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Iterable
import csv
from datetime import date, datetime, timedelta, timezone, tzinfo
import io
import math
from typing import Any

TIME_KEYS = (
    "start", "start_time", "starts_at", "startsat", "from", "period_start",
    "time", "timestamp", "datetime", "date_time", "datum", "readingdate", "hour",
    "valid_from", "start_date",
)
END_KEYS = (
    "end", "end_time", "ends_at", "endsat", "till", "to", "until", "period_end", "valid_to", "end_date",
)
VALUE_KEYS = (
    "price", "value", "total", "prijs", "price_eur", "marketprice", "market_price",
    "electricity_price", "energy_price", "all_in", "allin", "tariff", "tarief",
    "price_ct", "pv_estimate", "value_inc_vat", "value_exc_vat",
)

Point = tuple[datetime, "datetime | None", float]


def parse_time(value: Any, tz: tzinfo = timezone.utc) -> datetime | None:
    """Parse datetimes, ISO strings and epoch numbers (s or ms). Naive = ``tz``."""
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_time(int(text), tz)
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=tz)
    return result


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return None
    return None


def _pick(keys: dict[str, str], preferred: Iterable[str], forced: str | None) -> str | None:
    if forced:
        return keys.get(forced.lower())
    for name in preferred:
        if name in keys:
            return keys[name]
    return None


def _nested(item: dict, path: str) -> Any:
    """``price_tax_included.amount`` -> item["price_tax_included"]["amount"] (case-insensitive)."""
    value: Any = item
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        keys = {str(k).lower(): k for k in value}
        if part.lower() not in keys:
            return None
        value = value[keys[part.lower()]]
    return value


def _from_dict_list(
    items: list, tz: tzinfo, time_key: str | None, value_key: str | None
) -> list[Point]:
    points: list[Point] = []
    nested = bool(value_key and "." in value_key)
    for item in items:
        if not isinstance(item, dict):
            return []
        keys = {str(k).lower(): k for k in item}
        tk = _pick(keys, TIME_KEYS, time_key)
        if nested:
            if _nested(item, value_key) is None:
                return []
            vk = None
        else:
            vk = _pick(keys, VALUE_KEYS, value_key)
            if vk is None:
                return []
        if tk is None:
            return []
        start = parse_time(item[tk], tz)
        value = _number(_nested(item, value_key) if nested else item[vk])
        if start is None or value is None:
            continue
        ek = _pick(keys, END_KEYS, None)
        end = parse_time(item[ek], tz) if ek else None
        if end is not None and tk.lower() == "period_end":
            end = None
        points.append((start, end, value))
    return points


def extract_series(
    data: Any,
    tz: tzinfo = timezone.utc,
    time_key: str | None = None,
    value_key: str | None = None,
    _depth: int = 0,
) -> list[Point]:
    """Find every timestamped series in ``data`` and merge them (deduplicated)."""
    found: list[Point] = []
    if _depth > 5 or data is None:
        return found
    if isinstance(data, list) and data:
        if all(isinstance(i, dict) for i in data):
            found = _from_dict_list(data, tz, time_key, value_key)
            if not found:
                for item in data:
                    found.extend(extract_series(item, tz, time_key, value_key, _depth + 1))
        elif all(isinstance(i, (list, tuple)) and len(i) >= 2 for i in data):
            for item in data:
                start = parse_time(item[0], tz)
                value = _number(item[1])
                if start is not None and value is not None:
                    found.append((start, None, value))
    elif isinstance(data, dict):
        timed = {parse_time(k, tz): _number(v) for k, v in data.items() if isinstance(k, str)}
        if timed and len(timed) >= 2 and all(k is not None and v is not None for k, v in timed.items()):
            found = [(k, None, v) for k, v in timed.items()]
        else:
            for value in data.values():
                if isinstance(value, (list, dict)):
                    found.extend(extract_series(value, tz, time_key, value_key, _depth + 1))
    if _depth == 0:
        return dedupe(found)
    return found


def dedupe(points: list[Point]) -> list[Point]:
    """Sort by start; when a start appears twice the later entry wins."""
    merged: dict[datetime, Point] = {}
    for point in points:
        merged[point[0]] = point
    return [merged[k] for k in sorted(merged)]


def parse_csv(text: str, tz: tzinfo = timezone.utc) -> list[Point]:
    """``time,value`` CSV (header optional, ``;`` separator allowed)."""
    dialect = ";" if text.count(";") > text.count(",") else ","
    points: list[Point] = []
    for row in csv.reader(io.StringIO(text), delimiter=dialect):
        if len(row) < 2:
            continue
        start = parse_time(row[0], tz)
        value = _number(row[1])
        if start is not None and value is not None:
            points.append((start, None, value))
    return dedupe(points)


def solcast_points(items: Any, tz: tzinfo = timezone.utc) -> list[tuple[datetime, datetime, float, float, float]]:
    """Solcast data -> ``(start, end, p50, p10, p90)`` in kW.

    Accepts the API ``forecasts`` list (``period_end`` + ``period``) and the
    ``detailedForecast`` attribute of the Solcast HA integration
    (``period_start``, 30 minute periods).
    """
    out = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        p50 = _number(item.get("pv_estimate"))
        if p50 is None:
            continue
        p10 = _number(item.get("pv_estimate10"))
        p90 = _number(item.get("pv_estimate90"))
        period = _period_minutes(item.get("period")) or 30
        if "period_start" in item:
            start = parse_time(item["period_start"], tz)
            if start is None:
                continue
            end = start + timedelta(minutes=period)
        elif "period_end" in item:
            end = parse_time(item["period_end"], tz)
            if end is None:
                continue
            start = end - timedelta(minutes=period)
        else:
            continue
        out.append((start, end, p50, p10 if p10 is not None else p50, p90 if p90 is not None else p50))
    return out


def _period_minutes(value: Any) -> int | None:
    """ISO-8601 ``PT30M`` / ``PT1H`` -> minutes."""
    if not isinstance(value, str) or not value.startswith("PT"):
        return None
    text = value[2:]
    minutes = 0
    number = ""
    for char in text:
        if char.isdigit():
            number += char
        elif char == "H" and number:
            minutes += int(number) * 60
            number = ""
        elif char == "M" and number:
            minutes += int(number)
            number = ""
    return minutes or None
