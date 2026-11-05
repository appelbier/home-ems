"""Data sources: day-ahead prices, price forecasts and Solcast PV forecasts."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
import json
import logging
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ATTRIBUTES,
    CONF_DATA,
    CONF_DAYS,
    CONF_ENTITY,
    CONF_HEADERS,
    CONF_INCLUDES_TAX,
    CONF_NAME,
    CONF_REFRESH_MINUTES,
    CONF_SCALE,
    CONF_SERVICE,
    CONF_SOLCAST_API_KEY,
    CONF_SOLCAST_CALLS,
    CONF_SOLCAST_ENTITIES,
    CONF_SOLCAST_SITES,
    CONF_TIME_KEY,
    CONF_UNIT,
    CONF_URL,
    CONF_VALUE_KEY,
)
from .parsing import Point, extract_series, parse_csv, parse_time, solcast_points
from .price_forecast import Tariff, unit_factor

_LOGGER = logging.getLogger(__name__)

SOLCAST_URL = "https://api.solcast.com.au/rooftop_sites/{site}/forecasts"
SOLCAST_WINDOW = (5, 21)  # local hours in which API calls are spread
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)


def _substitute(value: Any, variables: dict[str, str]) -> Any:
    """Replace ``{date}``, ``{start}``, ``{end}`` in service data."""
    if isinstance(value, str):
        for key, text in variables.items():
            value = value.replace("{" + key + "}", text)
        return value
    if isinstance(value, dict):
        return {k: _substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, variables) for v in value]
    return value


class PriceSource:
    """One configured price source, normalised to raw EUR/kWh."""

    def __init__(self, hass: HomeAssistant, conf: dict, tariff: Tariff, tz: tzinfo) -> None:
        self.hass = hass
        self.conf = conf
        self.tariff = tariff
        self.tz = tz
        self.name = conf.get(CONF_NAME) or conf.get(CONF_ENTITY) or conf.get(CONF_SERVICE) or conf.get(CONF_URL)
        if CONF_ENTITY in conf:
            default_refresh = 1
        elif CONF_SERVICE in conf:
            default_refresh = 30
        else:
            default_refresh = 180
        self.refresh = timedelta(minutes=conf.get(CONF_REFRESH_MINUTES, default_refresh))
        self.points: list[Point] = []
        self.fetched_at: datetime | None = None
        self.error: str | None = None
        self.last_success: datetime | None = None
        self.calibration: tuple[float, float] = (1.0, 0.0)

    @property
    def entity_id(self) -> str | None:
        return self.conf.get(CONF_ENTITY)

    def cache(self) -> dict:
        """Last good points, so a restart (or a sensor that is not restored,
        like one set by an external script) does not lose the forecast."""
        return {
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "points": [[s.isoformat(), e.isoformat() if e else None, v] for s, e, v in self.points],
        }

    def restore(self, cache: dict | None) -> None:
        if not cache:
            return
        self.last_success = parse_time(cache.get("last_success"))
        self.points = [
            (parse_time(s), parse_time(e) if e else None, float(v)) for s, e, v in cache.get("points", [])
        ]

    async def async_points(self, now: datetime, force: bool = False) -> list[Point]:
        if force or self.fetched_at is None or now - self.fetched_at >= self.refresh:
            try:
                points, hint = await self._fetch(now)
                self.points = self.normalise(points, hint)
                self.error = None
                self.last_success = now
            except Exception as err:  # noqa: BLE001 - keep planning on the last good data
                self.error = f"{type(err).__name__}: {err}"
                if self.points:
                    self.error += " (using the last successful forecast)"
                _LOGGER.warning("Price source %s failed: %s", self.name, self.error)
            self.fetched_at = now
        return self.points

    def normalise(self, points: list[Point], unit_hint: str | None) -> list[Point]:
        if CONF_SCALE in self.conf:
            factor = self.conf[CONF_SCALE]
        else:
            factor = unit_factor(
                self.conf.get(CONF_UNIT, "auto"),
                [p[2] for p in points],
                unit_hint,
                all_in=self.conf.get(CONF_INCLUDES_TAX, False),
            )
        out = []
        for start, end, value in points:
            value = value * factor
            if self.conf.get(CONF_INCLUDES_TAX):
                value = float(self.tariff.to_raw(value))
            out.append((start, end, value))
        return out

    def normalise_value(self, value: float, unit_hint: str | None) -> float:
        return self.normalise([(dt_util.utcnow(), None, value)], unit_hint)[0][2]

    async def _fetch(self, now: datetime) -> tuple[list[Point], str | None]:
        time_key = self.conf.get(CONF_TIME_KEY)
        value_key = self.conf.get(CONF_VALUE_KEY)
        if CONF_ENTITY in self.conf:
            state = self.hass.states.get(self.conf[CONF_ENTITY])
            if state is None:
                raise ValueError("entity not found")
            attrs = dict(state.attributes)
            wanted = self.conf.get(CONF_ATTRIBUTES)
            data = {k: attrs.get(k) for k in wanted} if wanted else attrs
            points = extract_series(data, self.tz, time_key, value_key)
            if not points:
                raise ValueError("no timestamped prices found in the entity attributes")
            return points, attrs.get("unit_of_measurement") or attrs.get("unit")

        if CONF_SERVICE in self.conf:
            domain, service = self.conf[CONF_SERVICE].split(".", 1)
            points: list[Point] = []
            today = now.astimezone(self.tz).date()
            for offset in range(self.conf.get(CONF_DAYS, 2)):
                day = today + timedelta(days=offset)
                start = datetime.combine(day, datetime.min.time(), self.tz)
                variables = {
                    "date": day.isoformat(),
                    "start": start.isoformat(),
                    "end": (start + timedelta(days=1)).isoformat(),
                }
                data = _substitute(self.conf.get(CONF_DATA, {}), variables)
                try:
                    response = await self.hass.services.async_call(
                        domain, service, data, blocking=True, return_response=True
                    )
                except Exception as err:  # noqa: BLE001 - tomorrow is often not published yet
                    if offset == 0:
                        raise
                    _LOGGER.debug("Price service %s for %s: %s", self.name, day, err)
                    continue
                points.extend(extract_series(response, self.tz, time_key, value_key))
            if not points:
                raise ValueError("service returned no timestamped prices")
            return points, None

        session = async_get_clientsession(self.hass)
        async with session.get(
            self.conf[CONF_URL], headers=self.conf.get(CONF_HEADERS) or None, timeout=HTTP_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            text = await resp.text()
        try:
            points = extract_series(json.loads(text), self.tz, time_key, value_key)
        except ValueError:
            points = parse_csv(text, self.tz)
        if not points:
            raise ValueError("URL returned no timestamped prices")
        return points, None


class SolarSource:
    """Solcast PV forecast, from the Solcast HA integration or the API directly.

    The hobbyist API allows ~10 calls per UTC day, so direct calls are spread
    over daylight hours and the result is cached (and persisted by the
    coordinator) so restarts do not burn calls.
    """

    def __init__(self, hass: HomeAssistant, conf: dict, tz: tzinfo) -> None:
        self.hass = hass
        self.conf = conf
        self.tz = tz
        self.points: list[tuple[datetime, datetime, float, float, float]] = []
        self.fetched_at: datetime | None = None
        self.error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.conf.get(CONF_SOLCAST_ENTITIES)) or bool(
            self.conf.get(CONF_SOLCAST_API_KEY) and self.conf.get(CONF_SOLCAST_SITES)
        )

    @property
    def uses_api(self) -> bool:
        return not self.conf.get(CONF_SOLCAST_ENTITIES) and self.configured

    def restore(self, cache: dict | None) -> None:
        if not cache:
            return
        self.fetched_at = parse_time(cache.get("fetched_at"))
        self.points = [
            (parse_time(s), parse_time(e), p50, p10, p90)
            for s, e, p50, p10, p90 in cache.get("points", [])
        ]

    def cache(self) -> dict:
        return {
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "points": [[s.isoformat(), e.isoformat(), p50, p10, p90] for s, e, p50, p10, p90 in self.points],
        }

    def api_due(self, now: datetime) -> bool:
        if self.fetched_at is None or now - self.fetched_at >= timedelta(hours=24):
            return True
        sites = max(1, len(self.conf.get(CONF_SOLCAST_SITES, [])))
        per_day = max(1, self.conf.get(CONF_SOLCAST_CALLS, 8) // sites)
        start_h, end_h = SOLCAST_WINDOW
        interval = timedelta(hours=(end_h - start_h) / per_day)
        local_hour = now.astimezone(self.tz).hour
        return start_h <= local_hour < end_h and now - self.fetched_at >= interval

    async def async_points(self, now: datetime, force: bool = False):
        if self.conf.get(CONF_SOLCAST_ENTITIES):
            self.points = self._from_entities()
            self.fetched_at = now
            return self.points
        if self.uses_api and (force or self.api_due(now)):
            try:
                self.points = await self._from_api()
                self.error = None
            except Exception as err:  # noqa: BLE001 - keep the cached forecast
                self.error = f"{type(err).__name__}: {err}"
                _LOGGER.warning("Solcast fetch failed: %s", self.error)
            self.fetched_at = now
        return self.points

    def _from_entities(self):
        merged: dict[datetime, tuple] = {}
        for entity_id in self.conf[CONF_SOLCAST_ENTITIES]:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            items = state.attributes.get("detailedForecast") or state.attributes.get("detailedHourly")
            for point in solcast_points(items, self.tz):
                merged[point[0]] = point
        if not merged:
            self.error = "no detailedForecast attribute found on the Solcast entities"
        else:
            self.error = None
        return [merged[k] for k in sorted(merged)]

    async def _from_api(self):
        session = async_get_clientsession(self.hass)
        headers = {"Authorization": f"Bearer {self.conf[CONF_SOLCAST_API_KEY]}"}
        total: dict[datetime, list] = {}
        for site in self.conf[CONF_SOLCAST_SITES]:
            async with session.get(
                SOLCAST_URL.format(site=site),
                params={"format": "json", "hours": "168"},
                headers=headers,
                timeout=HTTP_TIMEOUT,
            ) as resp:
                if resp.status == 429:
                    raise RuntimeError("Solcast API call limit reached (HTTP 429)")
                resp.raise_for_status()
                payload = await resp.json()
            for start, end, p50, p10, p90 in solcast_points(payload.get("forecasts"), self.tz):
                entry = total.setdefault(start, [start, end, 0.0, 0.0, 0.0])
                entry[2] += p50
                entry[3] += p10
                entry[4] += p90
        if not total:
            raise ValueError("Solcast returned no forecasts")
        return [tuple(total[k]) for k in sorted(total)]
