/*
 * family-tasks-card — Lovelace card for the family_tasks integration.
 *
 * Self-contained: vanilla custom elements + shadow DOM, no build step, no
 * external imports. Served by the integration at /family_tasks/family-tasks-card.js.
 *
 * Data comes from the integration's websocket API:
 *   family_tasks/config     -> { people, lists (with service + capabilities), completed_days }
 *   family_tasks/subscribe  -> stream of { tasks: [...] } snapshots
 * Writes go through the family_tasks.* actions (services).
 *
 * All user-supplied text is inserted with textContent / text nodes, never innerHTML.
 */

const CARD_VERSION = "0.1.0";
const DOMAIN = "family_tasks";
const DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const PRIORITY_RANK = { high: 0, medium: 1, low: 2 };
const SVG_NS = "http://www.w3.org/2000/svg";

/* ------------------------------------------------------------------ helpers */

/** Tiny DOM builder. String children become text nodes (safe). */
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k.startsWith("on") && typeof v === "function") {
        el.addEventListener(k.slice(2), v);
      } else if (k === "class") {
        el.className = v;
      } else if (k === "style" && typeof v === "object") {
        for (const [sk, sv] of Object.entries(v)) {
          if (sv !== undefined && sv !== null) el.style.setProperty(sk, sv);
        }
      } else if (k === "value") {
        el.value = v;
      } else if (typeof v === "boolean") {
        if (k in el) el[k] = v;
        else el.setAttribute(k, "");
      } else {
        el.setAttribute(k, String(v));
      }
    }
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function icon(name) {
  return h("ha-icon", { icon: name });
}

function checkSvg() {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", "M5 12.5l4.5 4.5L19 7.5");
  svg.append(path);
  return svg;
}

function errMsg(err) {
  if (!err) return "Unknown error";
  if (typeof err === "string") return err;
  return err.message || err.error?.message || err.code || "Unknown error";
}

function safeColor(c) {
  return typeof c === "string" && c.length > 0 && c.length < 64 ? c : "var(--primary-color)";
}

function sameSet(a, b) {
  const sa = new Set(a);
  const sb = new Set(b);
  if (sa.size !== sb.size) return false;
  for (const x of sa) if (!sb.has(x)) return false;
  return true;
}

function loadPrefs(key) {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? JSON.parse(raw) || {} : {};
  } catch (_e) {
    return {};
  }
}

function savePrefs(key, prefs) {
  try {
    window.localStorage.setItem(key, JSON.stringify(prefs));
  } catch (_e) {
    /* storage unavailable: preferences just won't persist */
  }
}

/* ------------------------------------------------------------- date helpers */

const pad = (n) => String(n).padStart(2, "0");

function localDateStr(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function parseDue(task) {
  if (!task || !task.due) return null;
  if (!task.due_has_time && /^\d{4}-\d{2}-\d{2}$/.test(task.due)) {
    const [y, m, d] = task.due.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  const d = new Date(task.due);
  return Number.isNaN(d.getTime()) ? null : d;
}

function dueParts(task) {
  const d = parseDue(task);
  if (!d) return { date: "", time: "" };
  return {
    date: localDateStr(d),
    time: task.due_has_time ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : "",
  };
}

function dayDiff(d) {
  const a = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const n = new Date();
  const b = new Date(n.getFullYear(), n.getMonth(), n.getDate());
  return Math.round((a - b) / 86400000);
}

function friendlyDue(task, lang) {
  const d = parseDue(task);
  if (!d) return "";
  const diff = dayDiff(d);
  let label;
  if (diff === 0) label = "Today";
  else if (diff === 1) label = "Tomorrow";
  else if (diff === -1) label = "Yesterday";
  else if (diff > 1 && diff < 7) label = d.toLocaleDateString(lang, { weekday: "long" });
  else {
    const opts = { weekday: "short", month: "short", day: "numeric" };
    if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
    label = d.toLocaleDateString(lang, opts);
  }
  if (task.due_has_time) {
    label += ", " + d.toLocaleTimeString(lang, { hour: "numeric", minute: "2-digit" });
  }
  return label;
}

/* ------------------------------------------------------------ rrule helpers */

/** Map an RRULE to a simple repeat mode the dialog can edit, or "custom". */
function parseRrule(rrule) {
  if (!rrule) return { mode: "none", days: [] };
  const parts = {};
  for (const p of String(rrule).replace(/^RRULE:/i, "").split(";")) {
    if (!p) continue;
    const [k, v = ""] = p.split("=");
    parts[k.toUpperCase()] = v.toUpperCase();
  }
  const keys = Object.keys(parts).filter(
    (k) => !(k === "INTERVAL" && parts[k] === "1") && k !== "WKST",
  );
  const only = (...allowed) => keys.every((k) => allowed.includes(k));
  if (parts.FREQ === "DAILY" && only("FREQ")) return { mode: "daily", days: [] };
  if (parts.FREQ === "MONTHLY" && only("FREQ")) return { mode: "monthly", days: [] };
  if (parts.FREQ === "WEEKLY" && only("FREQ", "BYDAY")) {
    const days = parts.BYDAY ? parts.BYDAY.split(",") : [];
    if (days.every((d) => DAYS.includes(d))) return { mode: "weekly", days };
  }
  return { mode: "custom", days: [] };
}

function buildRrule(mode, days) {
  if (mode === "daily") return "FREQ=DAILY";
  if (mode === "monthly") return "FREQ=MONTHLY";
  if (mode === "weekly") {
    const sorted = DAYS.filter((d) => days.includes(d));
    return sorted.length ? `FREQ=WEEKLY;BYDAY=${sorted.join(",")}` : "FREQ=WEEKLY";
  }
  return "";
}

function describeRrule(rrule) {
  const r = parseRrule(rrule);
  if (r.mode === "daily") return "Daily";
  if (r.mode === "monthly") return "Monthly";
  if (r.mode === "weekly") {
    if (!r.days.length) return "Weekly";
    return "Weekly on " + DAYS.filter((d) => r.days.includes(d)).map((d) => DAY_LABELS[DAYS.indexOf(d)]).join(", ");
  }
  return String(rrule || "");
}

/* ------------------------------------------------------------------- styles */

const STYLES = `
:host { display: block; }
* { box-sizing: border-box; }
button { font: inherit; color: inherit; cursor: pointer; -webkit-tap-highlight-color: transparent; }
button:focus-visible, [role="button"]:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
ha-card { display: flex; flex-direction: column; height: 100%; overflow: hidden; container-type: inline-size; }

.header { display: flex; align-items: center; gap: 12px; padding: 16px 16px 8px; }
.title { flex: 1; min-width: 0; font-size: 1.5rem; font-weight: 500; color: var(--primary-text-color);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hactions { display: flex; gap: 8px; flex: none; }
.hbtn, .add { display: inline-flex; align-items: center; gap: 6px; min-height: 48px; padding: 0 16px;
  border-radius: 24px; border: 1px solid var(--divider-color); background: transparent;
  color: var(--primary-text-color); font-size: 1rem; }
.add { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; font-weight: 500; }
.add:disabled { opacity: 0.5; cursor: default; }

.filters { display: flex; gap: 8px; padding: 4px 16px 12px; overflow-x: auto; scrollbar-width: none; flex: none; }
.filters::-webkit-scrollbar { display: none; }
.fchip { flex: none; display: inline-flex; align-items: center; gap: 8px; min-height: 48px; padding: 0 16px;
  border-radius: 24px; border: 2px solid var(--chip-color, var(--divider-color)); background: transparent;
  color: var(--primary-text-color); font-size: 1rem; }
.fchip.has-avatar { padding-left: 6px; }
.fchip.active { background: var(--chip-color, var(--primary-color)); border-color: var(--chip-color, var(--primary-color));
  color: var(--text-primary-color, #fff); }
.count { min-width: 24px; height: 24px; padding: 0 6px; border-radius: 12px; background: rgba(127, 127, 127, 0.22);
  font-size: 0.85rem; display: inline-flex; align-items: center; justify-content: center; }

.list { flex: 1; overflow-y: auto; padding: 0 8px 8px; }
.group { padding: 16px 8px 4px; font-size: 0.85rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--secondary-text-color); }
.row { display: flex; align-items: center; gap: 12px; min-height: 64px; padding: 8px;
  border-bottom: 1px solid var(--divider-color); cursor: pointer; border-radius: 8px; }
.row:last-child { border-bottom: none; }
.row:active { background: rgba(127, 127, 127, 0.12); }
.row.sub { padding-left: 32px; }
.row.pending { opacity: 0.55; }
.check { flex: none; width: 48px; height: 48px; border: none; background: none; padding: 0;
  display: flex; align-items: center; justify-content: center; }
.check .box { width: 32px; height: 32px; border-radius: 50%; border: 2.5px solid var(--secondary-text-color);
  display: flex; align-items: center; justify-content: center; transition: background 0.15s, border-color 0.15s; }
.check svg { width: 22px; height: 22px; opacity: 0; fill: none; stroke: var(--text-primary-color, #fff);
  stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.check.done .box { background: var(--primary-color); border-color: var(--primary-color); }
.check.done svg { opacity: 1; }
.main { flex: 1; min-width: 0; }
.summary { font-size: 1.2rem; color: var(--primary-text-color); overflow-wrap: anywhere; }
.completed .summary { text-decoration: line-through; color: var(--secondary-text-color); }
.meta { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; margin-top: 2px;
  font-size: 0.95rem; color: var(--secondary-text-color); }
.meta ha-icon { --mdc-icon-size: 18px; }
.meta > span { display: inline-flex; align-items: center; gap: 4px; }
.due.overdue { color: var(--error-color); font-weight: 600; }
.prio-high { color: var(--error-color); }
.tag { padding: 1px 8px; border-radius: 10px; background: rgba(127, 127, 127, 0.15); font-size: 0.85rem; }
.chips { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; max-width: 45%; flex: none; }
.pchip { display: inline-flex; align-items: center; gap: 6px; height: 36px; padding: 0 12px 0 3px;
  border-radius: 18px; background: color-mix(in srgb, var(--chip-color) 20%, transparent);
  border: 1.5px solid var(--chip-color); color: var(--primary-text-color); font-size: 0.95rem; white-space: nowrap; }

.avatar { border-radius: 50%; object-fit: cover; flex: none; }
.avatar.sm { width: 30px; height: 30px; }
.avatar.lg { width: 44px; height: 44px; }
img.avatar { border: 2px solid var(--chip-color, transparent); background: var(--chip-color, transparent); }
.avatar.initial { display: inline-flex; align-items: center; justify-content: center;
  background: var(--chip-color, var(--primary-color)); color: #fff; font-weight: 600; }
.avatar.sm.initial { font-size: 0.9rem; }
.avatar.lg.initial { font-size: 1.25rem; }

.empty { padding: 32px 16px; text-align: center; color: var(--secondary-text-color); font-size: 1.1rem; }
.empty.error { color: var(--error-color); }

.toast { position: fixed; left: 50%; bottom: 24px; z-index: 1000; transform: translate(-50%, 150%); opacity: 0;
  max-width: min(600px, calc(100vw - 32px)); padding: 14px 20px; border-radius: 12px;
  background: var(--primary-text-color); color: var(--card-background-color, #fff); font-size: 1rem;
  transition: transform 0.2s, opacity 0.2s; pointer-events: none; }
.toast.show { transform: translate(-50%, 0); opacity: 1; }

@container (max-width: 520px) {
  .chips { max-width: 40%; }
  .pchip { padding: 0 3px; }
  .pchip .pname { display: none; }
  .hbtn span { display: none; }
  .title { font-size: 1.25rem; }
}

/* dialog */
.overlay { position: fixed; inset: 0; z-index: 999; background: rgba(0, 0, 0, 0.5);
  display: flex; align-items: center; justify-content: center; padding: 16px; }
.dialog { background: var(--ha-card-background, var(--card-background-color, #fff)); color: var(--primary-text-color);
  border-radius: var(--ha-card-border-radius, 16px); width: min(760px, 100%); max-height: 100%;
  display: flex; flex-direction: column; box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4); }
.dhead { padding: 20px 24px 8px; font-size: 1.4rem; font-weight: 500; }
.dbody { padding: 8px 24px; overflow-y: auto; display: flex; flex-direction: column; gap: 18px; }
.flabel { display: block; font-size: 0.9rem; font-weight: 500; color: var(--secondary-text-color); margin-bottom: 8px; }
input.text, textarea, input[type="date"], input[type="time"] {
  font: inherit; font-size: 1.15rem; color: var(--primary-text-color);
  background: var(--secondary-background-color, rgba(127, 127, 127, 0.08));
  border: 1px solid var(--divider-color); border-radius: 10px; padding: 12px 14px; min-height: 52px; width: 100%;
  color-scheme: light dark; }
input:focus, textarea:focus { outline: 2px solid var(--primary-color); outline-offset: -1px; }
textarea { min-height: 96px; resize: vertical; }
.people { display: flex; flex-wrap: wrap; gap: 10px; }
.pbtn { display: inline-flex; align-items: center; gap: 10px; min-height: 60px; padding: 0 20px 0 8px;
  border-radius: 30px; border: 2px solid var(--chip-color); background: transparent;
  color: var(--primary-text-color); font-size: 1.1rem; }
.pbtn .tick { --mdc-icon-size: 22px; display: none; }
.pbtn.on { background: color-mix(in srgb, var(--chip-color) 32%, transparent); font-weight: 600; }
.pbtn.on .tick { display: inline-flex; }
.hint { font-size: 0.95rem; color: var(--secondary-text-color); }
.duerow { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.duerow input { flex: 1 1 170px; width: auto; }
.quick { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.segs { display: inline-flex; flex-wrap: wrap; border: 1px solid var(--divider-color); border-radius: 12px;
  overflow: hidden; max-width: 100%; }
.seg { min-height: 52px; min-width: 76px; padding: 0 16px; border: none; background: transparent;
  color: var(--primary-text-color); font-size: 1.05rem; border-right: 1px solid var(--divider-color); }
.seg:last-child { border-right: none; }
.seg.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
.days { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.day { min-width: 56px; height: 52px; border-radius: 26px; border: 1px solid var(--divider-color);
  background: transparent; font-size: 1rem; color: var(--primary-text-color); padding: 0 10px; }
.day.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
.derror { color: var(--error-color); font-size: 1rem; }
.derror:empty { display: none; }
.dfoot { display: flex; gap: 10px; padding: 12px 24px 20px; align-items: center; flex-wrap: wrap; }
.spacer { flex: 1; }
.btn { min-height: 52px; padding: 0 24px; border-radius: 26px; font-size: 1.05rem;
  border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color); }
.btn.small { min-height: 44px; padding: 0 16px; font-size: 1rem; }
.btn.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; font-weight: 500; }
.btn.danger { color: var(--error-color); border-color: var(--error-color); }
.btn.danger.confirm { background: var(--error-color); color: #fff; }
.btn:disabled { opacity: 0.5; cursor: default; }
@media (max-width: 600px) {
  .overlay { padding: 0; align-items: stretch; }
  .dialog { border-radius: 0; max-height: none; height: 100%; }
  .dhead, .dbody, .dfoot { padding-left: 16px; padding-right: 16px; }
}
`;

/* --------------------------------------------------------------------- card */

class FamilyTasksCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tasks = null;
    this._ftConfig = null;
    this._error = null;
    this._pending = new Map(); // uid -> timeout id (optimistic toggle in flight)
    this._dialog = null;
    this._built = false;
    this._unsubP = null;
    this._subToken = null;
  }

  static getConfigElement() {
    return document.createElement("family-tasks-card-editor");
  }

  static getStubConfig(hass) {
    const states = (hass && hass.states) || {};
    const entity =
      Object.keys(states).find((id) => id === "todo.family_to_do") ||
      Object.keys(states).find((id) => id.startsWith("todo.family")) ||
      "todo.family_to_do";
    return { entity };
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("Set 'entity' to a family_tasks to-do list (for example todo.family_to_do)");
    }
    const old = this._config;
    this._config = { sort: "due", show_completed: true, default_filter: "everyone", ...config };
    this._prefKey = `family-tasks-card:${this._config.entity}:${this._config.title || ""}`;
    const prefs = loadPrefs(this._prefKey);
    this._filter = prefs.filter || this._config.default_filter || "everyone";
    this._sort = prefs.sort === "person" || prefs.sort === "due" ? prefs.sort : this._config.sort;
    this._build();
    if (old && old.entity !== this._config.entity) {
      this._unsubscribe();
      this._tasks = null;
    }
    if (this.isConnected && this._hass && !this._subToken) this._subscribe();
    this._render();
  }

  set hass(hass) {
    const prev = this._hass;
    this._hass = hass;
    if (!this._config) return;
    if (this.isConnected && (!this._subToken || (prev && prev.connection !== hass.connection))) {
      this._subscribe();
    }
    const sig = this._peopleSig();
    if (sig !== this._lastSig) {
      this._lastSig = sig;
      this._render();
    }
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (this._hass && this._config && !this._subToken) this._subscribe();
  }

  disconnectedCallback() {
    this._unsubscribe();
  }

  getCardSize() {
    const n = this._tasks ? this._visibleTasks().length : 3;
    return Math.max(3, Math.min(14, 2 + n));
  }

  getGridOptions() {
    return { columns: 12, rows: 8, min_columns: 6, min_rows: 4 };
  }

  /* ---- data ---- */

  async _subscribe() {
    this._unsubscribe();
    const hass = this._hass;
    if (!hass || !hass.connection) return;
    const token = {};
    this._subToken = token;
    try {
      const cfg = await hass.callWS({ type: `${DOMAIN}/config` });
      if (this._subToken !== token) return;
      this._ftConfig = cfg || { people: [], lists: [] };
      this._ftConfig.people = this._ftConfig.people || [];
      this._error = null;
      const msg = { type: `${DOMAIN}/subscribe` };
      if (this._config.entity) msg.entity_id = this._config.entity;
      const p = hass.connection.subscribeMessage((m) => {
        if (this._subToken !== token) return;
        this._tasks = Array.isArray(m && m.tasks) ? m.tasks : [];
        for (const t of this._pending.values()) clearTimeout(t);
        this._pending.clear();
        this._error = null;
        this._render();
        if (this._dialog) this._dialog.tasksUpdated(this._tasks);
      }, msg);
      this._unsubP = p;
      await p;
    } catch (err) {
      if (this._subToken !== token) return;
      this._unsubP = null;
      this._error = `Family tasks: ${errMsg(err)}`;
    }
    this._lastSig = this._peopleSig();
    this._render();
  }

  _unsubscribe() {
    this._subToken = null;
    const p = this._unsubP;
    this._unsubP = null;
    if (p) {
      p.then((unsub) => {
        try {
          unsub();
        } catch (_e) {
          /* already gone */
        }
      }).catch(() => {});
    }
  }

  _peopleSig() {
    if (!this._hass || !this._ftConfig) return "";
    const parts = this._ftConfig.people.map((p) => {
      const s = this._hass.states[p.person];
      const a = (s && s.attributes) || {};
      return `${p.person}|${a.entity_picture || ""}|${a.friendly_name || ""}`;
    });
    parts.push((this._hass.locale && this._hass.locale.language) || this._hass.language || "");
    return parts.join(";");
  }

  _people() {
    return (this._ftConfig && this._ftConfig.people) || [];
  }

  /** Mapped people assigned to a task, in configured order. */
  _taskPeople(task) {
    const persons = new Set(task.assignees || []);
    const tags = new Set((task.assignee_tags || []).map((t) => String(t).toLowerCase()));
    return this._people().filter((p) => persons.has(p.person) || tags.has(String(p.tag).toLowerCase()));
  }

  _personName(p) {
    const s = this._hass && this._hass.states[p.person];
    return p.name || (s && s.attributes && s.attributes.friendly_name) || p.tag;
  }

  _filterKey() {
    const f = String(this._filter || "everyone").toLowerCase();
    if (f === "everyone" || f === "unassigned") return f;
    return this._people().some((p) => String(p.tag).toLowerCase() === f) ? f : "everyone";
  }

  _isDone(task) {
    const done = task.status === "completed";
    return this._pending.has(task.uid) ? !done : done;
  }

  _visibleTasks() {
    if (!this._tasks) return [];
    const key = this._filterKey();
    const showDone = this._config.show_completed !== false;
    let ts = this._tasks.filter((t) => showDone || t.status !== "completed" || this._pending.has(t.uid));
    if (key === "unassigned") ts = ts.filter((t) => this._taskPeople(t).length === 0);
    else if (key !== "everyone") {
      ts = ts.filter((t) => this._taskPeople(t).some((p) => String(p.tag).toLowerCase() === key));
    }
    const people = this._people();
    const dueKey = (t) => {
      const d = parseDue(t);
      return d ? d.getTime() : Number.MAX_SAFE_INTEGER;
    };
    const pr = (t) => (t.priority in PRIORITY_RANK ? PRIORITY_RANK[t.priority] : 3);
    const personIdx = (t) => {
      const ps = this._taskPeople(t);
      return ps.length ? people.indexOf(ps[0]) : people.length;
    };
    const byPerson = this._sort === "person" && key === "everyone";
    const cmpOpen = (a, b) =>
      (byPerson ? personIdx(a) - personIdx(b) : 0) ||
      dueKey(a) - dueKey(b) ||
      pr(a) - pr(b) ||
      String(a.summary || "").localeCompare(String(b.summary || ""));
    const open = ts.filter((t) => t.status !== "completed").sort(cmpOpen);
    const done = ts
      .filter((t) => t.status === "completed")
      .sort((a, b) => String(b.completed || "").localeCompare(String(a.completed || "")));
    return open.concat(done);
  }

  _counts() {
    const counts = { everyone: 0, unassigned: 0 };
    for (const t of this._tasks || []) {
      if (t.status === "completed") continue;
      counts.everyone++;
      const ps = this._taskPeople(t);
      if (!ps.length) counts.unassigned++;
      for (const p of ps) {
        const k = String(p.tag).toLowerCase();
        counts[k] = (counts[k] || 0) + 1;
      }
    }
    return counts;
  }

  /* ---- actions ---- */

  _setFilter(value) {
    this._filter = value;
    savePrefs(this._prefKey, { filter: this._filter, sort: this._sort });
    this._render();
  }

  _toggleSort() {
    this._sort = this._sort === "due" ? "person" : "due";
    savePrefs(this._prefKey, { filter: this._filter, sort: this._sort });
    this._render();
  }

  async _toggleDone(task) {
    if (this._pending.has(task.uid)) return;
    const service = task.status === "completed" ? "reopen_task" : "complete_task";
    // Optimistic flip until the next snapshot arrives (or 15 s pass).
    this._pending.set(
      task.uid,
      setTimeout(() => {
        this._pending.delete(task.uid);
        this._renderList();
      }, 15000),
    );
    this._renderList();
    try {
      await this._hass.callService(DOMAIN, service, { uid: task.uid });
    } catch (err) {
      clearTimeout(this._pending.get(task.uid));
      this._pending.delete(task.uid);
      this._renderList();
      this._toast(`Couldn't update “${task.summary || "task"}”: ${errMsg(err)}`);
    }
  }

  _openDialog(task) {
    if (!this._ftConfig || !this._hass) return;
    this._closeDialog();
    this._dialog = new TaskDialog(this, task);
    this._dialogHost.replaceChildren(this._dialog.el);
    this._dialog.opened();
  }

  _closeDialog() {
    if (this._dialog) this._dialog.destroy();
    this._dialog = null;
    if (this._dialogHost) this._dialogHost.replaceChildren();
  }

  _toast(msg) {
    this._toastEl.textContent = msg;
    this._toastEl.classList.add("show");
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => this._toastEl.classList.remove("show"), 6000);
  }

  /* ---- rendering ---- */

  _build() {
    if (this._built) return;
    this._built = true;
    this._titleEl = h("div", { class: "title" });
    this._sortBtn = h("button", { class: "hbtn", title: "Change sort order", onclick: () => this._toggleSort() });
    this._addBtn = h(
      "button",
      { class: "add", onclick: () => this._openDialog(null) },
      icon("mdi:plus"),
      h("span", null, "Add"),
    );
    this._filtersEl = h("div", { class: "filters", role: "toolbar", "aria-label": "Filter by person" });
    this._listEl = h("div", { class: "list" });
    this._card = h(
      "ha-card",
      null,
      h("div", { class: "header" }, this._titleEl, h("div", { class: "hactions" }, this._sortBtn, this._addBtn)),
      this._filtersEl,
      this._listEl,
    );
    this._dialogHost = h("div");
    this._toastEl = h("div", { class: "toast", role: "status", "aria-live": "polite" });
    this.shadowRoot.append(h("style", null, STYLES), this._card, this._dialogHost, this._toastEl);
  }

  /** Capabilities of the configured list (all native if unknown). */
  _caps() {
    const lists = (this._ftConfig && this._ftConfig.lists) || [];
    const l = lists.find((x) => x.entity_id === (this._config && this._config.entity));
    return Object.assign(
      { assignees: "native", priority: "native", recurrence: "native", description: "native", due_time: true },
      (l && l.capabilities) || {},
    );
  }

  _listName() {
    const lists = (this._ftConfig && this._ftConfig.lists) || [];
    const l = lists.find((x) => x.entity_id === this._config.entity);
    const st = this._hass && this._hass.states[this._config.entity];
    return (l && l.name) || (st && st.attributes && st.attributes.friendly_name) || "Family tasks";
  }

  _render() {
    if (!this._built || !this._config) return;
    const title = this._config.title !== undefined ? this._config.title : this._listName();
    this._titleEl.textContent = title;
    this._sortBtn.replaceChildren(
      icon(this._sort === "due" ? "mdi:calendar-clock" : "mdi:account-multiple"),
      h("span", null, this._sort === "due" ? "By date" : "By person"),
    );
    this._addBtn.disabled = !this._ftConfig;
    this._renderFilters();
    this._renderList();
  }

  _avatar(p, size) {
    const s = this._hass && this._hass.states[p.person];
    const pic = s && s.attributes && s.attributes.entity_picture;
    if (pic) return h("img", { class: `avatar ${size}`, src: pic, alt: "", loading: "lazy", draggable: "false" });
    const name = this._personName(p) || "?";
    return h("span", { class: `avatar ${size} initial`, "aria-hidden": "true" }, name.trim().charAt(0).toUpperCase());
  }

  _personChip(p) {
    return h(
      "span",
      { class: "pchip", style: { "--chip-color": safeColor(p.color) }, title: this._personName(p) },
      this._avatar(p, "sm"),
      h("span", { class: "pname" }, this._personName(p)),
    );
  }

  _renderFilters() {
    const counts = this._counts();
    const active = this._filterKey();
    const mk = (value, label, person) => {
      const key = String(value).toLowerCase();
      const on = active === key;
      return h(
        "button",
        {
          class: `fchip${on ? " active" : ""}${person ? " has-avatar" : ""}`,
          "aria-pressed": String(on),
          style: person ? { "--chip-color": safeColor(person.color) } : null,
          onclick: () => this._setFilter(value),
        },
        person ? this._avatar(person, "sm") : null,
        h("span", null, label),
        counts[key] ? h("span", { class: "count" }, String(counts[key])) : null,
      );
    };
    this._filtersEl.replaceChildren(
      mk("everyone", "Everyone"),
      ...this._people().map((p) => mk(p.tag, this._personName(p), p)),
      mk("unassigned", "Unassigned"),
    );
  }

  _renderList() {
    const el = this._listEl;
    if (!el) return;
    if (this._error) {
      el.replaceChildren(h("div", { class: "empty error" }, this._error));
      return;
    }
    if (!this._tasks) {
      el.replaceChildren(h("div", { class: "empty" }, "Loading…"));
      return;
    }
    const tasks = this._visibleTasks();
    if (!tasks.length) {
      const key = this._filterKey();
      el.replaceChildren(h("div", { class: "empty" }, key === "everyone" ? "Nothing to do 🎉" : "No tasks here"));
      return;
    }
    const byPerson = this._sort === "person" && this._filterKey() === "everyone";
    const rows = [];
    let lastGroup = null;
    for (const t of tasks) {
      let group = null;
      if (t.status === "completed") group = "Done";
      else if (byPerson) {
        const ps = this._taskPeople(t);
        group = ps.length ? this._personName(ps[0]) : "Unassigned";
      }
      if (group && group !== lastGroup) rows.push(h("div", { class: "group" }, group));
      lastGroup = group;
      rows.push(this._row(t));
    }
    el.replaceChildren(...rows);
  }

  _row(t) {
    const done = this._isDone(t);
    const pending = this._pending.has(t.uid);
    const lang = (this._hass && this._hass.locale && this._hass.locale.language) || undefined;
    const overdue = t.overdue && !done;
    const meta = [];
    const due = friendlyDue(t, lang);
    if (due) {
      meta.push(
        h(
          "span",
          { class: `due${overdue ? " overdue" : ""}` },
          icon(overdue ? "mdi:alert-circle-outline" : t.due_has_time ? "mdi:clock-outline" : "mdi:calendar-blank-outline"),
          due,
        ),
      );
    }
    if (t.rrule) meta.push(h("span", { title: describeRrule(t.rrule) }, icon("mdi:repeat")));
    if (t.priority === "high") meta.push(h("span", { class: "prio-high", title: "High priority" }, icon("mdi:flag")));
    if (t.has_alarm) meta.push(h("span", { title: "Has a reminder" }, icon("mdi:bell-outline")));
    if (t.description) meta.push(h("span", { title: "Has notes" }, icon("mdi:note-text-outline")));
    for (const tag of t.other_tags || []) meta.push(h("span", { class: "tag" }, tag));

    const check = h(
      "button",
      {
        class: `check${done ? " done" : ""}`,
        role: "checkbox",
        "aria-checked": String(done),
        "aria-label": done ? "Mark as not done" : "Mark as done",
        onclick: (e) => {
          e.stopPropagation();
          this._toggleDone(t);
        },
      },
      h("span", { class: "box" }, checkSvg()),
    );
    return h(
      "div",
      {
        class: `row${done ? " completed" : ""}${pending ? " pending" : ""}${t.parent_uid ? " sub" : ""}`,
        role: "button",
        tabindex: "0",
        onclick: () => this._openDialog(t),
        onkeydown: (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            this._openDialog(t);
          }
        },
      },
      check,
      h(
        "div",
        { class: "main" },
        h("div", { class: "summary" }, t.parent_uid ? "↳ " : "", t.summary || "(untitled)"),
        meta.length ? h("div", { class: "meta" }, meta) : null,
      ),
      h("div", { class: "chips" }, this._taskPeople(t).map((p) => this._personChip(p))),
    );
  }
}

/* ------------------------------------------------------------------- dialog */

class TaskDialog {
  constructor(card, task) {
    this.card = card;
    this.task = task;
    this.isEdit = !!task;
    // What the list's service supports (same contract for every service;
    // only the available controls differ). See docs/ARCHITECTURE.md §3.
    this.caps = card._caps();
    this.busy = false;
    const due = task ? dueParts(task) : { date: "", time: "" };
    const rr = parseRrule(task && task.rrule);
    this.orig = {
      summary: (task && task.summary) || "",
      assignees: task ? card._taskPeople(task).map((p) => p.person) : [],
      date: due.date,
      time: due.time,
      priority: (task && task.priority) || null,
      description: (task && task.description) || "",
      repeat: rr,
    };
    let initialAssignees = this.orig.assignees;
    if (!task) {
      const key = card._filterKey();
      const p = card._people().find((x) => String(x.tag).toLowerCase() === key);
      if (p) initialAssignees = [p.person];
    }
    this.state = {
      summary: this.orig.summary,
      assignees: new Set(initialAssignees),
      date: due.date,
      time: due.time,
      priority: this.orig.priority,
      description: this.orig.description,
      repeat: rr.mode,
      days: new Set(rr.days),
    };
    this._onKey = (e) => {
      if (e.key === "Escape") this.close();
    };
    this.build();
  }

  build() {
    const card = this.card;
    const s = this.state;
    const label = (text, key) =>
      this.caps[key] === "emulated" ? `${text} · Home Assistant only` : text;
    const supported = (key) => this.caps[key] !== "none";

    // Title
    this.titleInput = h("input", {
      class: "text",
      type: "text",
      value: s.summary,
      placeholder: "What needs doing?",
      enterkeyhint: "done",
      "aria-label": "Title",
      oninput: (e) => {
        s.summary = e.target.value;
      },
      onkeydown: (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          this.save();
        }
      },
    });

    // People
    const peopleBtns = card._people().map((p) => {
      const btn = h(
        "button",
        {
          class: `pbtn${s.assignees.has(p.person) ? " on" : ""}`,
          style: { "--chip-color": safeColor(p.color) },
          "aria-pressed": String(s.assignees.has(p.person)),
          onclick: () => {
            if (s.assignees.has(p.person)) s.assignees.delete(p.person);
            else s.assignees.add(p.person);
            const on = s.assignees.has(p.person);
            btn.classList.toggle("on", on);
            btn.setAttribute("aria-pressed", String(on));
          },
        },
        card._avatar(p, "lg"),
        h("span", null, card._personName(p)),
        h("ha-icon", { class: "tick", icon: "mdi:check" }),
      );
      return btn;
    });
    const peopleField = h(
      "div",
      null,
      h("span", { class: "flabel" }, label("Who", "assignees")),
      peopleBtns.length
        ? h("div", { class: "people" }, peopleBtns)
        : h("div", { class: "hint" }, "No people are set up in the Family Tasks options."),
    );

    // Due
    this.dateInput = h("input", {
      type: "date",
      value: s.date,
      "aria-label": "Due date",
      onchange: (e) => {
        s.date = e.target.value;
        this.syncDue();
      },
    });
    this.timeInput = h("input", {
      type: "time",
      value: s.time,
      "aria-label": "Due time (optional)",
      onchange: (e) => {
        s.time = e.target.value;
        this.syncDue();
      },
    });
    const setDate = (offset) => {
      const d = new Date();
      d.setDate(d.getDate() + offset);
      s.date = localDateStr(d);
      this.syncDue();
    };
    this.clearDueBtn = h(
      "button",
      {
        class: "btn small",
        onclick: () => {
          s.date = "";
          s.time = "";
          this.syncDue();
        },
      },
      "No due date",
    );
    this.clearTimeBtn = h(
      "button",
      {
        class: "btn small",
        onclick: () => {
          s.time = "";
          this.syncDue();
        },
      },
      "No time",
    );
    const dueField = h(
      "div",
      null,
      h("span", { class: "flabel" }, "Due"),
      h("div", { class: "duerow" }, this.dateInput, this.timeInput),
      h(
        "div",
        { class: "quick" },
        h("button", { class: "btn small", onclick: () => setDate(0) }, "Today"),
        h("button", { class: "btn small", onclick: () => setDate(1) }, "Tomorrow"),
        this.clearTimeBtn,
        this.clearDueBtn,
      ),
    );

    // Priority
    const prioField = h(
      "div",
      null,
      h("span", { class: "flabel" }, label("Priority", "priority")),
      this.segmented(
        [
          [null, "None"],
          ["low", "Low"],
          ["medium", "Medium"],
          ["high", "High"],
        ],
        () => s.priority,
        (v) => {
          s.priority = v;
        },
      ),
    );

    // Repeat
    const repeatOpts = [
      ["none", "None"],
      ["daily", "Daily"],
      ["weekly", "Weekly"],
      ["monthly", "Monthly"],
    ];
    if (this.orig.repeat.mode === "custom") repeatOpts.push(["custom", "Custom (keep)"]);
    this.dayBtns = DAYS.map((d, i) => {
      const b = h(
        "button",
        {
          class: `day${s.days.has(d) ? " on" : ""}`,
          "aria-pressed": String(s.days.has(d)),
          onclick: () => {
            if (s.days.has(d)) s.days.delete(d);
            else s.days.add(d);
            this.syncRepeat();
          },
        },
        DAY_LABELS[i],
      );
      b.dataset.day = d;
      return b;
    });
    this.daysRow = h("div", { class: "days" }, this.dayBtns);
    this.customNote = h("div", { class: "hint" });
    if (this.orig.repeat.mode === "custom") {
      this.customNote.textContent = `Current rule: ${this.task.rrule}`;
    }
    const repeatField = h(
      "div",
      null,
      h("span", { class: "flabel" }, label("Repeat", "recurrence")),
      this.segmented(
        repeatOpts,
        () => s.repeat,
        (v) => {
          s.repeat = v;
          if (v === "weekly" && s.days.size === 0) {
            const base = s.date ? new Date(`${s.date}T12:00:00`) : new Date();
            s.days.add(DAYS[(base.getDay() + 6) % 7]);
          }
          this.syncRepeat();
        },
      ),
      this.daysRow,
      this.customNote,
    );

    // Notes
    this.notesInput = h("textarea", {
      "aria-label": "Notes",
      placeholder: "Notes (optional)",
      oninput: (e) => {
        s.description = e.target.value;
      },
    });
    this.notesInput.value = s.description;
    const notesField = h("div", null, h("span", { class: "flabel" }, label("Notes", "description")), this.notesInput);

    // Other tags (read-only, reassure that they're kept)
    const otherTags = (this.task && this.task.other_tags) || [];
    const tagsNote = otherTags.length
      ? h("div", { class: "hint" }, `Other tags (kept as they are): ${otherTags.join(", ")}`)
      : null;

    this.errorEl = h("div", { class: "derror", role: "alert" });

    // Footer
    this.saveBtn = h("button", { class: "btn primary", onclick: () => this.save() }, this.isEdit ? "Save" : "Add task");
    this.cancelBtn = h("button", { class: "btn", onclick: () => this.close() }, "Cancel");
    this.deleteBtn = this.isEdit
      ? h("button", { class: "btn danger", onclick: () => this.deleteClicked() }, "Delete")
      : null;

    const dialog = h(
      "div",
      {
        class: "dialog",
        role: "dialog",
        "aria-modal": "true",
        "aria-label": this.isEdit ? "Edit task" : "Add task",
      },
      h("div", { class: "dhead" }, this.isEdit ? "Edit task" : "Add task"),
      h(
        "div",
        { class: "dbody" },
        this.titleInput,
        supported("assignees") ? peopleField : null,
        dueField,
        supported("recurrence") ? repeatField : null,
        supported("priority") ? prioField : null,
        supported("description") ? notesField : null,
        tagsNote,
        this.errorEl,
      ),
      h("div", { class: "dfoot" }, this.deleteBtn, h("span", { class: "spacer" }), this.cancelBtn, this.saveBtn),
    );
    this.el = h(
      "div",
      {
        class: "overlay",
        onclick: (e) => {
          if (e.target === this.el && !this.busy) this.close();
        },
      },
      dialog,
    );
    if (this.caps.due_time === false) {
      // service stores dates only
      this.timeInput.style.display = "none";
      this.clearTimeBtn.style.display = "none";
    }
    this.syncDue();
    this.syncRepeat();
  }

  segmented(options, get, set) {
    const wrap = h("div", { class: "segs", role: "radiogroup" });
    const refresh = () => {
      for (const b of wrap.children) {
        const on = b._value === get();
        b.classList.toggle("on", on);
        b.setAttribute("aria-checked", String(on));
      }
    };
    for (const [value, label] of options) {
      const b = h(
        "button",
        {
          class: "seg",
          role: "radio",
          onclick: () => {
            set(value);
            refresh();
          },
        },
        label,
      );
      b._value = value;
      wrap.append(b);
    }
    refresh();
    return wrap;
  }

  syncDue() {
    const s = this.state;
    if (this.dateInput.value !== s.date) this.dateInput.value = s.date;
    if (this.timeInput.value !== s.time) this.timeInput.value = s.time;
    this.timeInput.disabled = !s.date;
    this.clearTimeBtn.disabled = !s.time;
    this.clearDueBtn.disabled = !s.date && !s.time;
  }

  syncRepeat() {
    const s = this.state;
    this.daysRow.style.display = s.repeat === "weekly" ? "" : "none";
    this.customNote.style.display = s.repeat === "custom" ? "" : "none";
    for (const b of this.dayBtns) {
      const on = s.days.has(b.dataset.day);
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    }
  }

  opened() {
    document.addEventListener("keydown", this._onKey);
    if (!this.isEdit) setTimeout(() => this.titleInput.focus(), 50);
  }

  destroy() {
    document.removeEventListener("keydown", this._onKey);
    clearTimeout(this._deleteTimer);
  }

  close() {
    this.card._closeDialog();
  }

  showError(msg) {
    this.errorEl.textContent = msg || "";
    if (msg) this.errorEl.scrollIntoView({ block: "nearest" });
  }

  setBusy(busy) {
    this.busy = busy;
    this.saveBtn.disabled = busy;
    this.cancelBtn.disabled = busy;
    if (this.deleteBtn) this.deleteBtn.disabled = busy;
  }

  /** Called with every new snapshot while the dialog is open. */
  tasksUpdated(tasks) {
    if (!this.isEdit || this.busy) return;
    if (!tasks.some((t) => t.uid === this.task.uid)) {
      this.showError("This task was deleted or completed elsewhere. Saving may fail.");
    }
  }

  async run(service, data) {
    this.setBusy(true);
    this.showError("");
    try {
      await this.card._hass.callService(DOMAIN, service, data);
      this.close();
    } catch (err) {
      this.setBusy(false);
      this.showError(errMsg(err));
    }
  }

  deleteClicked() {
    if (this.busy) return;
    if (!this.deleteBtn.classList.contains("confirm")) {
      this.deleteBtn.classList.add("confirm");
      this.deleteBtn.textContent = "Tap again to delete";
      clearTimeout(this._deleteTimer);
      this._deleteTimer = setTimeout(() => {
        this.deleteBtn.classList.remove("confirm");
        this.deleteBtn.textContent = "Delete";
      }, 4000);
      return;
    }
    clearTimeout(this._deleteTimer);
    this.run("delete_task", { uid: this.task.uid });
  }

  save() {
    if (this.busy) return;
    const s = this.state;
    const summary = s.summary.trim();
    if (!summary) {
      this.showError("Give the task a title.");
      this.titleInput.focus();
      return;
    }
    if ((s.repeat === "daily" || s.repeat === "weekly" || s.repeat === "monthly") && !s.date) {
      this.showError("Repeating tasks need a due date.");
      return;
    }
    const dueData = () => {
      if (!s.date) return { due_date: null };
      if (s.time) return { due_datetime: `${s.date} ${s.time}:00` };
      return { due_date: s.date };
    };

    if (!this.isEdit) {
      const data = { list: this.card._config.entity, summary, assignees: [...s.assignees] };
      if (s.date) Object.assign(data, dueData());
      if (s.description.trim()) data.description = s.description;
      if (s.priority) data.priority = s.priority;
      const rr = buildRrule(s.repeat, [...s.days]);
      if (rr) data.rrule = rr;
      this.run("add_task", data);
      return;
    }

    const o = this.orig;
    const data = { uid: this.task.uid };
    if (summary !== o.summary) data.summary = summary;
    if (!sameSet(s.assignees, o.assignees)) data.assignees = [...s.assignees];
    if (s.date !== o.date || s.time !== o.time) Object.assign(data, dueData());
    if (s.description !== o.description) data.description = s.description;
    if (s.priority !== o.priority) data.priority = s.priority;
    if (s.repeat !== "custom") {
      const changed = s.repeat !== o.repeat.mode || (s.repeat === "weekly" && !sameSet(s.days, o.repeat.days));
      if (changed) data.rrule = buildRrule(s.repeat, [...s.days]);
    }
    if (Object.keys(data).length === 1) {
      this.close();
      return;
    }
    this.run("update_task", data);
  }
}

/* ------------------------------------------------------------------- editor */

class FamilyTasksCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
  }

  setConfig(config) {
    this._config = { ...config };
    this._sync();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._loading) return;
    this._loading = true;
    hass
      .callWS({ type: `${DOMAIN}/config` })
      .then((c) => {
        this._ft = c || { people: [], lists: [] };
        this._build();
      })
      .catch((err) => {
        this._ft = { people: [], lists: [], error: errMsg(err) };
        this._build();
      });
  }

  _change(key, value) {
    const c = { ...this._config };
    if (value === "" || value === undefined || value === null) delete c[key];
    else c[key] = value;
    this._config = c;
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: c }, bubbles: true, composed: true }));
  }

  _build() {
    const ft = this._ft;
    const style = h(
      "style",
      null,
      `
      .form { display: flex; flex-direction: column; gap: 16px; padding: 8px 0; }
      label { display: flex; flex-direction: column; gap: 6px; font-size: 0.9rem; color: var(--secondary-text-color); }
      label.inline { flex-direction: row; align-items: center; gap: 10px; color: var(--primary-text-color); font-size: 1rem; }
      select, input[type="text"] { font: inherit; font-size: 1rem; padding: 10px 12px; min-height: 44px;
        border-radius: 8px; border: 1px solid var(--divider-color); color: var(--primary-text-color);
        background: var(--secondary-background-color, transparent); color-scheme: light dark; }
      input[type="checkbox"] { width: 22px; height: 22px; }
      .err { color: var(--error-color); }
      .hint { color: var(--secondary-text-color); font-size: 0.85rem; }
    `,
    );
    const entityOpts = (ft.lists || []).map((l) => h("option", { value: l.entity_id }, `${l.name} (${l.entity_id})`));
    const current = this._config.entity;
    if (current && !(ft.lists || []).some((l) => l.entity_id === current)) {
      entityOpts.unshift(h("option", { value: current }, `${current} (not a Family Tasks list)`));
    }
    if (!current) entityOpts.unshift(h("option", { value: "" }, "Choose a list…"));
    this._entity = h("select", { onchange: (e) => this._change("entity", e.target.value) }, entityOpts);
    this._title = h("input", {
      type: "text",
      placeholder: "Defaults to the list name",
      onchange: (e) => this._change("title", e.target.value),
    });
    this._sort = h(
      "select",
      { onchange: (e) => this._change("sort", e.target.value === "due" ? "" : e.target.value) },
      h("option", { value: "due" }, "Due date"),
      h("option", { value: "person" }, "Person"),
    );
    this._filter = h(
      "select",
      { onchange: (e) => this._change("default_filter", e.target.value === "everyone" ? "" : e.target.value) },
      h("option", { value: "everyone" }, "Everyone"),
      (ft.people || []).map((p) => h("option", { value: p.tag }, p.name || p.tag)),
      h("option", { value: "unassigned" }, "Unassigned"),
    );
    this._showDone = h("input", {
      type: "checkbox",
      onchange: (e) => this._change("show_completed", e.target.checked ? "" : false),
    });
    this.shadowRoot.replaceChildren(
      style,
      h(
        "div",
        { class: "form" },
        ft.error ? h("div", { class: "err" }, `Couldn't load Family Tasks: ${ft.error}`) : null,
        h("label", null, "List", this._entity),
        h("label", null, "Title", this._title),
        h("label", null, "Sort by", this._sort),
        h("label", null, "Start with filter", this._filter),
        h("label", { class: "inline" }, this._showDone, "Show recently completed tasks"),
        h(
          "div",
          { class: "hint" },
          `How long completed tasks stay visible is set in the integration options (currently ${
            ft.completed_days !== undefined ? ft.completed_days : 7
          } days).`,
        ),
      ),
    );
    this._sync();
  }

  _sync() {
    if (!this._entity) return;
    const c = this._config;
    const root = this.shadowRoot;
    const set = (el, v) => {
      if (root.activeElement !== el && el.value !== v) el.value = v;
    };
    set(this._entity, c.entity || "");
    set(this._title, c.title || "");
    set(this._sort, c.sort || "due");
    set(this._filter, c.default_filter || "everyone");
    this._showDone.checked = c.show_completed !== false;
  }
}

/* ----------------------------------------------------------------- register */

if (!customElements.get("family-tasks-card")) {
  customElements.define("family-tasks-card", FamilyTasksCard);
}
if (!customElements.get("family-tasks-card-editor")) {
  customElements.define("family-tasks-card-editor", FamilyTasksCardEditor);
}
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "family-tasks-card")) {
  window.customCards.push({
    type: "family-tasks-card",
    name: "Family Tasks",
    description: "Shared family to-do list with assignees, built for touch screens.",
    preview: true,
  });
}
console.info(`%c FAMILY-TASKS-CARD %c v${CARD_VERSION} `, "background:#03a9f4;color:#fff;font-weight:600", "");
