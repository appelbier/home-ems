/*
 * Home EMS schedule card: list, add and remove manual slots and heavy loads.
 *
 *   type: custom:home-ems-schedule-card
 *   slots_entity: sensor.home_ems_manual_slots    # default
 *   loads_entity: sensor.home_ems_heavy_loads     # default
 *   presets:                                      # quick fill for heavy loads; [] hides them
 *     - {name: Sauna, power_kw: 6, duration_min: 90, icon: mdi:hot-tub}
 *
 * Calls the home_ems.add_* / remove_* actions. Times are entered in the Home
 * Assistant time zone. Served by the integration itself; no dashboard resource needed.
 */

const CARD_VERSION = "0.3.0";
const DOMAIN = "home_ems";

const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const SLOT_MODES = {
  charge: { color: "#1e88e5", icon: "mdi:battery-arrow-up" },
  export: { color: "#43a047", icon: "mdi:transmission-tower-export" },
  hold: { color: "#fb8c00", icon: "mdi:battery-lock" },
  self_consumption: { color: "#9e9e9e", icon: "mdi:home-battery-outline" },
};
const LOAD_COLOR = "#d81b60";
const POWER_MODES = ["charge", "export"];  // modes where a power limit means something

const TEXT = {
  en: {
    title: "Manual slots & heavy loads",
    add_slot: "Slot", add_load: "Heavy load",
    new_slot: "New manual slot", new_load: "New heavy load",
    slots: "Manual slots", loads: "Heavy loads", none: "None yet",
    modes: { charge: "Grid charge", export: "Export", hold: "Hold", self_consumption: "Self use" },
    mode: "Mode", when: "When", once: "Once", weekly: "Weekly", fixed: "Fixed time", flexible: "Flexible",
    from: "From", to: "Until", days: "Days", start: "Start", earliest: "Earliest start", latest: "Finished by",
    power: "Power", power_hint: "max", name: "Name", optional: "optional", duration: "Duration",
    every_day: "Every day", workdays: "Weekdays", weekend: "Weekend",
    day_short: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    flexible_hint: "The planner picks the cheapest start in this window.",
    add: "Add", cancel: "Cancel", delete: "Delete", confirm: "Delete?",
    active: "Active", yaml: "YAML", yaml_hint: "From configuration.yaml: comes back after a restart",
    planned: "Planned", not_planned: "Not planned yet", window: "Window",
    err_end: "The end must be after the start.", err_days: "Pick at least one day.",
    err_same: "Start and end time are the same.", err_name: "Give the load a name.",
    err_power: "Give a power above 0 kW.", err_duration: "Duration must be 5 to 1440 minutes.",
    err_window: "The window is shorter than the duration.",
    presets: [
      { name: "Washing machine", power_kw: 2, duration_min: 120, icon: "mdi:washing-machine" },
      { name: "Dryer", power_kw: 2.5, duration_min: 90, icon: "mdi:tumble-dryer" },
      { name: "Dishwasher", power_kw: 1.8, duration_min: 150, icon: "mdi:dishwasher" },
      { name: "Sauna", power_kw: 6, duration_min: 90, icon: "mdi:hot-tub" },
    ],
  },
  nl: {
    title: "Handmatige slots & zware lasten",
    add_slot: "Slot", add_load: "Zware last",
    new_slot: "Nieuw handmatig slot", new_load: "Nieuwe zware last",
    slots: "Handmatige slots", loads: "Zware lasten", none: "Nog niets ingepland",
    modes: { charge: "Laden (net)", export: "Terugleveren", hold: "Vasthouden", self_consumption: "Zelfverbruik" },
    mode: "Modus", when: "Wanneer", once: "Eenmalig", weekly: "Wekelijks", fixed: "Vast tijdstip", flexible: "Flexibel",
    from: "Van", to: "Tot", days: "Dagen", start: "Start", earliest: "Vroegste start", latest: "Klaar vóór",
    power: "Vermogen", power_hint: "max", name: "Naam", optional: "optioneel", duration: "Duur",
    every_day: "Elke dag", workdays: "Werkdagen", weekend: "Weekend",
    day_short: ["ma", "di", "wo", "do", "vr", "za", "zo"],
    flexible_hint: "De planner kiest de goedkoopste start binnen dit venster.",
    add: "Toevoegen", cancel: "Annuleren", delete: "Verwijderen", confirm: "Verwijderen?",
    active: "Actief", yaml: "YAML", yaml_hint: "Uit configuration.yaml: komt terug na een herstart",
    planned: "Gepland", not_planned: "Nog niet ingepland", window: "Venster",
    err_end: "Het einde moet na de start liggen.", err_days: "Kies minstens één dag.",
    err_same: "Start- en eindtijd zijn gelijk.", err_name: "Geef de last een naam.",
    err_power: "Geef een vermogen boven 0 kW.", err_duration: "De duur moet 5 tot 1440 minuten zijn.",
    err_window: "Het venster is korter dan de duur.",
    presets: [
      { name: "Wasmachine", power_kw: 2, duration_min: 120, icon: "mdi:washing-machine" },
      { name: "Droger", power_kw: 2.5, duration_min: 90, icon: "mdi:tumble-dryer" },
      { name: "Vaatwasser", power_kw: 1.8, duration_min: 150, icon: "mdi:dishwasher" },
      { name: "Sauna", power_kw: 6, duration_min: 90, icon: "mdi:hot-tub" },
    ],
  },
};

const esc = (value) =>
  String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/* Wall-clock "YYYY-MM-DDTHH:MM" of `date` in time zone `tz`, the format of
 * <input type="datetime-local">. Arithmetic on these strings is done as if they
 * were UTC, so a DST change inside a window is ignored (the server converts). */
function wallClock(date, tz) {
  const parts = {};
  for (const p of new Intl.DateTimeFormat("en-CA", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    ...(tz ? { timeZone: tz } : {}),
  }).formatToParts(date)) parts[p.type] = p.value;
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

function addMinutes(wall, minutes) {
  const d = new Date(`${wall}:00Z`);
  d.setUTCMinutes(d.getUTCMinutes() + minutes);
  return d.toISOString().slice(0, 16);
}

function minutesBetween(a, b) {
  return (new Date(`${b}:00Z`) - new Date(`${a}:00Z`)) / 60000;
}

class HomeEmsScheduleCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    if (config.presets !== undefined && !Array.isArray(config.presets)) throw new Error("presets must be a list");
    this._config = {
      slots_entity: "sensor.home_ems_manual_slots",
      loads_entity: "sensor.home_ems_heavy_loads",
      title: null,
      ...config,
    };
    this._draft = null;
    this._error = "";
    this._busy = false;
    this._confirm = null;
    this._seen = null;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
      this.shadowRoot.addEventListener("input", (ev) => this._onInput(ev));
      this.shadowRoot.addEventListener("change", (ev) => this._onInput(ev));
      this.shadowRoot.addEventListener("submit", (ev) => {
        ev.preventDefault();  // Enter in a field
        this._submit();
      });
    }
    this._lang = null;
    if (this._hass) this._renderAll();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    this.style.colorScheme = hass.themes && hass.themes.darkMode ? "dark" : "light";
    if (this._lang !== this._language()) {
      this._renderAll();
      return;
    }
    const seen = [this._config.slots_entity, this._config.loads_entity].map((id) => hass.states[id]);
    if (this._seen && seen.every((s, i) => s === this._seen[i])) return;
    this._renderList();
  }

  getCardSize() {
    return 5;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  _language() {
    const lang = (this._hass && ((this._hass.locale && this._hass.locale.language) || this._hass.language)) || "en";
    return lang.toLowerCase().startsWith("nl") ? "nl" : "en";
  }

  get _tz() {
    return this._hass && this._hass.config && this._hass.config.time_zone;
  }

  _num(value, digits = 1) {
    return new Intl.NumberFormat(this._locale, { maximumFractionDigits: digits }).format(value);
  }

  _fmt(iso, opts) {
    const tz = this._tz;
    return new Intl.DateTimeFormat(this._locale, { ...opts, ...(tz ? { timeZone: tz } : {}) }).format(new Date(iso));
  }

  _dateTime(iso) {
    return this._fmt(iso, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }

  _clock(iso) {
    return this._fmt(iso, { hour: "2-digit", minute: "2-digit" });
  }

  _range(startIso, endIso) {
    const sameDay = wallClock(new Date(startIso), this._tz).slice(0, 10) === wallClock(new Date(endIso), this._tz).slice(0, 10);
    return `${this._dateTime(startIso)} – ${sameDay ? this._clock(endIso) : this._dateTime(endIso)}`;
  }

  _days(days) {
    const t = this._t;
    const list = [...days].map(Number).sort();
    if (list.length === 7) return t.every_day;
    if (list.join() === "0,1,2,3,4") return t.workdays;
    if (list.join() === "5,6") return t.weekend;
    return list.map((d) => t.day_short[d]).join(", ");
  }

  _duration(minutes) {
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    const unit = this._lang === "nl" ? "u" : "h";
    return [h ? `${h} ${unit}` : "", m ? `${m} min` : ""].filter(Boolean).join(" ");
  }

  _presets() {
    return this._config.presets !== undefined ? this._config.presets : this._t.presets;
  }

  // ------------------------------------------------------------------ rendering

  _renderAll() {
    if (!this._config || !this._hass || !this.shadowRoot) return;
    this._lang = this._language();
    this._t = TEXT[this._lang];
    this._locale = (this._hass.locale && this._hass.locale.language) || this._hass.language || "en";
    const t = this._t;
    this.shadowRoot.innerHTML = `${STYLE}
      <ha-card>
        <div class="head">
          <div class="card-header">${esc(this._config.title || t.title)}</div>
          <div class="add">
            <button type="button" class="btn outline" data-action="open" data-kind="slot"><ha-icon icon="mdi:plus"></ha-icon>${t.add_slot}</button>
            <button type="button" class="btn outline" data-action="open" data-kind="load"><ha-icon icon="mdi:plus"></ha-icon>${t.add_load}</button>
          </div>
        </div>
        <div class="form-host"></div>
        <div class="lists"></div>
      </ha-card>`;
    this._renderForm();
    this._seen = null;
    this._renderList();
  }

  _items(entityId) {
    const state = this._hass.states[entityId];
    return (state && state.attributes && state.attributes.items) || [];
  }

  _renderList() {
    const host = this.shadowRoot && this.shadowRoot.querySelector(".lists");
    if (!host) return;
    const c = this._config;
    this._seen = [c.slots_entity, c.loads_entity].map((id) => this._hass.states[id]);
    const t = this._t;
    const order = (item) => item.start || item.earliest || `~${item.start_time || ""}`;
    const sorted = (items) => [...items].sort((a, b) => (order(a) < order(b) ? -1 : 1));
    const slots = sorted(this._items(c.slots_entity));
    const loads = sorted(this._items(c.loads_entity));
    const section = (title, icon, items, render) => `
      <div class="section">
        <div class="section-title"><ha-icon icon="${icon}"></ha-icon>${title}<span class="count">${items.length}</span></div>
        ${items.length ? `<ul>${items.map(render).join("")}</ul>` : `<div class="empty">${t.none}</div>`}
      </div>`;
    host.innerHTML =
      section(t.slots, "mdi:calendar-clock", slots, (s) => this._slotItem(s)) +
      section(t.loads, "mdi:lightning-bolt", loads, (h) => this._loadItem(h));
  }

  _badges(item, extra = []) {
    const t = this._t;
    const out = [...extra];
    if (item.weekdays && item.weekdays.length) out.push(`<span class="badge"><ha-icon icon="mdi:repeat"></ha-icon>${t.weekly}</span>`);
    if (item.source === "yaml") out.push(`<span class="badge" title="${esc(t.yaml_hint)}">${t.yaml}</span>`);
    return out.join("");
  }

  _deleteButton(kind, id) {
    const t = this._t;
    const confirming = this._confirm === `${kind}:${id}`;
    return `<button type="button" class="del ${confirming ? "confirm" : ""}" data-action="delete" data-kind="${kind}" data-id="${esc(id)}"
      title="${t.delete}" aria-label="${t.delete}">${confirming ? t.confirm : `<ha-icon icon="mdi:trash-can-outline"></ha-icon>`}</button>`;
  }

  _slotItem(s) {
    const t = this._t;
    const m = SLOT_MODES[s.mode] || SLOT_MODES.self_consumption;
    const label = t.modes[s.mode] || s.mode;
    const when = s.start && s.end
      ? this._range(s.start, s.end)
      : `${this._days(s.weekdays || [])} · ${s.start_time}–${s.end_time}`;
    const now = Date.now();
    const active = s.start && s.end && new Date(s.start) <= now && now < new Date(s.end);
    const power = POWER_MODES.includes(s.mode) ? (s.power_kw ? `${this._num(s.power_kw)} kW` : t.power_hint) : "";
    return `<li class="item" style="--c:${m.color}">
      <span class="icon"><ha-icon icon="${m.icon}"></ha-icon></span>
      <div class="body">
        <div class="title">${esc(s.name || label)}${s.name ? `<span class="sub">${esc(label)}</span>` : ""}
          ${this._badges(s, active ? [`<span class="badge on">${t.active}</span>`] : [])}</div>
        <div class="meta">${esc(when)}</div>
      </div>
      ${power ? `<div class="side">${esc(power)}</div>` : ""}
      ${this._deleteButton("slot", s.id)}
    </li>`;
  }

  _loadItem(h) {
    const t = this._t;
    let when;
    let plan = "";
    if (h.flexible) {
      when = `${t.window} ${this._range(h.earliest, h.latest)}`;
      plan = h.chosen_start
        ? `<div class="meta plan"><ha-icon icon="mdi:calendar-check"></ha-icon>${t.planned}: ${esc(this._range(h.chosen_start, new Date(new Date(h.chosen_start).getTime() + h.duration_min * 60000).toISOString()))}</div>`
        : `<div class="meta dim">${t.not_planned}</div>`;
    } else if (h.start) {
      when = this._dateTime(h.start);
    } else {
      when = `${this._days(h.weekdays || [])} · ${h.start_time}`;
    }
    const flex = h.flexible ? [`<span class="badge"><ha-icon icon="mdi:arrow-expand-horizontal"></ha-icon>${t.flexible}</span>`] : [];
    return `<li class="item" style="--c:${LOAD_COLOR}">
      <span class="icon"><ha-icon icon="mdi:lightning-bolt"></ha-icon></span>
      <div class="body">
        <div class="title">${esc(h.name)}${this._badges(h, flex)}</div>
        <div class="meta">${esc(when)} · ${esc(this._duration(h.duration_min))}</div>
        ${plan}
      </div>
      <div class="side">${this._num(h.power_kw)} kW<span class="sub">${this._num((h.power_kw * h.duration_min) / 60)} kWh</span></div>
      ${this._deleteButton("load", h.id)}
    </li>`;
  }

  _newDraft(kind) {
    const now = new Date();
    const start = addMinutes(wallClock(now, this._tz), 15 - (now.getMinutes() % 15));
    if (kind === "slot") {
      return { kind, mode: "charge", repeat: "once", start, end: addMinutes(start, 120), days: [5, 6],
        start_time: "02:00", end_time: "05:00", power: "", name: "" };
    }
    return { kind, repeat: "flexible", name: "", power: "", duration: 60, start, days: [0, 1, 2, 3, 4],
      start_time: "07:00", earliest: start, latest: addMinutes(start, 720) };
  }

  _renderForm() {
    const host = this.shadowRoot && this.shadowRoot.querySelector(".form-host");
    if (!host) return;
    const d = this._draft;
    for (const btn of this.shadowRoot.querySelectorAll(".add .btn")) {
      btn.setAttribute("aria-pressed", String(Boolean(d && d.kind === btn.dataset.kind)));
    }
    if (!d) {
      host.innerHTML = "";
      return;
    }
    const t = this._t;
    const body = d.kind === "slot" ? this._slotForm(d) : this._loadForm(d);
    host.innerHTML = `
      <form class="form" style="--c:${d.kind === "slot" ? SLOT_MODES[d.mode].color : LOAD_COLOR}" novalidate>
        <div class="form-title">${d.kind === "slot" ? t.new_slot : t.new_load}
          <button type="button" class="icon-btn" data-action="close" aria-label="${t.cancel}"><ha-icon icon="mdi:close"></ha-icon></button></div>
        ${body}
        ${this._error ? `<div class="error"><ha-icon icon="mdi:alert-circle-outline"></ha-icon>${esc(this._error)}</div>` : ""}
        <div class="buttons">
          <button type="button" class="btn text" data-action="close">${t.cancel}</button>
          <button type="submit" class="btn primary" ${this._busy ? "disabled" : ""}>${t.add}</button>
        </div>
      </form>`;
  }

  _seg(field, options, value) {
    return `<div class="seg" role="group">${options
      .map(([v, label, icon, color]) => `<button type="button" class="opt" data-action="set" data-field="${field}" data-value="${v}"
        aria-pressed="${String(v) === String(value)}" ${color ? `style="--c:${color}"` : ""}>${icon ? `<ha-icon icon="${icon}"></ha-icon>` : ""}<span>${label}</span></button>`)
      .join("")}</div>`;
  }

  _field(label, input, cls = "") {
    return `<label class="field ${cls}"><span class="lbl">${label}</span>${input}</label>`;
  }

  _input(type, field, value, attrs = "") {
    return `<input type="${type}" data-field="${field}" value="${esc(value)}" ${attrs}>`;
  }

  _chips(action, items, attrs = "") {
    return `<div class="chips">${items
      .map(([value, label, pressed]) => `<button type="button" class="chip" data-action="${action}" data-value="${value}" ${attrs}
        ${pressed === undefined ? "" : `aria-pressed="${pressed}"`}>${label}</button>`)
      .join("")}</div>`;
  }

  _dayPicker(d) {
    const t = this._t;
    const days = t.day_short.map((label, i) => [i, label, d.days.includes(i)]);
    return `<div class="field wide"><span class="lbl">${t.days}</span>
      ${this._chips("day", days)}
      ${this._chips("days", [["0,1,2,3,4", t.workdays], ["5,6", t.weekend], ["0,1,2,3,4,5,6", t.every_day]], 'data-small="1"')}
    </div>`;
  }

  _slotForm(d) {
    const t = this._t;
    const modes = Object.entries(SLOT_MODES).map(([k, m]) => [k, t.modes[k], m.icon, m.color]);
    let when;
    if (d.repeat === "once") {
      when = `<div class="row">
        ${this._field(t.from, this._input("datetime-local", "start", d.start, 'step="900" required'))}
        ${this._field(t.to, this._input("datetime-local", "end", d.end, 'step="900" required'))}
      </div>
      ${this._chips("span", [60, 120, 180, 240].map((m) => [m, `+${this._duration(m)}`]), 'data-from="start" data-to="end"')}`;
    } else {
      when = `${this._dayPicker(d)}<div class="row">
        ${this._field(t.from, this._input("time", "start_time", d.start_time, 'step="900" required'))}
        ${this._field(t.to, this._input("time", "end_time", d.end_time, 'step="900" required'))}
      </div>`;
    }
    const power = POWER_MODES.includes(d.mode)
      ? this._field(`${t.power} <span class="opt-lbl">kW · ${t.optional}</span>`,
        this._input("number", "power", d.power, `min="0.1" step="0.1" inputmode="decimal" placeholder="${t.power_hint}"`))
      : "";
    return `
      <div class="field wide"><span class="lbl">${t.mode}</span>${this._seg("mode", modes, d.mode)}</div>
      <div class="field wide"><span class="lbl">${t.when}</span>${this._seg("repeat", [["once", t.once, "mdi:calendar"], ["weekly", t.weekly, "mdi:repeat"]], d.repeat)}</div>
      ${when}
      <div class="row">
        ${power}
        ${this._field(`${t.name} <span class="opt-lbl">${t.optional}</span>`, this._input("text", "name", d.name, 'maxlength="60"'))}
      </div>`;
  }

  _loadForm(d) {
    const t = this._t;
    const presets = this._presets();
    const presetChips = presets.length
      ? `<div class="presets">${presets
        .map((p, i) => `<button type="button" class="chip preset" data-action="preset" data-value="${i}">
          <ha-icon icon="${esc(p.icon || "mdi:lightning-bolt")}"></ha-icon>${esc(p.name)}<span class="sub">${this._num(p.power_kw)} kW</span></button>`)
        .join("")}</div>`
      : "";
    let when;
    if (d.repeat === "fixed") {
      when = `<div class="row">${this._field(t.start, this._input("datetime-local", "start", d.start, 'step="900" required'))}</div>`;
    } else if (d.repeat === "weekly") {
      when = `${this._dayPicker(d)}<div class="row">${this._field(t.start, this._input("time", "start_time", d.start_time, 'step="900" required'))}</div>`;
    } else {
      when = `<div class="row">
        ${this._field(t.earliest, this._input("datetime-local", "earliest", d.earliest, 'step="900" required'))}
        ${this._field(t.latest, this._input("datetime-local", "latest", d.latest, 'step="900" required'))}
      </div>
      ${this._chips("span", [360, 720, 1440].map((m) => [m, `+${this._duration(m)}`]), 'data-from="earliest" data-to="latest"')}
      <div class="hint"><ha-icon icon="mdi:information-outline"></ha-icon>${t.flexible_hint}</div>`;
    }
    return `
      ${presetChips}
      <div class="row">
        ${this._field(t.name, this._input("text", "name", d.name, 'maxlength="60" required'), "grow")}
        ${this._field(`${t.power} <span class="opt-lbl">kW</span>`, this._input("number", "power", d.power, 'min="0.1" step="0.1" inputmode="decimal" required'))}
      </div>
      <div class="field wide"><span class="lbl">${t.duration} <span class="opt-lbl">min</span></span>
        <div class="inline">${this._input("number", "duration", d.duration, 'min="5" max="1440" step="5" inputmode="numeric" required class="short"')}
        ${this._chips("duration", [30, 60, 90, 120, 180].map((m) => [m, this._duration(m), Number(d.duration) === m]))}</div>
      </div>
      <div class="field wide"><span class="lbl">${t.when}</span>${this._seg("repeat", [
        ["flexible", t.flexible, "mdi:arrow-expand-horizontal"], ["fixed", t.fixed, "mdi:calendar"], ["weekly", t.weekly, "mdi:repeat"],
      ], d.repeat)}</div>
      ${when}`;
  }

  // ------------------------------------------------------------------- events

  _onInput(ev) {
    const field = ev.target.dataset && ev.target.dataset.field;
    if (!field || !this._draft) return;
    const d = this._draft;
    const previous = d[field];
    d[field] = ev.target.value;
    // Moving the start keeps the window length, so the end never ends up before it.
    const end = (d.kind === "slot" ? { start: "end" } : { earliest: "latest" })[field];
    if (end && d[end] && previous && ev.target.value) {
      const span = minutesBetween(previous, d[end]);
      if (span > 0 && minutesBetween(ev.target.value, d[end]) <= 0) {
        d[end] = addMinutes(ev.target.value, span);
        const input = this.shadowRoot.querySelector(`[data-field="${end}"]`);
        if (input) input.value = d[end];
      }
    }
    if (field === "duration") {
      for (const chip of this.shadowRoot.querySelectorAll('[data-action="duration"]')) {
        chip.setAttribute("aria-pressed", String(Number(chip.dataset.value) === Number(ev.target.value)));
      }
    }
  }

  _onClick(ev) {
    const el = ev.target.closest("[data-action], button[type=submit]");
    if (!el) return;
    ev.preventDefault();
    const d = this._draft;
    const { action, value } = el.dataset;
    if (!action) {
      this._submit();
      return;
    }
    switch (action) {
      case "open":
        this._draft = d && d.kind === el.dataset.kind ? null : this._newDraft(el.dataset.kind);
        this._error = "";
        break;
      case "close":
        this._draft = null;
        this._error = "";
        break;
      case "set":
        d[el.dataset.field] = value;
        break;
      case "day": {
        const day = Number(value);
        d.days = d.days.includes(day) ? d.days.filter((x) => x !== day) : [...d.days, day].sort();
        break;
      }
      case "days":
        d.days = value.split(",").map(Number);
        break;
      case "span":
        d[el.dataset.to] = addMinutes(d[el.dataset.from], Number(value));
        break;
      case "duration":
        d.duration = Number(value);
        break;
      case "preset": {
        const p = this._presets()[Number(value)];
        Object.assign(d, { name: p.name, power: String(p.power_kw), duration: Number(p.duration_min) });
        if (d.repeat === "flexible" && minutesBetween(d.earliest, d.latest) < d.duration) {
          d.latest = addMinutes(d.earliest, d.duration);
        }
        break;
      }
      case "delete":
        this._delete(el.dataset.kind, el.dataset.id);
        return;
      default:
        return;
    }
    this._renderForm();
    const first = action === "open" && this.shadowRoot.querySelector(".form input, .form .opt");
    if (first) first.focus();
  }

  async _delete(kind, id) {
    const key = `${kind}:${id}`;
    clearTimeout(this._confirmTimer);
    if (this._confirm !== key) {
      this._confirm = key;
      this._confirmTimer = setTimeout(() => {
        this._confirm = null;
        this._renderList();
      }, 4000);
      this._renderList();
      return;
    }
    this._confirm = null;
    try {
      await this._hass.callService(DOMAIN, kind === "slot" ? "remove_manual_slot" : "remove_heavy_load", { id });
    } catch (err) {
      this._toast(err);
    }
    this._renderList();
  }

  _toast(err) {
    this.dispatchEvent(new CustomEvent("hass-notification", {
      detail: { message: (err && err.message) || String(err) }, bubbles: true, composed: true,
    }));
  }

  _validate() {
    const d = this._draft;
    const t = this._t;
    const data = {};
    if (d.kind === "slot") {
      data.mode = d.mode;
      if (d.repeat === "once") {
        if (!d.start || !d.end || minutesBetween(d.start, d.end) <= 0) return [t.err_end];
        Object.assign(data, { start: `${d.start}:00`, end: `${d.end}:00` });
      } else {
        if (!d.days.length) return [t.err_days];
        if (!d.start_time || !d.end_time || d.start_time === d.end_time) return [t.err_same];
        Object.assign(data, { weekdays: d.days.map((i) => WEEKDAYS[i]), start_time: d.start_time, end_time: d.end_time });
      }
      if (POWER_MODES.includes(d.mode) && d.power !== "") {
        const power = Number(d.power);
        if (!(power > 0)) return [t.err_power];
        data.power_kw = power;
      }
      if (d.name.trim()) data.name = d.name.trim();
      return [null, "add_manual_slot", data];
    }
    if (!d.name.trim()) return [t.err_name];
    const power = Number(d.power);
    const duration = Number(d.duration);
    if (!(power > 0)) return [t.err_power];
    if (!(duration >= 5 && duration <= 1440)) return [t.err_duration];
    Object.assign(data, { name: d.name.trim(), power_kw: power, duration_min: Math.round(duration) });
    if (d.repeat === "fixed") {
      if (!d.start) return [t.err_end];
      data.start = `${d.start}:00`;
    } else if (d.repeat === "weekly") {
      if (!d.days.length) return [t.err_days];
      Object.assign(data, { weekdays: d.days.map((i) => WEEKDAYS[i]), start_time: d.start_time });
    } else {
      if (!d.earliest || !d.latest || minutesBetween(d.earliest, d.latest) <= 0) return [t.err_end];
      if (minutesBetween(d.earliest, d.latest) < duration) return [t.err_window];
      Object.assign(data, { earliest: `${d.earliest}:00`, latest: `${d.latest}:00` });
    }
    return [null, "add_heavy_load", data];
  }

  async _submit() {
    if (!this._draft || this._busy) return;
    const [error, service, data] = this._validate();
    this._error = error || "";
    if (error) {
      this._renderForm();
      return;
    }
    this._busy = true;
    this._renderForm();
    try {
      await this._hass.callService(DOMAIN, service, data);
      this._draft = null;
    } catch (err) {
      this._error = (err && err.message) || String(err);
    }
    this._busy = false;
    this._renderForm();
  }
}

const STYLE = `<style>
  :host { display: block; }
  ha-card { overflow: hidden; }
  .head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; padding: 12px 16px 4px; }
  .card-header { font-size: 1.25em; font-weight: 500; color: var(--primary-text-color); }
  .add { display: flex; gap: 8px; }
  button { font: inherit; cursor: pointer; }
  button:focus-visible, input:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
  .btn { display: inline-flex; align-items: center; gap: 4px; border-radius: 18px; padding: 6px 14px; font-size: 0.9em; font-weight: 500;
    border: 1px solid transparent; background: none; color: var(--primary-color); }
  .btn ha-icon { --mdc-icon-size: 18px; margin-left: -4px; }
  .btn.outline { border-color: var(--divider-color); }
  .btn.outline[aria-pressed="true"] { background: color-mix(in srgb, var(--primary-color) 14%, transparent); border-color: transparent; }
  .btn.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); padding: 8px 20px; }
  .btn.primary[disabled] { opacity: 0.5; cursor: progress; }
  .btn.text:hover, .btn.outline:hover { background: color-mix(in srgb, var(--primary-color) 8%, transparent); }

  .form { margin: 8px 16px 4px; padding: 12px 14px 14px; border-radius: 12px; display: flex; flex-direction: column; gap: 12px;
    border: 1px solid var(--divider-color); border-top: 3px solid var(--c);
    background: color-mix(in srgb, var(--c) 5%, var(--card-background-color, var(--ha-card-background, #fff))); }
  .form-title { display: flex; align-items: center; justify-content: space-between; font-weight: 500; color: var(--primary-text-color); }
  .icon-btn { border: none; background: none; color: var(--secondary-text-color); padding: 4px; border-radius: 50%; display: inline-flex; }
  .icon-btn:hover { background: var(--secondary-background-color); }
  .row { display: flex; flex-wrap: wrap; gap: 12px; }
  .row > .field { flex: 1 1 160px; min-width: 0; }
  .row > .field.grow { flex: 3 1 200px; }
  .field { display: flex; flex-direction: column; gap: 4px; }
  .lbl { font-size: 0.75em; color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: 0.04em; }
  .opt-lbl { text-transform: none; letter-spacing: 0; opacity: 0.8; }
  input { font: inherit; color: var(--primary-text-color); background: var(--card-background-color, var(--ha-card-background, #fff));
    border: 1px solid var(--divider-color); border-radius: 8px; padding: 8px 10px; min-width: 0; width: 100%; box-sizing: border-box; }
  input.short { width: 90px; flex: none; }
  input:focus { border-color: var(--primary-color); outline: none; }
  .inline { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }

  .seg { display: flex; flex-wrap: wrap; gap: 6px; }
  .opt { --c: var(--primary-color); display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 18px;
    border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); font-size: 0.9em; }
  .opt ha-icon { --mdc-icon-size: 18px; color: var(--c); }
  .opt[aria-pressed="true"] { background: color-mix(in srgb, var(--c) 18%, transparent); border-color: var(--c); font-weight: 500; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip { display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px; border-radius: 14px; font-size: 0.85em;
    border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); }
  .chip:hover { background: var(--secondary-background-color); }
  .chip[aria-pressed="true"] { background: color-mix(in srgb, var(--c) 20%, transparent); border-color: var(--c); }
  .chip[data-small] { border-style: dashed; color: var(--secondary-text-color); font-size: 0.8em; }
  .chip ha-icon { --mdc-icon-size: 16px; color: var(--c); }
  .chip .sub, .side .sub, .title .sub { color: var(--secondary-text-color); font-size: 0.85em; font-weight: 400; }
  .field .chips + .chips { margin-top: 2px; }
  .presets { display: flex; flex-wrap: wrap; gap: 6px; }
  .hint, .error { display: flex; gap: 6px; align-items: flex-start; font-size: 0.85em; }
  .hint { color: var(--secondary-text-color); }
  .hint ha-icon, .error ha-icon { --mdc-icon-size: 16px; flex: none; margin-top: 1px; }
  .error { color: var(--error-color, #db4437); padding: 8px 10px; border-radius: 8px; background: color-mix(in srgb, var(--error-color, #db4437) 10%, transparent); }
  .buttons { display: flex; justify-content: flex-end; gap: 8px; }

  .lists { padding: 4px 0 8px; }
  .section { padding: 8px 16px 4px; }
  .section-title { display: flex; align-items: center; gap: 6px; font-size: 0.8em; font-weight: 500; text-transform: uppercase;
    letter-spacing: 0.04em; color: var(--secondary-text-color); margin-bottom: 4px; }
  .section-title ha-icon { --mdc-icon-size: 16px; }
  .count { margin-left: 2px; padding: 0 6px; border-radius: 8px; background: var(--secondary-background-color); }
  .empty { color: var(--secondary-text-color); font-size: 0.9em; padding: 6px 0 8px; }
  ul { list-style: none; margin: 0; padding: 0; }
  .item { display: flex; align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--divider-color); }
  .item:last-child { border-bottom: none; }
  .icon { flex: none; width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
    background: color-mix(in srgb, var(--c) 18%, transparent); }
  .icon ha-icon { --mdc-icon-size: 20px; color: var(--c); }
  .body { flex: 1; min-width: 0; }
  .title { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; font-weight: 500; color: var(--primary-text-color); }
  .meta { font-size: 0.85em; color: var(--secondary-text-color); margin-top: 1px; }
  .meta.plan { display: flex; align-items: center; gap: 4px; color: var(--primary-text-color); }
  .meta.plan ha-icon { --mdc-icon-size: 15px; color: ${SLOT_MODES.export.color}; }
  .dim { opacity: 0.8; }
  .badge { display: inline-flex; align-items: center; gap: 2px; font-size: 0.72em; font-weight: 500; padding: 1px 6px; border-radius: 8px;
    color: var(--secondary-text-color); background: var(--secondary-background-color); }
  .badge ha-icon { --mdc-icon-size: 12px; }
  .badge.on { color: var(--text-primary-color, #fff); background: var(--c); }
  .side { flex: none; text-align: right; font-size: 0.9em; font-variant-numeric: tabular-nums; color: var(--primary-text-color);
    display: flex; flex-direction: column; }
  .del { flex: none; border: none; background: none; color: var(--secondary-text-color); border-radius: 16px; padding: 6px; display: inline-flex;
    font-size: 0.8em; font-weight: 500; }
  .del:hover { background: var(--secondary-background-color); color: var(--error-color, #db4437); }
  .del.confirm { background: var(--error-color, #db4437); color: #fff; padding: 6px 10px; }
  .del ha-icon { --mdc-icon-size: 20px; }
</style>`;

if (!customElements.get("home-ems-schedule-card")) {
  customElements.define("home-ems-schedule-card", HomeEmsScheduleCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "home-ems-schedule-card",
    name: "Home EMS schedule",
    description: "Add and remove Home EMS manual slots and heavy loads.",
    preview: false,
  });
  console.info(`%c HOME-EMS-SCHEDULE-CARD %c ${CARD_VERSION} `, "background:#d81b60;color:#fff", "");
}
