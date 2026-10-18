// Minimal DOM helpers. One seam, no framework.
//
// SAFETY: `h()` only ever sets text nodes for string children and never touches
// innerHTML, so a value that came off the vehicle's CAN bus (which is untrusted
// input) can never inject markup. Everything the UI renders goes through here.

const SVG_NS = "http://www.w3.org/2000/svg";

export function h(tag, props, ...children) {
  const [name, ...classes] = String(tag).split(".");
  const svg = name === "svg" || name === "path" || name === "circle" || name === "g"
    || name === "text" || name === "line";
  const node = svg ? document.createElementNS(SVG_NS, name) : document.createElement(name);
  if (classes.length) node.setAttribute("class", classes.join(" "));

  if (props && (typeof props !== "object" || props instanceof Node || Array.isArray(props))) {
    children.unshift(props);
    props = null;
  }
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class" || key === "className") {
      node.setAttribute("class", [...classes, value].join(" ").trim());
    } else if (key === "dataset") {
      for (const [dk, dv] of Object.entries(value)) node.dataset[dk] = dv;
    } else if (key === "style" && typeof value === "object") {
      for (const [sk, sv] of Object.entries(value)) {
        if (sk.startsWith("--")) node.style.setProperty(sk, sv);
        else node.style[sk] = sv;
      }
    } else if (key === "on" && typeof value === "object") {
      for (const [ev, fn] of Object.entries(value)) node.addEventListener(ev, fn);
    } else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "text" || key === "textContent") {
      node.textContent = String(value);
    } else if (key === "html") {
      throw new Error("h(): 'html' is not allowed (untrusted bus data)");
    } else if (key === "value") {
      node.value = value;
    } else if (value === true) {
      node.setAttribute(key, "");
    } else {
      node.setAttribute(key, String(value));
    }
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) append(node, child);
    else if (child instanceof Node) node.appendChild(child);
    else node.appendChild(document.createTextNode(String(child)));
  }
}

export function frag(...children) {
  const f = document.createDocumentFragment();
  append(f, children);
  return f;
}

export function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function mount(root, ...children) {
  clear(root);
  append(root, children);
  return root;
}

export function on(node, event, fn, options) {
  node.addEventListener(event, fn, options);
  return node;
}

export function $ (selector, root = document) { return root.querySelector(selector); }
export function $$ (selector, root = document) { return [...root.querySelectorAll(selector)]; }

// A tiny keyed-list renderer: reuses existing rows when the key is unchanged.
export function list(container, items, keyFn, renderFn) {
  const existing = new Map(
    [...container.children].filter((c) => c.dataset.key).map((c) => [c.dataset.key, c]),
  );
  const next = [];
  for (const item of items) {
    const key = String(keyFn(item));
    let node = existing.get(key);
    if (node) existing.delete(key);
    else node = renderFn(item);
    node.dataset.key = key;
    next.push(node);
  }
  clear(container);
  for (const node of next) container.appendChild(node);
  return container;
}

export function debounce(fn, ms = 200) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

export function downloadBlob(name, blob) {
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export function download(name, text, type = "text/plain") {
  downloadBlob(name, new Blob([text], { type }));
}
