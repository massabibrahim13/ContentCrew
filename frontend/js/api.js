/**
 * The only module that talks to the Flask backend.
 *
 * Every call either returns parsed JSON or throws an ApiError whose `message`
 * is safe to show the user. Network failures and timeouts become ApiErrors too,
 * so callers never have to handle raw fetch errors.
 */

const DEFAULT_TIMEOUT_MS = 15000;

export class ApiError extends Error {
  constructor(message, { status = 0, code = "error", fields = {}, retryable = false } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.fields = fields;
    this.retryable = retryable;
  }
}

async function request(path, { method = "GET", body, timeout = DEFAULT_TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeout);

  let response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers: body === undefined ? { Accept: "application/json" }
                                  : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (err) {
    const timedOut = err && err.name === "AbortError";
    throw new ApiError(
      timedOut ? "The server took too long to respond. Try again."
               : "Can't reach the ContentCrew server. Check that it's running, then try again.",
      { code: timedOut ? "timeout" : "network_error", retryable: true },
    );
  } finally {
    window.clearTimeout(timer);
  }

  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }

  if (!response.ok) {
    const error = (data && data.error) || {};
    throw new ApiError(error.message || `The request failed (HTTP ${response.status}).`, {
      status: response.status,
      code: error.code || "http_error",
      fields: error.fields || {},
      retryable: Boolean(error.retryable) || response.status >= 500,
    });
  }
  return data;
}

export const api = {
  getContext: () => request("/context"),
  saveContext: (context) => request("/context", { method: "PUT", body: context }),
  getSampleContext: () => request("/context/sample"),

  sendMessage: (message, sessionId) =>
    request("/chat", { method: "POST", body: sessionId ? { message, session_id: sessionId } : { message } }),
  getSession: (sessionId) => request(`/sessions/${encodeURIComponent(sessionId)}`),
  retrySession: (sessionId) => request(`/sessions/${encodeURIComponent(sessionId)}/retry`, { method: "POST", body: {} }),
  getStatus: (sessionId) => request(`/status?session_id=${encodeURIComponent(sessionId)}`),
  getHealth: () => request("/health"),

  decideResearch: (sessionId, decision, feedback) =>
    request("/approval/research", { method: "POST", body: { session_id: sessionId, decision, feedback } }),
  decideContent: (sessionId, decision, feedback) =>
    request("/approval/content", { method: "POST", body: { session_id: sessionId, decision, feedback } }),

  getBlog: (blogId) => request(`/blog/${encodeURIComponent(blogId)}`),
  updateBlog: (blogId, changes) => request(`/blog/${encodeURIComponent(blogId)}`, { method: "PATCH", body: changes }),
};

/**
 * Live events for one session (Server-Sent Events).
 * The browser reconnects by itself and resumes from the last event id.
 *
 *   const stream = openEventStream(id, lastId, { onEvent, onConnectionChange });
 *   stream.close();
 */
export function openEventStream(sessionId, afterId, { onEvent, onConnectionChange }) {
  const url = `/api/sessions/${encodeURIComponent(sessionId)}/events?after=${Number(afterId) || 0}`;
  const source = new EventSource(url);
  let connected = false;

  source.onopen = () => {
    connected = true;
    onConnectionChange?.("open");
  };
  source.onmessage = (message) => {
    let event;
    try {
      event = JSON.parse(message.data);
    } catch {
      return; // ignore malformed frames rather than breaking the timeline
    }
    onEvent(event);
  };
  source.onerror = () => {
    // readyState CONNECTING means the browser is retrying on its own.
    if (source.readyState === EventSource.CLOSED) onConnectionChange?.("closed");
    else if (connected) onConnectionChange?.("reconnecting");
    connected = false;
  };

  return { close: () => source.close() };
}
