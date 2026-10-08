// A sign-in that survives a page refresh but not closing the tab. Only the access token is kept:
// the pages use nothing else, and without the refresh token a stolen copy dies within the hour.

// Restored only with this much life left, so a call made right after a refresh does not fail.
export const MARGIN_MS = 60_000;

export function tokenExpiry(accessToken) {
  try {
    const part = accessToken.split(".")[1].replaceAll("-", "+").replaceAll("_", "/");
    return JSON.parse(atob(part)).exp * 1000 || 0;
  } catch {
    return 0;
  }
}

export function saveSession(key, tokens, email) {
  try {
    sessionStorage.setItem(key, JSON.stringify({ tokens: { AccessToken: tokens.AccessToken }, email }));
  } catch {
    // Blocked storage just means signing in again after a refresh.
  }
}

export function restoreSession(key, now = Date.now()) {
  try {
    const saved = JSON.parse(sessionStorage.getItem(key));
    if (saved?.tokens?.AccessToken && tokenExpiry(saved.tokens.AccessToken) - now > MARGIN_MS) return saved;
    sessionStorage.removeItem(key);
  } catch {
    // Unreadable or unavailable: treat as signed out.
  }
  return null;
}

export function clearSession(key) {
  try {
    sessionStorage.removeItem(key);
  } catch {
    // Nothing to clear.
  }
}
