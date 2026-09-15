import config from "./config.mjs";
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

function newId() {
  return crypto.randomUUID().replaceAll("-", "");
}

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

// ---- Cognito, called directly: no SDK -----------------------------------------------------------

async function cognito(target, body) {
  const response = await fetch(`https://cognito-idp.${config.region}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.message || data.__type || `${target} failed`);
  return data;
}

const toBuffer = (value) => {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0)).buffer;
};

const toBase64Url = (buffer) =>
  btoa(String.fromCharCode(...new Uint8Array(buffer)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");

function requestOptions(options) {
  return {
    ...options,
    challenge: toBuffer(options.challenge),
    allowCredentials: (options.allowCredentials ?? []).map((c) => ({ ...c, id: toBuffer(c.id) })),
  };
}

function creationOptions(options) {
  return {
    ...options,
    challenge: toBuffer(options.challenge),
    user: { ...options.user, id: toBuffer(options.user.id) },
    excludeCredentials: (options.excludeCredentials ?? []).map((c) => ({ ...c, id: toBuffer(c.id) })),
  };
}

function credentialJson(credential) {
  if (typeof credential.toJSON === "function") return credential.toJSON();
  const response = credential.response;
  const encoded = {};
  for (const name of ["clientDataJSON", "authenticatorData", "signature", "userHandle", "attestationObject"]) {
    if (response[name]) encoded[name] = toBase64Url(response[name]);
  }
  if (typeof response.getTransports === "function") encoded.transports = response.getTransports();
  return {
    id: credential.id,
    rawId: toBase64Url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment ?? undefined,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: encoded,
  };
}

async function signInWithPassword(email, password) {
  const result = await cognito("InitiateAuth", {
    AuthFlow: "USER_AUTH",
    ClientId: config.clientId,
    AuthParameters: { USERNAME: email, PREFERRED_CHALLENGE: "PASSWORD", PASSWORD: password },
  });
  if (result.AuthenticationResult) return result.AuthenticationResult;

  // Accounts are admin-created with a temporary password, which must be replaced on first sign-in.
  if (result.ChallengeName === "NEW_PASSWORD_REQUIRED") {
    const newPassword = $("new-password").value;
    if (!newPassword) {
      $("new-password-row").hidden = false;
      throw new Error("Choose a new password (12+ characters, upper and lower case, a number), then sign in again");
    }
    const done = await cognito("RespondToAuthChallenge", {
      ClientId: config.clientId,
      ChallengeName: "NEW_PASSWORD_REQUIRED",
      Session: result.Session,
      ChallengeResponses: { USERNAME: email, NEW_PASSWORD: newPassword },
    });
    return done.AuthenticationResult;
  }
  throw new Error(`Unsupported challenge ${result.ChallengeName}`);
}

async function signInWithPasskey(email) {
  const start = await cognito("InitiateAuth", {
    AuthFlow: "USER_AUTH",
    ClientId: config.clientId,
    AuthParameters: { USERNAME: email, PREFERRED_CHALLENGE: "WEB_AUTHN" },
  });
  if (start.ChallengeName !== "WEB_AUTHN") throw new Error("No passkey is registered for this account");
  const options = JSON.parse(start.ChallengeParameters.CREDENTIAL_REQUEST_OPTIONS);
  const credential = await navigator.credentials.get({ publicKey: requestOptions(options) });
  const done = await cognito("RespondToAuthChallenge", {
    ClientId: config.clientId,
    ChallengeName: "WEB_AUTHN",
    Session: start.Session,
    ChallengeResponses: { USERNAME: email, CREDENTIAL: JSON.stringify(credentialJson(credential)) },
  });
  return done.AuthenticationResult;
}

async function registerPasskey() {
  const accessToken = state.tokens.AccessToken;
  const { CredentialCreationOptions } = await cognito("StartWebAuthnRegistration", { AccessToken: accessToken });
  const credential = await navigator.credentials.create({ publicKey: creationOptions(CredentialCreationOptions) });
  await cognito("CompleteWebAuthnRegistration", { AccessToken: accessToken, Credential: credentialJson(credential) });
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
      await afterSignIn(await signInWithPassword(email, $("password").value), email);
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
      await registerPasskey();
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
      status(`Transfer ${decision.action === "allow" || decision.action === "monitor" ? "accepted" : "held: " + decision.action.replace("_", " ")}.`);
      // The next transfer is a new scoring session.
      state.sessionId = newId();
    }),
  );
}

wire();
