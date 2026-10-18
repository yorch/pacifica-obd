/* pacifica-obd docs — self-contained behaviour layer.
 *
 * No dependencies, no network calls, works from file:// as well as from
 * GitHub Pages: theme toggle with persistence, the mobile navigation drawer,
 * the client-side search over the generated index, copy buttons, the table of
 * contents scroll-spy and the back-to-top button.
 */
(function () {
  "use strict";

  var root = document.documentElement;
  var body = document.body;
  var THEME_KEY = "pacifica-obd-theme";

  /* The docs live one directory deep, so every generated URL (which is
   * site-root relative) needs this prefix. */
  var script = document.querySelector('script[src$="site.js"]');
  var REL = script ? script.getAttribute("src").replace(/assets\/site\.js.*$/, "") : "";

  function $(selector, scope) {
    return (scope || document).querySelector(selector);
  }

  function $$(selector, scope) {
    return Array.prototype.slice.call((scope || document).querySelectorAll(selector));
  }

  function on(element, type, handler, options) {
    if (element) element.addEventListener(type, handler, options);
  }

  /* ---------------------------------------------------------------- theme --- */

  function applyTheme(theme) {
    root.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch (error) {
      /* private mode: the theme simply does not persist */
    }
    $$('meta[name="theme-color"]').forEach(function (meta) {
      meta.setAttribute("content", theme === "dark" ? "#0b0f16" : "#ffffff");
    });
    var toggle = $("[data-theme-toggle]");
    if (toggle) {
      toggle.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
      toggle.setAttribute("aria-label", "Dark theme");
      toggle.setAttribute("title", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
    }
  }

  function currentTheme() {
    return root.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }

  applyTheme(currentTheme());

  on($("[data-theme-toggle]"), "click", function () {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  });

  /* -------------------------------------------------------- mobile drawer --- */

  var navToggle = $("[data-nav-toggle]");
  var sidebar = $("[data-sidebar]");
  var scrim = $("[data-scrim]");

  function setDrawer(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("is-open", open);
    if (navToggle) navToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (scrim) scrim.hidden = !open;
    body.classList.toggle("has-drawer", open);
  }

  on(navToggle, "click", function () {
    setDrawer(!sidebar.classList.contains("is-open"));
  });
  on($("[data-nav-close]"), "click", function () {
    setDrawer(false);
  });
  on(scrim, "click", function () {
    setDrawer(false);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    if (sidebar && sidebar.classList.contains("is-open")) setDrawer(false);
  });

  /* --------------------------------------------------------------- search --- */

  var search = $("[data-search]");
  var searchPanel = $("[data-search-panel]");
  var searchInput = $("[data-search-input]");
  var searchResults = $("[data-search-results]");
  var searchHint = $("[data-search-hint]");
  var index = window.PACIFICA_SEARCH_INDEX || [];
  var flat = [];

  index.forEach(function (page) {
    var sections = page.s && page.s.length ? page.s : [{ i: "", t: page.t, x: "" }];
    sections.forEach(function (section) {
      flat.push({
        url: page.u,
        page: page.t,
        heading: page.h || page.t,
        id: section.i || "",
        title: section.t,
        text: section.x || "",
        haystack: (page.t + " " + section.t + " " + (page.h || "") + " " + (section.x || "")).toLowerCase()
      });
    });
  });

  function tokenize(query) {
    return query
      .toLowerCase()
      .split(/[^a-z0-9_+#.-]+/)
      .filter(function (token) {
        return token.length > 1 || /^[0-9a-z]$/.test(token);
      });
  }

  function countOccurrences(haystack, needle) {
    var total = 0;
    var at = haystack.indexOf(needle);
    while (at !== -1 && total < 12) {
      total += 1;
      at = haystack.indexOf(needle, at + needle.length);
    }
    return total;
  }

  function score(entry, tokens) {
    var total = 0;
    for (var i = 0; i < tokens.length; i += 1) {
      var token = tokens[i];
      var inTitle = entry.title.toLowerCase().indexOf(token) !== -1;
      var inPage = entry.page.toLowerCase().indexOf(token) !== -1;
      var occurrences = countOccurrences(entry.haystack, token);
      if (!occurrences) return 0; /* every token has to appear somewhere */
      total += inTitle ? 14 : 0;
      total += inPage ? 6 : 0;
      total += Math.min(occurrences, 8);
      if (entry.title.toLowerCase() === token) total += 20;
    }
    return total;
  }

  function escapeHtml(value) {
    return value
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function highlight(text, tokens) {
    var escaped = escapeHtml(text);
    tokens.forEach(function (token) {
      if (!token) return;
      var pattern = new RegExp("(" + escapeHtml(token).replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi");
      escaped = escaped.replace(pattern, "<mark>$1</mark>");
    });
    return escaped;
  }

  function snippet(entry, tokens) {
    var text = entry.text || "";
    var lower = text.toLowerCase();
    var at = -1;
    for (var i = 0; i < tokens.length && at === -1; i += 1) at = lower.indexOf(tokens[i]);
    if (at === -1) {
      return text.slice(0, 150) + (text.length > 150 ? "…" : "");
    }
    var start = Math.max(0, at - 60);
    var end = Math.min(text.length, at + 150);
    return (start > 0 ? "…" : "") + text.slice(start, end) + (end < text.length ? "…" : "");
  }

  function runSearch(query) {
    var tokens = tokenize(query);
    if (!tokens.length) return [];
    var results = [];
    flat.forEach(function (entry) {
      var value = score(entry, tokens);
      if (value > 0) results.push({ entry: entry, score: value });
    });
    results.sort(function (a, b) {
      return b.score - a.score || a.entry.page.localeCompare(b.entry.page);
    });
    var top = results.slice(0, 12).map(function (item) {
      return item.entry;
    });
    top.total = results.length;
    return top;
  }

  function openSearch(open) {
    if (!searchPanel || !search) return;
    search.classList.toggle("is-open", open);
    searchPanel.hidden = !open;
    var toggle = $("[data-search-toggle]");
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open && searchInput) searchInput.focus();
  }

  function renderResults(query, results) {
    if (!searchResults || !searchHint) return;
    var tokens = tokenize(query);
    if (!query.trim()) {
      searchResults.innerHTML = "";
      searchHint.textContent = "Start typing to search all " + index.length + " pages.";
      return;
    }
    if (!results.length) {
      searchResults.innerHTML = "";
      searchHint.textContent =
        "No matches for “" + query + "”. Try fewer words, a command name or a DID.";
      return;
    }
    searchResults.innerHTML = results
      .map(function (entry, position) {
        var href = REL + entry.url + (entry.id ? "#" + entry.id : "");
        return (
          '<li class="search-result" role="option" id="search-option-' + position + '" aria-selected="false">' +
          '<a href="' + escapeHtml(href) + '" data-search-result>' +
          '<span class="search-result-top">' +
          '<span class="search-result-page">' + highlight(entry.page, tokens) + "</span>" +
          (entry.title && entry.title !== entry.page
            ? '<span class="search-result-sep">›</span><span class="search-result-title">' + highlight(entry.title, tokens) + "</span>"
            : "") +
          "</span>" +
          '<span class="search-result-snippet">' + highlight(snippet(entry, tokens), tokens) + "</span>" +
          "</a></li>"
        );
      })
      .join("");
    var total = results.total || results.length;
    searchHint.textContent =
      (total > results.length
        ? "Top " + results.length + " of " + total + " matches"
        : total + (total === 1 ? " match" : " matches")) + " · ↑↓ to move · Enter to open";
  }

  var activeResult = -1;

  function moveActive(delta) {
    var items = $$(".search-result", searchResults);
    if (!items.length) return;
    if (activeResult >= 0) items[activeResult].setAttribute("aria-selected", "false");
    activeResult = (activeResult + delta + items.length) % items.length;
    items[activeResult].setAttribute("aria-selected", "true");
    searchInput.setAttribute("aria-activedescendant", items[activeResult].id);
    items[activeResult].scrollIntoView({ block: "nearest" });
  }

  if (searchInput) {
    var timer = 0;
    searchInput.addEventListener("input", function () {
      window.clearTimeout(timer);
      var value = searchInput.value;
      timer = window.setTimeout(function () {
        activeResult = -1;
        searchInput.removeAttribute("aria-activedescendant");
        renderResults(value, runSearch(value));
        searchInput.setAttribute("aria-expanded", value.trim() ? "true" : "false");
        openSearch(true);
      }, 60);
    });
    searchInput.addEventListener("focus", function () {
      renderResults(searchInput.value, runSearch(searchInput.value));
      openSearch(true);
    });
    searchInput.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        moveActive(1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        moveActive(-1);
      } else if (event.key === "Enter") {
        var items = $$(".search-result a", searchResults);
        if (items.length) {
          event.preventDefault();
          (activeResult >= 0 ? items[activeResult] : items[0]).click();
        }
      } else if (event.key === "Escape") {
        searchInput.value = "";
        renderResults("", []);
        searchInput.removeAttribute("aria-activedescendant");
        openSearch(false);
        var searchToggle = $("[data-search-toggle]");
        if (searchToggle) searchToggle.focus();
      }
    });
  }

  on($("[data-search-toggle]"), "click", function () {
    openSearch(true);
  });
  on($("[data-search-close]"), "click", function () {
    openSearch(false);
  });
  on(searchResults, "click", function (event) {
    if (event.target.closest("a")) openSearch(false);
  });

  document.addEventListener("keydown", function (event) {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openSearch(true);
    }
  });

  /* The palette backdrop is the .search element itself (see site.css). */
  document.addEventListener("click", function (event) {
    if (!search || (search.contains(event.target) && event.target !== search)) return;
    openSearch(false);
  });

  var searchKbd = $("[data-search-kbd]");
  if (searchKbd && !/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) {
    searchKbd.textContent = "Ctrl K";
  }

  /* Hide the desktop dropdown when focus leaves the search entirely. */
  on(search, "focusout", function (event) {
    if (!search.contains(event.relatedTarget) && !searchInput.value.trim()) openSearch(false);
  });

  /* ---------------------------------------------------------- copy buttons --- */

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy") ? resolve() : reject(new Error("copy failed"));
      } catch (error) {
        reject(error);
      } finally {
        document.body.removeChild(area);
      }
    });
  }

  var toast = $("[data-toast]");
  var toastTimer = 0;

  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toast.hidden = true;
    }, 2200);
  }

  $$("[data-copy-code]").forEach(function (button) {
    button.addEventListener("click", function () {
      var figure = button.closest(".code-block");
      var code = figure ? $("code", figure) : null;
      if (!code) return;
      var label = $(".copy-text", button);
      copyText(code.innerText)
        .then(function () {
          button.classList.add("is-copied");
          button.setAttribute("aria-label", "Copied");
          if (label) label.textContent = "Copied";
          showToast("Code copied to the clipboard");
          window.setTimeout(function () {
            button.classList.remove("is-copied");
            button.setAttribute("aria-label", "Copy this code block");
            if (label) label.textContent = "Copy";
          }, 1800);
        })
        .catch(function () {
          showToast("Couldn’t copy — select the code and copy it by hand");
        });
    });
  });

  /* Copy the deep link when a heading anchor is activated (no navigation jar). */
  $$(".headerlink").forEach(function (link) {
    link.addEventListener("click", function () {
      var url = window.location.href.split("#")[0] + link.getAttribute("href");
      copyText(url)
        .then(function () {
          showToast("Section link copied");
        })
        .catch(function () {
          /* copying is a convenience: the browser still navigates */
        });
    });
  });

  /* The generator emits the narrow-screen TOC before the article; it reads
   * better under the page title. */
  var mobileToc = $(".page-toc--mobile");
  var pageTitle = $(".prose h1");
  if (mobileToc && pageTitle && !pageTitle.closest(".hero")) {
    pageTitle.parentNode.insertBefore(mobileToc, pageTitle.nextSibling);
  }

  /* On narrow screens the TOC is a disclosure, collapsed by default, so a
   * thirty-entry reference page does not push the text below the fold. */
  var mobileTocTitle = mobileToc ? $(".toc-title", mobileToc) : null;
  var mobileTocList = mobileToc ? $("ul", mobileToc) : null;
  if (mobileTocTitle && mobileTocList) {
    var count = $$(":scope > li", mobileTocList).filter(function (item) {
      return !item.classList.contains("toc-level-3");
    }).length;
    var tocButton = document.createElement("button");
    tocButton.type = "button";
    tocButton.className = "toc-toggle";
    tocButton.setAttribute("aria-expanded", "false");
    mobileTocList.id = mobileTocList.id || "page-toc-mobile-list";
    tocButton.setAttribute("aria-controls", mobileTocList.id);
    tocButton.innerHTML =
      '<span>On this page</span><span class="toc-count">' + count + (count === 1 ? " section" : " sections") + "</span>";
    mobileTocTitle.textContent = "";
    mobileTocTitle.appendChild(tocButton);
    mobileToc.classList.add("is-collapsible");
    mobileTocList.hidden = true;
    tocButton.addEventListener("click", function () {
      var open = tocButton.getAttribute("aria-expanded") !== "true";
      tocButton.setAttribute("aria-expanded", open ? "true" : "false");
      mobileTocList.hidden = !open;
    });
    on(mobileTocList, "click", function (event) {
      if (!event.target.closest("a")) return;
      tocButton.setAttribute("aria-expanded", "false");
      mobileTocList.hidden = true;
    });
  }

  /* ---------------------------------------------------------------- tables --- */

  /* Wrap each table in its own scroll container so wide tables scroll inside
   * the page instead of widening it, and tall ones keep a sticky header. */
  $$(".prose table").forEach(function (table) {
    if (table.parentNode.classList.contains("table-wrap")) return;
    var wrap = document.createElement("div");
    wrap.className = "table-wrap";
    if (table.rows.length > 14) wrap.classList.add("is-tall");
    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(table);
  });

  function markScrollableTables() {
    $$(".table-wrap").forEach(function (wrap) {
      var scrolls = wrap.scrollWidth > wrap.clientWidth + 1 || wrap.scrollHeight > wrap.clientHeight + 1;
      wrap.classList.toggle("is-scrollable", scrolls);
      /* A scrollable region must be reachable from the keyboard. */
      if (scrolls) {
        wrap.setAttribute("tabindex", "0");
        wrap.setAttribute("role", "region");
        wrap.setAttribute("aria-label", "Table (scrollable)");
      } else {
        wrap.removeAttribute("tabindex");
        wrap.removeAttribute("role");
        wrap.removeAttribute("aria-label");
      }
    });
  }

  markScrollableTables();
  window.addEventListener("resize", markScrollableTables, { passive: true });

  /* ------------------------------------------------------------ scroll-spy --- */

  var tocLinks = $$(".page-toc a");
  var headings = $$(".prose h2[id], .prose h3[id], .prose h4[id]");
  var ticking = false;

  function updateActiveSection() {
    ticking = false;
    var toTop = $("[data-to-top]");
    if (toTop) toTop.hidden = window.scrollY < 640;
    if (!headings.length) return;
    var offset = (parseInt(getComputedStyle(document.body).getPropertyValue("--topbar-height"), 10) || 64) + 24;
    var active = headings[0];
    for (var i = 0; i < headings.length; i += 1) {
      if (headings[i].getBoundingClientRect().top - offset <= 0) active = headings[i];
      else break;
    }
    var nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 4;
    if (nearBottom) active = headings[headings.length - 1];
    var id = active.getAttribute("id");
    tocLinks.forEach(function (link) {
      var isActive = link.getAttribute("href") === "#" + id;
      link.classList.toggle("is-active", isActive);
      if (isActive) link.setAttribute("aria-current", "true");
      else link.removeAttribute("aria-current");
    });
  }

  window.addEventListener(
    "scroll",
    function () {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(updateActiveSection);
    },
    { passive: true }
  );
  window.addEventListener("resize", updateActiveSection, { passive: true });
  updateActiveSection();

  on($("[data-to-top]"), "click", function () {
    var still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: 0, behavior: still ? "auto" : "smooth" });
    var main = $("#main");
    if (main) main.focus({ preventScroll: true });
  });

  /* Deep links inside the page should not slide under the sticky top bar. */
  on(window, "hashchange", function () {
    var target = document.getElementById(window.location.hash.slice(1));
    if (target) target.scrollIntoView();
  });
})();
