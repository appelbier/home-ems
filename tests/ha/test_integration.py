"""End-to-end test: load the integration in a real Home Assistant instance."""

from __future__ import annotations

from datetime import datetime, time, timedelta
import math

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

DOMAIN = "home_ems"


def _price_attrs(now):
    """Two days of 15 minute raw prices: cheap at night, expensive at 18-21h."""
    start = dt_util.start_of_local_day(now)
    today, tomorrow = [], []
    for i in range(2 * 96):
        t = start + timedelta(minutes=15 * i)
        hour = t.hour
        value = 0.03 if hour < 6 else (0.25 if 17 <= hour < 21 else 0.10)
        item = {"start": t.isoformat(), "end": (t + timedelta(minutes=15)).isoformat(), "value": value}
        (today if i < 96 else tomorrow).append(item)
    return {"raw_today": today, "raw_tomorrow": tomorrow, "unit_of_measurement": "EUR/kWh"}


def _solcast_attrs(now):
    start = dt_util.start_of_local_day(now)
    items = []
    for i in range(48):
        t = start + timedelta(minutes=30 * i)
        h = t.hour + t.minute / 60
        kw = max(0.0, 4 * math.sin(math.pi * (h - 8) / 9)) if 8 < h < 17 else 0.0
        items.append({"period_start": t.isoformat(), "pv_estimate": kw, "pv_estimate10": kw * 0.5, "pv_estimate90": kw * 1.2})
    return {"detailedForecast": items}


def _config():
    record = [{"action": "test.record", "data": {"mode": "{{ mode }}", "power_w": "{{ power_w }}", "target": "{{ target_soc }}"}}]
    return {
        DOMAIN: {
            "plan_days": 3,
            "battery": {
                "capacity_kwh": 150,
                "soc_entity": "sensor.battery_soc",
                "max_charge_kw": 10,
                "max_discharge_kw": 10,
                "reserve_soc_pct": 10,
            },
            "grid": {"import_limit_kw": 17, "export_limit_kw": 10},
            "prices": {
                "source": {"entity": "sensor.day_ahead"},
                "energy_tax": 0.0916,
                "import_markup": 0.02,
            },
            "solar": {"solcast_entities": ["sensor.solcast_today"]},
            "load": {"default_daily_kwh": 15},
            "ev": {
                "battery_kwh": 60,
                "connected_entity": "binary_sensor.car_connected",
                "soc_entity": "sensor.car_soc",
                "start": [{"action": "test.ev", "data": {"power_w": "{{ power_w }}"}}],
                "stop": [{"action": "test.ev", "data": {"power_w": 0}}],
            },
            "control": {
                "charge": record,
                "export": record,
                "hold": record,
                "self_consumption": record,
            },
            "heavy_loads": [{"name": "Sauna", "power_kw": 6, "duration_min": 60, "weekdays": "sat", "start_time": "19:00"}],
        }
    }


async def test_full_cycle(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    now = dt_util.now()
    hass.states.async_set("sensor.battery_soc", "15", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.day_ahead", "0.10", _price_attrs(now))
    hass.states.async_set("sensor.solcast_today", "20", _solcast_attrs(now))
    hass.states.async_set("binary_sensor.car_connected", "on")
    hass.states.async_set("sensor.car_soc", "40")

    calls: list[ServiceCall] = []
    ev_calls: list[ServiceCall] = []
    hass.services.async_register("test", "record", calls.append)
    hass.services.async_register("test", "ev", ev_calls.append)

    assert await async_setup_component(hass, DOMAIN, _config())
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success, coordinator.last_exception

    mode = hass.states.get("sensor.home_ems_mode")
    assert mode is not None and mode.state in ("charge", "export", "hold", "self_consumption")
    plan = hass.states.get("sensor.home_ems_plan")
    assert len(plan.attributes["slots"]) >= 2 * 96
    assert plan.attributes["days"][0]["date"]
    assert hass.states.get("sensor.home_ems_import_price").state not in ("unknown", "unavailable")
    assert float(hass.states.get("sensor.home_ems_pv_forecast").attributes["days"][0]["kwh"]) >= 0
    ev = hass.states.get("sensor.home_ems_ev")
    assert ev.attributes["energy_needed_kwh"] > 0
    assert 0 < ev.attributes["planned_kwh"] <= ev.attributes["energy_needed_kwh"] + 4.1 * 0.25
    assert hass.states.get("number.home_ems_max_charge_power").state == "10.0"
    assert hass.states.get("number.home_ems_hold_soc").state == hass.states.get("number.home_ems_reserve_soc").state
    assert hass.states.get("number.home_ems_hold_strength").state == "0.1"
    assert hass.states.get("number.home_ems_trading_aggressiveness").state == "100.0"
    savings = hass.states.get("sensor.home_ems_battery_savings")
    assert savings is not None and float(savings.state) == plan.attributes["battery_savings"]
    assert {"savings", "no_battery_cost", "wear_cost", "stored_value_change"} <= set(plan.attributes)

    # Low battery and cheap nights ahead: the plan must grid-charge somewhere.
    assert any(s["mode"] == "charge" for s in plan.attributes["slots"])
    assert hass.states.get("sensor.home_ems_next_charge").state not in ("unknown", "unavailable")

    # No inverter commands until control is switched on.
    assert calls == []
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.home_ems_control_inverter"}, blocking=True)
    await hass.async_block_till_done()
    assert calls, "battery control action was not run"
    assert calls[-1].data["mode"] == hass.states.get("sensor.home_ems_mode").state
    assert ev_calls, "EV control action was not run"

    # A manual export slot for the current hour overrides the plan.
    start = dt_util.now().replace(tzinfo=None)
    response = await hass.services.async_call(
        DOMAIN,
        "add_manual_slot",
        {"mode": "export", "power_kw": 5, "start": start - timedelta(minutes=1), "end": start + timedelta(hours=1)},
        blocking=True,
        return_response=True,
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.home_ems_mode").state == "export"
    assert calls[-1].data["mode"] == "export"
    assert hass.states.get("sensor.home_ems_manual_slots").state == "1"
    await hass.services.async_call(DOMAIN, "remove_manual_slot", {"id": response["id"]}, blocking=True)
    await coordinator.async_refresh()
    assert hass.states.get("sensor.home_ems_manual_slots").state == "0"

    # Flexible heavy load gets a recommended start inside its window.
    earliest = dt_util.now().replace(tzinfo=None) + timedelta(hours=2)
    await hass.services.async_call(
        DOMAIN,
        "add_heavy_load",
        {"name": "Dishwasher", "power_kw": 2, "duration_min": 120, "earliest": earliest, "latest": earliest + timedelta(hours=20)},
        blocking=True,
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    items = hass.states.get("sensor.home_ems_heavy_loads").attributes["items"]
    flex = next(i for i in items if i["name"] == "Dishwasher")
    chosen = dt_util.parse_datetime(flex["chosen_start"])
    assert chosen is not None
    assert dt_util.as_utc(earliest.replace(tzinfo=dt_util.get_default_time_zone())) <= chosen

    # Settings persist through the number entity.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.home_ems_reserve_soc", "value": 20}, blocking=True
    )
    assert coordinator.settings["reserve_soc_pct"] == 20


async def test_example_configuration_is_valid(hass: HomeAssistant) -> None:
    from pathlib import Path

    import yaml

    from custom_components.home_ems.config_schema import CONFIG_SCHEMA

    path = Path(__file__).resolve().parents[2] / "examples" / "configuration.yaml"
    conf = CONFIG_SCHEMA(yaml.safe_load(path.read_text(encoding="utf-8")))[DOMAIN]
    assert conf["manual_slots"][0]["weekdays"] == [6]
    assert conf["heavy_loads"][0]["weekdays"] == [5]
    assert set(conf["control"]) >= {"charge", "export", "hold", "self_consumption"}


async def test_deye_webasto_zonneplan_example_is_valid(hass: HomeAssistant) -> None:
    from pathlib import Path

    import yaml

    from custom_components.home_ems.config_schema import CONFIG_SCHEMA

    path = Path(__file__).resolve().parents[2] / "examples" / "deye-webasto-zonneplan.yaml"
    conf = CONFIG_SCHEMA(yaml.safe_load(path.read_text(encoding="utf-8")))[DOMAIN]
    assert conf["prices"]["net_metering_until"].isoformat() == "2027-01-01"
    assert "suspendedev" in conf["ev"]["connected_states"]


async def test_octopus_rates_net_metering_and_ocpp_ev(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    """Zonneplan-for-Predbat style rates in cents, salderen end date, OCPP EV without SoC."""
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    now = dt_util.now()
    start = dt_util.start_of_local_day(now)
    rates = []
    for i in range(2 * 96):
        t = start + timedelta(minutes=15 * i)
        cents = 18.0 if t.hour < 6 else 32.0
        rates.append({"valid_from": t.isoformat(), "valid_to": (t + timedelta(minutes=15)).isoformat(), "value_inc_vat": cents})
    hass.states.async_set("sensor.battery_soc", "50")
    hass.states.async_set("sensor.zonneplan_rates", "32", {"rates": rates})
    hass.states.async_set("sensor.charger_status_connector", "SuspendedEV")
    hass.states.async_set("sensor.charger_energy_session", "4.0")

    switch_day = (now + timedelta(days=1)).date()
    conf = {
        DOMAIN: {
            "plan_days": 3,
            "battery": {"capacity_kwh": 144, "soc_entity": "sensor.battery_soc", "max_charge_kw": 15, "max_discharge_kw": 15},
            "prices": {
                "source": {"entity": "sensor.zonneplan_rates", "includes_tax": True},
                "net_metering_until": switch_day.isoformat(),
                "export_mode": "raw",
            },
            "ev": {
                "connected_entity": "sensor.charger_status_connector",
                "session_energy_entity": "sensor.charger_energy_session",
            },
        }
    }
    assert await async_setup_component(hass, DOMAIN, conf)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN]
    await coordinator.async_set_setting("ev_energy_kwh", 10.0)
    await coordinator.async_refresh()
    assert coordinator.last_update_success, coordinator.last_exception

    # All-in cents come back as the same EUR/kWh consumer price.
    price = float(hass.states.get("sensor.home_ems_import_price").state)
    assert min(abs(price - 0.18), abs(price - 0.32)) < 1e-3
    slots = hass.states.get("sensor.home_ems_plan").attributes["slots"]
    switch = dt_util.as_utc(datetime.combine(switch_day, time(0), dt_util.get_default_time_zone()))
    before = [s for s in slots if dt_util.parse_datetime(s["start"]) < switch and s["price_source"] == "day_ahead"]
    after = [s for s in slots if dt_util.parse_datetime(s["start"]) >= switch and s["price_source"] == "day_ahead"]
    assert before and after
    assert all(s["export_price"] == s["import_price"] for s in before)
    assert all(s["export_price"] < s["import_price"] - 0.05 for s in after)

    ev = hass.states.get("sensor.home_ems_ev")
    assert ev.attributes["connected"] is True
    assert ev.attributes["session_kwh"] == 4.0
    assert ev.attributes["energy_needed_kwh"] == 6.0


async def test_forecast_outage_falls_back_to_history(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    """A forecast source that returns a flat placeholder is ignored; a real one is used."""
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    now = dt_util.now()
    hass.states.async_set("sensor.battery_soc", "50")
    hass.states.async_set("sensor.day_ahead", "0.10", _price_attrs(now))

    day3 = dt_util.start_of_local_day(now) + timedelta(days=2)

    def forecast(flat: bool) -> dict:
        items = []
        for i in range(4 * 24):  # hourly, days 3-6
            t = day3 + timedelta(hours=i)
            price = 40.11 if flat else (25.0 + 10.0 * (17 <= t.hour < 21) - 8.0 * (t.weekday() >= 5))
            items.append({"time": t.isoformat(), "price": price})
        return {"forecast": items}

    hass.states.async_set("sensor.price_forecast", "0", forecast(flat=True))
    conf = {
        DOMAIN: {
            "plan_days": 6,
            "battery": {"capacity_kwh": 144, "soc_entity": "sensor.battery_soc"},
            "prices": {
                "source": {"entity": "sensor.day_ahead"},
                "forecasts": [{"name": "forecast", "entity": "sensor.price_forecast", "includes_tax": True, "unit": "ct_kwh"}],
            },
        }
    }
    assert await async_setup_component(hass, DOMAIN, conf)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN]
    await coordinator.async_refresh()
    assert coordinator.last_update_success, coordinator.last_exception

    slots = hass.states.get("sensor.home_ems_plan").attributes["slots"]
    later = [s for s in slots if dt_util.parse_datetime(s["start"]) >= day3]
    assert later and not any(s["price_source"] == "forecast" for s in later)
    warnings = hass.states.get("sensor.home_ems_mode").attributes["warnings"]
    assert any("ignored flat values" in w for w in warnings)

    hass.states.async_set("sensor.price_forecast", "0", forecast(flat=False))
    await coordinator.async_refresh_forecasts()
    await coordinator.async_refresh()
    slots = hass.states.get("sensor.home_ems_plan").attributes["slots"]
    covered = [s for s in slots if day3 <= dt_util.parse_datetime(s["start"]) < day3 + timedelta(days=4)]
    assert covered and all(s["price_source"] == "forecast" for s in covered)
    # The 10 ct evening peak survives the cents -> raw -> consumer price round trip,
    # damped by the default trust for 2-5 days ahead (0.65-0.86).
    assert min(s["import_price"] for s in covered) < max(s["import_price"] for s in covered) - 0.06
    skill = hass.states.get("sensor.home_ems_import_price").attributes["sources"]["skill"]["forecast"]
    assert skill[0]["trust"] > skill[5]["trust"]

    # Switched off, the forecast is planned as published (full 10 ct peak).
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.home_ems_automatic_forecast_trust"}, blocking=True
    )
    await coordinator.async_refresh()
    slots = hass.states.get("sensor.home_ems_plan").attributes["slots"]
    covered = [s for s in slots if day3 <= dt_util.parse_datetime(s["start"]) < day3 + timedelta(days=4)]
    assert min(s["import_price"] for s in covered) < max(s["import_price"] for s in covered) - 0.09
    info = hass.states.get("sensor.home_ems_import_price").attributes["sources"]["forecasts"][0]
    assert info["last_success"] and info["covers_until"]


async def test_forecast_survives_restart_without_source_sensor(hass: HomeAssistant) -> None:
    """A sensor set by an external script is gone after an HA restart: keep the stored forecast."""
    from custom_components.home_ems.price_forecast import Tariff
    from custom_components.home_ems.sources import PriceSource

    tz = dt_util.get_default_time_zone()
    conf = {"name": "forecast", "entity": "sensor.energy_forecast_raw_data", "attributes": ["daily_forecast_data"],
            "includes_tax": True, "unit": "eur_kwh"}
    start = dt_util.start_of_local_day() + timedelta(days=2)
    data = [{"start": (start + timedelta(hours=h)).replace(tzinfo=None).isoformat(),
             "end": (start + timedelta(hours=h + 1)).replace(tzinfo=None).isoformat(), "value": 0.25 + 0.01 * h}
            for h in range(24)]
    hass.states.async_set("sensor.energy_forecast_raw_data", "updated", {"daily_forecast_data": data})

    source = PriceSource(hass, conf, Tariff(), tz)
    points = await source.async_points(dt_util.utcnow())
    assert len(points) == 24 and source.error is None
    saved = source.cache()

    hass.states.async_remove("sensor.energy_forecast_raw_data")
    restarted = PriceSource(hass, conf, Tariff(), tz)
    restarted.restore(saved)
    points = await restarted.async_points(dt_util.utcnow())
    assert len(points) == 24
    assert "last successful forecast" in restarted.error
    assert restarted.last_success is not None


async def test_zonneplan_native_sensor_prices(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    start = dt_util.as_utc(dt_util.start_of_local_day())
    forecast = [
        {"start_date": (start + timedelta(minutes=15 * i)).isoformat().replace("+00:00", ".000000Z"),
         "price_tax_included": {"amount": 2357600 if i % 2 else 3100000, "currency": "EUR"}}
        for i in range(2 * 96)
    ]
    hass.states.async_set("sensor.zonneplan_current_quarter_hourly_electricity_tariff", "0.2358", {"forecast": forecast})
    hass.states.async_set("sensor.battery_soc", "50")
    conf = {
        DOMAIN: {
            "plan_days": 2,
            "battery": {"capacity_kwh": 144, "soc_entity": "sensor.battery_soc"},
            "prices": {
                "source": {
                    "entity": "sensor.zonneplan_current_quarter_hourly_electricity_tariff",
                    "attributes": ["forecast"],
                    "time_key": "start_date",
                    "value_key": "price_tax_included.amount",
                    "scale": 0.0000001,
                    "includes_tax": True,
                },
            },
        }
    }
    assert await async_setup_component(hass, DOMAIN, conf)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN]
    await coordinator.async_refresh()
    assert coordinator.last_update_success, coordinator.last_exception
    slots = hass.states.get("sensor.home_ems_plan").attributes["slots"]
    firm = {round(s["import_price"], 4) for s in slots if s["price_source"] == "day_ahead"}
    assert firm == {0.2358, 0.31}


async def test_stored_meter_spike_is_removed_on_startup(recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage) -> None:
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    now = dt_util.utcnow()
    spike_ts = int((now - timedelta(days=1)).timestamp()) // 900 * 900
    normal_ts = spike_ts - 900
    hass_storage["home_ems.data"] = {
        "version": 1,
        "minor_version": 1,
        "key": "home_ems.data",
        "data": {"load_history": {str(spike_ts): 13237.0, str(normal_ts): 0.2}},
    }
    hass.states.async_set("sensor.battery_soc", "50")
    hass.states.async_set("sensor.day_ahead", "0.10", _price_attrs(dt_util.now()))
    conf = {DOMAIN: {"plan_days": 2, "battery": {"capacity_kwh": 144, "soc_entity": "sensor.battery_soc"},
                     "prices": {"source": {"entity": "sensor.day_ahead"}}}}
    assert await async_setup_component(hass, DOMAIN, conf)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN]
    assert spike_ts not in coordinator.load_history and normal_ts in coordinator.load_history
    await coordinator.async_refresh()
    warnings = hass.states.get("sensor.home_ems_mode").attributes["warnings"]
    assert any("13237 kWh" in w for w in warnings)
    assert max(d["load_kwh"] for d in hass.states.get("sensor.home_ems_plan").attributes["days"]) < 100


async def test_exclude_simulated_load_period(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    """Simulator values stay out of the load model, also after re-reading the statistics."""
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    hass.states.async_set("sensor.battery_soc", "50")
    hass.states.async_set("sensor.day_ahead", "0.10", _price_attrs(dt_util.now()))
    conf = {DOMAIN: {"plan_days": 2, "battery": {"capacity_kwh": 144, "soc_entity": "sensor.battery_soc"},
                     "prices": {"source": {"entity": "sensor.day_ahead"}},
                     "load": {"energy_entity": "sensor.load_total"}}}
    assert await async_setup_component(hass, DOMAIN, conf)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN]

    hour0 = int(dt_util.utcnow().timestamp()) // 3600 * 3600 - 48 * 3600
    sim_start, sim_end = hour0 + 10 * 3600, hour0 + 16 * 3600  # 6 simulated hours
    rows = [{"start": float(hour0 + h * 3600), "change": 3.0 if sim_start <= hour0 + h * 3600 < sim_end else 0.8}
            for h in range(40)]

    async def fake_statistics(start, end, ids, period, units, types):
        return {"sensor.load_total": rows} if period == "hour" else {}

    coordinator._statistics = fake_statistics  # noqa: SLF001
    await coordinator._sync_load_history(3)  # noqa: SLF001
    assert coordinator.load_history[sim_start] == 0.75

    tz = dt_util.get_default_time_zone()
    response = await hass.services.async_call(
        DOMAIN, "exclude_load_history",
        {"start": dt_util.utc_from_timestamp(sim_start).astimezone(tz).replace(tzinfo=None),
         "end": dt_util.utc_from_timestamp(sim_end).astimezone(tz).replace(tzinfo=None)},
        blocking=True, return_response=True,
    )
    assert response["removed_slots"] == 6 * 4
    await coordinator._sync_load_history(3)  # noqa: SLF001 - the hourly re-sync must not bring them back
    assert not any(sim_start <= ts < sim_end for ts in coordinator.load_history)
    assert coordinator.load_history[sim_end] == 0.2
    await coordinator.async_refresh()
    periods = hass.states.get("sensor.home_ems_load_forecast").attributes["excluded_periods"]
    assert len(periods) == 1

    await hass.services.async_call(DOMAIN, "clear_load_exclusions", {}, blocking=True)
    assert coordinator.load_history[sim_start] == 0.75


async def test_changed_yaml_limit_overrides_stored_setting(recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage) -> None:
    hass_storage["home_ems.data"] = {
        "version": 1,
        "minor_version": 1,
        "key": "home_ems.data",
        "data": {
            "settings": {"max_discharge_kw": 15, "max_charge_kw": 12},
            "settings_defaults": {"max_discharge_kw": 15, "max_charge_kw": 15},
        },
    }
    hass.states.async_set("sensor.battery_soc", "50")
    hass.states.async_set("sensor.day_ahead", "0.10", _price_attrs(dt_util.now()))
    battery = {"capacity_kwh": 144, "soc_entity": "sensor.battery_soc", "max_charge_kw": 15, "max_discharge_kw": 27}
    conf = {DOMAIN: {"plan_days": 2, "battery": battery, "prices": {"source": {"entity": "sensor.day_ahead"}}}}
    assert await async_setup_component(hass, DOMAIN, conf)
    await hass.async_block_till_done()
    settings = hass.data[DOMAIN].settings
    assert settings["max_discharge_kw"] == 27  # YAML changed: new YAML value
    assert settings["max_charge_kw"] == 12  # YAML unchanged: dashboard value kept


async def test_plan_card_is_served_and_loaded(recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_client) -> None:
    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager

    assert await async_setup_component(hass, "http", {})
    # Stand-in for the frontend component (its UI package is not installed in tests).
    hass.data[DATA_EXTRA_MODULE_URL] = UrlManager(lambda *_: None, [])
    hass.config.components.add("frontend")
    hass.states.async_set("sensor.battery_soc", "50")
    hass.states.async_set("sensor.day_ahead", "0.10", _price_attrs(dt_util.now()))
    conf = {DOMAIN: {"plan_days": 2, "battery": {"capacity_kwh": 144, "soc_entity": "sensor.battery_soc"},
                     "prices": {"source": {"entity": "sensor.day_ahead"}}}}
    assert await async_setup_component(hass, DOMAIN, conf)
    await hass.async_block_till_done()

    client = await hass_client()
    for card in ("home-ems-plan-card", "home-ems-schedule-card"):
        urls = [u for u in hass.data[DATA_EXTRA_MODULE_URL].urls if u.startswith(f"/home_ems/{card}.js?v=")]
        assert len(urls) == 1
        response = await client.get(urls[0])
        assert response.status == 200
        assert f"customElements.define(\"{card}\"" in await response.text()

    await hass.data[DOMAIN].async_refresh()
    slot = hass.states.get("sensor.home_ems_plan").attributes["slots"][0]
    assert "cost" in slot and "price_source" in slot


async def test_schedule_card_payloads(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    """The exact JSON the schedule card sends: wall-clock strings in the HA time zone, weekday names."""
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    hass.states.async_set("sensor.battery_soc", "50")
    hass.states.async_set("sensor.day_ahead", "0.10", _price_attrs(dt_util.now()))
    conf = {DOMAIN: {"plan_days": 2, "battery": {"capacity_kwh": 144, "soc_entity": "sensor.battery_soc"},
                     "prices": {"source": {"entity": "sensor.day_ahead"}}}}
    assert await async_setup_component(hass, DOMAIN, conf)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN]

    wall = (dt_util.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:00")
    later = (dt_util.now() + timedelta(hours=14)).strftime("%Y-%m-%dT%H:00")
    calls = [
        ("add_manual_slot", {"mode": "charge", "start": f"{wall}:00", "end": f"{later}:00", "power_kw": 6.5, "name": "Top-up"}),
        ("add_manual_slot", {"mode": "hold", "weekdays": ["sat", "sun"], "start_time": "22:00", "end_time": "06:00"}),
        ("add_heavy_load", {"name": "Sauna", "power_kw": 6, "duration_min": 90, "earliest": f"{wall}:00", "latest": f"{later}:00"}),
        ("add_heavy_load", {"name": "Boiler", "power_kw": 3, "duration_min": 60, "weekdays": ["mon", "tue", "wed", "thu", "fri"], "start_time": "07:00"}),
        ("add_heavy_load", {"name": "Oven", "power_kw": 2.5, "duration_min": 45, "start": f"{wall}:00"}),
    ]
    for service, data in calls:
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    slots = hass.states.get("sensor.home_ems_manual_slots").attributes["items"]
    once = next(s for s in slots if s["name"] == "Top-up")
    assert dt_util.parse_datetime(once["start"]) == dt_util.parse_datetime(f"{wall}:00").replace(tzinfo=dt_util.get_default_time_zone())
    assert once["power_kw"] == 6.5
    weekly = next(s for s in slots if s["mode"] == "hold")
    assert weekly["weekdays"] == [5, 6] and weekly["start_time"] == "22:00"
    loads = {h["name"]: h for h in hass.states.get("sensor.home_ems_heavy_loads").attributes["items"]}
    assert loads["Sauna"]["flexible"] and loads["Sauna"]["chosen_start"]
    assert loads["Boiler"]["weekdays"] == [0, 1, 2, 3, 4]
    assert not loads["Oven"]["flexible"]

    # The plan marks the slots where the loads run.
    plan = hass.states.get("sensor.home_ems_plan").attributes
    oven = [s for s in plan["slots"] if "Oven" in s.get("heavy", [])]
    assert len(oven) == 3 and all(s["heavy_kwh"] >= 2.5 * 0.25 - 0.01 for s in oven)
    assert any("Sauna" in s.get("heavy", []) for s in plan["slots"])
    assert sum(d["heavy_kwh"] for d in plan["days"]) >= 2.5 * 0.75 + 6 * 1.5 - 0.05

    await hass.services.async_call(DOMAIN, "remove_heavy_load", {"id": loads["Oven"]["id"]}, blocking=True)
    await coordinator.async_refresh()
    assert hass.states.get("sensor.home_ems_heavy_loads").state == "2"


async def test_control_commands_suit_the_deye_firmware(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    """The firmware stops Charge at SoC >= target and Discharge at SoC <= target (whole %)."""
    import numpy as np

    from custom_components.home_ems.coordinator import _stop_soc
    from custom_components.home_ems.optimizer import PlanInput, PlanResult
    from custom_components.home_ems.timeseries import slot_grid

    assert _stop_soc("charge", 48.2, 48.0, 20, 100) == 49
    assert _stop_soc("charge", 47.9, 48.0, 20, 100) == 49  # never the current SoC
    assert _stop_soc("charge", 100.0, 100.0, 20, 100) == 100
    assert _stop_soc("export", 40.6, 48.0, 20, 100) == 40
    assert _stop_soc("export", 47.9, 48.0, 20, 100) == 47
    assert _stop_soc("export", 18.0, 21.0, 20, 100) == 20  # never below the reserve

    now = dt_util.now()
    hass.states.async_set("sensor.battery_soc", "50")
    hass.states.async_set("sensor.day_ahead", "0.10", _price_attrs(now))
    hass.states.async_set("sensor.solcast_today", "10", _solcast_attrs(now))
    assert await async_setup_component(hass, DOMAIN, _config())
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN]
    n = 4
    slots = slot_grid(now, n)

    def current(mode, battery, power, load=0.5, pv=0.0, soc=75.0):
        plan = PlanResult(
            soc_kwh=soc + battery * np.arange(n + 1),
            battery_kwh=np.full(n, battery),
            grid_kwh=np.zeros(n),
            cost=np.zeros(n),
            mode=[mode] * n,
            power_kw=np.full(n, power),
            curtail=np.zeros(n, dtype=bool),
            total_cost=0.0,
            final_value=0.0,
        )
        inp = PlanInput(
            duration_h=np.full(n, 0.25), load_kwh=np.full(n, load), pv_kwh=np.full(n, pv),
            import_price=np.full(n, 0.3), export_price=np.full(n, 0.3), initial_soc_kwh=soc,
        )
        return coordinator._current_slot(plan, slots, 150, inp)

    assert current("export", 0.0, 0.0, load=0.1, pv=1.0)["control_mode"] == "hold"  # PV to grid
    assert current("self_consumption", 0.0, 0.0)["control_mode"] == "hold"  # kept at the reserve
    assert current("self_consumption", -0.5, 0.0)["control_mode"] == "self_consumption"
    assert current("self_consumption", 0.4, 0.0, load=0.1, pv=1.0)["control_mode"] == "self_consumption"
    charge = current("charge", 0.25, 1.0)
    assert charge["control_mode"] == "charge" and charge["stop_soc"] == 51
