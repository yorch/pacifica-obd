// SSE client built on fetch + ReadableStream.
//
// We deliberately do *not* use EventSource: it cannot send an Authorization
// header (or a body), and this app authenticates every /api call. The parser
// handles complete SSE frames, tracks the last event id, and reconnects with
// backoff so a dropped stream resumes instead of silently ending.

import { getToken } from "./api.js";

export function openStream(path, { onEvent, onOpen, onError, onClose, retries = 4 } = {}) {
  let controller = new AbortController();
  let lastEventId = null;
  let attempt = 0;
  let closed = false;
  let timer = null;

  async function run() {
    const headers = { Accept: "text/event-stream" };
    if (getToken()) headers.Authorization = `Bearer ${getToken()}`;
    if (lastEventId) headers["Last-Event-ID"] = lastEventId;
    let response;
    try {
      response = await fetch(path, { headers, signal: controller.signal });
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
    let event = "message";
    const dataLines = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith(":")) continue;            // heartbeat/comment
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      else if (line.startsWith("id:")) lastEventId = line.slice(3).trim();
    }
    if (event === "closed") { close(); if (onClose) onClose("closed"); return; }
    if (!dataLines.length) return;
    let data = null;
    try { data = JSON.parse(dataLines.join("\n")); } catch { data = dataLines.join("\n"); }
    if (event === "done") { close(); if (onClose) onClose(data?.reason || "done"); return; }
    if (onEvent) onEvent(event, data, lastEventId);
  }

  function scheduleRetry(error) {
    if (closed) return;
    attempt += 1;
    if (attempt > retries) return fail(error);
    const delay = Math.min(4000, 400 * attempt);
    if (onError) onError(error, attempt);
    timer = setTimeout(() => { if (!closed) run(); }, delay);
  }

  function fail(error) {
    close();
    if (onError) onError(error, 0);
  }

  function close() {
    if (closed) return;
    closed = true;
    clearTimeout(timer);
    try { controller.abort(); } catch { /* ignore */ }
  }

  run();
  return { close, get closed() { return closed; } };
}
