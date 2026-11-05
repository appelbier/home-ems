/*
 * Home EMS plan card: the week plan as a table, one collapsible block per day.
 *
 *   type: custom:home-ems-plan-card
 *   entity: sensor.home_ems_plan            # default
 *   resolution: 60                          # minutes per row: 15, 30, 60 or 120
 *   expand: today                           # today | all | none
 *   price_unit: cent                        # cent | eur
 *   columns: [import, export, mode, battery, soc, pv, load, ev, grid, cost]
 *
 * Served by the integration itself; no dashboard resource needed.
 */

const CARD_VERSION = "0.3.1";

const MODES = {
  charge: { color: "#1e88e5", icon: "mdi:battery-arrow-up" },
  export: { color: "#43a047", icon: "mdi:transmission-tower-export" },
  hold: { color: "#fb8c00", icon: "mdi:battery-lock" },
  self_consumption: { color: "#9e9e9e", icon: "mdi:home-battery-outline" },
  pv_export: { color: "#f9a825", icon: "mdi:solar-power" },  // export mode, battery idle
};
const HEAVY = { color: "#d81b60", icon: "mdi:lightning-bolt" };

const ALL_COLUMNS = ["import", "export", "mode", "battery", "soc", "pv", "load", "ev", "grid", "cost"];

const TEXT = {
  en: {
    title: "Week plan",
    modes: { charge: "Grid charge", export: "Export", hold: "Hold", self_consumption: "Self use", pv_export: "PV to grid" },
    cols: {
      time: "Time", import: "Price", export: "Export", mode: "Mode", battery: "Battery",
      soc: "SoC", pv: "PV", load: "Load", ev: "EV", grid: "Grid", cost: "Cost",
    },
    now: "Now", soc_now: "SoC", savings: "Savings", battery_savings: "Battery total", plan_cost: "Plan cost", end_soc: "End SoC",
    savings_hint: "Smart plan versus plain self consumption with the same battery, net of wear and energy left at the end",
    battery_hint: "Plan versus no battery at all, net of wear and energy left at the end",
    firm_until: "Prices fixed until", forecast: "forecast price", manual: "manual slot",
    curtail: "PV curtailed", charged: "charge", exported: "export", min: "min", heavy: "heavy load",
    no_plan: "No plan yet: waiting for Home EMS.", missing: "Entity not found",
  },
  nl: {
    title: "Weekplan",
    modes: { charge: "Laden (net)", export: "Terugleveren", hold: "Vasthouden", self_consumption: "Zelfverbruik", pv_export: "PV naar net" },
    cols: {
      time: "Tijd", import: "Prijs", export: "Terug", mode: "Modus", battery: "Accu",
      soc: "SoC", pv: "PV", load: "Verbruik", ev: "Auto", grid: "Net", cost: "Kosten",
    },
    now: "Nu", soc_now: "SoC", savings: "Besparing", battery_savings: "Accu totaal", plan_cost: "Plankosten", end_soc: "Eind-SoC",
    savings_hint: "Slim plan tegenover gewoon zelfverbruik met dezelfde accu, na slijtage en inclusief de energie die aan het eind nog in de accu zit",
    battery_hint: "Plan tegenover helemaal geen accu, na slijtage en inclusief de energie die aan het eind nog in de accu zit",
    firm_until: "Prijzen vast tot", forecast: "voorspelde prijs", manual: "handmatig slot",
    curtail: "PV afgeregeld", charged: "laden", exported: "terug", min: "min", heavy: "zware last",
    no_plan: "Nog geen plan: wacht op Home EMS.", missing: "Entiteit niet gevonden",
  },
};

const esc = (value) =>
  String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function quantile(sorted, q) {
  if (!sorted.length) return 0;
  const pos = (sorted.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.min(lo + 1, sorted.length - 1);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

/* Group 15 minute slots into rows of `minutes`. Times are local ISO strings from
 * the integration; the UTC offset is part of the key so the repeated hour when
 * the clocks go back stays two rows. */
function groupSlots(slots, minutes) {
  const rows = [];
  let current = null;
  for (const slot of slots) {
    const t = slot.start;
    const index = Math.floor((Number(t.slice(11, 13)) * 60 + Number(t.slice(14, 16))) / minutes);
    const key = `${t.slice(0, 10)}|${index}|${t.slice(19)}`;
    if (!current || current.key !== key) {
      current = { key, day: t.slice(0, 10), start: t, slots: [] };
      rows.push(current);
    }
    current.slots.push(slot);
  }
  return rows;
}

function summarise(slots, firmUntil) {
  const sum = (field) => slots.reduce((acc, s) => acc + (Number(s[field]) || 0), 0);
  const counts = {};
  for (const s of slots) counts[s.mode] = (counts[s.mode] || 0) + 1;
  let mode;
  if (counts.charge || counts.export) {
    mode = (counts.charge || 0) >= (counts.export || 0) ? "charge" : "export";
  } else {
    mode = (counts.hold || 0) > (counts.self_consumption || 0) ? "hold" : "self_consumption";
  }
  const hours = slots.length * 0.25;
  const battery = sum("battery_kwh");
  const exported = slots.filter((s) => s.mode === "export").reduce((a, s) => a + Math.max(-s.battery_kwh, 0), 0);
  return {
    mode: mode === "export" && exported < 0.05 ? "pv_export" : mode,
    modeMinutes: (counts[mode] || 0) * 15,
    partial: (counts[mode] || 0) < slots.length,
    batteryKw: hours ? battery / hours : 0,
    charged: slots.filter((s) => s.mode === "charge").reduce((a, s) => a + Math.max(s.battery_kwh, 0), 0),
    exported,
    soc: slots[slots.length - 1].soc,
    minSoc: Math.min(...slots.map((s) => s.soc)),
    pv: sum("pv_kwh"),
    load: sum("load_kwh"),
    ev: sum("ev_kwh"),
    heavy: sum("heavy_kwh"),
    heavyNames: [...new Set(slots.flatMap((s) => s.heavy || []))],
    gridImport: slots.reduce((a, s) => a + Math.max(s.grid_kwh, 0), 0),
    grid: sum("grid_kwh"),
    cost: sum("cost"),
    importPrice: sum("import_price") / slots.length,
    exportPrice: sum("export_price") / slots.length,
    forecast: firmUntil ? slots.some((s) => new Date(s.start) >= firmUntil) : false,
    sources: [...new Set(slots.map((s) => s.price_source).filter(Boolean))],
    manual: slots.some((s) => s.manual),
    curtail: slots.some((s) => s.curtail),
  };
}

class HomeEmsPlanCard extends HTMLElement {
  static getStubConfig() {
    return { entity: "sensor.home_ems_plan" };
  }

  setConfig(config) {
    const resolution = Number(config.resolution || 60);
    if (![15, 30, 60, 120].includes(resolution)) {
      throw new Error("resolution must be 15, 30, 60 or 120");
    }
    const columns = config.columns || ALL_COLUMNS;
    const unknown = columns.filter((c) => !ALL_COLUMNS.includes(c));
    if (unknown.length) throw new Error(`unknown column(s): ${unknown.join(", ")}`);
    this._config = {
      entity: "sensor.home_ems_plan",
      mode_entity: "sensor.home_ems_mode",
      price_entity: "sensor.home_ems_import_price",
      expand: "today",
      price_unit: "cent",
      title: null,
      ...config,
      resolution,
      columns,
      explicitColumns: Boolean(config.columns),
    };
    this._expanded = null;
    this._seen = null;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.addEventListener("click", (ev) => {
        const row = ev.target.closest("[data-day]");
        if (!row) return;
        const day = row.dataset.day;
        if (this._expanded.has(day)) this._expanded.delete(day);
        else this._expanded.add(day);
        this._render();
      });
      this.shadowRoot.addEventListener("keydown", (ev) => {
        if ((ev.key === "Enter" || ev.key === " ") && ev.target.closest("[data-day]")) {
          ev.preventDefault();
          ev.target.closest("[data-day]").click();
        }
      });
    }
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const c = this._config;
    if (!c) return;
    const seen = [c.entity, c.mode_entity, c.price_entity].map((id) => hass.states[id]);
    if (this._seen && seen.every((s, i) => s === this._seen[i]) && this._lang === this._language()) return;
    this._seen = seen;
    this._render();
  }

  getCardSize() {
    return 10;
  }

  getGridOptions() {
    return { columns: "full", min_columns: 6 };
  }

  _language() {
    const lang = (this._hass && ((this._hass.locale && this._hass.locale.language) || this._hass.language)) || "en";
    return lang.toLowerCase().startsWith("nl") ? "nl" : "en";
  }

  _num(value, digits) {
    return new Intl.NumberFormat(this._locale, { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
  }

  _price(value) {
    return this._config.price_unit === "eur" ? `€${this._num(value, 3)}` : `${this._num(value * 100, 1)}`;
  }

  _money(value) {
    const text = this._num(Math.abs(value), 2);
    return value < -0.005 ? `−€${text}` : `€${text}`;
  }

  _kwh(value) {
    if (Math.abs(value) < 0.05) return "";
    return value < 0 ? `−${this._num(-value, 1)}` : this._num(value, 1);
  }

  _dayLabel(day) {
    const date = new Date(`${day}T12:00:00Z`);
    return new Intl.DateTimeFormat(this._locale, { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" }).format(date);
  }

  _timeLabel(iso) {
    const date = new Date(iso);
    const tz = this._hass.config && this._hass.config.time_zone;
    return new Intl.DateTimeFormat(this._locale, {
      weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", ...(tz ? { timeZone: tz } : {}),
    }).format(date);
  }

  _priceStyle(price) {
    const { lo, hi } = this._scale;
    const t = hi > lo ? Math.min(Math.max((price - lo) / (hi - lo), 0), 1) : 0.5;
    const hue = Math.round(120 * (1 - t));
    return `background: hsla(${hue}, 70%, 45%, 0.28)`;
  }

  _modeChip(mode, extra = "") {
    const m = MODES[mode] || MODES.self_consumption;
    const label = this._t.modes[mode] || mode;
    return `<span class="chip" style="--mode:${m.color}"><ha-icon icon="${m.icon}"></ha-icon><span class="label">${esc(label)}</span>${extra}</span>`;
  }

  _render() {
    if (!this._config || !this._hass || !this.shadowRoot) return;
    const c = this._config;
    this._lang = this._language();
    this._t = TEXT[this._lang];
    this._locale = (this._hass.locale && this._hass.locale.language) || this._hass.language || "en";
    const t = this._t;
    const plan = this._hass.states[c.entity];
    const title = esc(c.title || t.title);

    if (!plan) {
      this.shadowRoot.innerHTML = `${STYLE}<ha-card header="${title}"><div class="msg">${t.missing}: ${esc(c.entity)}</div></ha-card>`;
      return;
    }
    const slots = (plan.attributes && plan.attributes.slots) || [];
    if (!slots.length) {
      this.shadowRoot.innerHTML = `${STYLE}<ha-card header="${title}"><div class="msg">${t.no_plan}</div></ha-card>`;
      return;
    }

    const modeState = this._hass.states[c.mode_entity];
    const priceState = this._hass.states[c.price_entity];
    const firmIso = priceState && priceState.attributes.firm_until;
    const firmUntil = firmIso ? new Date(firmIso) : null;
    const prices = slots.map((s) => s.import_price).sort((a, b) => a - b);
    this._scale = { lo: quantile(prices, 0.05), hi: quantile(prices, 0.95) };

    const columns = c.columns.filter(
      (col) => col !== "ev" || c.explicitColumns || slots.some((s) => s.ev_kwh > 0),
    );
    const days = [];
    const byDay = {};
    for (const s of slots) {
      const day = s.start.slice(0, 10);
      if (!byDay[day]) {
        byDay[day] = [];
        days.push(day);
      }
      byDay[day].push(s);
    }
    if (this._expanded === null) {
      this._expanded = new Set(c.expand === "all" ? days : c.expand === "none" ? [] : days.slice(0, 1));
    }

    const header = this._header(plan, modeState, firmUntil);
    const head = `<tr><th class="c-time">${t.cols.time}</th>${columns
      .map((col) => `<th class="c-${col}">${t.cols[col]}${col === "import" || col === "export" ? this._priceUnit() : ""}</th>`)
      .join("")}</tr>`;

    let body = "";
    days.forEach((day, dayIndex) => {
      const open = this._expanded.has(day);
      body += this._dayRow(day, summarise(byDay[day], firmUntil), columns, open);
      if (!open) return;
      groupSlots(byDay[day], c.resolution).forEach((row, i) => {
        body += this._slotRow(row, summarise(row.slots, firmUntil), columns, dayIndex === 0 && i === 0);
      });
    });

    this.shadowRoot.innerHTML = `${STYLE}
      <ha-card>
        <div class="card-header">${title}</div>
        ${header}
        <div class="wrap"><table>
          <thead>${head}</thead>
          <tbody>${body}</tbody>
        </table></div>
        <div class="legend">
          ${Object.keys(MODES).map((m) => this._modeChip(m)).join("")}
          <span class="note"><span class="fc">≈</span> ${t.forecast}</span>
          <span class="note"><ha-icon icon="mdi:pin"></ha-icon> ${t.manual}</span>
          <span class="note"><ha-icon class="heavy" icon="${HEAVY.icon}"></ha-icon> ${t.heavy}</span>
        </div>
      </ha-card>`;
  }

  _priceUnit() {
    return this._config.price_unit === "eur" ? "" : ` <span class="unit">ct</span>`;
  }

  _header(plan, modeState, firmUntil) {
    const t = this._t;
    const a = plan.attributes;
    const items = [];
    if (modeState) {
      const m = modeState.attributes;
      const power = m.power_kw ? ` · ${this._num(m.power_kw, 1)} kW` : "";
      items.push(`<div class="stat"><span class="k">${t.now}</span>${this._modeChip(modeState.state, power ? `<span class="label">${power}</span>` : "")}</div>`);
      if (m.soc !== undefined) items.push(this._stat(t.soc_now, `${this._num(m.soc, 0)}%`));
    }
    if (a.savings !== undefined && a.savings !== null) items.push(this._stat(t.savings, this._money(a.savings), t.savings_hint));
    if (a.battery_savings !== undefined && a.battery_savings !== null) {
      items.push(this._stat(t.battery_savings, this._money(a.battery_savings), t.battery_hint));
    }
    if (a.plan_cost !== undefined) items.push(this._stat(t.plan_cost, this._money(a.plan_cost)));
    if (a.final_soc !== undefined) items.push(this._stat(t.end_soc, `${this._num(a.final_soc, 0)}%`));
    if (firmUntil) items.push(this._stat(t.firm_until, esc(this._timeLabel(firmUntil.toISOString()))));
    const warnings = (modeState && modeState.attributes.warnings) || [];
    const warn = warnings.length
      ? `<div class="warnings"><ha-icon icon="mdi:alert-outline"></ha-icon><ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>`
      : "";
    return `<div class="stats">${items.join("")}</div>${warn}`;
  }

  _stat(key, value, hint = "") {
    return `<div class="stat"${hint ? ` title="${esc(hint)}"` : ""}><span class="k">${key}</span><span class="v">${value}</span></div>`;
  }

  _cells(s, columns, isDay) {
    const t = this._t;
    return columns
      .map((col) => {
        switch (col) {
          case "import": {
            const fc = s.forecast ? `<span class="fc" title="${esc(t.forecast)}: ${esc(s.sources.join(", "))}">≈</span>` : "";
            return `<td class="c-import num" style="${this._priceStyle(s.importPrice)}">${fc}${this._price(s.importPrice)}</td>`;
          }
          case "export":
            return `<td class="c-export num dim">${this._price(s.exportPrice)}</td>`;
          case "mode": {
            if (isDay) {
              const parts = [];
              if (s.charged >= 0.05) parts.push(`<span class="tag" style="--mode:${MODES.charge.color}">▲ ${this._num(s.charged, 0)}<span class="u"> kWh</span></span>`);
              if (s.exported >= 0.05) parts.push(`<span class="tag" style="--mode:${MODES.export.color}">▼ ${this._num(s.exported, 0)}<span class="u"> kWh</span></span>`);
              if (s.heavyNames.length) parts.push(`<span class="tag" style="--mode:${HEAVY.color}" title="${esc(t.heavy)}: ${esc(s.heavyNames.join(", "))}"><ha-icon icon="${HEAVY.icon}"></ha-icon>${this._num(s.heavy, s.heavy < 9.95 ? 1 : 0)}<span class="u"> kWh</span></span>`);
              return `<td class="c-mode">${parts.join(" ") || `<span class="dim">${esc(t.modes.self_consumption)}</span>`}</td>`;
            }
            const minutes = s.partial ? `<span class="mins">${s.modeMinutes}′</span>` : "";
            const pin = s.manual ? `<ha-icon class="pin" icon="mdi:pin" title="${esc(t.manual)}"></ha-icon>` : "";
            const curtail = s.curtail ? `<ha-icon class="pin" icon="mdi:solar-power-variant-outline" title="${esc(t.curtail)}"></ha-icon>` : "";
            const heavy = s.heavyNames.length
              ? `<ha-icon class="pin heavy" icon="${HEAVY.icon}" title="${esc(t.heavy)}: ${esc(s.heavyNames.join(", "))} (${this._num(s.heavy, 1)} kWh)"></ha-icon>`
              : "";
            return `<td class="c-mode">${this._modeChip(s.mode, minutes)}${pin}${heavy}${curtail}</td>`;
          }
          case "battery": {
            if (isDay) return `<td class="c-battery num dim">${t.min} ${this._num(s.minSoc, 0)}%</td>`;
            const kw = s.batteryKw;
            if (Math.abs(kw) < 0.05) return `<td class="c-battery num dim">–</td>`;
            const cls = kw > 0 ? "up" : "down";
            return `<td class="c-battery num ${cls}">${kw > 0 ? "▲" : "▼"} ${this._num(Math.abs(kw), 1)} kW</td>`;
          }
          case "soc": {
            const soc = Math.max(0, Math.min(100, s.soc));
            return `<td class="c-soc"><div class="bar"><div class="fill" style="width:${soc}%"></div><span>${this._num(soc, 0)}%</span></div></td>`;
          }
          case "pv":
            return `<td class="c-pv num">${this._kwh(s.pv)}</td>`;
          case "load":
            return `<td class="c-load num">${this._kwh(s.load)}</td>`;
          case "ev":
            return `<td class="c-ev num">${this._kwh(s.ev)}</td>`;
          case "grid": {
            const v = isDay ? s.gridImport : s.grid;
            return `<td class="c-grid num ${v < -0.05 ? "down" : ""}">${this._kwh(v)}</td>`;
          }
          case "cost":
            return `<td class="c-cost num ${s.cost < -0.005 ? "down" : ""}">${Math.abs(s.cost) < 0.005 ? "" : this._money(s.cost)}</td>`;
          default:
            return "<td></td>";
        }
      })
      .join("");
  }

  _dayRow(day, s, columns, open) {
    return `<tr class="day ${open ? "open" : ""}" data-day="${day}" tabindex="0" role="button" aria-expanded="${open}">
      <td class="c-time"><ha-icon icon="${open ? "mdi:chevron-down" : "mdi:chevron-right"}"></ha-icon>${esc(this._dayLabel(day))}</td>
      ${this._cells(s, columns, true)}
    </tr>`;
  }

  _slotRow(row, s, columns, isNow) {
    return `<tr class="slot ${isNow ? "now" : ""}" style="--mode:${(MODES[s.mode] || MODES.self_consumption).color}">
      <td class="c-time">${row.start.slice(11, 16)}</td>
      ${this._cells(s, columns, false)}
    </tr>`;
  }
}

const STYLE = `<style>
  :host { display: block; }
  ha-card { overflow: hidden; }
  .card-header { padding: 16px 16px 4px; font-size: 1.25em; font-weight: 500; color: var(--primary-text-color); }
  .msg { padding: 16px; color: var(--secondary-text-color); }
  .stats { display: flex; flex-wrap: wrap; gap: 8px 20px; padding: 4px 16px 12px; }
  .stat { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .stat .k { font-size: 0.75em; color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: 0.04em; }
  .stat .v { font-size: 1.05em; font-weight: 500; color: var(--primary-text-color); font-variant-numeric: tabular-nums; }
  .warnings { display: flex; gap: 8px; margin: 0 16px 12px; padding: 8px 12px; border-radius: 8px;
    background: rgba(255, 152, 0, 0.12); color: var(--primary-text-color); font-size: 0.85em; }
  .warnings ha-icon { color: var(--warning-color, #ff9800); --mdc-icon-size: 18px; flex: none; }
  .warnings ul { margin: 0; padding-left: 16px; }
  .wrap { container-type: inline-size; container-name: plan; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 0.875em; color: var(--primary-text-color); }
  th { position: sticky; top: 0; z-index: 1; background: var(--card-background-color, var(--ha-card-background, #fff));
    text-align: right; font-weight: 500; font-size: 0.8em; color: var(--secondary-text-color); padding: 6px 8px;
    border-bottom: 1px solid var(--divider-color); white-space: nowrap; }
  th.c-time, th.c-mode, th.c-soc { text-align: left; }
  td { padding: 3px 8px; white-space: nowrap; border-bottom: 1px solid var(--divider-color); }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .unit { font-weight: 400; opacity: 0.7; }
  .dim { color: var(--secondary-text-color); }
  .up { color: ${MODES.charge.color}; }
  .down { color: ${MODES.export.color}; }
  tr.day { cursor: pointer; background: var(--secondary-background-color, rgba(127, 127, 127, 0.08)); font-weight: 500; }
  tr.day:hover { filter: brightness(0.97); }
  tr.day:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -2px; }
  tr.day td { padding-top: 7px; padding-bottom: 7px; }
  tr.day ha-icon { --mdc-icon-size: 18px; margin-right: 4px; vertical-align: -3px; color: var(--secondary-text-color); }
  tr.slot td.c-time { border-left: 3px solid var(--mode); color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
  tr.now td { background-image: linear-gradient(rgba(3, 169, 244, 0.10), rgba(3, 169, 244, 0.10)); }
  tr.now td.c-time { color: var(--primary-text-color); font-weight: 600; }
  .chip { display: inline-flex; align-items: center; gap: 4px; padding: 1px 8px 1px 4px; border-radius: 12px;
    background: color-mix(in srgb, var(--mode) 18%, transparent); color: var(--primary-text-color); font-size: 0.95em; }
  .chip ha-icon { --mdc-icon-size: 16px; color: var(--mode); }
  .chip .mins { font-size: 0.8em; color: var(--secondary-text-color); }
  .tag { display: inline-block; padding: 0 6px; border-radius: 10px; font-size: 0.9em;
    background: color-mix(in srgb, var(--mode) 18%, transparent); }
  .pin { --mdc-icon-size: 14px; margin-left: 4px; color: var(--secondary-text-color); }
  .heavy, .pin.heavy { color: ${HEAVY.color}; }
  .tag ha-icon { --mdc-icon-size: 13px; color: ${HEAVY.color}; vertical-align: -2px; margin-right: 1px; }
  .fc { margin-right: 3px; color: var(--secondary-text-color); font-weight: 600; }
  .bar { position: relative; width: 72px; height: 16px; border-radius: 4px; overflow: hidden;
    background: color-mix(in srgb, var(--primary-color, #03a9f4) 12%, transparent); }
  .bar .fill { position: absolute; inset: 0 auto 0 0; background: color-mix(in srgb, var(--primary-color, #03a9f4) 45%, transparent); }
  .bar span { position: relative; display: block; text-align: center; font-size: 0.8em; line-height: 16px; font-variant-numeric: tabular-nums; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 12px; padding: 10px 16px 14px; font-size: 0.8em; color: var(--secondary-text-color); }
  .legend .note { display: inline-flex; align-items: center; gap: 2px; }
  .legend .note ha-icon { --mdc-icon-size: 14px; }
  @container plan (max-width: 640px) { .c-export, .c-grid { display: none; } }
  @container plan (max-width: 480px) {
    .c-load, .c-ev, .c-cost, .c-pv, .c-battery { display: none; }
    .tag .u { display: none; }
    .chip .label { display: none; }
    .chip { padding-right: 4px; }
    .bar { width: 48px; }
    td, th { padding-left: 5px; padding-right: 5px; }
  }
</style>`;

if (!customElements.get("home-ems-plan-card")) {
  customElements.define("home-ems-plan-card", HomeEmsPlanCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "home-ems-plan-card",
    name: "Home EMS plan",
    description: "Week plan of Home EMS as a table per day.",
    preview: false,
  });
  console.info(`%c HOME-EMS-PLAN-CARD %c ${CARD_VERSION} `, "background:#1e88e5;color:#fff", "");
}
