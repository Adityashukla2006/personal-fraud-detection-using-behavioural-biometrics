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

const $ = (id) => document.getElementById(id);

const state = {
  tokens: null,
  email: null,
  sessionId: null,
  recorders: new Map(),
  deviceId: loadDeviceId(),
};

function loadDeviceId() {
  try {
    let id = localStorage.getItem("bfd-device");
    if (!id) {
      id = newId();
      localStorage.setItem("bfd-device", id);
    }
    return id;
  } catch {
    return newId();
  }
}

function status(message, isError = false) {
  const element = $("status");
  element.textContent = message;
  element.classList.toggle("error", isError);
}

// ---- Capture wiring -----------------------------------------------------------------------------

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

// ---- Scoring ------------------------------------------------------------------------------------

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function transaction() {
  const amount = Number($("amount").value);
  const payee = $("payee").value.trim();
  if (!payee || !(amount > 0)) return null;
  // The payee account is hashed here; the raw number never leaves the browser.
  return { payee_id: await sha256Hex(payee), amount };
}

async function checkpoint(name, group, details = null) {
  const body = {
    session_id: state.sessionId,
    checkpoint: name,
    device: deviceDescriptor(state.deviceId),
    behaviour: takeBehaviour(group),
  };
  if (details) body.transaction = details;

  const started = performance.now();
  const response = await fetch(`${config.apiUrl}/score`, {
    method: "POST",
    headers: { Authorization: `Bearer ${state.tokens.AccessToken}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const roundTrip = performance.now() - started;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || data.message || `Scoring failed (${response.status})`);
  renderDecision(data, roundTrip, response.headers.get("server-timing"));
  return data;
}

function renderDecision(decision, roundTrip, serverTiming) {
  const handlerMs = /dur=([\d.]+)/.exec(serverTiming ?? "")?.[1];
  $("decision").hidden = false;
  $("decision-action").textContent = decision.action.replace("_", " ");
  $("decision-action").dataset.action = decision.action;
  $("decision-checkpoint").textContent = decision.checkpoint;
  $("decision-risk").textContent = decision.risk === null ? "n/a" : decision.risk.toFixed(1);
  $("decision-confidence").textContent = `${Math.round(decision.confidence * 100)}%`;
  $("decision-latency").textContent =
    `${roundTrip.toFixed(0)} ms round trip` + (handlerMs ? `, ${handlerMs} ms in handler` : "");
  $("decision-constraints").textContent = decision.constraints.join(", ") || "none";

  const list = $("decision-contributions");
  list.replaceChildren(
    ...decision.contributions.map(({ channel, value }) => {
      const item = document.createElement("li");
      item.textContent = `${channel} ${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
      return item;
    }),
  );
}

// ---- Transfers and step-up ----------------------------------------------------------------------

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function api(method, path, body) {
  const response = await fetch(`${config.apiUrl}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${state.tokens.AccessToken}`,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  return { ok: response.ok, status: response.status, data: await response.json().catch(() => ({})) };
}

// Statuses the workflow is still moving through; anything else is where the transfer rests.
const IN_FLIGHT = new Set(["pending", "processing", "awaiting_step_up"]);

async function pollTransfer(transferId, attempts = 20) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const { ok, data } = await api("GET", `/transfers/${transferId}`);
    if (ok && !IN_FLIGHT.has(data.status)) return data;
    await sleep(750);
  }
  throw new Error("The transfer is still processing. Check again shortly.");
}

async function stepUp(transferId) {
  // The workflow records the hold a moment after scoring returns, so the first attempts may arrive
  // before there is anything to verify.
  let start;
  for (let attempt = 0; attempt < 10; attempt += 1) {
    start = await api("POST", `/transfers/${transferId}/stepup`);
    const notReady = start.status === 404 || (start.status === 409 && start.data.status === "pending");
    if (!notReady) break;
    await sleep(500);
  }
  if (!start.ok) throw new Error(start.data.error || "Step-up could not start");

  const credential = await navigator.credentials.get({ publicKey: requestOptions(start.data.options) });
  const verify = await api("POST", `/transfers/${transferId}/stepup/verify`, {
    session: start.data.session,
    credential: credentialJson(credential),
  });
  if (!verify.ok) throw new Error(verify.data.error || "Step-up failed");
  return pollTransfer(transferId);
}

function showTransfer(status, balance) {
  $("transfer-state").hidden = false;
  $("transfer-status").textContent = status.replaceAll("_", " ");
  $("transfer-status").dataset.status = status;
  $("transfer-balance").textContent =
    balance === undefined || balance === null ? "" : `Balance ${Number(balance).toLocaleString()}`;
}

async function handleTransfer(transfer) {
  if (!transfer) return;
  showTransfer(transfer.status);
  if (transfer.status === "failed") throw new Error("The transfer could not be started. Nothing was debited.");
  if (transfer.status === "awaiting_step_up") {
    $("stepup").hidden = false;
    $("stepup").dataset.transferId = transfer.transfer_id;
    status("This transfer is held until you verify with your passkey.");
    return;
  }
  const settled = await pollTransfer(transfer.transfer_id);
  showTransfer(settled.status, settled.balance);
  status(`Transfer ${settled.status.replaceAll("_", " ")}.`);
}

// ---- UI -----------------------------------------------------------------------------------------

async function afterSignIn(tokens, email) {
  state.tokens = tokens;
  state.email = email;
  state.sessionId = newId();
  $("signin").hidden = true;
  $("account").hidden = false;
  $("who").textContent = email;
  $("password").value = "";
  $("new-password").value = "";
  $("new-password-row").hidden = true;
  await checkpoint("login", "login");
  status("Signed in.");
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

function wire() {
  attachCapture();

  $("signin-form").addEventListener(
    "submit",
    guard(async () => {
      const email = $("email").value.trim();
      status("Signing in...");
      try {
        const tokens = await signInWithPassword(email, $("password").value, $("new-password").value);
        await afterSignIn(tokens, email);
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

  $("register-passkey").addEventListener(
    "click",
    guard(async () => {
      status("Follow your browser's prompt to create a passkey...");
      await registerPasskey(state.tokens.AccessToken);
      status("Passkey added. You can sign in with it next time.");
    }),
  );

  $("signout").addEventListener("click", () => {
    state.tokens = null;
    $("account").hidden = true;
    $("decision").hidden = true;
    $("signin").hidden = false;
    status("Signed out.");
  });

  $("payee").addEventListener("change", guard(() => checkpoint("payee", "payee")));
  $("amount").addEventListener(
    "change",
    guard(async () => {
      const details = await transaction();
      if (details) await checkpoint("amount", "amount", details);
    }),
  );

  $("transfer-form").addEventListener(
    "submit",
    guard(async () => {
      const details = await transaction();
      if (!details) throw new Error("Enter a payee account and a positive amount");
      const decision = await checkpoint("confirmation", "amount", details);
      // The next transfer is a new scoring session.
      state.sessionId = newId();
      await handleTransfer(decision.transfer);
    }),
  );

  $("stepup").addEventListener(
    "click",
    guard(async () => {
      status("Waiting for your passkey...");
      const settled = await stepUp($("stepup").dataset.transferId);
      $("stepup").hidden = true;
      showTransfer(settled.status, settled.balance);
      status(`Transfer ${settled.status.replaceAll("_", " ")}.`);
    }),
  );
}

wire();
