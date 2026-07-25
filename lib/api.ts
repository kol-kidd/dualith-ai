/**
 * Single entry point for talking to the Dualith backend.
 *
 * The backend requires `X-Dualith-Token` on every mutating call and on the
 * WebSocket handshake. The token is issued per server run and fetched once
 * from `/api/setup/status`; holding it in a module-level variable keeps every
 * call site from having to thread it through props.
 */

let sessionToken = "";

/** Store the token read from `/api/setup/status`. */
export function setSessionToken(token: string): void {
  sessionToken = token ?? "";
}

export function getSessionToken(): string {
  return sessionToken;
}

/**
 * Drop-in replacement for `fetch` that attaches the session token.
 *
 * Takes a fully-built URL so existing call sites keep their shape.
 */
export function apiFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (sessionToken) headers.set("X-Dualith-Token", sessionToken);
  return fetch(url, { ...init, headers });
}

/** WebSocket URL carrying the token — headers aren't available on a WS handshake. */
export function socketUrl(base: string): string {
  return sessionToken
    ? `${base}/ws?token=${encodeURIComponent(sessionToken)}`
    : `${base}/ws`;
}
