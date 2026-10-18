// Hash router: `#/live`, `#/db?table=dtcs`. Deep links and the back button work
// with zero framework; the server only ever needs to serve index.html at "/".

export function parseHash(hash = location.hash) {
  const raw = (hash || "").replace(/^#/, "");
  const [pathPart, queryPart] = raw.split("?");
  const path = `/${(pathPart || "").replace(/^\/+/, "")}`.replace(/\/+$/, "") || "/";
  return { path, params: new URLSearchParams(queryPart || "") };
}

export function navigate(path, params) {
  const search = params ? `?${new URLSearchParams(params).toString()}` : "";
  location.hash = `#${path}${search}`;
}

export function onRoute(handler) {
  const run = () => handler(parseHash());
  window.addEventListener("hashchange", run);
  run();
  return () => window.removeEventListener("hashchange", run);
}

export function href(path, params) {
  const search = params ? `?${new URLSearchParams(params).toString()}` : "";
  return `#${path}${search}`;
}
