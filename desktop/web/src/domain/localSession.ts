/** In-memory CSRF only; bootstrap fragments are removed before any request. */
let csrfToken = "";
let sessionReady: Promise<void> | undefined;

export function initializeLocalSession(): Promise<void> {
  if (sessionReady) return sessionReady;
  sessionReady = (async () => {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const bootstrap = fragment.get("ml-bootstrap");
    if (bootstrap !== null) {
      history.replaceState(null, "", window.location.pathname + window.location.search);
      const response = await fetch("/api/local-session/bootstrap", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ bootstrap }),
      });
      if (!response.ok) throw new Error("本机引导已过期或已使用，请通过快捷入口重新打开。");
    }
    const response = await fetch("/api/local-session", { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error("请通过本机MarketListener快捷入口打开页面，建立安全会话。");
    const session = await response.json() as { audience: string; csrfToken: string };
    if (session.audience !== "product" || !session.csrfToken) throw new Error("本机会话权限域无效，请重新打开。");
    csrfToken = session.csrfToken;
  })().catch((error: unknown) => { sessionReady = undefined; csrfToken = ""; throw error; });
  return sessionReady;
}

export async function authorizedFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  await initializeLocalSession();
  const headers = new Headers(init.headers);
  if (!["GET", "HEAD"].includes((init.method ?? "GET").toUpperCase())) headers.set("X-MarketListener-CSRF", csrfToken);
  const response = await fetch(input, { ...init, headers, credentials: "same-origin" });
  if (response.status === 401) { sessionReady = undefined; csrfToken = ""; }
  return response;
}
