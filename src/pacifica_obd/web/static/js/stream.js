// SSE client built on fetch + ReadableStream.
//
// We deliberately do *not* use EventSource: it cannot send an Authorization
// header, a custom header, or a POST body, and this app authenticates every
// /api call. The parser handles complete SSE frames, tracks the last event id,
// and reconnects with backoff so a dropped stream resumes instead of silently
// ending.
//
// `openStream` is the GET form (live data); `postStream` is the POST form used
// by the assistant, whose request body carries the prompt. A POST stream is
// never retried automatically: replaying a chat turn would duplicate a model
// call (and its cost), so a failure surfaces to the caller instead.

import { getToken } from "./api.js";

function authHeaders() {
  const headers = { Accept: "text/event-stream", "X-Pacifica-Request": "1" };
  if (getToken()) headers.Authorization = `Bearer ${getToken()}`;
  return headers;
}

function parseFrame(frame, state) {
  let event = "message";
  const dataLines = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue;            // heartbeat/comment
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    else if (line.startsWith("id:")) state.lastEventId = line.slice(3).trim();
  }
  return { event, data: dataLines };
}

function run({ method, path, body, onEvent, onOpen, onError, onClose, retries, headers }) {
  let controller = new AbortController();
  const state = { lastEventId: null };
  let attempt = 0;
  let closed = false;
  let timer = null;

  async function go() {
    const requestHeaders = { ...authHeaders(), ...(headers || {}) };
    if (body !== undefined) requestHeaders["Content-Type"] = "application/json";
    if (state.lastEventId) requestHeaders["Last-Event-ID"] = state.lastEventId;
    const init = { method, headers: requestHeaders, signal: controller.signal };
    if (body !== undefined) init.body = JSON.stringify(body);
    let response;
    try {
      response = await fetch(path, init);
    } catch (error) {
      if (closed) return;
      return scheduleRetry(error);
    }
    if (!response.ok || !response.body) {
      const text = await response.text().catch(() => "");
      return fail(new Error(text || `stream failed (${response.status})`));
    }
    attempt = 0;
    if (onOpen) onOpen();
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let index;
        while ((index = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          handleFrame(frame);
        }
      }
      if (!closed) scheduleRetry(new Error("stream ended"));
    } catch (error) {
      if (!closed && error.name !== "AbortError") scheduleRetry(error);
    }
  }

  function handleFrame(frame) {
    const { event, data: dataLines } = parseFrame(frame, state);
    if (event === "closed") { close(); if (onClose) onClose("closed"); return; }
    if (!dataLines.length) return;
    let data = null;
    try { data = JSON.parse(dataLines.join("\n")); } catch { data = dataLines.join("\n"); }
    if (event === "done") { close(); if (onClose) onClose(data?.reason || "done"); return; }
    if (onEvent) onEvent(event, data, state.lastEventId);
  }

  function scheduleRetry(error) {
    if (closed || !retries) return fail(error);
    attempt += 1;
    if (attempt > retries) return fail(error);
    const delay = Math.min(4000, 400 * attempt);
    if (onError) onError(error, attempt);
    timer = setTimeout(() => { if (!closed) go(); }, delay);
  }

  function fail(error) { close(); if (onError) onError(error, 0); }

  function close() {
    if (closed) return;
    closed = true;
    clearTimeout(timer);
    try { controller.abort(); } catch { /* ignore */ }
  }

  go();
  return { close, get closed() { return closed; } };
}

export function openStream(path, handlers = {}) {
  return run({ method: "GET", path, retries: handlers.retries ?? 4, ...handlers });
}

export function postStream(path, body, handlers = {}) {
  // No automatic retry for a POST stream: a replayed turn costs tokens.
  return run({ method: "POST", path, body, retries: 0, ...handlers });
}
