# Home EMS – week-ahead battery planner for Home Assistant

A Home Assistant custom integration that plans the charge and discharge slots
of a hybrid inverter with a large battery **a full week ahead**, in 15-minute
slots. It is built for the case where short-horizon planners struggle: a big
battery (e.g. 150 kWh) that should fill up during cheap weekends and carry the
house through expensive weekdays.

## What it does

| Feature | How |
|---|---|
| 15 min dynamic prices | Firm day-ahead prices from any HA price integration (Nord Pool, ENTSO-e, EnergyZero, Frank, …) via entity attributes or a service call |
| Price forecast beyond day-ahead | Optional external forecast sources (URL / entity), auto-calibrated against real day-ahead prices, plus a built-in weekday × time-of-day model learned from price history |
| Solar forecast | Solcast, either from the Solcast HA integration's `detailedForecast` or straight from the API (call budget spread over the day, cached across restarts). P10/P50 blend via *PV pessimism* |
| House load prediction | Learns from the recorder's energy statistics: same weekday of the past 4 weeks + recent days, robust against outliers (weighted median / MAD), with a fading level correction |
| Week optimisation | Dynamic programming over the full horizon (globally optimal for the forecast; a 7-day cycle takes ~0.5 s on a desktop PC, expect a few seconds on a Raspberry Pi) |
| Configurable rates | Max charge / discharge power, max grid-charge price, min battery-export price, reserve and max SoC, battery wear cost — all as live number entities |
| Manual time slots | Force `charge` / `export` / `hold` / `self_consumption` one-off or weekly (YAML or service) |
| Heavy loads | Fixed, weekly recurring, or *flexible* (planner picks the cheapest start in a window) |
| EV charging | Modes off / solar / smart / max, target SoC + ready-by time, optional "EV may use house battery" |
| Inverter control | You supply HA actions per mode; the integration runs them with `power_w`, `target_soc`, … variables. Starts in plan-only mode |

### Why a week horizon matters (simulation)

`tools/simulate_week.py` runs a synthetic week (150 kWh, 6.4 kWp in late
October, 15 kWh/day, cheap weekends):

```
day         avg imp  grid in grid out  charge  soc end    cost
Fri 30        0.281      0.0      0.0    0.0h     60.1    0.00
Sat 31        0.202     49.2     -0.0    4.0h     98.2    8.81
Sun 01        0.196     58.7     -0.0    5.8h    149.8    9.59
Mon 02        0.282      0.0      0.0    0.0h    137.9    0.00
...
```

The plan fills the battery on Saturday and Sunday and imports nothing from
Monday to Thursday. Over a 3-week rolling test, the 7-day horizon was about 11%
cheaper than re-planning with a 36 h horizon, assuming a perfect forecast.

## Installation

1. Install with [HACS](https://hacs.xyz): HACS → ⋮ → **Custom repositories** →
   `https://github.com/appelbier/home-ems`, type **Integration** → **Add**, then
   search for *Home EMS week planner* → **Download**. HACS shows new releases as
   updates. Without HACS, copy `custom_components/home_ems` into your HA
   `config/custom_components/`.
2. Add a `home_ems:` block to `configuration.yaml`. Start from
   [examples/configuration.yaml](examples/configuration.yaml), or from
   [examples/deye-webasto-zonneplan.yaml](examples/deye-webasto-zonneplan.yaml)
   for a Deye with the ESPHome remote-mode firmware, a Webasto Next on OCPP and
   Zonneplan prices (it also has a GoodWe variant).
3. Restart Home Assistant. The plan appears on `sensor.home_ems_plan`. Inverter
   control stays **off** until you turn on `switch.home_ems_control_inverter`.

Requirements: Home Assistant 2025.1 or newer with the recorder enabled. `numpy`
is installed automatically.

## Configuration notes

### Prices

* `prices.source` gives the firm day-ahead prices. Use one of:
  * `entity:` – a sensor whose attributes contain a list of timestamped prices
    (`raw_today`/`raw_tomorrow`, `prices`, `prices_today`, …). The format is
    detected automatically; use `attributes:` to pick specific ones and
    `time_key` / `value_key` for unusual formats.
  * `service:` – a price service with a response, e.g.
    `nordpool.get_prices_for_date`. `{date}`, `{start}` and `{end}` in `data`
    are filled in for today and tomorrow.
  * `url:` – JSON or CSV over HTTP.
* Octopus-style rate lists (`valid_from` / `valid_to` / `value_inc_vat`, as
  made for Predbat) are read directly, so an existing
  `sensor.zonneplan_rates_for_predbat` template works as a source.
* `unit`: `auto`, `eur_kwh`, `eur_mwh` or `ct_kwh`. With `auto`, values above 2
  are read as cents for all-in sources and as EUR/MWh for raw ones. `scale:`
  sets an explicit multiplier to EUR/kWh instead.
* `includes_tax: true` for sources that show all-in consumer prices. They are
  converted back to raw day-ahead prices using your tariff settings.
* Tariff: `vat_pct`, `energy_tax`, `import_markup`, `export_mode`
  (`raw` after net metering ends, or `import` while *salderen* still applies),
  `export_fee`, `export_vat`. Check the current energy tax: the default
  (0.0916 €/kWh excl. VAT) is a placeholder.
* `net_metering_until: "2027-01-01"`: until that date export earns the import
  price (salderen), after it `export_mode` applies. Plans that span New Year's
  Day get the right export value on both sides.
* `history_source:` (optional, e.g. a sensor whose state is the current price)
  seeds the weekday price model from up to 4 weeks of hourly recorder
  statistics. Without it, the model starts from the day-ahead source's own
  statistics, or else builds up history as prices come in.

#### EPEX Predictor (free, recommended)

[EPEX Predictor](https://github.com/b3nn0/EpexPredictor) is an open source model
with a free public API (also used by evcc). It predicts the day-ahead prices
per quarter for about 7 days from weather forecasts, for NL, BE, DE, AT, DK and
SE. Home EMS reads it directly:

```yaml
prices:
  forecasts:
    - name: epexpredictor
      url: "https://epexpredictor.batzill.com/prices?region=NL&unit=EUR_PER_KWH&timezone=Europe/Amsterdam"
      unit: eur_kwh
      includes_tax: false       # raw EPEX prices; Home EMS adds tax and markup
      refresh_minutes: 180      # please keep it modest, it is a free service
```

With several forecast sources, the first one wins where they overlap and the
next ones fill what is left.

Hourly values fill four 15-minute slots. When a source is unreachable, the last
successful forecast keeps being used for the slots it still covers. When it
returns a flat line for a day (a placeholder instead of a prediction), that day
is ignored with a warning. Either way, uncovered days fall back to the
built-in weekday model below, never to a flat price.
`sensor.home_ems_import_price` → `sources` shows each forecast's
`last_success` and `covers_until`. On every update the forecast is fitted
(`a·x + b`) against the real day-ahead prices it overlaps, so different tax or
supplier assumptions are corrected automatically. The fit is shown in
`sensor.home_ems_import_price` → `sources`.

Without an external forecast, days beyond the day-ahead prices come from the
built-in model: the same weekday and time in the last 4 weeks, shifted towards
the price level of the last few days. This keeps the weekend/weekday pattern.
Forecast slots get a small `forecast_margin` penalty, so firm prices are
preferred when the difference is marginal. The plan is recomputed every few
minutes, so the near term always uses the best data.

#### Forecast trust (automatic)

Home EMS scores every forecast it used once the real day-ahead price comes in,
per source and per day ahead (exponentially weighted, half-life two weeks).
From that it learns, per source and lead day:

* **trust**: how much of the predicted swing around the week's mean price
  really happens. Forecast prices are damped by this factor before planning.
  An optimiser picks exactly the peaks and dips of a forecast, which is where a
  noisy forecast is most wrong; damping stops it from buying now for a
  "cheap Thursday" or saving energy for a "peak on Saturday" that will not be
  that extreme.
* **bias**: the average error left after the calibration above.
* **error**: the spread that is left. Half of it (incl. VAT) is the forecast
  margin for that slot; the `forecast_price_margin` number is the minimum.

Until a source has about four days of scored hours, a default is used: full
trust for tomorrow, about 7 % less per day further ahead (minimum 40 %). The
history model starts at full trust (it is an average already). The learned
values are in `sensor.home_ems_import_price` → `sources` → `skill`, and the
example dashboard shows them under Prices.

`switch.home_ems_automatic_forecast_trust` (on by default) turns the damping and
the automatic margin off: forecasts are then planned as published, with only
the `forecast_price_margin`. Scoring continues while it is off, so the learned
values are ready when you switch it back on.

With this, the max grid-charge price and min export price numbers are no longer
needed to steer the plan: leave them at 1.00 / 0.00 and use them only as hard
personal limits.

### Solar (Solcast)

* **Solcast integration** (recommended if you already use it for Predbat):
  list its `forecast_today` … `forecast_day_7` sensors under
  `solcast_entities`. No extra API calls are made.
* **Direct API**: `solcast_api_key` + `solcast_sites`. Calls are spread
  between 05:00 and 21:00 within `api_calls_per_day` (shared by all sites).
  The last forecast is stored, so restarts don't use extra calls.
* `number.home_ems_pv_pessimism`: 0 = P50, 1 = P10 (default 0.3).

### Battery power limits

* `max_charge_kw`: grid charging setpoint.
* `max_discharge_kw`: what the battery may deliver to the house in self
  consumption (e.g. the inverter rating or the fuse of its circuit).
* `max_export_kw` (optional, default `max_discharge_kw`): cap on a forced
  export setpoint. What actually reaches the grid is also limited by
  `grid.export_limit_kw`.

The first two are also `number` entities. A value changed there survives
restarts until you change the same option in YAML; then the YAML value wins.

### Reserve, hold and max SoC

* **Reserve SoC**: hard floor. The planner never plans below it.
* **Hold SoC** (`battery.hold_soc_pct`, default = reserve, so off): the level
  the battery is normally kept at. It is a soft floor: every kWh below it costs
  **hold strength** (`battery.hold_cost`, EUR per kWh per day, default 0.10) for
  as long as it stays below. The battery therefore only dips under the hold
  level when that saves more, for an expensive evening or a long dark spell,
  and it is topped up again (PV first, else a cheap grid slot) when staying
  below would cost more than refilling. A short dip in a price peak is cheap,
  a week at the reserve is not. With 0.10, refilling from the grid (round trip
  losses + wear, about 0.08 EUR/kWh) wins after roughly a day below; raise it
  to hold more firmly.
* **Max SoC**: soft ceiling.

### Trading aggressiveness

`number.home_ems_trading_aggressiveness` (0–100 %, default 100) sets how
eagerly the planner trades. Every kWh the battery does differently from plain
self consumption (charging from the grid, exporting, or holding energy back
while the house draws from the grid) must save at least a hurdle:

| Aggressiveness | Hurdle per kWh | Behaviour |
|---|---|---|
| 100 % | 0 | Lowest expected cost: every profitable trade |
| 75 % | 0.05 EUR | Only clearly profitable trades |
| 50 % | 0.10 EUR | Only large spreads (cheap nights vs. evening peaks, negative prices) |
| 1 % | 0.20 EUR | Almost never |
| 0 % | – | Pure self consumption, identical to the baseline |

The hurdle is not a cost: plan costs and savings stay real euros. Unlike the
battery wear cost (a physical cost on every kWh through the battery, also in
self consumption), it only applies to trading, so it expresses how much risk you
accept, not what the battery costs. Manual slots are executed regardless.

### House load

`load.energy_entity` must be a cumulative kWh sensor with long-term statistics
(`state_class: total_increasing`), e.g. your inverter's load energy. The model
reads 28 days of hourly statistics plus the 5-minute statistics (≈10 days) at
startup, refreshes hourly, and keeps its own 15-minute history. List the EV
charger's energy sensor (and other separately planned loads) under
`subtract_entities`. Until 7 days of history exist, a typical profile scaled to
`default_daily_kwh` is used.

A sensor that resets every day (e.g. "load today") is fine, as long as it has
`state_class: total_increasing`. A template sensor needs it set explicitly:

```yaml
template:
  - sensor:
      - name: load_today_template
        unit_of_measurement: kWh
        device_class: energy
        state_class: total_increasing
        state: "{{ ... }}"
```

Without it the recorder keeps only state history, no long-term statistics, and
the load model stays empty (a warning names the cause). Statistics start when
the state class is added; earlier days are not filled in afterwards. Check
Developer tools → Statistics: the sensor must be listed without issues.

### EV

* `connected_entity`: any sensor or binary sensor. "Connected" matches
  `connected_states`, which by default covers `on`, `connected`, `charging`
  and the OCPP statuses `Preparing`, `SuspendedEV`, `SuspendedEVSE` and
  `Finishing`.
* With `soc_entity`, the energy needed follows from target SoC × `battery_kwh`
  ÷ `efficiency`. Without it, set `number.home_ems_ev_energy_to_add` (charger
  kWh for this session). If `session_energy_entity` is set, the energy the
  charger already delivered is subtracted.
* `start` / `stop` actions get `power_w`, `power_kw`, `current_a`
  (per phase) and `phases`.

### Inverter control

You configure HA actions for each mode. Each run gets these variables:

| Variable | Meaning |
|---|---|
| `mode` | `charge`, `export`, `hold`, `self_consumption` (what to do now, see below) |
| `plan_mode` | The planner's mode for the slot, before the mapping below |
| `power_w` / `power_kw` | Battery charge (charge) or discharge (export) power for the current block, 0 otherwise |
| `target_soc` | Whole-percent SoC (%) to stop at: rounded up for charge and down for export, at least 1 % away from the current SoC and never below the reserve |
| `soc`, `reserve_soc`, `hold_soc` | Current SoC, reserve and hold level (%) |
| `curtail` | `true` when exporting would cost money (negative price) |
| `until` | End of the current block (ISO time) |
| `import_price`, `export_price` | Current prices (EUR/kWh) |

Modes:
* **self_consumption** – normal operation: PV surplus charges the battery and
  the battery covers the house.
* **charge** – grid-charge at `power_w` towards `target_soc`.
* **export** – discharge at `power_w` (battery side): the house first, the rest to the grid.
* **hold** – battery idle; the house runs from PV and the grid, a PV surplus
  goes to the grid.

What is sent can differ from the plan's mode, because a stopped battery is a
hold whatever the planner calls it:
* a 0 W charge or export block ("PV to grid" in the plan card) is sent as `hold`;
* self consumption while the plan keeps the battery from discharging (at the
  reserve, with the house drawing more than PV) is sent as `hold`, so the
  inverter's own self use does not drain below the reserve.

`sensor.home_ems_mode` shows what was sent in its `control_mode` and `stop_soc`
attributes. If `hold` or `export` has no actions configured, `self_consumption` is used.
Actions run again when the mode or power changes, and every
`reapply_minutes`.

### Runtime entities

| Entity | Purpose |
|---|---|
| `sensor.home_ems_mode` | Current mode with power, target SoC, warnings |
| `sensor.home_ems_plan` | Plan cost; attributes `slots` (every 15 min) and `days` |
| `sensor.home_ems_target_soc`, `sensor.home_ems_next_charge` | Current target / next grid-charge block |
| `sensor.home_ems_planned_savings` | Plan versus plain self consumption with the same battery, over the horizon |
| `sensor.home_ems_battery_savings` | Plan versus no battery at all, over the horizon |
| `sensor.home_ems_import_price`, `…_export_price` | Current consumer prices with forecast |
| `sensor.home_ems_pv_forecast`, `…_load_forecast` | Remaining today, plus per-day totals |
| `sensor.home_ems_heavy_loads`, `…_manual_slots` | Items, incl. chosen starts of flexible loads |
| `sensor.home_ems_ev` | EV state and planned slots |
| `switch.home_ems_control_inverter` | Plan-only vs. active control |
| `switch.home_ems_allow_grid_charging`, `…_allow_battery_export`, `…_ev_may_use_house_battery` | Policy switches |
| `switch.home_ems_automatic_forecast_trust` | Damp forecast prices and set their margin from measured accuracy |
| `number.home_ems_*` | Max charge/discharge power, reserve/hold/max SoC, hold strength, trading aggressiveness, max grid-charge price, min export price, wear cost, PV pessimism, load margin, forecast margin, EV target / energy |
| `select.home_ems_ev_charge_mode`, `time.home_ems_ev_ready_by` | EV settings |
| `button.home_ems_recalculate`, `…_refresh_forecasts` | Manual triggers |

### Services

```yaml
# One-off forced charge
action: home_ems.add_manual_slot
data: {mode: charge, power_kw: 8, start: "2026-11-01 11:00", end: "2026-11-01 15:00"}

# Weekly: no battery discharge on weekday mornings
action: home_ems.add_manual_slot
data: {mode: hold, weekdays: [mon, tue, wed, thu, fri], start_time: "06:00", end_time: "07:00"}

# Flexible heavy load: the planner picks the cheapest start (see sensor.home_ems_heavy_loads)
action: home_ems.add_heavy_load
data: {name: Dishwasher, power_kw: 2, duration_min: 120, earliest: "2026-10-30 20:00", latest: "2026-10-31 07:00"}
```

```yaml
# Keep a period with wrong meter values (e.g. a simulator test) out of the load model
action: home_ems.exclude_load_history
data: {start: "2026-10-29 08:00", end: "2026-10-29 19:00"}
```

Excluded periods stay excluded when the history is re-read from the recorder.
They are listed on `sensor.home_ems_load_forecast` → `excluded_periods`, and
`home_ems.clear_load_exclusions` undoes them. Readings above `load.max_kw`
(default 30 kW) are ignored automatically.

Also: `remove_manual_slot` / `remove_heavy_load` (by `id`), `clear_manual_slots`
/ `clear_heavy_loads`, `recalculate`, `refresh_forecasts`. Slots and loads added
by service survive restarts.

## Dashboard

[examples/dashboard.yaml](examples/dashboard.yaml) is a complete view (sections
layout): status tiles, the plan, all settings grouped by battery / prices /
forecast / EV, price-source status, and a form for manual slots and heavy loads.
It is a complete dashboard configuration: create a new dashboard (Settings →
Dashboards → Add dashboard → from scratch), open its raw configuration editor
(✏️ → ⋮ → Raw configuration editor) and paste the file over everything there.
To add it to an existing dashboard, copy only the `- title: Home EMS` item
into that dashboard's `views:` list.

### Plan card

The integration serves `custom:home-ems-plan-card` itself and loads it on every
dashboard, so in a browser there is no resource to add (refresh the browser once
after installing or updating).

The iOS companion app does not always load cards this way and shows
"Configuration error" instead. Then add both cards as dashboard resources
(advanced mode on in your profile; Settings → Dashboards → ⋮ → Resources →
Add resource, type *JavaScript module*): `/home_ems/home-ems-plan-card.js` and
`/home_ems/home-ems-schedule-card.js`. Loading them twice in a browser is
harmless.

It shows the week as a table with one row per day.
Click a day to open it, hour by hour. Each row has:

* the import price, coloured from cheap (green) to expensive (red); `≈` marks
  forecast prices
* the mode, with minutes when it covers only part of the row
* battery power, SoC, PV, load (incl. EV), EV, grid and cost
* a pin for manual slots and a lightning bolt where a heavy load runs (hover
  for the name); the day row adds the heavy-load kWh

On a phone the less important columns are hidden.

```yaml
type: custom:home-ems-plan-card
entity: sensor.home_ems_plan   # default
resolution: 60                 # minutes per row: 15, 30, 60 or 120
expand: today                  # today | all | none
price_unit: cent               # cent | eur
columns: [import, export, mode, battery, soc, pv, load, ev, grid, cost]  # optional
```

The week chart in the example uses apexcharts-card (HACS: RomRider/apexcharts-card).

### Schedule card

`custom:home-ems-schedule-card` (also served by the integration) lists the
manual slots and heavy loads and adds or removes them without the Actions page:

* **Slot**: pick the mode, once (from/until) or weekly (days plus times; a
  window may run past midnight), and an optional power and name.
* **Heavy load**: name, power and duration (quick buttons for common
  appliances), then *flexible* (the planner picks the cheapest start in a
  window and shows it), *fixed time* or *weekly*.
* Delete with the bin icon; it asks once more before removing. YAML items get a
  badge: removing them lasts until the next restart.

Times are entered in the Home Assistant time zone.

```yaml
type: custom:home-ems-schedule-card
slots_entity: sensor.home_ems_manual_slots   # default
loads_entity: sensor.home_ems_heavy_loads    # default
presets:                                     # optional; [] hides the quick buttons
  - {name: Sauna, power_kw: 6, duration_min: 90, icon: mdi:hot-tub}
```

## How the planner works

* State: battery SoC on a 0.25 kWh grid (configurable `soc_step_kwh`), with
  the value function interpolated between grid points.
* Actions per slot: self consumption (follows the load exactly), hold, grid
  charge in power steps, export in power steps. Only actions an inverter can
  execute are allowed.
* Cost: import × import price − export × export price + wear cost, with
  curtailment at negative export prices, the grid connection limit, and soft
  penalties for reserve / max SoC / forced slots.
* Trading aggressiveness adds a hurdle per kWh the battery does differently
  from plain self consumption (see Trading aggressiveness).
* Prices beyond the day-ahead prices are damped and get a margin according to
  each forecast's measured accuracy for that many days ahead (see Forecast
  trust).
* End of horizon: energy left in the battery is valued at a low quantile of the
  horizon's import prices, without the forecast margin (override with
  `battery.terminal_value`).
* Savings (both sensors and the plan card) are net of battery wear and count
  the change in stored energy at that same end value. A plan that ends the week
  fuller than it started is not penalised for the energy it bought to get there,
  and energy already in the battery at the start is not counted as free.
* EV and flexible loads are placed using the marginal energy cost from a first
  pass, then the battery plan is solved again with them included.

## Development

```bash
pip install -r requirements_test.txt
python -m pytest tests
```

`tests/test_core.py` covers the planner, forecasts, parsing and EV logic
(numpy only). `tests/ha/` boots a real Home Assistant core with recorder
through `pytest-homeassistant-custom-component`, and checks setup, entities,
inverter/EV actions, services and the example configuration.

`python tools/simulate_week.py` prints the week comparison above.

## License

[MIT](LICENSE). Provided as is, without warranty: Home EMS can control your
inverter and battery. Start with inverter control off (the default), check the
plan, and use it at your own risk.
