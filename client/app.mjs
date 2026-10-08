import config from "./config.mjs";
import {
  NewPasswordRequired,
  credentialJson,
  newId,
  registerPasskey,
  requestOptions,
  signInWithPasskey,
  signInWithPassword,
} from "./auth.mjs";
import {
  buildBehaviour,
  createRecorder,
  deviceDescriptor,
  keyDown,
  keyUp,
  paste,
  resetRecorder,
} from "./capture.mjs";
import { createHood, stepsFor } from "./cloud.mjs";
import { OPEN, settledNotices } from "./notices.mjs";
import { clearSession, restoreSession, saveSession } from "./session.mjs";

const $ = (id) => document.getElementById(id);
const SESSION_KEY = "bfd-session";
// While a transfer is still open, the statement is re-read this often to notice it settle.
const WATCH_MS = 5000;
const VIEWS = ["signin", "dashboard", "transfer", "activity", "security"];
const STEPS = ["payee", "amount", "review", "done"];

const state = {
  tokens: null,
  email: null,
  sessionId: null,
  recorders: new Map(),
  deviceId: loadDeviceId(),
  balance: null,
  draft: {},
  transferId: null,
  lastConfirmBehaviour: null,
  watching: false,
  settledHere: new Set(),
};

const hood = createHood($("hood"));

// ---- Local storage, all optional ----------------------------------------------------------------

function stored(key, fallback) {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : JSON.parse(value);
  } catch {
    return fallback;
  }
}

function store(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Private windows and blocked storage simply forget.
  }
}

function loadDeviceId() {
  const existing = stored("bfd-device", null);
  if (existing) return existing;
  const id = newId();
  store("bfd-device", id);
  return id;
}

// Beneficiary names stay in this browser, keyed by the hash the server sees, so statements can say
// who a transfer went to without the name or account number ever being sent.
const payeeBook = () => stored("bfd-payees", {});
function rememberPayee(payeeId, name, account) {
  store("bfd-payees", { ...payeeBook(), [payeeId]: { name, last4: account.slice(-4) } });
}

// ---- Formatting ---------------------------------------------------------------------------------

const rupees = (value) =>
  new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(Number(value));

const ACTION = {
  allow: { label: "Allowed", level: "good" },
  monitor: { label: "Monitored", level: "good" },
  step_up: { label: "Step-up", level: "warning" },
  restrict: { label: "Restricted", level: "serious" },
  block: { label: "Blocked", level: "critical" },
};

const STATUS = {
  released: { label: "Completed", level: "good" },
  processing: { label: "Processing", level: "good" },
  pending: { label: "Processing", level: "good" },
  awaiting_step_up: { label: "Awaiting verification", level: "warning" },
  under_review: { label: "Under review", level: "serious" },
  blocked: { label: "Blocked", level: "critical" },
  cancelled: { label: "Cancelled", level: "critical" },
  rejected: { label: "Declined", level: "critical" },
  failed: { label: "Failed", level: "critical" },
};

const CHANNELS = {
  behaviour: "Typing rhythm",
  automation: "Automation",
  transaction: "Amount pattern",
  context: "Device, account",
  payee: "Payee",
};

const REASONS = {
  behaviour: "the typing on this transfer doesn't match how you usually type",
  automation: "the typing on this transfer looked automated",
  transaction: "this amount is unusual for you",
  context: "you're on a device we haven't seen much",
  payee: "you haven't paid this beneficiary before",
};

// ---- Feedback -----------------------------------------------------------------------------------

let toastTimer;
function status(message, isError = false) {
  const toast = $("status");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), isError ? 6000 : 3500);
}

function guard(action) {
  return async (event) => {
    event?.preventDefault?.();
    const button = event?.submitter ?? (event?.currentTarget instanceof HTMLButtonElement ? event.currentTarget : null);
    if (button) button.disabled = true;
    try {
      await action(event);
    } catch (error) {
      status(error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  };
}

function wireTooltips() {
  const tip = $("tooltip");
  document.addEventListener("pointermove", (event) => {
    const target = event.target.closest?.("[data-tip]");
    if (!target) {
      tip.hidden = true;
      return;
    }
    tip.textContent = target.dataset.tip;
    tip.hidden = false;
    tip.style.left = `${Math.min(event.clientX + 12, innerWidth - tip.offsetWidth - 8)}px`;
    tip.style.top = `${event.clientY + 14}px`;
  });
}

// ---- Capture ------------------------------------------------------------------------------------

function attachCapture() {
  for (const input of document.querySelectorAll("input[data-capture]")) {
    const recorder = createRecorder();
    state.recorders.set(input.id, recorder);
    input.addEventListener("keydown", (e) => keyDown(recorder, e.code, e.key, e.timeStamp, e.repeat));
    input.addEventListener("keyup", (e) => keyUp(recorder, e.code, e.timeStamp));
    input.addEventListener("paste", () => paste(recorder));
  }
}

function takeBehaviour(group) {
  const recorders = [...document.querySelectorAll(`input[data-capture="${group}"]`)].map((input) =>
    state.recorders.get(input.id),
  );
  const behaviour = buildBehaviour(recorders);
  recorders.forEach(resetRecorder);
  return behaviour;
}

// Demo attack controls. Each rewrites only timing, as attacking software would, then turns off.
function robotic(behaviour) {
  const sizes = behaviour.fields.length ? behaviour.fields.map((f) => f.hold.length) : [12, 12, 12];
  return {
    fields: sizes.map((keys) => ({
      hold: Array(keys).fill(0.09),
      down_down: Array(keys - 1).fill(0.15),
      up_down: Array(keys - 1).fill(0.06),
      backspaces: 0,
      corrections: 0,
      pastes: 0,
    })),
  };
}

function applyDemo(behaviour) {
  if ($("demo-replay").checked) {
    $("demo-replay").checked = false;
    if (!state.lastConfirmBehaviour) {
      status("Confirm one transfer first, so there is typing to replay.", true);
      return behaviour;
    }
    status("Replaying your last confirmed transfer's typing.");
    return structuredClone(state.lastConfirmBehaviour);
  }
  if ($("demo-bot").checked) {
    $("demo-bot").checked = false;
    status("Sending bot-like typing for this check.");
    return robotic(behaviour);
  }
  return behaviour;
}

// ---- API ----------------------------------------------------------------------------------------

async function call(method, path, body) {
  const started = performance.now();
  let response;
  try {
    response = await fetch(`${config.apiUrl}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${state.tokens.AccessToken}`,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    hood.record(method, path, 0, null, "network error");
    throw new Error("We couldn't reach the bank. Check your connection and try again.");
  }
  const roundTrip = performance.now() - started;
  const data = await response.json().catch(() => ({}));
  return { ok: response.ok, status: response.status, data, roundTrip, timing: response.headers.get("server-timing") };
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function checkpoint(name, group, transaction = null) {
  const body = {
    session_id: state.sessionId,
    checkpoint: name,
    device: deviceDescriptor(state.deviceId),
    behaviour: applyDemo(takeBehaviour(group)),
  };
  if (transaction) body.transaction = transaction;
  hood.showPayload(body);

  const result = await call("POST", "/score", body);
  const keys = body.behaviour.fields.reduce((sum, field) => sum + field.hold.length, 0);
  hood.record("POST", "/score", result.status, result.roundTrip, `${name} checkpoint · ${keys} keystrokes`);
  if (!result.ok) throw new Error(result.data.error || result.data.message || "Security check failed");

  const values = hood.latency(result.roundTrip, result.timing);
  hood.trace(stepsFor("score", { values, action: result.data.action }));
  renderDecision(result.data);
  if (name === "confirmation") state.lastConfirmBehaviour = body.behaviour;
  return result.data;
}

// ---- Decision rendering -------------------------------------------------------------------------

function renderDecision(decision) {
  const action = ACTION[decision.action] ?? { label: decision.action, level: "good" };
  const root = document.querySelector("[data-hood=decision]");
  const risk = decision.risk === null || decision.risk === undefined ? null : Number(decision.risk);

  const head = document.createElement("div");
  head.className = "decision-head";
  head.innerHTML = `<div class="decision-risk"></div><span class="pill"></span>`;
  head.querySelector(".decision-risk").append(
    risk === null ? "n/a" : risk.toFixed(1),
    Object.assign(document.createElement("small"), { textContent: " / 100 risk" }),
  );
  const pill = head.querySelector(".pill");
  pill.dataset.level = action.level;
  pill.textContent = action.label;

  const gauge = document.createElement("div");
  gauge.className = "gauge";
  const fill = document.createElement("span");
  fill.dataset.level = action.level;
  fill.style.width = `${Math.max(2, risk ?? 0)}%`;
  gauge.dataset.tip = `Risk ${risk === null ? "n/a" : risk.toFixed(1)}: ${action.label}`;
  gauge.appendChild(fill);

  const facts = document.createElement("dl");
  facts.className = "facts";
  for (const [label, value] of [
    ["Checkpoint", decision.checkpoint],
    ["Confidence", `${Math.round(decision.confidence * 100)}%`],
    ["Policy rules", decision.constraints?.length ? decision.constraints.join(", ").replaceAll("_", " ") : "none applied"],
  ]) {
    facts.append(
      Object.assign(document.createElement("dt"), { textContent: label }),
      Object.assign(document.createElement("dd"), { textContent: value }),
    );
  }

  const contributions = decision.contributions ?? [];
  const scale = Math.max(1, ...contributions.map((c) => Math.abs(c.value)));
  const list = document.createElement("ul");
  list.className = "contrib";
  for (const { channel, value } of contributions) {
    const row = document.createElement("li");
    const label = Object.assign(document.createElement("span"), { textContent: CHANNELS[channel] ?? channel });
    const track = document.createElement("span");
    track.className = "track";
    const bar = document.createElement("span");
    bar.className = `bar ${value >= 0 ? "up" : "down"}`;
    bar.style.width = `${(Math.abs(value) / scale) * 50}%`;
    track.dataset.tip = `${CHANNELS[channel] ?? channel}: ${value >= 0 ? "raises" : "lowers"} risk by ${Math.abs(value).toFixed(2)}`;
    track.appendChild(bar);
    const number = Object.assign(document.createElement("span"), {
      className: "value",
      textContent: `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(2)}`,
    });
    row.append(label, track, number);
    list.appendChild(row);
  }
  const key = document.createElement("p");
  key.className = "contrib-key";
  key.innerHTML =
    '<span><i style="background:var(--div-high)"></i>raises risk</span><span><i style="background:var(--div-low)"></i>lowers risk</span>';

  root.replaceChildren(head, gauge, facts, list, key);

  $("protect-dot").dataset.level = action.level;
  $("protect-title").textContent =
    action.level === "good" ? "Behavioural protection is on" : `Last check: ${action.label.toLowerCase()}`;
  $("protect-detail").textContent =
    `${decision.checkpoint} check, risk ${risk === null ? "n/a" : risk.toFixed(0)} of 100, just now.`;
}

// ---- Account ------------------------------------------------------------------------------------

function accountNumber(sub) {
  const digits = [...sub.replaceAll("-", "")].map((c) => parseInt(c, 16) % 10).join("");
  return `•••• •••• ${digits.slice(0, 4)}`;
}

function subject() {
  try {
    const part = state.tokens.AccessToken.split(".")[1].replaceAll("-", "+").replaceAll("_", "/");
    return JSON.parse(atob(part)).sub ?? "";
  } catch {
    return "";
  }
}

function transferRow(transfer) {
  const payee = payeeBook()[transfer.payee_id];
  const status = STATUS[transfer.status] ?? { label: transfer.status, level: "good" };
  const name = payee?.name ?? `Beneficiary ${transfer.payee_id?.slice(0, 6) ?? ""}`;
  const when = transfer.created_at
    ? new Date(transfer.created_at * 1000).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
    : "";

  const row = document.createElement("li");
  const icon = Object.assign(document.createElement("span"), { className: "txn-icon", textContent: name.trim()[0]?.toUpperCase() ?? "?" });
  const main = document.createElement("span");
  main.className = "txn-main";
  main.append(
    Object.assign(document.createElement("b"), { textContent: name }),
    Object.assign(document.createElement("span"), { textContent: `${payee ? `A/c ••${payee.last4} · ` : ""}${when}` }),
  );
  const amount = document.createElement("span");
  amount.className = "txn-amount";
  const pill = Object.assign(document.createElement("small"), { className: "pill", textContent: status.label });
  pill.dataset.level = status.level;
  const debited = transfer.status === "released";
  amount.append(`${debited ? "−" : ""}${rupees(transfer.amount)}`, pill);
  row.append(icon, main, amount);
  return row;
}

function fillList(list, transfers, limit) {
  const rows = transfers.slice(0, limit).map(transferRow);
  if (!rows.length) {
    rows.push(Object.assign(document.createElement("li"), { className: "txn-empty", textContent: "No transactions yet. Your transfers will appear here." }));
  }
  list.replaceChildren(...rows);
}

// Quiet reads are the background watch: they stay out of the under-the-hood panel.
async function loadAccount(quiet = false) {
  const result = await call("GET", "/account");
  if (!quiet) hood.record("GET", "/account", result.status, result.roundTrip, "balance and statement");
  if (!result.ok) throw new Error("We couldn't load your account.");
  if (!quiet) {
    hood.latencyOnly(result.roundTrip, "API Gateway, λ transfers and DynamoDB");
    hood.trace(stepsFor("account", { values: { network: result.roundTrip } }));
  }
  state.balance = result.data.balance;
  $("balance").textContent = rupees(result.data.balance);
  fillList($("recent-list"), result.data.transfers, 5);
  fillList($("activity-list"), result.data.transfers, 10);
  notify(result.data.transfers);
}

// ---- Notifications ------------------------------------------------------------------------------

function notify(transfers) {
  // Remembered per user, so a transfer settled while signed out is still announced on return.
  const key = `bfd-seen-${subject()}`;
  const describe = (t) => `${rupees(t.amount)} to ${payeeBook()[t.payee_id]?.name ?? "a new beneficiary"}`;
  const { notices, seen } = settledNotices(stored(key, null), transfers, describe);
  store(key, seen);
  for (const notice of notices) {
    // This page already showed the outcome of transfers it settled itself.
    if (!state.settledHere.has(notice.transferId)) showNotice(notice);
  }
  state.watching = transfers.some((t) => OPEN.has(t.status));
}

function showNotice({ level, title, text }) {
  const card = document.createElement("div");
  card.className = "notice";
  card.dataset.level = level;
  card.setAttribute("role", "status");
  const body = document.createElement("div");
  body.append(Object.assign(document.createElement("b"), { textContent: title }), Object.assign(document.createElement("p"), { textContent: text }));
  const close = Object.assign(document.createElement("button"), { type: "button", className: "notice-close", textContent: "×" });
  close.setAttribute("aria-label", "Dismiss");
  close.addEventListener("click", () => card.remove());
  card.append(body, close);
  $("notices").prepend(card);
}

function watch() {
  setInterval(() => {
    if (state.tokens && state.watching && document.visibilityState === "visible") loadAccount(true).catch(() => {});
  }, WATCH_MS);
}

// ---- Views --------------------------------------------------------------------------------------

function show(view) {
  if (!state.tokens) view = "signin";
  if (!VIEWS.includes(view)) view = "dashboard";
  for (const section of document.querySelectorAll("[data-view]")) {
    section.hidden = section.dataset.view !== view;
  }
  for (const link of document.querySelectorAll("[data-nav]")) {
    if (link.dataset.nav === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (view === "transfer" && !state.transferId) step("payee");
  if (view === "security") $("device-info").textContent = JSON.stringify(deviceDescriptor(state.deviceId), null, 2);
  window.scrollTo({ top: 0 });
}

function route() {
  show(location.hash.slice(1) || "dashboard");
}

function step(name) {
  for (const form of document.querySelectorAll(".step")) form.hidden = form.dataset.step !== name;
  const index = STEPS.indexOf(name);
  for (const item of document.querySelectorAll("#stepper li")) {
    const position = STEPS.indexOf(item.dataset.step);
    item.dataset.state = position < index ? "done" : position === index ? "current" : "";
  }
  document.querySelector(`.step[data-step="${name}"] input`)?.focus();
}

function fieldError(id, message) {
  $(id).textContent = message ?? "";
  $(id).hidden = !message;
  if (message) throw new Error(message);
}

function resetTransfer() {
  state.draft = {};
  state.transferId = null;
  for (const id of ["payee-name", "payee-account", "payee-ifsc", "amount", "remarks", "confirm-account"]) $(id).value = "";
  for (const id of ["payee-error", "amount-error", "review-error"]) $(id).hidden = true;
  step("payee");
}

// ---- Transfer flow ------------------------------------------------------------------------------

async function draftTransaction() {
  return { payee_id: await sha256Hex(state.draft.account), amount: state.draft.amount };
}

function showResult({ level, icon, title, text, details = [], stepUp = false }) {
  step("done");
  $("result-icon").dataset.level = level;
  $("result-icon").textContent = icon;
  $("result-title").textContent = title;
  $("result-text").textContent = text;
  $("result-details").replaceChildren(
    ...details.flatMap(([label, value]) => [
      Object.assign(document.createElement("dt"), { textContent: label }),
      Object.assign(document.createElement("dd"), { textContent: value }),
    ]),
  );
  $("result-stepup").hidden = !stepUp;
}

function receipt(extra = []) {
  return [
    ["To", `${state.draft.name} · A/c ••${state.draft.account.slice(-4)}`],
    ["Amount", rupees(state.draft.amount)],
    ["Reference", (state.transferId ?? "").slice(0, 12).toUpperCase()],
    ...extra,
  ];
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const IN_FLIGHT = new Set(["pending", "processing", "awaiting_step_up"]);

async function pollTransfer(transferId, attempts = 20) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const result = await call("GET", `/transfers/${transferId}`);
    hood.record("GET", "/transfers/{id}", result.status, result.roundTrip, result.data.status ?? "");
    if (result.ok && !IN_FLIGHT.has(result.data.status)) return result.data;
    await sleep(750);
  }
  throw new Error("The transfer is still processing. Check your statement shortly.");
}

async function settle(transfer) {
  const status = transfer.status;
  if (OPEN.has(status)) state.watching = true;
  else state.settledHere.add(state.transferId);
  if (status === "released") {
    showResult({
      level: "good", icon: "✓", title: "Transfer successful",
      text: `${rupees(state.draft.amount)} is on its way to ${state.draft.name}.`,
      details: receipt(transfer.balance !== null && transfer.balance !== undefined ? [["Balance", rupees(transfer.balance)]] : []),
    });
  } else if (status === "under_review") {
    showResult({
      level: "serious", icon: "!", title: "Held for review",
      text: "Our fraud team will look at this transfer before it goes anywhere. The money has not left your account.",
      details: receipt(),
    });
  } else if (status === "blocked" || status === "cancelled") {
    showResult({
      level: "critical", icon: "×", title: "Transfer stopped",
      text: "We stopped this transfer to protect your account. Nothing was debited. If this was you, contact us.",
      details: receipt(),
    });
  } else if (status === "rejected") {
    showResult({ level: "critical", icon: "×", title: "Transfer declined", text: "There isn't enough balance for this transfer.", details: receipt() });
  } else {
    showResult({ level: "critical", icon: "×", title: "Transfer failed", text: "The transfer couldn't be started. Nothing was debited.", details: receipt() });
  }
  await loadAccount().catch(() => {});
}

async function stepUp() {
  const transferId = state.transferId;
  let start;
  // The workflow records the hold a moment after scoring returns.
  for (let attempt = 0; attempt < 10; attempt += 1) {
    start = await call("POST", `/transfers/${transferId}/stepup`);
    hood.record("POST", "/transfers/{id}/stepup", start.status, start.roundTrip, "passkey challenge");
    const notReady = start.status === 404 || (start.status === 409 && start.data.status === "pending");
    if (!notReady) break;
    await sleep(500);
  }
  if (!start.ok) throw new Error(start.data.error || "Verification could not start");
  hood.trace(stepsFor("stepup-start"));
  hood.latencyOnly(start.roundTrip, "API Gateway, λ transfers and Cognito");

  const credential = await navigator.credentials.get({ publicKey: requestOptions(start.data.options) });
  const verify = await call("POST", `/transfers/${transferId}/stepup/verify`, {
    session: start.data.session,
    credential: credentialJson(credential),
  });
  hood.record("POST", "/transfers/{id}/stepup/verify", verify.status, verify.roundTrip, "passkey verified");
  if (!verify.ok) throw new Error(verify.data.error || "Verification failed");
  hood.trace(stepsFor("stepup-verify"));
  hood.latencyOnly(verify.roundTrip, "API Gateway, λ transfers, Cognito and Step Functions");
  return pollTransfer(transferId);
}

function askStepUp(decision) {
  const top = (decision.contributions ?? []).find((c) => c.value > 0);
  $("stepup-reason").textContent =
    `We've held this transfer because ${REASONS[top?.channel] ?? "it looks different from how you usually bank"}. Nothing has left your account.`;
  const dialog = $("stepup-dialog");
  dialog.returnValue = "";
  dialog.showModal();
  dialog.addEventListener(
    "close",
    guard(async () => {
      if (dialog.returnValue !== "verify") {
        showResult({
          level: "warning", icon: "!", title: "Waiting for your verification",
          text: "This transfer is on hold until you verify with your passkey. It expires on its own if you don't.",
          details: receipt(), stepUp: true,
        });
        return;
      }
      status("Waiting for your passkey…");
      await settle(await stepUp());
    }),
    { once: true },
  );
}

async function confirmTransfer() {
  const decision = await checkpoint("confirmation", "confirm", await draftTransaction());
  state.sessionId = newId();
  const transfer = decision.transfer;
  if (!transfer) throw new Error("The transfer couldn't be started. Nothing was debited.");
  state.transferId = transfer.transfer_id;
  rememberPayee((await draftTransaction()).payee_id, state.draft.name, state.draft.account);

  if (transfer.status === "awaiting_step_up") {
    askStepUp(decision);
    return;
  }
  if (transfer.status === "processing") {
    showResult({ level: "good", icon: "…", title: "Sending…", text: "Your transfer is being processed.", details: receipt() });
    await settle(await pollTransfer(transfer.transfer_id));
    return;
  }
  await settle(transfer);
}

// ---- Sign-in ------------------------------------------------------------------------------------

async function afterSignIn(tokens, email, roundTrip) {
  saveSession(SESSION_KEY, tokens, email);
  enter(tokens, email);
  hood.record("POST", "cognito-idp · InitiateAuth", 200, roundTrip, "signed in");
  hood.trace(stepsFor("signin"));
  location.hash = "#dashboard";
  route();
  await checkpoint("login", "login");
  await loadAccount();
}

// A refresh resumes the tab's sign-in. The login checkpoint scored the real sign-in already.
async function resume({ tokens, email }) {
  enter(tokens, email);
  route();
  await loadAccount();
}

function enter(tokens, email) {
  state.tokens = tokens;
  state.email = email;
  state.sessionId = newId();
  $("app").dataset.signedIn = "true";
  $("password").value = "";
  $("new-password").value = "";
  $("new-password-row").hidden = true;

  const name = email.split("@")[0].replace(/[._-]+/g, " ");
  $("who").textContent = email;
  $("avatar").textContent = name[0]?.toUpperCase() ?? "?";
  const hour = new Date().getHours();
  $("greeting").textContent = `${hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening"}, ${name}`;
  $("account-number").textContent = accountNumber(subject());
}

async function timed(promise) {
  const started = performance.now();
  const value = await promise;
  return [value, performance.now() - started];
}

function signOut() {
  clearSession(SESSION_KEY);
  state.tokens = null;
  state.watching = false;
  $("notices").replaceChildren();
  state.lastConfirmBehaviour = null;
  $("app").dataset.signedIn = "false";
  resetTransfer();
  location.hash = "";
  route();
  status("You've signed out.");
}

// ---- Wiring -------------------------------------------------------------------------------------

function setHood(open) {
  $("hood").hidden = !open;
  $("hood-toggle").setAttribute("aria-pressed", String(open));
  store("bfd-hood", open);
}

function wire() {
  attachCapture();
  wireTooltips();

  $("signin-form").addEventListener(
    "submit",
    guard(async () => {
      const email = $("email").value.trim();
      status("Signing in…");
      try {
        const [tokens, roundTrip] = await timed(signInWithPassword(email, $("password").value, $("new-password").value));
        await afterSignIn(tokens, email, roundTrip);
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
      if (!email) throw new Error("Enter your email address first");
      status("Waiting for your passkey…");
      const [tokens, roundTrip] = await timed(signInWithPasskey(email));
      await afterSignIn(tokens, email, roundTrip);
    }),
  );

  $("signout").addEventListener("click", signOut);

  $("payee-form").addEventListener(
    "submit",
    guard(async () => {
      const name = $("payee-name").value.trim();
      const account = $("payee-account").value.replace(/\s+/g, "");
      const ifsc = $("payee-ifsc").value.trim().toUpperCase();
      fieldError("payee-error", !name ? "Enter the beneficiary's name" : null);
      fieldError("payee-error", /^\d{9,18}$/.test(account) ? null : "Account numbers have 9 to 18 digits");
      fieldError("payee-error", /^[A-Z]{4}0[A-Z0-9]{6}$/.test(ifsc) ? null : "IFSC codes look like BFDB0001234");
      state.draft = { ...state.draft, name, account, ifsc };
      await checkpoint("payee", "payee");
      $("amount-to").textContent = `To ${name}, A/c ••${account.slice(-4)}`;
      step("amount");
    }),
  );

  $("amount-form").addEventListener(
    "submit",
    guard(async () => {
      const amount = Number($("amount").value.replaceAll(",", ""));
      fieldError("amount-error", amount > 0 && Number.isFinite(amount) ? null : "Enter an amount above zero");
      fieldError("amount-error", Math.round(amount * 100) === amount * 100 ? null : "Use at most two decimal places");
      fieldError(
        "amount-error",
        state.balance === null || amount <= state.balance ? null : `Your available balance is ${rupees(state.balance)}`,
      );
      state.draft = { ...state.draft, amount };
      await checkpoint("amount", "amount", await draftTransaction());
      $("review-name").textContent = state.draft.name;
      $("review-account").textContent = state.draft.account.replace(/(\d{4})(?=\d)/g, "$1 ");
      $("review-ifsc").textContent = state.draft.ifsc;
      $("review-amount").textContent = rupees(amount);
      step("review");
    }),
  );

  $("review-form").addEventListener(
    "submit",
    guard(async () => {
      const again = $("confirm-account").value.replace(/\s+/g, "");
      fieldError("review-error", again === state.draft.account ? null : "The account numbers don't match");
      await confirmTransfer();
    }),
  );

  for (const button of document.querySelectorAll("[data-back]")) {
    button.addEventListener("click", () => step(button.dataset.back));
  }

  $("result-stepup").addEventListener(
    "click",
    guard(async () => {
      status("Waiting for your passkey…");
      await settle(await stepUp());
    }),
  );

  $("another").addEventListener("click", resetTransfer);
  $("refresh").addEventListener("click", guard(loadAccount));

  $("register-passkey").addEventListener(
    "click",
    guard(async () => {
      status("Follow your browser's prompt to create a passkey…");
      await registerPasskey(state.tokens.AccessToken);
      status("Passkey added. You can sign in and verify with it.");
    }),
  );

  $("hood-toggle").addEventListener("click", () => setHood($("hood").hidden));
  $("hood-close").addEventListener("click", () => setHood(false));
  setHood(stored("bfd-hood", innerWidth > 1100));

  addEventListener("hashchange", () => {
    if (location.hash === "#transfer" && state.transferId && !document.querySelector('.step[data-step="done"]').hidden) {
      resetTransfer();
    }
    route();
  });
  route();
  const saved = restoreSession(SESSION_KEY);
  if (saved) guard(() => resume(saved))();
  watch();

  // The page itself came from S3 through CloudFront.
  const navigation = performance.getEntriesByType("navigation")[0];
  hood.record("GET", "/index.html", 200, navigation ? navigation.responseEnd - navigation.requestStart : null, "CloudFront → S3");
  hood.trace(stepsFor("page"));
}

wire();
