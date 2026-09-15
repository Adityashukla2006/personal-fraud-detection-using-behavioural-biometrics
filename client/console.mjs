import config from "./config.mjs";
import { NewPasswordRequired, signInWithPasskey, signInWithPassword } from "./auth.mjs";

const $ = (id) => document.getElementById(id);

const state = { tokens: null, uid: null, tab: "users", timer: null };

// ---- Small DOM helpers: everything shown is built with textContent, never innerHTML, because
// ---- decision and event data ultimately comes from users.

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === undefined || value === null) continue;
    if (name === "text") node.textContent = value;
    else if (name.startsWith("data-")) node.setAttribute(name, value);
    else node[name] = value;
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function status(message, isError = false) {
  $("status").textContent = message;
  $("status").classList.toggle("error", isError);
}

const TONES = {
  allow: "good", monitor: "good", released: "good", recorded: "good",
  step_up: "warn", awaiting_step_up: "warn", under_review: "warn", pending: "warn", processing: "warn",
  restrict: "bad", block: "bad", cancelled: "bad", rejected: "bad",
};

const badge = (value) => el("span", { className: "badge", "data-tone": TONES[value], text: String(value ?? "n/a").replaceAll("_", " ") });
const number = (value, digits = 1) => (value === null || value === undefined ? "n/a" : Number(value).toFixed(digits));
const when = (value) => {
  if (value === null || value === undefined || value === "") return "n/a";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
};

function tile(label, value, hint) {
  return el("div", { className: "tile" }, el("div", { className: "label", text: label }), el("div", { className: "value", text: value }), hint ? el("div", { className: "hint", text: hint }) : null);
}

function table(node, headings, rows) {
  node.replaceChildren(
    el("thead", {}, el("tr", {}, headings.map((heading) => el("th", { text: heading })))),
    el("tbody", {}, rows.length ? rows : el("tr", {}, el("td", { colSpan: headings.length, className: "muted", text: "Nothing yet." }))),
  );
}

// ---- API ----------------------------------------------------------------------------------------

async function api(method, path) {
  const response = await fetch(`${config.apiUrl}${path}`, {
    method,
    headers: { Authorization: `Bearer ${state.tokens.AccessToken}` },
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 403) throw new Error("This account is not in the analyst group.");
  if (!response.ok) throw new Error(data.error || data.message || `${method} ${path} failed (${response.status})`);
  return data;
}

// ---- Users --------------------------------------------------------------------------------------

async function loadUsers() {
  const { users } = await api("GET", "/console/users");
  $("users").replaceChildren(
    ...users.map((user) =>
      el("li", {},
        el("button", {
          type: "button",
          onclick: () => selectUser(user.uid),
          "data-uid": user.uid,
        }, user.email ?? user.uid, el("small", { text: `${user.status} · ${user.uid}` })),
      ),
    ),
  );
  markSelected();
}

function markSelected() {
  for (const button of document.querySelectorAll("#users button")) {
    button.setAttribute("aria-current", String(button.dataset.uid === state.uid));
  }
}

async function selectUser(uid) {
  state.uid = uid;
  markSelected();
  await loadUser();
}

function contributionBars(contributions) {
  const largest = Math.max(0.0001, ...contributions.map((c) => Math.abs(Number(c.value))));
  return el("div", { className: "bars" },
    contributions.map(({ channel, value }) => {
      const v = Number(value);
      return el("div", { className: "bar" },
        el("span", { text: channel }),
        el("span", { className: "track" }, el("span", { className: "fill", "data-sign": v < 0 ? "down" : "up", style: `width:${(Math.abs(v) / largest) * 100}%` })),
        el("span", { text: `${v >= 0 ? "+" : ""}${v.toFixed(2)}` }),
      );
    }),
  );
}

async function release(transferId) {
  await api("POST", `/console/transfers/${state.uid}/${transferId}/release`);
  status("Release sent to the workflow. The ledger updates in a moment.");
  setTimeout(() => loadUser().catch((error) => status(error.message, true)), 1500);
}

async function loadUser() {
  if (!state.uid) return;
  const detail = await api("GET", `/console/users/${state.uid}`);
  $("user-empty").hidden = true;
  $("user-detail").hidden = false;

  const profiles = detail.adaptation.profiles;
  const held = detail.transfers.filter((t) => ["awaiting_step_up", "under_review"].includes(t.status)).length;
  $("tiles").replaceChildren(
    tile("Balance", detail.balance === null ? "not opened" : Number(detail.balance).toLocaleString(), "mock ledger, debits only on release"),
    tile("Decisions", String(detail.decisions.length), "most recent 25"),
    tile("Transfers held", String(held), "awaiting step-up or review"),
    tile("Profiles", String(profiles.length), profiles.map((p) => `${p.device_class} v${p.version}`).join(", ") || "none learned yet"),
    tile("Verified step-ups", String(detail.adaptation.verifications.length), `${detail.adaptation.verifications.filter((v) => v.admitted).length} admitted to the buffer`),
  );

  table($("transfers"), ["Created", "Amount", "Action", "Status", "Reason", ""],
    detail.transfers.map((t) =>
      el("tr", {},
        el("td", { text: when(t.created_at) }),
        el("td", { text: Number(t.amount).toLocaleString() }),
        el("td", {}, badge(t.action)),
        el("td", {}, badge(t.status)),
        el("td", { text: t.reason ?? "" }),
        el("td", {}, t.status === "under_review"
          ? el("button", { type: "button", className: "small", text: "Release", onclick: () => release(t.transfer_id).catch((error) => status(error.message, true)) })
          : null),
      ),
    ),
  );

  table($("decisions"), ["Time", "Checkpoint", "Action", "Risk", "Confidence", "Top contributions", "Policy"],
    detail.decisions.map((d) =>
      el("tr", {},
        el("td", { text: when(d.created) }),
        el("td", { text: d.checkpoint }),
        el("td", {}, badge(d.action)),
        el("td", { text: number(d.risk) }),
        el("td", { text: `${Math.round(Number(d.confidence ?? 0) * 100)}%` }),
        el("td", {}, d.contributions.length ? contributionBars(d.contributions) : el("span", { className: "muted", text: "fail-open: no evidence" })),
        el("td", { text: d.constraints.join(", ") }),
      ),
    ),
  );

  renderAdaptation(detail.adaptation);
}

function renderAdaptation(adaptation) {
  const blocks = [];

  const verifications = el("table");
  table(verifications, ["Received", "Decision", "Method", "Trust", "Admitted"],
    adaptation.verifications.map((v) =>
      el("tr", {},
        el("td", { text: when(v.received_at) }),
        el("td", { className: "mono", text: v.decision_id }),
        el("td", { text: v.method }),
        el("td", { text: number(v.trust, 2) }),
        el("td", {}, badge(v.admitted ? "recorded" : "rejected")),
      ),
    ),
  );
  blocks.push(el("h3", { text: "Verified step-ups and their trust" }), el("div", { className: "scroll" }, verifications));

  const devices = el("table");
  table(devices, ["Device", "Class", "Verified sessions", "Buffered (class)"],
    adaptation.devices.map((d) =>
      el("tr", {},
        el("td", { className: "mono", text: d.device_id }),
        el("td", { text: d.device_class }),
        el("td", { text: String(d.sessions) }),
        el("td", { text: String(adaptation.buffered[d.device_class] ?? 0) }),
      ),
    ),
  );
  blocks.push(el("h3", { text: "Devices" }), el("div", { className: "scroll" }, devices));

  for (const profile of adaptation.profiles) {
    const vector = el("table");
    table(vector, ["Feature", "Centre", "Scale"],
      profile.features.map((name, index) =>
        el("tr", {}, el("td", { text: name }), el("td", { text: number(profile.mu[index], 4) }), el("td", { text: number(profile.sigma[index], 4) })),
      ),
    );
    blocks.push(
      el("h3", { text: `Profile: ${profile.device_class}` }),
      el("p", { className: "muted", text: `version ${profile.version} · ${profile.sessions ?? "?"} sessions · ${profile.saturations} budget saturations · anchored ${when(profile.anchored_at)}${profile.demo_seed ? ` · demo seed (${profile.demo_seed})` : ""}` }),
      el("div", { className: "scroll" }, vector),
    );
  }
  if (!adaptation.profiles.length) {
    blocks.push(el("p", { className: "muted", text: "No profile yet. One is bootstrapped after five trusted, passkey-verified sessions on a device class." }));
  }
  $("adaptation").replaceChildren(...blocks);
}

// ---- Audit lake ---------------------------------------------------------------------------------

async function loadLake() {
  const { events } = await api("GET", "/console/lake");
  $("lake").replaceChildren(
    ...(events.length ? events : [null]).map((event) =>
      event === null
        ? el("li", { className: "muted", text: "No events in the last two days." })
        : el("li", {},
            el("details", {},
              el("summary", {},
                el("span", { className: "muted", text: when(event.time) }),
                badge(event.type),
                el("span", { text: event.detail.action ?? event.detail.status ?? event.detail.verification?.method ?? "" }),
                el("span", { className: "mono", text: event.subject ?? "" }),
              ),
              el("pre", { text: JSON.stringify(event.detail, null, 2) }),
            ),
          ),
    ),
  );
}

// ---- Research -----------------------------------------------------------------------------------

async function csv(path) {
  const response = await fetch(path);
  if (!response.ok) return [];
  const [header, ...lines] = (await response.text()).trim().split(/\r?\n/);
  const names = header.split(",");
  return lines.map((line) => Object.fromEntries(line.split(",").map((value, index) => [names[index], value])));
}

const percent = (value) => `${(Number(value) * 100).toFixed(1)}%`;

async function loadResearch() {
  const [variants, detection, fusion, latency, baseline] = await Promise.all([
    csv("research/tables/poisoning_variants.csv"),
    csv("research/tables/phase8_detection.csv"),
    csv("research/tables/fusion_evaluation.csv"),
    csv("research/tables/phase4_latency.csv"),
    csv("research/tables/baseline_eer.csv"),
  ]);

  const tiles = [];
  if (baseline.length) {
    const mean = (key) => baseline.reduce((sum, row) => sum + Number(row[key]), 0) / baseline.length;
    tiles.push(tile("EER, 31 raw features", percent(mean("eer_set_a_mad")), "published baseline 9.6%"));
    tiles.push(tile("EER, 9 deployed features", percent(mean("eer_set_b_mad")), "what the live scorer uses"));
  }
  for (const row of latency) {
    tiles.push(tile(row.measure === "handler" ? "Handler latency p50 / p95" : "Round trip p50 / p95", `${Number(row.p50_ms).toFixed(0)} / ${Number(row.p95_ms).toFixed(0)} ms`, `${row.samples} warm requests`));
  }
  if (variants.length) {
    tiles.push(tile("P0 static impersonation", percent(variants[0].P0_impersonation), `false rejection under drift ${percent(variants[0].P0_false_rejection)}`));
  }
  for (const row of variants) {
    tiles.push(tile(`P3 ${row.config.replaceAll("_", " ")}`, percent(row.P3_impersonation), `impersonation; false rejection under drift ${percent(row.P3_false_rejection)}`));
  }
  for (const row of fusion.filter((entry) => entry.model === "fitted")) {
    tiles.push(tile(`Fitted fusion, ${row.attack}`, percent(row.detection_rate), row.attack === "genuine" ? "friction on held-out subjects" : "detected on held-out subjects"));
  }
  for (const row of detection) {
    tiles.push(tile(`Live, ${row.attack}`, percent(row.detection_rate), row.attack === "genuine" ? `friction over ${row.sessions} sessions` : `detected over ${row.sessions} sessions`));
  }
  $("research-numbers").replaceChildren(...tiles);

  const figures = [
    ["research/figures/baseline_eer.png", "Per-subject EER on the CMU benchmark: the 31 raw per-key features against the 9 deployable aggregates."],
    ["research/figures/poisoning_variants.png", "Poisoning, every variant tried, P3 against the static profile. Each variant was fixed before any ran and all are shown. No variant makes adaptation resist impersonation better than never adapting; the best trades a smaller rise in impersonation for far lower false rejection under genuine drift."],
    ["research/figures/poisoning_policies.png", "The first full run, P0 to P3, with budgets at the 95th percentile of genuine drift and the scale adapted. P3 tolerates drift best but accepts the attacker most."],
    ["research/figures/budget_tradeoff.png", "That run's budgets swept together. The curve is flat: at that drift level the budget does not bind."],
    ["research/figures/poisoning_policies_shared_budget.png", "The first run, with one budget shared between centre and scale: the flaw that led to a separate scale budget."],
  ];
  $("research-figures").replaceChildren(
    ...figures.map(([src, caption]) => el("figure", {}, el("img", { src, alt: caption, loading: "lazy" }), el("figcaption", { text: caption }))),
  );
}

// ---- Shell --------------------------------------------------------------------------------------

async function refresh() {
  if (!state.tokens) return;
  try {
    if (state.tab === "users") await loadUser();
    if (state.tab === "lake") await loadLake();
  } catch (error) {
    status(error.message, true);
  }
}

function showTab(name) {
  state.tab = name;
  for (const tab of document.querySelectorAll("[role=tab]")) tab.setAttribute("aria-selected", String(tab.dataset.tab === name));
  for (const panel of document.querySelectorAll("[data-panel]")) panel.hidden = panel.dataset.panel !== name;
  if (name === "lake") loadLake().catch((error) => status(error.message, true));
  if (name === "research") loadResearch().catch((error) => status(error.message, true));
}

function scheduleRefresh() {
  clearInterval(state.timer);
  if ($("auto-refresh").checked) state.timer = setInterval(refresh, 5000);
}

async function afterSignIn(tokens, email) {
  state.tokens = tokens;
  $("signin").hidden = true;
  $("workspace").hidden = false;
  $("signout").hidden = false;
  $("analyst").textContent = email;
  await loadUsers();
  scheduleRefresh();
  status("");
}

function guard(action) {
  return async (event) => {
    event?.preventDefault();
    try {
      await action(event);
    } catch (error) {
      status(error.message, true);
    }
  };
}

$("signin-form").addEventListener(
  "submit",
  guard(async () => {
    const email = $("email").value.trim();
    status("Signing in...");
    try {
      await afterSignIn(await signInWithPassword(email, $("password").value, $("new-password").value), email);
    } catch (error) {
      if (error instanceof NewPasswordRequired) $("new-password-row").hidden = false;
      throw error;
    }
  }),
);

$("passkey-signin").addEventListener(
  "click",
  guard(async () => {
    const email = $("email").value.trim();
    if (!email) throw new Error("Enter your email first");
    status("Waiting for your passkey...");
    await afterSignIn(await signInWithPasskey(email), email);
  }),
);

$("signout").addEventListener("click", () => {
  state.tokens = null;
  clearInterval(state.timer);
  $("workspace").hidden = true;
  $("signout").hidden = true;
  $("signin").hidden = false;
  $("analyst").textContent = "";
});

for (const tab of document.querySelectorAll("[role=tab]")) tab.addEventListener("click", () => showTab(tab.dataset.tab));
$("reload-users").addEventListener("click", guard(loadUsers));
$("reload-lake").addEventListener("click", guard(loadLake));
$("auto-refresh").addEventListener("change", scheduleRefresh);
