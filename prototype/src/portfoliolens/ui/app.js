// PortfolioLens UI. No build step, no dependencies.
// Security note: every dynamic value is rendered through the escaping `html` template below.

/* ------------------------------------------------------------------ helpers */

const $ = (selector, root = document) => root.querySelector(selector);

const esc = (value) =>
  String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const raw = (value) => ({ __raw: String(value) });
const fmt = (value) => {
  if (value === null || value === undefined || value === false) return "";
  if (Array.isArray(value)) return value.map(fmt).join("");
  if (typeof value === "object" && "__raw" in value) return value.__raw;
  return esc(value);
};
const html = (strings, ...values) => raw(strings.reduce((out, s, i) => out + s + (i < values.length ? fmt(values[i]) : ""), ""));

const num = (x) => Number(x);
const pct = (x, digits = 2) => `${num(x).toFixed(digits)}%`;
const signed = (x, digits = 2) => `${num(x) < 0 ? "−" : "+"}${Math.abs(num(x)).toFixed(digits)}`;
const inr = (x) => new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(num(x));
const fmtDate = (iso) => new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { dateStyle: "medium" });
const fmtDateTime = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage unavailable */ } },
};

/* ------------------------------------------------------------------ vocabulary */

const CATEGORIES = [
  ["large_cap", "Large Cap"], ["large_mid_cap", "Large & Mid Cap"], ["mid_cap", "Mid Cap"], ["small_cap", "Small Cap"],
  ["multi_cap", "Multi Cap"], ["flexi_cap", "Flexi Cap"], ["elss", "ELSS"], ["focused", "Focused"],
  ["sectoral_thematic", "Sectoral / Thematic"], ["value", "Value"], ["contra", "Contra"], ["dividend_yield", "Dividend Yield"],
];
const METRICS = [
  ["large_cap_pct", "Large-cap %"], ["mid_cap_pct", "Mid-cap %"], ["small_cap_pct", "Small-cap %"], ["equity_pct", "Equity %"],
  ["sector_pct", "Sector %"], ["single_issuer_pct", "Single-issuer %"], ["stock_count", "Stock count"],
];
const METRIC_TITLE = {
  large_cap_pct: "Large-cap allocation", mid_cap_pct: "Mid-cap allocation", small_cap_pct: "Small-cap allocation",
  equity_pct: "Equity allocation", sector_pct: "Sector allocation", single_issuer_pct: "Largest single issuer",
  stock_count: "Number of stocks",
};
const COMPARATORS = [["min", "At least"], ["max", "At most"]];
const FUNDING = [["pro_rata", "Pro-rata trim of holdings"], ["cash", "Cash"]];
const STATUS = {
  PASS: ["ok", "Pass"], COMPLIANT: ["ok", "Compliant"], BREACH: ["risk", "Breach"],
  INDETERMINATE: ["warn", "Indeterminate"], NEEDS_REVIEW: ["warn", "Needs review"],
};
const METHOD = {
  isin_join: "ISIN match", name_match: "Name match", llm_identity: "Model match", unresolved: "Unresolved", not_equity: "Not equity",
};
const KIND = { compliance: "Compliance", validation: "Validation", question: "Scope guard", screen: "Screening", whatif: "What-if" };
const SUGGESTIONS = [
  "What is the large cap allocation?", "Which constraints are breached?", "How many issuers are held?",
  "Predict next month's drift",
];

const labelOf = (pairs, value) => (pairs.find(([key]) => key === value) || [value, value])[1];
const options = (pairs, current) =>
  pairs.map(([value, text]) => html`<option value="${value}" ${value === current ? raw("selected") : ""}>${text}</option>`);
const pill = (status, large = false) => {
  const [cls, text] = STATUS[status] || ["", status];
  return html`<span class="pill ${cls} ${large ? "lg" : ""}">${text}</span>`;
};

/* ------------------------------------------------------------------ state & API */

const freshFlow = () => ({ step: 1, summary: null, proposal: null, m: null, limits: [], ack: false });

const state = {
  samples: null,
  sampleNote: "",
  notice: null,
  flow: freshFlow(),
  form: {
    holdings: "", sid: "", asOf: "2026-09-30", sample: "", reviewer: store.get("pl.reviewer") || "",
    candidates: "", trades: "", weight: "3", screenFunding: "pro_rata", whatifFunding: "cash", sortBy: "headroom",
    decision: "approved", note: "", question: "",
  },
  run: null, runTab: "findings", acks: new Set(), qa: [], screen: null, whatif: null,
  backtest: undefined,
};

class ApiError extends Error {
  constructor(message, { status = 0, issues = null } = {}) {
    super(message);
    this.status = status;
    this.issues = issues;
  }
}

async function api(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError("Cannot reach the PortfolioLens server. Is it still running?");
  }
  const isJson = (response.headers.get("content-type") || "").includes("json");
  const data = isJson ? await response.json() : await response.text();
  if (!response.ok) throw toApiError(response.status, data);
  return data;
}

function toApiError(status, data) {
  if (data && typeof data === "object") {
    if (Array.isArray(data.detail)) {
      const message = data.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
      return new ApiError(message, { status });
    }
    if (data.message) return new ApiError(data.message, { status, issues: data.issues });
  }
  return new ApiError(`Request failed (${status})`, { status });
}

function errorNotice(error, title) {
  const issues = (error.issues || []).map((i) => `${i.row ? `Row ${i.row}` : "File"}${i.field ? ` · ${i.field}` : ""}: ${i.message}`);
  return { kind: "error", title, message: issues.length ? "" : error.message, lines: issues };
}

function setPath(target, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  const holder = keys.reduce((node, key) => node[key], target);
  holder[last] = value;
}

async function busy(button, text, task) {
  const original = button ? button.innerHTML : "";
  if (button) {
    button.disabled = true;
    button.innerHTML = `<span class="spinner" aria-hidden="true"></span>${esc(text)}`;
  }
  try {
    return await task();
  } finally {
    if (button && button.isConnected) {
      button.disabled = false;
      button.innerHTML = original;
    }
  }
}

/* ------------------------------------------------------------------ shared components */

const view = $("#view");
const show = (template) => { view.innerHTML = template.__raw; };

function noticeHtml(notice = state.notice) {
  if (!notice) return "";
  const lines = notice.lines && notice.lines.length ? html`<ul>${notice.lines.map((line) => html`<li>${line}</li>`)}</ul>` : "";
  return html`
    <div class="notice ${notice.kind}" role="${notice.kind === "error" ? "alert" : "status"}">
      <div class="notice-title">${notice.title}</div>
      ${notice.message ? html`<div>${notice.message}</div>` : ""}
      ${lines}
    </div>`;
}

const loading = (text = "Loading…") => html`<div class="loading"><span class="spinner" aria-hidden="true"></span>${text}</div>`;

function stepper(step) {
  const steps = ["Inputs", "Mandate", "Results"];
  return html`
    <ol class="stepper" style="list-style:none;padding:0" aria-label="Progress">
      ${steps.map((name, i) => {
        const n = i + 1;
        const cls = n < step ? "done" : n === step ? "active" : "";
        return html`${i ? html`<li class="step-sep" aria-hidden="true"></li>` : ""}
          <li class="step ${cls}" ${n === step ? raw('aria-current="step"') : ""}>
            <span class="step-dot">${n < step ? "✓" : n}</span>${name}
          </li>`;
      })}
    </ol>`;
}

function setActiveNav(name) {
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

/* ------------------------------------------------------------------ routing */

let navToken = 0;

async function route() {
  const token = ++navToken;
  const [name = "new", arg, sub] = (location.hash.replace(/^#\/?/, "") || "new").split("/");
  setActiveNav(name === "run" ? "runs" : name);
  state.notice = null;
  window.scrollTo(0, 0);
  try {
    if (name === "runs") await renderRuns(token);
    else if (name === "run" && arg) await renderRun(arg, token, sub);
    else if (name === "backtest") await renderBacktest(token);
    else renderNew();
  } catch (error) {
    if (token === navToken) renderFatal(error);
  }
  view.focus({ preventScroll: true });
}

function renderFatal(error) {
  const missing = error instanceof ApiError && error.status === 404;
  show(html`
    <div class="card empty">
      <h2>${missing ? "Not found" : "Something went wrong"}</h2>
      <p>${error.message}</p>
      <a class="btn primary" href="#/new">Start a new check</a>
    </div>`);
}

/* ------------------------------------------------------------------ view: new check */

async function ensureSamples() {
  if (state.samples) return;
  try { state.samples = await api("/samples"); } catch { state.samples = []; }
}

function renderNew() {
  show(html`
    <header class="page-head">
      <div>
        <h1>New compliance check</h1>
        <p class="muted">Upload month-end holdings and the Scheme Information Document. The mandate is extracted for your confirmation before any number is computed.</p>
      </div>
    </header>
    ${stepper(state.flow.step)}
    ${noticeHtml()}
    ${state.flow.step === 1 ? stepInputs() : stepMandate()}`);
  if (state.flow.step === 1 && !state.samples) ensureSamples().then(() => { if (!location.hash || location.hash === "#/new") renderNew(); });
}

function stepInputs() {
  const f = state.form;
  return html`
    <div class="grid cols-main-side">
      <section class="card">
        <div class="field">
          <div class="field-head">
            <label for="holdings">Holdings</label>
            <label class="link">Upload CSV<input class="sr-only" type="file" accept=".csv,.txt" data-action="upload" data-target="holdings"></label>
          </div>
          <textarea id="holdings" class="mono" rows="11" spellcheck="false" data-bind="form.holdings"
            placeholder="isin,name,instrument_type,market_value,pct_of_nav">${raw("\n")}${f.holdings}</textarea>
          <p class="hint">Columns: isin, name, instrument_type (equity, cash, debt or other), market_value, pct_of_nav. Net assets are the sum of market values.</p>
        </div>
        <div class="field">
          <div class="field-head">
            <label for="sid">Scheme Information Document</label>
            <label class="link">Upload text<input class="sr-only" type="file" accept=".md,.txt" data-action="upload" data-target="sid"></label>
          </div>
          <textarea id="sid" rows="9" spellcheck="false" data-bind="form.sid"
            placeholder="Paste the SID extract: the scheme type, benchmark and any investment limits.">${raw("\n")}${f.sid}</textarea>
          <p class="hint">Plain text or Markdown. Every extracted value must quote this text word for word.</p>
        </div>
      </section>

      <aside class="stack">
        <section class="card">
          <div class="field">
            <label for="as-of">As-of date</label>
            <input id="as-of" type="date" data-bind="form.asOf" value="${f.asOf}">
            <p class="hint">Selects the SEBI rulebook and market-cap list in force on this date.</p>
          </div>
          <div class="field">
            <label for="sample">Example</label>
            <select id="sample" data-action="load-sample">
              <option value="">Choose an example…</option>
              ${(state.samples || []).map((s) => html`<option value="${s.name}" ${f.sample === s.name ? raw("selected") : ""}>${s.case_id} · ${s.title}</option>`)}
            </select>
            <p class="hint">${state.sampleNote || "Synthetic cases from the backtest. Loads holdings, SID and date."}</p>
          </div>
          <hr class="divider">
          <button class="btn primary" style="width:100%" data-action="continue">Extract mandate</button>
        </section>
        <section class="card subtle">
          <h3>Three human gates</h3>
          <ol class="small muted" style="margin:8px 0 0;padding-left:18px">
            <li>You confirm the mandate before it is used.</li>
            <li>You acknowledge every flagged item.</li>
            <li>You sign off. Nothing is filed automatically.</li>
          </ol>
        </section>
      </aside>
    </div>`;
}

function evidence(citation) {
  if (!citation) return "";
  return html`<blockquote class="evidence">“${citation.quote}” <span class="mono">SID chars ${citation.start}–${citation.end}</span></blockquote>`;
}

function stepMandate() {
  const { proposal: p, m, limits, summary } = state.flow;
  return html`
    <div class="grid cols-main-side">
      <section class="card">
        <div class="card-head">
          <div>
            <h2>Proposed mandate</h2>
            <div class="sub">Extracted by ${p.extractor}. Correct anything that is wrong; nothing is computed until you confirm.</div>
          </div>
          <span class="pill warn plain">Gate 1</span>
        </div>
        ${p.issues.length ? html`
          <div class="notice warn" role="status">
            <div class="notice-title">Needs your attention</div>
            <ul>${p.issues.map((i) => html`<li>${i}</li>`)}</ul>
          </div>` : ""}
        <div class="form-grid">
          <div class="field"><label for="m-name">Scheme name</label>
            <input id="m-name" type="text" data-bind="flow.m.scheme_name" value="${m.scheme_name}"></div>
          <div class="field"><label for="m-cat">Category</label>
            <select id="m-cat" data-bind="flow.m.category">
              <option value="">Choose a category…</option>${options(CATEGORIES, m.category)}
            </select></div>
          <div class="field"><label for="m-bench">Benchmark</label>
            <input id="m-bench" type="text" data-bind="flow.m.benchmark" value="${m.benchmark}"></div>
          <div class="field"><label for="m-sector">Sector</label>
            <input id="m-sector" type="text" data-bind="flow.m.sector" value="${m.sector}">
            <p class="hint">Only for sectoral or thematic schemes. Use the market-cap list's sector name.</p></div>
        </div>
        ${p.category_citation ? html`<div class="field"><span class="label">Category evidence</span>${evidence(p.category_citation)}</div>` : ""}

        <hr class="divider">
        <div class="card-head">
          <div>
            <h3>Self-imposed limits</h3>
            <div class="sub">Numeric limits the SID states beyond SEBI's rules. Where both apply, the stricter one binds.</div>
          </div>
          <button class="btn sm" data-action="add-limit">Add limit</button>
        </div>
        ${limits.length ? limits.map((l, i) => limitRow(l, i)) : html`<p class="muted">None found in the SID.</p>`}
      </section>

      <aside class="stack">
        <section class="card">
          <div class="field">
            <label for="reviewer">Reviewer</label>
            <input id="reviewer" type="text" autocomplete="name" data-bind="form.reviewer" value="${state.form.reviewer}">
            <p class="hint">Recorded in the audit trail with your confirmation.</p>
          </div>
          ${p.issues.length ? html`
            <div class="check" style="margin-top:16px">
              <input id="ack" type="checkbox" data-bind="flow.ack" ${state.flow.ack ? raw("checked") : ""}>
              <label for="ack">I have reviewed the issues above. Not needed if you correct the mandate.</label>
            </div>` : ""}
          <hr class="divider">
          <div class="stack">
            <button class="btn primary" data-action="confirm">Confirm mandate and run check</button>
            <button class="btn ghost" data-action="back">Back to inputs</button>
          </div>
        </section>
        <section class="card subtle">
          <h3>Inputs</h3>
          <p class="small muted" style="margin-top:6px">${summary.rows} positions · net assets <span class="tnum">${inr(summary.net_assets)}</span></p>
          <p class="mono muted" style="margin-top:4px" title="SHA-256 of the holdings file">${summary.holdings_sha256.slice(0, 16)}…</p>
        </section>
      </aside>
    </div>`;
}

function limitRow(limit, i) {
  return html`
    <div class="limit-row">
      <div class="field"><label for="lm-${i}">Metric</label>
        <select id="lm-${i}" data-bind="flow.limits.${i}.metric">${options(METRICS, limit.metric)}</select></div>
      <div class="field"><label for="lc-${i}">Direction</label>
        <select id="lc-${i}" data-bind="flow.limits.${i}.comparator">${options(COMPARATORS, limit.comparator)}</select></div>
      <div class="field"><label for="lt-${i}">Threshold</label>
        <input id="lt-${i}" type="number" step="any" min="0" data-bind="flow.limits.${i}.threshold" value="${limit.threshold}"></div>
      <button class="btn ghost sm" data-action="remove-limit" data-index="${i}">Remove</button>
      <div class="evidence-wrap">${limit.citation ? evidence(limit.citation) : html`<p class="hint">Entered by you. No SID quote is attached.</p>`}</div>
    </div>`;
}

/* ------------------------------------------------------------------ view: run */

const RUN_TABS = ["findings", "review", "report", "ask", "explore"];

async function renderRun(id, token, sub) {
  show(loading("Loading run…"));
  const run = await api(`/checks/runs/${encodeURIComponent(id)}`);
  if (token !== navToken) return;
  if (!state.run || state.run.run_id !== id) {
    Object.assign(state, { runTab: "findings", acks: new Set(), qa: [], screen: null, whatif: null });
  }
  if (sub && RUN_TABS.includes(sub)) state.runTab = sub;
  state.run = run;
  drawRun();
}

const REVIEW_TAB_COUNT = (run) => (run.sign_off ? 0 : run.result.review_items.length);

function drawRun() {
  const r = state.run;
  const m = r.mandate.mandate;
  const facts = Object.fromEntries(r.result.facts.map((f) => [f.fact_id, f]));
  const fact = (id) => num(facts[id] ? facts[id].value : 0);
  const largest = facts["F-MAX-ISSUER-PCT"];
  const unclassified = fact("F-UNCLASSIFIED-EQUITY-PCT");
  const segments = [
    ["Large cap", fact("F-LARGE-CAP-PCT"), "#0f172a"], ["Mid cap", fact("F-MID-CAP-PCT"), "#475569"],
    ["Small cap", fact("F-SMALL-CAP-PCT"), "#94a3b8"], ["Unclassified equity", unclassified, "#f59e0b"],
    ["Cash and debt", Math.max(0, 100 - fact("F-EQUITY-PCT")), "#e2e8f0"],
  ];
  const pins = r.pins.map((p) => {
    const name = { rulebook: "Rulebook", security_master: "Market-cap list", engine: "Engine" }[p.kind] || p.kind;
    return html`<span class="chip" title="${p.sha256 ? `SHA-256 ${p.sha256}` : "Engine version"}">${name}<b class="mono">${p.version}</b></span>`;
  });

  show(html`
    <a class="back" href="#/runs">← Runs</a>
    <header class="page-head">
      <div>
        <h1>${m.scheme_name}</h1>
        <div class="meta">
          <span class="chip">${labelOf(CATEGORIES, m.category)}${m.sector ? ` · ${m.sector}` : ""}</span>
          <span class="chip">As of<b>${fmtDate(r.as_of)}</b></span>
          <span class="chip">Run<b class="mono">${r.run_id}</b></span>
          ${r.sign_off ? html`<span class="pill ${r.sign_off.decision === "approved" ? "ok" : "warn"}">${r.sign_off.decision === "approved" ? "Signed off" : "Escalated"} by ${r.sign_off.reviewer}</span>` : ""}
        </div>
        <div class="meta">${pins}<span class="chip">Mandate confirmed by<b>${r.mandate.confirmed_by}</b></span></div>
      </div>
      <div>${pill(r.result.status, true)}</div>
    </header>

    <div class="grid cols-4" style="margin-bottom:16px">
      <div class="card stat"><div class="stat-label">Net assets</div><div class="stat-value tnum">${inr(fact("F-NAV"))}</div><div class="stat-note">INR · ${r.holdings.length} positions</div></div>
      <div class="card stat"><div class="stat-label">Equity allocation</div><div class="stat-value tnum">${pct(fact("F-EQUITY-PCT"))}</div><div class="stat-note">${fact("F-STOCK-COUNT")} issuers</div></div>
      <div class="card stat"><div class="stat-label">Largest single issuer</div><div class="stat-value tnum">${pct(fact("F-MAX-ISSUER-PCT"))}</div><div class="stat-note" title="${largest ? largest.detail : ""}">${largest ? largest.detail : "-"}</div></div>
      <div class="card stat"><div class="stat-label">Unclassified equity</div><div class="stat-value tnum ${unclassified > 0 ? "margin-warn" : ""}">${pct(unclassified)}</div><div class="stat-note">${unclassified > 0 ? "Excluded from every cap bucket" : "All equity matched"}</div></div>
    </div>

    <section class="card" style="margin-bottom:24px">
      <div class="card-head"><div><h2>Portfolio mix</h2><div class="sub">Share of net assets by market-cap bucket</div></div></div>
      <div class="mix-bar" role="img" aria-label="${segments.map(([n, v]) => `${n} ${v.toFixed(2)} percent`).join(", ")}">
        ${segments.filter(([, v]) => v > 0).map(([n, v, c]) => html`<div class="mix-seg" style="width:${v}%;background:${c}" title="${n}: ${pct(v)}"></div>`)}
      </div>
      <div class="legend">${segments.map(([n, v, c]) => html`<span class="legend-item"><i style="background:${c}"></i>${n} <b class="tnum">${pct(v)}</b></span>`)}</div>
    </section>

    <div class="tabs" role="tablist" aria-label="Run sections" id="tabs">${tabButtons()}</div>
    <div id="notice-slot">${noticeHtml()}</div>
    <div id="tab-body">${tabBody()}</div>`);
}

function tabButtons() {
  const tabs = [["findings", "Findings"], ["review", "Review and sign-off"], ["report", "Report"], ["ask", "Ask"], ["explore", "Explore changes"]];
  const count = REVIEW_TAB_COUNT(state.run);
  return tabs.map(([id, text]) => html`
    <button class="tab" role="tab" data-action="tab" data-tab="${id}" aria-selected="${state.runTab === id}">
      ${text}${id === "review" && count ? html`<span class="count">${count}</span>` : ""}
    </button>`);
}

function drawTab() {
  $("#tabs").innerHTML = tabButtons().map((t) => t.__raw).join("");
  $("#notice-slot").innerHTML = noticeHtml().__raw;
  $("#tab-body").innerHTML = tabBody().__raw;
}

function tabBody() {
  const r = state.run;
  switch (state.runTab) {
    case "review": return reviewTab(r);
    case "report": return reportTab(r);
    case "ask": return askTab();
    case "explore": return exploreTab();
    default: return findingsTab(r);
  }
}

/* ---- findings */

function findingsTab(r) {
  const findings = r.result.findings;
  return html`
    <div class="card flush">${findings.map((f) => findingHtml(f, findings))}</div>

    <details class="card flush audit" style="margin-top:16px">
      <summary>Computed facts <span class="muted small">· every number in the report maps to one of these ids</span></summary>
      <div class="table-wrap"><table>
        <thead><tr><th>Fact</th><th>Description</th><th class="r">Value</th><th>Detail</th></tr></thead>
        <tbody>${r.result.facts.map((f) => html`<tr>
          <td class="mono">${f.fact_id}</td><td>${f.label}</td>
          <td class="r tnum">${f.unit === "pct" ? pct(f.value) : f.unit === "inr" ? inr(f.value) : num(f.value)}</td>
          <td class="muted">${f.detail}</td></tr>`)}</tbody>
      </table></div>
    </details>

    <details class="card flush audit" style="margin-top:16px">
      <summary>Holding classification <span class="muted small">· how each row was bucketed</span></summary>
      <div class="table-wrap"><table>
        <thead><tr><th>Row</th><th>Holding</th><th>Method</th><th>Bucket</th><th>Sector</th><th class="r">Confidence</th></tr></thead>
        <tbody>${r.classification.map((c) => html`<tr>
          <td class="tnum">${c.holding.row}</td>
          <td>${c.holding.name}<div class="mono muted">${c.resolved_isin || c.holding.isin || "no ISIN"}</div>${c.note ? html`<div class="small muted">${c.note}</div>` : ""}</td>
          <td>${c.method === "unresolved" ? html`<span class="pill warn">Unresolved</span>` : METHOD[c.method]}</td>
          <td>${c.bucket || "-"}</td><td>${c.sector || "-"}</td>
          <td class="r tnum">${c.method === "isin_join" || c.method === "not_equity" ? "-" : num(c.confidence).toFixed(2)}</td></tr>`)}</tbody>
      </table></div>
    </details>`;
}

function findingHtml(f, all) {
  const c = f.constraint;
  const isPct = c.metric !== "stock_count";
  const unit = isPct ? "%" : "";
  const low = num(f.value_low);
  const high = num(f.value_high);
  const margin = num(f.margin);
  const sibling = all.some((o) => o !== f && o.constraint.metric === c.metric && o.constraint.comparator === c.comparator && (o.constraint.sector || "") === (c.sector || ""));
  const valueText = f.status === "INDETERMINATE" ? `${low.toFixed(2)}–${high.toFixed(2)}${unit}` : isPct ? pct(low) : String(low);
  const marginClass = f.status === "PASS" ? "margin-pos" : f.status === "BREACH" ? "margin-neg" : "margin-warn";
  return html`
    <article class="finding">
      <div class="finding-top">
        <div>
          <div class="finding-title">
            ${pill(f.status)}<h3>${METRIC_TITLE[c.metric]}${c.sector ? ` · ${c.sector}` : ""}</h3>
            <span class="chip">${c.source_kind === "SID" ? "SID limit" : "SEBI"}</span>
            ${sibling ? html`<span class="chip">${f.binding ? "Stricter, binds" : "Superseded"}</span>` : ""}
          </div>
          <p class="finding-text">${c.text}</p>
        </div>
        <div class="kv" style="text-align:right">
          <div class="v tnum ${marginClass}">${signed(margin)}${isPct ? " pp" : ""}</div>
          <div class="k">margin to limit</div>
        </div>
      </div>
      ${isPct ? meter(f, c, low, high) : ""}
      <div class="finding-numbers">
        <div class="kv"><div class="k">Value</div><div class="v tnum">${valueText}</div></div>
        <div class="kv"><div class="k">Limit</div><div class="v tnum">${c.comparator === "min" ? "≥" : "≤"} ${num(c.threshold)}${unit}</div></div>
        <div class="kv"><div class="k">Source</div><div class="mono" style="margin-top:4px">${c.source_ref}</div></div>
      </div>
    </article>`;
}

function meter(f, c, low, high) {
  const threshold = num(c.threshold);
  const top = c.comparator === "min" ? 100 : Math.min(100, Math.ceil((Math.max(threshold, high) * 1.4) / 5) * 5);
  const at = (v) => Math.min(100, Math.max(0, (v / top) * 100));
  const cls = f.status === "BREACH" ? "risk" : f.status === "INDETERMINATE" ? "warn" : "";
  return html`
    <div class="meter" role="img" aria-label="${low.toFixed(2)} percent against a limit of ${threshold} percent">
      <div class="meter-track">
        ${f.status === "INDETERMINATE" ? html`<div class="meter-range" style="left:${at(low)}%;width:${at(high) - at(low)}%"></div>` : ""}
        <div class="meter-fill ${cls}" style="width:${at(low)}%"></div>
        <div class="meter-tick" style="left:calc(${at(threshold)}% - 1px)" title="Limit ${threshold}%"></div>
      </div>
      <div class="meter-scale"><span>0%</span><span>${top}%</span></div>
    </div>`;
}

/* ---- review & sign-off */

function reviewTab(r) {
  const items = r.result.review_items;
  const signed = r.sign_off;
  const list = items.length
    ? items.map((item) => html`
        <div class="review-item">
          ${signed ? "" : html`<input type="checkbox" id="ack-${item.review_id}" data-ack="${item.review_id}" ${state.acks.has(item.review_id) ? raw("checked") : ""}>`}
          <div class="body">
            <label for="ack-${item.review_id}"><span class="mono">${item.review_id}</span> <span class="chip">${item.kind.replaceAll("_", " ")}</span></label>
            <p style="margin-top:4px">${item.detail}</p>
          </div>
        </div>`)
    : html`<p class="muted" style="padding:0 20px 20px">Nothing was flagged for review.</p>`;

  const side = signed
    ? html`<div class="notice ok">
        <div class="notice-title">${signed.decision === "approved" ? "Signed off" : "Escalated"} by ${signed.reviewer}</div>
        <div class="small">${fmtDateTime(signed.signed_at)}</div>
        ${signed.note ? html`<div>${signed.note}</div>` : ""}
        <div class="small muted">Acknowledged ${signed.acknowledged_review_ids.length} item${signed.acknowledged_review_ids.length === 1 ? "" : "s"}.</div>
      </div>`
    : html`
      <h2>Sign-off</h2><p class="muted small" style="margin:4px 0 16px">Gate 3 · attributable and final for this run.</p>
      <div class="field"><label for="so-reviewer">Reviewer</label>
        <input id="so-reviewer" type="text" data-bind="form.reviewer" value="${state.form.reviewer}"></div>
      <div class="field"><label for="so-decision">Decision</label>
        <select id="so-decision" data-bind="form.decision">${options([["approved", "Approved"], ["escalated", "Escalated for further review"]], state.form.decision)}</select></div>
      <div class="field"><label for="so-note">Note <span class="muted">(optional)</span></label>
        <textarea id="so-note" rows="3" data-bind="form.note">${raw("\n")}${state.form.note}</textarea></div>
      <hr class="divider">
      <button class="btn primary" style="width:100%" data-action="sign">Sign off</button>
      ${items.length ? html`<p class="hint" style="margin-top:8px">${state.acks.size} of ${items.length} items acknowledged.</p>` : ""}`;

  return html`
    <div class="grid cols-main-side">
      <section class="card flush">
        <div style="padding:20px 20px 12px">
          <div class="card-head" style="margin-bottom:6px"><h2>Items for review</h2>
            ${signed || !items.length ? "" : html`<span class="pill warn plain">Gate 2</span>`}</div>
          <p class="muted">${signed ? "These items were acknowledged at sign-off." : "Acknowledge each item once you have examined it. Sign-off stays blocked until all are acknowledged."}</p>
          ${signed || !items.length ? "" : html`<button class="btn sm" style="margin-top:12px" data-action="ack-all">Acknowledge all</button>`}
        </div>
        ${list}
      </section>
      <aside class="card">${side}</aside>
    </div>`;
}

/* ---- report */

function reportTab(r) {
  return html`
    ${r.report.llm_note ? html`<div class="notice warn"><div class="notice-title">Model narrative</div><div>${r.report.llm_note}</div></div>` : ""}
    <section class="card">
      <div class="card-head">
        <div><h2>Exception report</h2><div class="sub">${r.report.narrative_source === "llm" ? "Model narrative, citation-checked against the facts." : "Template report: every statement is computed by the deterministic engine."}</div></div>
        <div class="btn-row">
          <button class="btn sm" data-action="copy-report">Copy</button>
          <button class="btn sm" data-action="download-report">Download .md</button>
        </div>
      </div>
      <div class="prose">${raw(renderMarkdown(r.report.markdown))}</div>
    </section>`;
}

function renderMarkdown(source) {
  const inline = (text) =>
    esc(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/(^|[\s(])_([^_]+)_(?=$|[\s).,;:])/g, "$1<em>$2</em>");
  const cells = (line) => line.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.replace(/\\\|/g, "|").trim());
  const lines = source.split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    let match;
    if (/^\s*$/.test(line)) { i++; continue; }
    if ((match = line.match(/^(#{1,3})\s+(.*)$/))) { out.push(`<h${match[1].length}>${inline(match[2])}</h${match[1].length}>`); i++; continue; }
    if (/^---+\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (line.startsWith(">")) {
      const buffer = [];
      while (i < lines.length && lines[i].startsWith(">")) buffer.push(lines[i++].replace(/^>\s?/, ""));
      out.push(`<blockquote>${inline(buffer.join(" "))}</blockquote>`);
      continue;
    }
    if (line.trim().startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(lines[i++]);
      const [head, , ...body] = rows;
      out.push(`<div class="table-wrap"><table><thead><tr>${cells(head).map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${
        body.map((row) => `<tr>${cells(row).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    if (/^\s*-\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*-\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*-\s+/, ""));
      out.push(`<ul>${items.map((t) => `<li>${inline(t)}</li>`).join("")}</ul>`);
      continue;
    }
    const paragraph = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,3}\s|---+\s*$|>|\||\s*-\s+)/.test(lines[i])) paragraph.push(lines[i++]);
    out.push(`<p>${inline(paragraph.join(" "))}</p>`);
  }
  return out.join("");
}

/* ---- ask */

function askTab() {
  return html`
    <section class="card">
      <div class="card-head"><div><h2>Ask about this run</h2><div class="sub">Answers use only the computed facts. Forecasts and recommendations are declined.</div></div></div>
      <label class="sr-only" for="question">Question</label>
      <div class="btn-row" style="flex-wrap:nowrap">
        <input id="question" type="text" data-bind="form.question" data-enter="ask" value="${state.form.question}" placeholder="e.g. What is the large cap allocation?">
        <button class="btn primary" data-action="ask">Ask</button>
      </div>
      <div class="suggestions">${SUGGESTIONS.map((q) => html`<button class="btn sm" data-action="suggest" data-q="${q}">${q}</button>`)}</div>
      <div class="qa">${state.qa.map((item) => html`
        <div>
          <div class="qa-q">${item.q}</div>
          <div class="qa-a">${item.a.text}</div>
          <div class="meta" style="margin-top:8px">
            ${item.a.declined ? html`<span class="pill warn">Declined</span>` : ""}
            <span class="chip">${item.a.source === "llm" ? "Model, citation-checked" : item.a.source === "scope_guard" ? "Scope guard" : "From computed facts"}</span>
            ${item.a.cited_ids.map((id) => html`<span class="chip mono">${id}</span>`)}
          </div>
        </div>`)}</div>
    </section>`;
}

/* ---- explore changes (P1) */

function exploreTab() {
  const f = state.form;
  return html`
    <div class="stack">
      <section class="card">
        <div class="card-head">
          <div><h2>Screen candidates</h2><div class="sub">Which securities can the fund buy without creating or deepening a breach? Eligibility only, never a forecast.</div></div>
          <button class="btn sm" data-action="example-pool">Use example pool</button>
        </div>
        <div class="grid cols-2">
          <div class="field"><label for="candidates">Candidates (CSV)</label>
            <textarea id="candidates" class="mono" rows="7" spellcheck="false" data-bind="form.candidates" placeholder="isin,name">${raw("\n")}${f.candidates}</textarea></div>
          <div>
            <div class="form-grid">
              <div class="field"><label for="weight">Purchase size (% of NAV)</label><input id="weight" type="number" min="0.01" max="100" step="any" data-bind="form.weight" value="${f.weight}"></div>
              <div class="field"><label for="sfund">Funded by</label><select id="sfund" data-bind="form.screenFunding">${options(FUNDING, f.screenFunding)}</select></div>
              <div class="field"><label for="sort">Order by</label><select id="sort" data-bind="form.sortBy">${options([["headroom", "Headroom after purchase"], ["name", "Name"]], f.sortBy)}</select></div>
            </div>
            <button class="btn primary" style="margin-top:16px" data-action="screen">Screen candidates</button>
          </div>
        </div>
        ${state.screen ? screenResult(state.screen) : ""}
      </section>

      <section class="card">
        <div class="card-head">
          <div><h2>What-if rebalance</h2><div class="sub">Recompute compliance for trades you propose. Nothing is executed.</div></div>
          <button class="btn sm" data-action="example-trades">Use example trades</button>
        </div>
        <div class="grid cols-2">
          <div class="field"><label for="trades">Trades (CSV)</label>
            <textarea id="trades" class="mono" rows="5" spellcheck="false" data-bind="form.trades" placeholder="action,isin,name,pct_of_nav">${raw("\n")}${f.trades}</textarea>
            <p class="hint">action is buy or sell; pct_of_nav is the size as a share of net assets.</p></div>
          <div>
            <div class="field"><label for="wfund">Buys funded by</label><select id="wfund" data-bind="form.whatifFunding">${options(FUNDING.slice().reverse(), f.whatifFunding)}</select></div>
            <button class="btn primary" style="margin-top:16px" data-action="whatif">Simulate</button>
          </div>
        </div>
        ${state.whatif ? whatifResult(state.whatif) : ""}
      </section>
    </div>`;
}

function screenResult(s) {
  const eligible = s.candidates.filter((c) => c.eligible).length;
  return html`
    <hr class="divider">
    <p><b>${eligible} of ${s.candidates.length}</b> eligible for a purchase of <span class="tnum">${pct(s.weight_pct)}</span> of NAV, funded by ${labelOf(FUNDING, s.funding).toLowerCase()}.</p>
    <div class="card flush" style="margin-top:12px"><div class="table-wrap"><table>
      <thead><tr><th>Candidate</th><th>Cap bucket</th><th>Result</th><th class="r">Headroom after</th><th>Why</th></tr></thead>
      <tbody>${s.candidates.map((c) => {
        const unclassified = c.blocking_constraint_ids.includes("UNCLASSIFIED");
        return html`<tr>
          <td>${c.name}<div class="mono muted">${c.isin || "no ISIN"}</div></td>
          <td>${c.bucket || "-"}</td>
          <td>${c.eligible ? html`<span class="pill ok">Eligible</span>` : unclassified ? html`<span class="pill warn">Needs identification</span>` : html`<span class="pill">Excluded</span>`}</td>
          <td class="r tnum ${c.post_trade_headroom_pp === null ? "" : num(c.post_trade_headroom_pp) < 0 ? "margin-neg" : "margin-pos"}">${c.post_trade_headroom_pp === null ? "-" : `${signed(c.post_trade_headroom_pp)} pp`}</td>
          <td class="${c.eligible ? "muted" : ""}">${c.reasons.map((reason) => html`<div>${reason}</div>`)}</td></tr>`;
      })}</tbody>
    </table></div></div>
    <p class="hint" style="margin-top:8px">${s.note}</p>`;
}

function whatifResult(w) {
  return html`
    <hr class="divider">
    <div class="meta" style="margin-top:0"><b>Run status</b> ${pill(w.before_status)} <span class="muted">→</span> ${pill(w.after_status)}</div>
    <div class="card flush" style="margin-top:12px"><div class="table-wrap"><table>
      <thead><tr><th>Constraint</th><th>Before</th><th>After</th><th>Change</th></tr></thead>
      <tbody>${w.deltas.map((d) => {
        const better = num(d.after_margin) > num(d.before_margin);
        return html`<tr>
          <td class="mono">${d.constraint_id}</td>
          <td>${pill(d.before_status)} <span class="tnum muted">${signed(d.before_margin)} pp</span></td>
          <td>${pill(d.after_status)} <span class="tnum muted">${signed(d.after_margin)} pp</span></td>
          <td class="${d.worsened ? "margin-neg" : better ? "margin-pos" : "muted"}">${d.worsened ? "Worse" : better ? "Better" : "No change"}</td></tr>`;
      })}</tbody>
    </table></div></div>
    <div class="meta">
      <span class="chip">New breaches<b>${w.new_breaches.length ? w.new_breaches.join(", ") : "none"}</b></span>
      <span class="chip">Resolved<b>${w.resolved_breaches.length ? w.resolved_breaches.join(", ") : "none"}</b></span>
    </div>
    <p class="hint" style="margin-top:8px">${w.note}</p>`;
}

/* ------------------------------------------------------------------ view: runs */

async function renderRuns(token) {
  show(html`<header class="page-head"><div><h1>Runs</h1></div></header>${loading()}`);
  const runs = await api("/checks/runs");
  if (token !== navToken) return;
  show(html`
    <header class="page-head">
      <div><h1>Runs</h1><p class="muted">Every check is stored with its pinned versions, citations and sign-off, so it can be re-derived later.</p></div>
      <a class="btn primary" href="#/new">New check</a>
    </header>
    ${runs.length ? html`
      <div class="card flush"><div class="table-wrap"><table>
        <thead><tr><th>Scheme</th><th>Category</th><th>As of</th><th>Status</th><th>Sign-off</th><th>Created</th></tr></thead>
        <tbody>${runs.map((r) => html`<tr class="clickable" data-action="open-run" data-id="${r.run_id}">
          <td><a href="#/run/${r.run_id}"><b>${r.scheme_name}</b></a><div class="mono muted">${r.run_id}</div></td>
          <td>${labelOf(CATEGORIES, r.category)}</td>
          <td class="tnum">${fmtDate(r.as_of)}</td>
          <td>${pill(r.status)}</td>
          <td>${r.signed_by ? html`<span class="pill ${r.decision === "approved" ? "ok" : "warn"}">${r.decision === "approved" ? "Approved" : "Escalated"}</span> <span class="muted small">${r.signed_by}</span>`
            : html`<span class="muted">${r.review_items ? `${r.review_items} to review` : "Awaiting sign-off"}</span>`}</td>
          <td class="muted tnum">${fmtDateTime(r.created_at)}</td></tr>`)}</tbody>
      </table></div></div>`
      : html`<div class="card empty"><h2>No runs yet</h2><p>Your first compliance check will appear here.</p><a class="btn primary" href="#/new">Start a check</a></div>`}`);
}

/* ------------------------------------------------------------------ view: backtest */

async function renderBacktest(token) {
  if (state.backtest === undefined) {
    show(html`<header class="page-head"><div><h1>Backtest</h1></div></header>${loading()}`);
    state.backtest = await api("/backtest/latest");
    if (token !== navToken) return;
  }
  drawBacktest();
}

function drawBacktest() {
  const b = state.backtest;
  const head = html`
    <header class="page-head">
      <div><h1>Backtest</h1>
        <p class="muted">Thirteen synthetic cases, each run twice: with the agent's extracted mandate (agents and engine together) and with the reviewer-corrected mandate (the engine alone).</p></div>
      <button class="btn primary" data-action="run-backtest">Run backtest</button>
    </header>`;
  if (!b) {
    show(html`${head}${noticeHtml()}<div class="card empty"><h2>No backtest yet</h2><p>Run it to see where the system passes and where it fails.</p></div>`);
    return;
  }
  const cases = new Map();
  b.outcomes.forEach((o) => cases.set(o.case_id, { ...(cases.get(o.case_id) || {}), [o.source]: o, title: o.title, kind: o.kind }));
  const failures = b.outcomes.filter((o) => !o.passed);
  const stat = (label, ok, total, note) => html`
    <div class="card stat"><div class="stat-label">${label}</div>
      <div class="stat-value tnum ${ok === total ? "margin-pos" : ""}">${ok}<span class="muted"> / ${total}</span></div><div class="stat-note">${note}</div></div>`;
  show(html`
    ${head}${noticeHtml()}
    <div class="grid cols-3" style="margin-bottom:12px">
      ${stat("Engine, reviewer-corrected mandate", b.summary.ground_truth.passed, b.summary.ground_truth.total, "Deterministic arithmetic and rules")}
      ${stat("End to end, extracted mandate", b.summary.extracted.passed, b.summary.extracted.total, "Agents plus engine, mandate unchanged")}
      ${stat("Extraction field accuracy", b.extraction_accuracy.correct, b.extraction_accuracy.total, "Category, sector and limits")}
    </div>
    <div class="meta" style="margin:0 0 24px"><span class="chip">Model<b>${b.llm}</b></span><span class="chip">Generated<b>${fmtDateTime(b.generated_at)}</b></span><span class="chip">Data<b>synthetic</b></span></div>

    <div class="card flush"><div class="table-wrap"><table>
      <thead><tr><th>Case</th><th>Scenario</th><th>Kind</th><th>Extracted</th><th>Engine</th></tr></thead>
      <tbody>${[...cases.entries()].map(([id, c]) => html`<tr>
        <td class="mono">${id}</td><td>${c.title}</td><td class="muted">${KIND[c.kind] || c.kind}</td>
        <td>${c.extracted ? html`<span class="pill ${c.extracted.passed ? "ok" : "risk"}">${c.extracted.passed ? "Pass" : "Fail"}</span>` : "-"}</td>
        <td>${c.ground_truth ? html`<span class="pill ${c.ground_truth.passed ? "ok" : "risk"}">${c.ground_truth.passed ? "Pass" : "Fail"}</span>` : "-"}</td></tr>`)}</tbody>
    </table></div></div>

    <h2 style="margin:32px 0 12px">Where it fails</h2>
    ${failures.length ? failures.map((o) => html`
      <section class="card" style="margin-bottom:12px">
        <div class="finding-title"><span class="pill risk">Fail</span><h3>Case ${o.case_id}: ${o.title}</h3><span class="chip">${o.source === "extracted" ? "extracted mandate" : "reviewer-corrected mandate"}</span></div>
        ${o.notes ? html`<p class="muted" style="margin-top:8px">${o.notes}</p>` : ""}
        ${o.error ? html`<p class="margin-neg mono" style="margin-top:8px">${o.error}</p>` : ""}
        <div class="table-wrap" style="margin-top:12px"><table>
          <thead><tr><th>Check</th><th>Expected</th><th>Got</th></tr></thead>
          <tbody>${o.checks.filter((c) => !c.ok).map((c) => html`<tr><td class="mono">${c.name}</td><td class="mono">${c.expected}</td><td class="mono margin-neg">${c.actual}</td></tr>`)}</tbody>
        </table></div>
      </section>`) : html`<p class="muted">No failures in this run.</p>`}`);
}

/* ------------------------------------------------------------------ actions */

function notify(kind, title, message = "", lines = []) {
  state.notice = { kind, title, message, lines };
}

const actions = {
  async "load-sample"(select) {
    const name = select.value;
    state.form.sample = name;
    state.sampleNote = "";
    state.notice = null;
    if (name) {
      try {
        const s = await api(`/samples/${encodeURIComponent(name)}`);
        Object.assign(state.form, {
          holdings: s.holdings_csv, sid: s.sid_text || "", asOf: s.as_of,
          candidates: s.candidates_csv || state.form.candidates, trades: s.trades_csv || state.form.trades,
          weight: s.weight_pct || state.form.weight,
        });
        state.sampleNote = `${s.title}. ${s.notes}`;
      } catch (error) {
        state.notice = errorNotice(error, "Could not load the example");
      }
    }
    renderNew();
  },

  upload(input) {
    const file = input.files && input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      state.form[input.dataset.target] = String(reader.result);
      const target = $(`#${input.dataset.target}`);
      if (target) target.value = state.form[input.dataset.target];
    };
    reader.readAsText(file);
    input.value = "";
  },

  async continue(button) {
    const f = state.form;
    state.notice = null;
    if (!f.holdings.trim()) { notify("error", "Add the holdings file", "Paste or upload the month-end holdings CSV."); return renderNew(); }
    await busy(button, "Checking inputs…", async () => {
      try {
        const summary = await api("/checks/validate", { method: "POST", body: { holdings_csv: f.holdings, sid_text: f.sid.trim() ? f.sid : null } });
        if (!f.sid.trim()) { notify("error", "Add the Scheme Information Document", "The mandate is read from the SID."); return; }
        const proposal = await api("/checks/extract-mandate", { method: "POST", body: { sid_text: f.sid } });
        state.flow = {
          step: 2, summary, proposal, ack: false,
          m: { scheme_name: proposal.scheme_name || "", category: proposal.category || "", benchmark: proposal.benchmark || "", sector: proposal.sector || "" },
          limits: proposal.self_imposed_limits.map((l) => ({ ...l, threshold: String(num(l.threshold)), _orig: limitKey(l) })),
        };
      } catch (error) {
        state.notice = errorNotice(error, "Input needs attention");
      }
    });
    renderNew();
  },

  back() { state.flow = freshFlow(); state.notice = null; renderNew(); },

  "add-limit"() {
    state.flow.limits.push({ metric: "equity_pct", comparator: "min", threshold: "", citation: null, _orig: null });
    renderNew();
  },

  "remove-limit"(button) {
    state.flow.limits.splice(Number(button.dataset.index), 1);
    renderNew();
  },

  async confirm(button) {
    const { proposal: p, m, limits } = state.flow;
    const reviewer = state.form.reviewer.trim();
    state.notice = null;
    const fail = (title, message) => { notify("error", title, message); renderNew(); };
    if (!reviewer) return fail("Enter your name", "Gate 1 records who confirmed the mandate.");
    if (!m.scheme_name.trim() || !m.category) return fail("Complete the mandate", "A scheme name and a category are required.");
    if (limits.some((l) => String(l.threshold).trim() === "" || !Number.isFinite(Number(l.threshold)))) {
      return fail("Check the limits", "Every limit needs a numeric threshold.");
    }
    const clean = (value) => (value || "").trim() || null;
    const changed =
      !p.scheme_name || !p.category ||
      m.scheme_name.trim() !== p.scheme_name || m.category !== p.category ||
      clean(m.benchmark) !== (p.benchmark || null) || clean(m.sector) !== (p.sector || null) ||
      limits.length !== p.self_imposed_limits.length || limits.some((l) => limitKey(l) !== l._orig);
    if (!changed && p.issues.length && !state.flow.ack) {
      return fail("Review the issues first", "Tick the box to confirm you have read them, or correct the mandate.");
    }
    const body = { proposal_id: p.proposal_id, reviewer };
    if (changed) {
      body.corrected_mandate = {
        scheme_name: m.scheme_name.trim(),
        category: m.category,
        category_citation: m.category === p.category ? p.category_citation : null,
        benchmark: clean(m.benchmark),
        sector: clean(m.sector),
        sector_citation: clean(m.sector) === (p.sector || null) ? p.sector_citation : null,
        self_imposed_limits: limits.map((l) => ({
          metric: l.metric, comparator: l.comparator, threshold: String(l.threshold),
          citation: limitKey(l) === l._orig ? l.citation : null,
        })),
      };
    } else {
      body.acknowledge_issues = state.flow.ack;
    }
    let runId = null;
    await busy(button, "Running check…", async () => {
      try {
        const confirmed = await api("/checks/confirm-mandate", { method: "POST", body });
        const run = await api("/checks/run", { method: "POST", body: { mandate_id: confirmed.mandate_id, holdings_csv: state.form.holdings, as_of: state.form.asOf } });
        store.set("pl.reviewer", reviewer);
        runId = run.run_id;
      } catch (error) {
        state.notice = errorNotice(error, "Could not run the check");
      }
    });
    if (runId) {
      state.flow = freshFlow();
      state.run = null;
      location.hash = `#/run/${runId}`;
    } else {
      renderNew();
    }
  },

  tab(button) { state.runTab = button.dataset.tab; state.notice = null; drawTab(); },

  "ack-all"() { state.run.result.review_items.forEach((i) => state.acks.add(i.review_id)); drawTab(); },

  async sign(button) {
    const r = state.run;
    const reviewer = state.form.reviewer.trim();
    state.notice = null;
    if (!reviewer) { notify("error", "Enter your name", "Sign-off is attributable."); return drawTab(); }
    await busy(button, "Signing…", async () => {
      try {
        state.run = await api(`/checks/runs/${r.run_id}/sign-off`, {
          method: "POST",
          body: { reviewer, decision: state.form.decision, acknowledged_review_ids: [...state.acks], note: state.form.note },
        });
        store.set("pl.reviewer", reviewer);
      } catch (error) {
        state.notice = errorNotice(error, "Sign-off blocked");
      }
    });
    if (state.notice) return drawTab();
    drawRun();
  },

  async ask(button) {
    const question = state.form.question.trim();
    if (!question) return;
    await busy(button, "Asking…", async () => {
      try {
        const answer = await api("/checks/explain", { method: "POST", body: { run_id: state.run.run_id, question } });
        state.qa.unshift({ q: question, a: answer });
        state.form.question = "";
        state.notice = null;
      } catch (error) {
        state.notice = errorNotice(error, "Could not answer");
      }
    });
    drawTab();
  },

  suggest(button) { state.form.question = button.dataset.q; actions.ask(null); },

  async "example-pool"() {
    try {
      const s = await api("/samples/11_screen_large_cap_pool");
      state.form.candidates = s.candidates_csv || "";
      state.form.weight = s.weight_pct || "3";
    } catch (error) { state.notice = errorNotice(error, "Could not load the example"); }
    drawTab();
  },

  async "example-trades"() {
    try {
      const s = await api("/samples/12_whatif_restores_compliance");
      state.form.trades = s.trades_csv || "";
    } catch (error) { state.notice = errorNotice(error, "Could not load the example"); }
    drawTab();
  },

  async screen(button) {
    state.notice = null;
    if (!state.form.candidates.trim()) { notify("error", "Add candidates", "Paste a CSV with columns isin and name."); return drawTab(); }
    await busy(button, "Screening…", async () => {
      try {
        state.screen = await api("/screen", {
          method: "POST",
          body: { run_id: state.run.run_id, candidates_csv: state.form.candidates, weight_pct: String(state.form.weight), funding: state.form.screenFunding, sort_by: state.form.sortBy },
        });
      } catch (error) { state.screen = null; state.notice = errorNotice(error, "Screening failed"); }
    });
    drawTab();
  },

  async whatif(button) {
    state.notice = null;
    if (!state.form.trades.trim()) { notify("error", "Add trades", "Paste a CSV with columns action, isin, name and pct_of_nav."); return drawTab(); }
    await busy(button, "Simulating…", async () => {
      try {
        state.whatif = await api("/whatif", { method: "POST", body: { run_id: state.run.run_id, trades_csv: state.form.trades, funding: state.form.whatifFunding } });
      } catch (error) { state.whatif = null; state.notice = errorNotice(error, "Simulation failed"); }
    });
    drawTab();
  },

  async "run-backtest"(button) {
    state.notice = null;
    await busy(button, "Running…", async () => {
      try { state.backtest = await api("/backtest/run", { method: "POST" }); }
      catch (error) { state.notice = errorNotice(error, "Backtest failed"); }
    });
    drawBacktest();
  },

  "open-run"(row) { location.hash = `#/run/${row.dataset.id}`; },

  "download-report"() {
    const url = URL.createObjectURL(new Blob([state.run.report.markdown], { type: "text/markdown" }));
    const link = Object.assign(document.createElement("a"), { href: url, download: `portfoliolens-${state.run.run_id}.md` });
    link.click();
    URL.revokeObjectURL(url);
  },
};

actions["copy-report"] = async (button) => {
  try {
    await navigator.clipboard.writeText(state.run.report.markdown);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Copy failed";
  }
  setTimeout(() => { if (button.isConnected) button.textContent = "Copy"; }, 1500);
};

function limitKey(limit) {
  return `${limit.metric}|${limit.comparator}|${Number(limit.threshold)}`;
}

/* ------------------------------------------------------------------ events */

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (!target || ["SELECT", "INPUT"].includes(target.tagName)) return;
  if (event.target.closest("a") && target.dataset.action === "open-run") return;
  const handler = actions[target.dataset.action];
  if (handler) handler(target);
});

document.addEventListener("change", (event) => {
  const target = event.target;
  if (target.matches("select[data-action], input[type=file][data-action]")) actions[target.dataset.action](target);
  else if (target.matches("[data-ack]")) {
    if (target.checked) state.acks.add(target.dataset.ack);
    else state.acks.delete(target.dataset.ack);
    drawTab();
  }
});

document.addEventListener("input", (event) => {
  const target = event.target;
  if (!target.dataset || !target.dataset.bind) return;
  setPath(state, target.dataset.bind, target.type === "checkbox" ? target.checked : target.value);
});

document.addEventListener("keydown", (event) => {
  const target = event.target;
  if (event.key === "Enter" && target.dataset && target.dataset.enter) {
    event.preventDefault();
    actions[target.dataset.enter](target.nextElementSibling);
  }
});

window.addEventListener("hashchange", route);

/* ------------------------------------------------------------------ boot */

route();
api("/health")
  .then((health) => {
    $("#llm-label").textContent = health.llm.split(" (")[0];
    $("#llm-chip").title = health.llm;
  })
  .catch(() => { $("#llm-label").textContent = "unreachable"; });
