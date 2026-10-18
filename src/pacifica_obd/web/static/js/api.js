// API client: same-origin JSON over fetch, with the error envelope decoded into
// a typed error and the auth token held in memory only (§4.6).

const TOKEN_KEY = "pacifica-obd.token";
let token = null;

// The token arrives in the URL *fragment* (`#t=…`): never sent to the server,
// never logged, never a Referer. We read it once, move it into sessionStorage
// and strip it from the address bar.
export function ingestTokenFromFragment() {
  const match = (location.hash || "").match(/[#&]t=([^&]+)/);
  if (match) {
    token = decodeURIComponent(match[1]);
    try { sessionStorage.setItem(TOKEN_KEY, token); } catch { /* private mode */ }
    const clean = location.hash.replace(/[#&]t=[^&]+/, "");
    history.replaceState(null, "", location.pathname + location.search + clean);
    return token;
  }
  try { token = sessionStorage.getItem(TOKEN_KEY); } catch { token = null; }
  return token;
}

export function getToken() { return token; }
export function setToken(value) {
  token = value || null;
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* ignore */ }
}

export class ApiError extends Error {
  constructor({ code, kind, message, status, detail }, httpStatus) {
    super(message || "request failed");
    this.name = "ApiError";
    this.code = code || "internal";
    this.kind = kind || "internal";
    this.status = status ?? null;
    this.detail = detail || {};
    this.httpStatus = httpStatus;
  }
}

function headers(extra = {}) {
  const out = { "X-Pacifica-Request": "1", ...extra };
  if (token) out.Authorization = `Bearer ${token}`;
  return out;
}

async function request(method, path, { body, signal, raw = false } = {}) {
  const init = { method, headers: headers(body ? { "Content-Type": "application/json" } : {}), signal };
  if (body !== undefined) init.body = JSON.stringify(body);
  const response = await fetch(path, init);
  if (raw) {
    // Even a blob/CSV download must fail loudly rather than saving an error body.
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      await throwFromBody(text, response);
    }
    return response;
  }
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = null; }
  if (!response.ok) await throwFromBody(text, response, payload);
  return payload;
}

async function throwFromBody(text, response, payload = null) {
  let decoded = payload;
  if (decoded === null) {
    try { decoded = text ? JSON.parse(text) : null; } catch { decoded = null; }
  }
  if (decoded && decoded.error) throw new ApiError(decoded.error, response.status);
  throw new ApiError({ code: "http_error", kind: "internal",
                       message: text || response.statusText }, response.status);
}

export const get = (path, options) => request("GET", path, options);
export const post = (path, body, options) => request("POST", path, { ...options, body });
export const rawResponse = (path, options) => request("GET", path, { ...options, raw: true });

export function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

// Convenience wrappers for the read endpoints the views use.
export const api = {
  health: () => get("/api/health"),
  status: () => get("/api/status"),
  ports: () => get("/api/ports"),
  connect: (body) => post("/api/connect", body || {}),
  disconnect: () => post("/api/disconnect", {}),
  info: () => get("/api/info"),
  vin: () => post("/api/vin", {}),
  bus: (bus) => post("/api/bus", { bus }),
  jobs: () => get("/api/jobs"),
  job: (id) => get(`/api/jobs/${id}`),
  cancelJob: (id) => post(`/api/jobs/${id}/cancel`, {}),

  obdDtc: () => get("/api/obd/dtc"),
  obdLive: () => get("/api/obd/live"),
  obdPids: () => get("/api/obd/pids"),
  obdReadiness: () => get("/api/obd/readiness"),
  obdFreeze: () => get("/api/obd/freeze"),
  obdScan: () => post("/api/obd/scan", {}),

  udsModules: (body) => post("/api/uds/modules", body),
  udsDtcAll: (body) => post("/api/uds/dtc-all", body),
  udsReadDid: (body) => post("/api/uds/read-did", body),
  udsModuleInfo: (body) => post("/api/uds/module-info", body),
  udsCapture: (body) => post("/api/uds/capture", body),
  moduleMap: () => get("/api/uds/module-map"),
  knownModules: (bus) => get(`/api/uds/modules/known${query({ bus })}`),
  datasetInfo: () => get("/api/dataset/info"),
  ecus: () => get("/api/uds/ecus"),
  dids: (ecu) => get(`/api/uds/dids${query({ ecu })}`),
  adaptations: (ecu) => get(`/api/uds/adaptations${query({ ecu })}`),

  watchStart: (body) => post("/api/live/watch/start", body),
  monitorStart: (body) => post("/api/uds/monitor/start", body),
  streamStop: (stream_id) => post("/api/live/watch/stop", { stream_id }),
  streams: () => get("/api/live/streams"),

  dbSchema: () => get("/api/db/schema"),
  dbTable: (table, params) => get(`/api/db/${table}${query(params)}`),
  dbSession: (id) => get(`/api/db/sessions/${id}`),
  dbVehicleSummary: (vin) => get(`/api/db/vehicles/${encodeURIComponent(vin)}/summary`),
  dbSeries: (params) => get(`/api/db/samples/series${query(params)}`),

  reports: () => get("/api/reports"),
  reportDump: (body) => post("/api/reports/dump", body),
  reportFromSession: (body) => post("/api/reports/from-session", body),
  reportMeta: (id) => get(`/api/reports/${id}`),
  reportCompare: (a, b) => post("/api/reports/compare", { a, b }),
};
