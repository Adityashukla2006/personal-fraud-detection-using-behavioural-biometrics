// Cognito sign-in and passkeys, called directly: no SDK. Shared by the banking client and the
// operator console so both authenticate exactly the same way.

import config from "./config.mjs";

export class NewPasswordRequired extends Error {
  constructor() {
    super("Choose a new password (12+ characters, upper and lower case, a number), then sign in again");
    this.name = "NewPasswordRequired";
  }
}

export function newId() {
  return crypto.randomUUID().replaceAll("-", "");
}

export async function cognito(target, body) {
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

export function requestOptions(options) {
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

export function credentialJson(credential) {
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

// Accounts are admin-created with a temporary password, which must be replaced on first sign-in.
export async function signInWithPassword(email, password, newPassword = "") {
  const result = await cognito("InitiateAuth", {
    AuthFlow: "USER_AUTH",
    ClientId: config.clientId,
    AuthParameters: { USERNAME: email, PREFERRED_CHALLENGE: "PASSWORD", PASSWORD: password },
  });
  if (result.AuthenticationResult) return result.AuthenticationResult;

  if (result.ChallengeName === "NEW_PASSWORD_REQUIRED") {
    if (!newPassword) throw new NewPasswordRequired();
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

export async function signInWithPasskey(email) {
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

export async function registerPasskey(accessToken) {
  const { CredentialCreationOptions } = await cognito("StartWebAuthnRegistration", { AccessToken: accessToken });
  const credential = await navigator.credentials.create({ publicKey: creationOptions(CredentialCreationOptions) });
  await cognito("CompleteWebAuthnRegistration", { AccessToken: accessToken, Credential: credentialJson(credential) });
}
