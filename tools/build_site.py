#!/usr/bin/env python3
"""Build the pacifica-obd documentation site as plain static HTML.

The Markdown in ``docs/`` (plus the repository ``README.md``) is the single
source of truth: this generator only renders it.  It is deliberately small and
dependency-light — Markdown and Pygments, nothing else.  No MkDocs, no Node.

    python tools/build_site.py --out /tmp/psite     # build somewhere disposable

The default output is ``site/`` when ``.gitignore`` already covers it and
``build/site/`` when it does not, so a bare run never leaves a committable
artifact behind.  ``.github/workflows/pages.yml`` passes ``--out site``
explicitly, because a CI artifact is never committed.

On top of plain Markdown the renderer adds:

* ``!!!``/``???`` callouts, ``:material-…:`` icons, ``++keys++`` and
  ``{.md-button}`` — the Material-for-MkDocs syntax these documents already use;
* link rewriting, so doc-to-doc links become site links and repository links
  become GitHub blob links (no dead internal hrefs on the published site);
* per-page table of contents, a client-side search index, a sitemap and a
  generated "all pages" index that keeps every rendered document reachable.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import struct
import sys
import unicodedata
import xml.etree.ElementTree as etree
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterable, Iterator, Sequence, Set
from typing import Any

import markdown
from markdown.extensions.codehilite import CodeHilite
from markdown.inlinepatterns import InlineProcessor
from markdown.preprocessors import Preprocessor
from markdown.treeprocessors import Treeprocessor
from pygments.formatters.html import HtmlFormatter


# --------------------------------------------------------------------------- #
# Site configuration
# --------------------------------------------------------------------------- #

REPO_SLUG = "yorch/pacifica-obd"
REPO_URL = f"https://github.com/{REPO_SLUG}"
EDIT_BRANCH = "main"
SITE_URL = "https://yorch.github.io/pacifica-obd/"
SITE_NAME = "pacifica-obd"
SITE_TAGLINE = "OBD-II / UDS diagnostics toolkit"
SITE_DESCRIPTION = (
    "Command-line OBD-II and UDS diagnostics for a 2021 Chrysler Pacifica "
    "(vLinker FS): VIN, DTCs, readiness monitors, live data, read-only module and "
    "DID discovery, and explicitly guarded write primitives."
)
SITE_AUTHOR = "Jorge Barnaby"
COPYRIGHT = "MIT licensed · not affiliated with Stellantis"

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIRNAME = "docs"
THEME_DIR = ROOT / "tools" / "site_theme"

# Directories under docs/ that hold no documentation pages.
SKIP_DIRS = {"examples", "stylesheets", "assets", "img", "images"}
SKIP_FILES = {"readme.md"}
# Site-relative routes the generator owns, so a document can never collide with one.
RESERVED_SLUGS = {"all-pages", "assets"}
# Marker that proves an output directory belongs to this generator before it is
# replaced; the filename is deliberately generator-specific, not generic.
BUILD_MARKER = ".site-build.json"
# Page order and titles for the sidebar.  Entries whose file is missing are
# dropped silently: documents land in docs/ independently, and a missing nav
# entry must never fail a build.
NAV: list[dict[str, Any]] = [
    {
        "title": "Guide",
        "items": [
            ("Home", "docs/index.md"),
            ("Getting started", "docs/getting-started.md"),
            ("Command reference", "docs/usage.md"),
        ],
    },
    {
        "title": "Reference",
        "items": [
            ("Technical reference", "docs/technical.md"),
            ("Vehicle support (RU family)", "docs/vehicle-support.md"),
            ("Project README", "README.md"),
        ],
    },
    {
        "title": "Deep dives",
        "items": [
            ("AI assistant setup", "docs/ai-setup.md"),
            ("Chrysler Security Gateway", "docs/chrysler-sgw.md"),
            ("Enhanced (manufacturer-aware) diagnostics", "docs/enhanced-diagnostics.md"),
            ("FCA modules", "docs/fca-modules.md"),
            ("Vehicle assessment", "docs/vehicle-assessment.md"),
            ("FCA seed/key selector", "docs/fca-seedkey-selector.md"),
            ("FCA built-in seed→key", "docs/fca-keys.md"),
        ],
    },
]

# Landing page candidates, best first.  docs/index.md is the curated home page
# (hero, feature cards); README.md is the fallback and is always published.
HOME_CANDIDATES = ("docs/index.md", "README.md")

ICON_FALLBACK = "dot"


# --------------------------------------------------------------------------- #
# Hand-drawn inline icons (24×24, stroke = currentColor, no third-party assets)
# --------------------------------------------------------------------------- #

_ICON_PATHS: dict[str, str] = {
    "dot": '<circle cx="12" cy="12" r="3.2"/>',
    "engine-outline": (
        '<path d="M4 9h2l1.5-2h6l1.5 2h2.5a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1Z"/>'
        '<path d="M17.5 11h2.5M7.5 11v3M11 11v3M14 11v3"/>'
    ),
    "gauge": '<path d="M4 18a8 8 0 1 1 16 0"/><path d="M12 14l4-4"/><circle cx="12" cy="18" r="1.5"/>',
    "car-wrench": (
        '<path d="M3 14h11l1.5-3h3.5a1 1 0 0 1 1 1v3h1"/>'
        '<path d="M3 14v3h2.5M11.5 17H9"/><circle cx="6" cy="18" r="1.7"/><circle cx="14.5" cy="18" r="1.7"/>'
        '<path d="M20 4.5a2.5 2.5 0 0 1-3 2.4l-2.4 2.4-1.6-1.6 2.4-2.4A2.5 2.5 0 0 1 20 4.5Z"/>'
    ),
    "identifier": (
        '<rect x="3" y="6" width="18" height="12" rx="2"/><circle cx="8.5" cy="11" r="1.8"/>'
        '<path d="M5.5 15.4c.6-1.3 1.7-2 3-2s2.4.7 3 2M14 10.5h4M14 13.5h3"/>'
    ),
    "database-clock": (
        '<ellipse cx="10.5" cy="6.5" rx="6.5" ry="2.6"/><path d="M4 6.5v9c0 1.4 2.9 2.6 6.5 2.6"/>'
        '<path d="M17 6.5v2.2"/><circle cx="17" cy="15.5" r="5"/><path d="M17 13.6v2l1.4 1.1"/>'
    ),
    "console": (
        '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M3 9h18"/>'
        '<path d="M7.5 12.5l2 2-2 2M12.5 16.5h4"/>'
    ),
    "terminal": (
        '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M3 9h18"/><path d="M7.5 12l2.5 2.5L7.5 17"/>'
    ),
    "rocket": (
        '<path d="M12 3c3.5 1.6 5 5 5 8.5L12 16l-5-4.5C7 8 8.5 4.6 12 3Z"/>'
        '<path d="M9.5 16.5 8 21l4-2 4 2-1.5-4.5"/><circle cx="12" cy="9.5" r="1.6"/>'
    ),
    "book": '<path d="M4 5.5A2 2 0 0 1 6 3.5h12v17H6a2 2 0 0 0-2 2Z"/><path d="M18 17.5H6a2 2 0 0 0-2 2"/>',
    "code": '<path d="M9 8l-4 4 4 4M15 8l4 4-4 4"/>',
    "lightbulb": (
        '<path d="M12 3a5.5 5.5 0 0 1 3 10.1V16H9v-2.9A5.5 5.5 0 0 1 12 3Z"/>'
        '<path d="M10 19h4M10.5 21.5h3"/>'
    ),
    "alert": '<path d="M12 4l8.5 15h-17Z"/><path d="M12 10v4.5"/><circle cx="12" cy="16.8" r=".9"/>',
    "check": '<circle cx="12" cy="12" r="8.5"/><path d="M8.2 12.3l2.6 2.6 5-5.4"/>',
    "shield": '<path d="M12 3l7 3v5.5c0 4-2.9 7.6-7 9.5-4.1-1.9-7-5.5-7-9.5V6Z"/><path d="M9 12l2 2 4-4.5"/>',
    "car": (
        '<path d="M4 15h16l-1.2-4.6A2 2 0 0 0 16.9 9H7.1a2 2 0 0 0-1.9 1.4L4 15Z"/>'
        '<path d="M3 15v3h2.5M21 15v3h-2.5"/><circle cx="7" cy="18" r="1.7"/><circle cx="17" cy="18" r="1.7"/>'
    ),
    "database": (
        '<ellipse cx="12" cy="6" rx="7" ry="2.8"/><path d="M5 6v12c0 1.6 3.1 2.8 7 2.8s7-1.2 7-2.8V6"/>'
        '<path d="M5 12c0 1.6 3.1 2.8 7 2.8s7-1.2 7-2.8"/>'
    ),
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "tag": '<path d="M4 4h7l9 9-7 7-9-9Z"/><circle cx="8.2" cy="8.2" r="1.3"/>',
    "list": (
        '<path d="M8.5 7h11M8.5 12h11M8.5 17h11"/>'
        '<circle cx="4.6" cy="7" r="1.1"/><circle cx="4.6" cy="12" r="1.1"/><circle cx="4.6" cy="17" r="1.1"/>'
    ),
    "info": '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5"/><circle cx="12" cy="8.3" r=".95"/>',
    "star": '<path d="M12 4l2.4 4.9 5.4.8-3.9 3.8.9 5.4-4.8-2.6-4.8 2.6.9-5.4L4.2 9.7l5.4-.8Z"/>',
    "download": '<path d="M12 4v10M8 10.5l4 4 4-4M5 19h14"/>',
    "pencil": '<path d="M4.5 19.5h4L19 9a2.1 2.1 0 0 0-3-3L4.5 17.5Z"/><path d="M14.5 7.5l2 2"/>',
    "arrow-right": '<path d="M4.5 12h14M13 7l5 5-5 5"/>',
    "moon": '<path d="M20 14.5A8.2 8.2 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z"/>',
    "sun": (
        '<circle cx="12" cy="12" r="4"/>'
        '<path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4"/>'
    ),
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "copy": '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 6.5A2.5 2.5 0 0 0 12.5 4H6.5A2.5 2.5 0 0 0 4 6.5v6A2.5 2.5 0 0 0 6.5 15"/>',
    "external": (
        '<path d="M14 4h6v6"/><path d="M20 4l-8.5 8.5"/>'
        '<path d="M18 14v4.5A1.5 1.5 0 0 1 16.5 20H5.5A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/>'
    ),
    "search": '<circle cx="11" cy="11" r="6.2"/><path d="M15.6 15.6L20.5 20.5"/>',
    "link": (
        '<path d="M10.5 13.5l5-5"/>'
        '<path d="M8.5 15.5l-1.5 1.5a3.2 3.2 0 0 1-4.5-4.5l3-3a3.2 3.2 0 0 1 4.5 0"/>'
        '<path d="M15.5 8.5L17 7a3.2 3.2 0 0 1 4.5 4.5l-3 3a3.2 3.2 0 0 1-4.5 0"/>'
    ),
    "wrench": '<path d="M20 4.5a4.5 4.5 0 0 1-5.7 4.3L6.6 16.5a2.2 2.2 0 1 0 1.9 1.9l7.7-7.7A4.5 4.5 0 0 0 20 4.5Z"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
}

_ICON_NAME_RE = re.compile(r"[^a-z0-9-]+")


def safe_icon_name(name: str) -> str:
    """Reduce a shortcode to a safe, known icon name."""
    cleaned = _ICON_NAME_RE.sub("", (name or "").lower())
    return cleaned or ICON_FALLBACK


def icon_svg(name: str) -> str:
    """Inline SVG for an icon name, falling back to a neutral mark."""
    body = _ICON_PATHS.get(safe_icon_name(name), _ICON_PATHS[ICON_FALLBACK])
    return (
        '<svg class="site-icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
        f'focusable="false">{body}</svg>'
    )


# --------------------------------------------------------------------------- #
# Pygments: every code block gets a language label and a copy button
# --------------------------------------------------------------------------- #

_LANG_SAFE_RE = re.compile(r"[^A-Za-z0-9_+#.\-]+")


def sanitize_lang(value: str | None) -> str:
    """Quote-free, attribute-safe language tag derived from a fence's info string."""
    lang = _LANG_SAFE_RE.sub("", (value or "").removeprefix("language-"))
    return (lang or "text")[:32]


class SiteCodeFormatter(HtmlFormatter):
    """Add a language label and a copy button to every highlighted code block.

    Pygments' formatter is the only hook that sees both the highlighted markup and
    the fence's language, so the chrome is emitted here instead of re-parsing the
    rendered HTML afterwards.  ``wrap`` runs inside the ``<div class="codehilite
    code-block">`` that Pygments writes last, which is why the caption is emitted
    there rather than around the div.
    """

    def __init__(self, lang_str: str | None = None, **options: Any) -> None:
        self._lang = sanitize_lang(lang_str)
        super().__init__(**options)

    def wrap(self, source: Iterable[tuple[int, str]]) -> Iterator[tuple[int, str]]:
        lang = html.escape(self._lang, quote=True)
        label = "" if self._lang == "text" else f'<span class="code-lang">{lang}</span>'
        yield 0, (
            f'<div class="code-head" data-lang="{lang}">{label}'
            '<button type="button" class="copy-button" data-copy-code '
            'aria-label="Copy this code block">'
            f'{icon_svg("copy")}<span class="copy-text">Copy</span>'
            "</button></div>"
        )
        yield from super().wrap(source)


# --------------------------------------------------------------------------- #
# Repository privacy: ignore rules, tracked files, and the boundary they define
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class IgnoreRule:
    """One ``.gitignore`` line, pre-compiled."""

    regex: re.Pattern[str]
    negated: bool
    directory_only: bool


def is_private(
    repo_relative: str, rules: Sequence[IgnoreRule], tracked: Set[str]
) -> bool:
    """A document is private when .gitignore covers it and git does not track it."""
    return bool(rules) and repo_relative not in tracked and is_ignored(repo_relative, rules)


@dataclass(frozen=True)
class Privacy:
    """The repository's ignore rules plus the set of paths git tracks.

    Carried through rendering so that any path a document points at — a page, a
    symlink target, an image anywhere in the repository — is held to the same
    boundary, not just the files discovered under ``docs/``.
    """

    rules: Sequence[IgnoreRule] = ()
    tracked: Set[str] = frozenset()

    def is_private(self, repo_relative: str) -> bool:
        return is_private(repo_relative, self.rules, self.tracked)

    def paths_under(self, root: Path, directory: str) -> list[str]:
        """Sorted repository-relative paths under ``directory`` that are private."""
        base = root / directory
        if not self.rules or not base.is_dir():
            return []
        return sorted(
            path.relative_to(root).as_posix()
            for path in base.rglob("*")
            if path.is_file() and self.is_private(path.relative_to(root).as_posix())
        )

NO_PRIVACY = Privacy()





# --------------------------------------------------------------------------- #
# Markdown syntax compatibility with the Material-for-MkDocs documents
# --------------------------------------------------------------------------- #

_MATERIAL_ICON_RE = r":(?P<set>[a-z0-9]+)-(?P<icon>[a-z0-9-]+):"
_KEYS_RE = r"\+\+(?P<keys>[A-Za-z0-9+\-_ ]*[A-Za-z0-9\-_])\+\+"

_DETAILS_MARKER = "site-details"
_DETAILS_MARKER_OPEN = "site-details-open"
_DETAILS_LINE_RE = re.compile(r"^(?P<indent> {0,3})\?\?\?(?P<open>\+)?[ \t]*(?P<rest>.*?)[ \t]*$", re.MULTILINE)
# `??? note "Title"`, `??? "Title"`, `??? note`, `???` and `??? note ""`.
_DETAILS_REST_RE = re.compile(r'^(?:(?P<type>[A-Za-z][\w-]*)[ \t]*)?(?:"(?P<title>[^"]*)")?$')


class IconInlineProcessor(InlineProcessor):
    """``:material-gauge:`` → an inline SVG, the way pymdownx.emoji does it.

    Running as an inline pattern (rather than as a regex over the source) means
    shortcodes inside code fences and code spans are never touched, and the
    trailing ``{ .lg .middle }`` attribute list still lands on the icon because
    attr_list sees the element's tail text.
    """

    def handleMatch(self, match: re.Match[str], data: str):  # noqa: N802 - Markdown API
        # Material's sets are referenced without their prefix in this repository
        # (`:material-gauge:`); the icon table is keyed by the bare name.
        name = safe_icon_name(match.group("icon"))
        element = etree.Element("span")
        element.set("class", f"site-icon site-icon--{name}")
        element.set("aria-hidden", "true")
        element.text = self.md.htmlStash.store(icon_svg(name))
        return element, match.start(0), match.end(0)


class KeysInlineProcessor(InlineProcessor):
    """``++ctrl+c++`` → ``<kbd>ctrl</kbd><span>+</span><kbd>c</kbd>``."""

    def handleMatch(self, match: re.Match[str], data: str):  # noqa: N802 - Markdown API
        element = etree.Element("span")
        element.set("class", "keys")
        for index, part in enumerate(part for part in match.group("keys").split("+") if part):
            if index:
                separator = etree.SubElement(element, "span")
                separator.set("class", "keys-sep")
                separator.text = "+"
            key = etree.SubElement(element, "kbd")
            key.text = part
        return element, match.start(0), match.end(0)


CODEHILITE_OPTIONS: dict[str, Any] = {
    "use_pygments": True,
    "guess_lang": False,
    "linenos": False,
    "css_class": "codehilite code-block",
    "pygments_formatter": SiteCodeFormatter,
}

_FENCE_LINE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^\n]*)$")


class FencePreprocessor(Preprocessor):
    """Highlight every fenced code block, at any indentation, before block parsing.

    The built-in ``fenced_code`` extension is a preprocessor that only sees fences
    at column zero, and a block processor cannot see a fence that spans a blank
    line (the block parser splits there first).  Handling fences here and replacing
    each one with a single stashed placeholder keeps the surrounding block intact —
    which is what makes fences work inside indented callout bodies — and gives every
    fence a language label and copy button through the shared Pygments formatter.

    Running before `DetailsPreprocessor` also means a ``???`` line inside a code
    sample is never mistaken for a collapsible callout.
    """

    def run(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        index = 0
        previous_was_fence = False
        while index < len(lines):
            match = _FENCE_LINE_RE.match(lines[index])
            closer = None
            if match is not None:
                indent = match.group("indent")
                fence = match.group("fence")
                pattern = re.compile(rf"^{re.escape(indent)} {{0,3}}{re.escape(fence)}[ \t]*$")
                for candidate in range(index + 1, len(lines)):
                    if pattern.match(lines[candidate]):
                        closer = candidate
                        break
            if closer is None:
                out.append(lines[index])
                index += 1
                previous_was_fence = False
                continue

            body = []
            for line in lines[index + 1: closer]:
                indent = match.group("indent")
                body.append(line[len(indent):] if line.startswith(indent) else line.lstrip())
            info = match.group("info").strip().strip("{}").strip()
            highlighter = CodeHilite(
                "\n".join(body),
                lang=info.split()[0] if info else None,
                tab_length=self.md.tab_length,
                **CODEHILITE_OPTIONS,
            )
            if previous_was_fence:
                # Separate two stacked blocks: one placeholder per paragraph keeps
                # Markdown from wrapping the second one in a <p>.
                out.append("")
            out.append(f"{match.group('indent')}{self.md.htmlStash.store(highlighter.hilite())}")
            previous_was_fence = True
            index = closer + 1
        return out


class DetailsPreprocessor(Preprocessor):
    """``???``/``???+`` (pymdownx.details) → a marked ``!!!`` admonition.

    The built-in ``admonition`` extension only understands ``!!!``, so the
    collapsible form is rewritten to ``!!! site-details note "Title"`` and a
    treeprocessor then flips the matching ``<div>`` into a ``<details>``.
    """

    def run(self, lines: list[str]) -> list[str]:
        def replace(match: re.Match[str]) -> str:
            marker = _DETAILS_MARKER_OPEN if match.group("open") else _DETAILS_MARKER
            rest = match.group("rest") or ""
            parsed = _DETAILS_REST_RE.match(rest) if rest else None
            if parsed is None:
                # Unrecognised trailing text: keep it out of the class list rather
                # than letting it become part of the admonition's CSS classes.
                parsed = _DETAILS_REST_RE.match("")
            kind = (parsed.group("type") or "note").lower() if parsed else "note"
            title = parsed.group("title") if parsed else None
            # The built-in extension always needs a quoted title: without one it
            # derives the label from the class name (`Site-details`), not the type.
            rendered_title = kind.capitalize() if title is None else title
            # The admonition line itself is quote-delimited: a stray `"` in a title
            # would truncate it, so titles are normalised to single quotes.
            rendered_title = rendered_title.replace('"', "'")
            return f'{match.group("indent")}!!! {marker} {kind} "{rendered_title}"'

        return _DETAILS_LINE_RE.sub(replace, "\n".join(lines)).split("\n")


class DetailsTreeprocessor(Treeprocessor):
    """Turn marker-classed admonitions into collapsible ``<details>`` blocks."""

    def run(self, root: etree.Element) -> etree.Element | None:
        for parent in list(root.iter()):
            for index, child in enumerate(list(parent)):
                if child.tag != "div":
                    continue
                classes = (child.get("class") or "").split()
                if _DETAILS_MARKER not in classes and _DETAILS_MARKER_OPEN not in classes:
                    continue
                opened = _DETAILS_MARKER_OPEN in classes
                child.set(
                    "class", " ".join(c for c in classes if not c.startswith(_DETAILS_MARKER))
                )
                child.tag = "details"
                if opened:
                    child.set("open", "open")
                for sub in child:
                    if sub.tag == "p" and "admonition-title" in (sub.get("class") or ""):
                        sub.tag = "summary"
                        break
                else:
                    summary = etree.Element("summary")
                    summary.text = "Details"
                    child.insert(0, summary)
                parent[index] = child
        return None


class LinkRewriter(Treeprocessor):
    """Rewrite document links so the published site has no dead internal hrefs.

    ``getting-started.md`` becomes ``../getting-started/index.html``; ``../AGENTS.md``
    and other repository files become GitHub blob links; images become raw GitHub
    links (a ``blob`` URL serves an HTML page, not image bytes); absolute and
    fragment-only links are left alone.  Working on the parsed tree (not on the
    HTML string) keeps code blocks and ``<a>`` text out of the rewrite.
    """

    def __init__(
        self,
        md: markdown.Markdown,
        page: Page,
        root: Path,
        pages_by_source: dict[str, str],
        unpublished: list[str],
        privacy: Privacy = NO_PRIVACY,
    ) -> None:
        super().__init__(md)
        self.page = page
        self.root = root
        self.pages_by_source = pages_by_source
        self.unpublished = unpublished
        self.privacy = privacy

    def run(self, root: etree.Element) -> etree.Element | None:
        for parent in list(root.iter()):
            for index, element in enumerate(list(parent)):
                if element.tag == "a":
                    href = element.get("href")
                    if href:
                        element.set("href", self.rewrite(href))
                elif element.tag == "img":
                    source = element.get("src")
                    rewritten = self.rewrite_image(source, element)
                    if rewritten is None:
                        parent[index] = self.missing_image(element)
                    else:
                        element.set("src", rewritten)
        return None

    def resolve(self, target: str) -> tuple[Path, str] | None:
        """Resolve a document-relative link to ``(absolute path, repo-relative path)``."""
        try:
            resolved = (self.root / Path(self.page.source).parent / target).resolve()
            return resolved, resolved.relative_to(self.root.resolve()).as_posix()
        except (ValueError, OSError):
            return None

    def rewrite(self, href: str) -> str:
        if href.startswith(("#", "/", "http://", "https://", "mailto:", "tel:", "//", "data:")):
            return href
        target, separator, fragment = href.partition("#")
        if not target:
            return href
        suffix = f"#{fragment}" if separator else ""
        resolved_pair = self.resolve(target)
        if resolved_pair is None:
            # Outside the repository (a link the published site cannot own).
            self.unpublished.append(f"{self.page.source} → {href}")
            return href
        resolved, repo_relative = resolved_pair
        page_target = self.pages_by_source.get(repo_relative)
        if page_target is not None:
            return f"{self.page.rel}{page_target}{suffix}"
        if resolved.is_dir():
            return f"{REPO_URL}/tree/{EDIT_BRANCH}/{repo_relative}{suffix}"
        if resolved.suffix != ".md" or not resolved.is_file():
            self.unpublished.append(f"{self.page.source} → {href}")
        return f"{REPO_URL}/blob/{EDIT_BRANCH}/{repo_relative}{suffix}"

    def rewrite_image(self, source: str | None, element: etree.Element) -> str | None:
        """Raw GitHub URL for a local image, or ``None`` when it cannot be published."""
        if not source or source.startswith(("http://", "https://", "//", "data:")):
            return source
        resolved_pair = self.resolve(source)
        if resolved_pair is None:
            self.unpublished.append(f"{self.page.source} → image {source}")
            return None
        resolved, repo_relative = resolved_pair
        if not resolved.is_file() or self.privacy.is_private(repo_relative):
            self.unpublished.append(f"{self.page.source} → image {source} (not publishable)")
            return None
        return f"https://raw.githubusercontent.com/{REPO_SLUG}/{EDIT_BRANCH}/{repo_relative}"

    @staticmethod
    def missing_image(element: etree.Element) -> etree.Element:
        """Replace an unpublishable image with a visible note instead of a broken icon."""
        placeholder = etree.Element("span")
        placeholder.set("class", "missing-image")
        placeholder.text = f"[image not published: {element.get('alt') or 'untitled'}]"
        return placeholder


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

@dataclass
class Section:
    """A heading and the text below it: feeds the on-page TOC and the search index."""

    id: str
    title: str
    level: int
    text: str = ""


@dataclass
class Page:
    """One rendered document."""

    source: str                     # repository-relative Markdown path
    url: str                        # canonical site-relative URL ("usage/", "" for home)
    target: str                     # linkable path from the site root ("usage/index.html")
    out_file: str                   # output path relative to the build directory
    title: str                      # sidebar and search label
    group: str                      # nav group, used by the generated indexes
    order: int
    nav: bool = False               # listed in the sidebar
    is_home: bool = False
    heading: str = ""               # the document's own H1
    description: str = ""
    body: str = ""
    sections: list[Section] = field(default_factory=list)
    toc: list[Section] = field(default_factory=list)

    @property
    def rel(self) -> str:
        """Prefix that turns a site-relative path into a page-relative one."""
        return "../" * len([part for part in self.url.split("/") if part])

    @property
    def edit_url(self) -> str:
        return f"{REPO_URL}/edit/{EDIT_BRANCH}/{self.source}"

    @property
    def self_link(self) -> str:
        return f"{self.rel}{self.target}"

    def link(self, target: Page) -> str:
        return f"{self.rel}{target.target}"


@dataclass
class Site:
    """Everything the templates need that is not page-local."""

    root: Path
    out_dir: Path
    site_url: str
    pages: list[Page] = field(default_factory=list)

    @property
    def home(self) -> Page | None:
        return next((page for page in self.pages if page.is_home), None)


# --------------------------------------------------------------------------- #
# .gitignore awareness
#
# A document the repository declares private must not be published, but a
# pattern can also overlap tracked pages (`docs/vehicle-*.md` matches both the
# private `docs/vehicle-assessment.md` and the published `docs/vehicle-support.md`).
# Git's rule is "ignored *and* untracked", so the git index is read directly —
# no subprocess, no `git` dependency at build time — and the parse is validated
# before it is trusted.
# --------------------------------------------------------------------------- #

def _glob_to_regex(pattern: str, anchored: bool, prefix: str = "") -> str:
    """Translate a gitignore glob into a regular expression body.

    ``anchored`` means the pattern carries a directory (``docs/x.md``), so it
    matches from the root of the ``.gitignore`` that holds it.  A nested
    ``.gitignore`` with a bare pattern (``docs/.gitignore`` holding ``*.md``)
    applies to the whole subtree below that directory, not just its children.
    """
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**/", index):
                parts.append("(?:[^/]+/)*")
                index += 3
                continue
            parts.append(".*" if pattern.startswith("**", index) else "[^/]*")
            index += 2 if pattern.startswith("**", index) else 1
            continue
        if char == "?":
            parts.append("[^/]")
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end != -1:
                inner = pattern[index + 1: end]
                parts.append(f"[{'^' + inner[1:] if inner.startswith('!') else inner}]")
                index = end + 1
                continue
            parts.append(re.escape(char))
        else:
            parts.append(re.escape(char))
        index += 1
    body = "".join(parts)
    if anchored:
        return f"^{prefix}{body}$"
    subtree = "(?:.*/)?" if prefix else "(?:^|.*/)"
    return f"^{prefix}{subtree}{body}$"


def parse_gitignore(path: Path, base: str = "") -> list[IgnoreRule]:
    """Compile a ``.gitignore`` file into ordered rules."""
    rules: list[IgnoreRule] = []
    if not path.is_file():
        return rules
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        line = line.replace("\\ ", " ").replace("\\#", "#").replace("\\!", "!")
        directory_only = line.endswith("/")
        # A leading `/` anchors the pattern to this file's directory (it is not
        # part of the path), and so does any other `/` inside the pattern.
        leading_slash = line.startswith("/")
        line = line.strip("/")
        if not line:
            continue
        anchored = leading_slash or "/" in line
        prefix = f"{base.rstrip('/')}/" if base else ""
        rules.append(
            IgnoreRule(
                regex=re.compile(_glob_to_regex(line, anchored, prefix)),
                negated=negated,
                directory_only=directory_only,
            )
        )
    return rules


def collect_ignore_rules(root: Path) -> list[IgnoreRule]:
    """Rules from the repository root and from every directory under docs/."""
    rules = parse_gitignore(root / ".gitignore")
    docs_dir = root / DOCS_DIRNAME
    if docs_dir.is_dir():
        for nested in sorted(docs_dir.rglob(".gitignore")):
            rules.extend(parse_gitignore(nested, nested.parent.relative_to(root).as_posix()))
    return rules


def is_ignored(repo_relative: str, rules: Sequence[IgnoreRule]) -> bool:
    """Git semantics: an excluded parent cannot be re-included, then last rule wins.

    Git refuses to re-include a file if one of its parent directories is excluded,
    so the ancestor check comes first and is not overridden by a file-level `!`.
    """
    parts = repo_relative.strip("/").split("/")

    def verdict(candidate: str, is_directory: bool) -> bool | None:
        found: bool | None = None
        for rule in rules:
            if rule.directory_only and not is_directory:
                continue
            if rule.regex.match(candidate):
                found = not rule.negated
        return found

    for depth in range(1, len(parts)):
        if verdict("/".join(parts[:depth]), is_directory=True) is True:
            return True
    return verdict("/".join(parts), is_directory=False) is True


def tracked_paths(root: Path) -> set[str] | None:
    """Every path recorded in ``.git/index``, or ``None`` if it cannot be trusted.

    Only used to decide whether a gitignored document is private (untracked) or a
    published page that merely matches an overlapping pattern.  Anything unusual —
    a worktree pointer, index format v4 (prefix-compressed names), extended entry
    flags, a split index, a short or unparsable file — returns ``None``, and the
    caller then applies the ignore rules conservatively (private documents stay
    unpublished) rather than publishing something it could not verify.
    """
    git_dir = root / ".git"
    if git_dir.is_file():
        try:
            pointer = git_dir.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not pointer.startswith("gitdir:"):
            return None
        git_dir = (root / pointer.split(":", 1)[1].strip()).resolve()
    index = git_dir / "index"
    if not index.is_file():
        return None
    try:
        data = index.read_bytes()
    except OSError:
        return None
    if len(data) < 12 or data[:4] != b"DIRC":
        return None
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3):  # v4 compresses names; without a full implementation, decline
        return None

    paths: set[str] = set()
    offset = 12
    try:
        for _ in range(count):
            flags = struct.unpack(">H", data[offset + 60: offset + 62])[0]
            if flags & 0x4000:
                return None  # extended flags add bytes this parser does not model
            end = data.index(b"\0", offset + 62)
            name = data[offset + 62: end]
            if not name or b"\0" in name:
                return None
            # Entries are padded with the terminating NUL and up to seven more NUL
            # bytes so that each entry occupies a multiple of eight bytes.
            offset = end + 1 + (-(end + 1 - offset) % 8)
            paths.add(name.decode("utf-8", "replace"))
    except (IndexError, ValueError, struct.error):
        return None

    sanity = [p for p in ("docs/index.md", "README.md", "pyproject.toml") if (root / p).is_file()]
    if len(paths) != count or not sanity or not all(path in paths for path in sanity):
        return None
    return paths




def discover_docs(
    root: Path,
    privacy: Privacy = NO_PRIVACY,
    warnings: list[str] | None = None,
) -> list[str]:
    """Repository-relative Markdown sources under docs/, sorted and deterministic.

    Documents that ``.gitignore`` marks private are skipped (the privacy boundary
    of this repository), and so is anything reached through a symlink: a link named
    like a public page must not publish a private or out-of-repository target.
    """
    docs_dir = root / DOCS_DIRNAME
    found: list[str] = []
    for path in sorted(docs_dir.rglob("*.md")):
        relative = path.relative_to(docs_dir)
        if any(part in SKIP_DIRS for part in relative.parts[:-1]):
            continue
        if relative.name.lower() in SKIP_FILES:
            continue
        if _is_symlinked(path, docs_dir):
            if warnings is not None:
                warnings.append(f"skipped symlinked document: {path.relative_to(root).as_posix()}")
            continue
        repo_relative = path.relative_to(root).as_posix()
        if privacy.is_private(repo_relative):
            if warnings is not None:
                warnings.append(f"skipped private document (.gitignore): {repo_relative}")
            continue
        found.append(repo_relative)
    return found


def _is_symlinked(path: Path, docs_dir: Path) -> bool:
    """True when the file, or any directory between it and docs/, is a symlink."""
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == docs_dir or current.parent == current:
            return False
        current = current.parent


def first_heading(text: str) -> str:
    match = re.search(r"^#\s+(?P<title>.+?)\s*$", text, re.MULTILINE)
    if not match:
        return ""
    title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group("title"))
    return re.sub(r"[`*_]", "", title).strip()


# Lines that start something other than prose: headings, callouts, lists, tables,
# fences, raw HTML.  `*` and `-` only count when they open a list item, otherwise
# emphasis like `**bold**` at the start of a continued line would end the paragraph.
_NOT_PROSE_RE = re.compile(r"^(?:#{1,6}\s|!{3}|\?{3}|\||>{1,2}|={2,}|<[A-Za-z/!?]|[-*+]\s|\d+[.)]\s)")


def first_paragraph(text: str, limit: int = 320) -> str:
    """The document's first real paragraph as plain text (for meta descriptions)."""
    lines: list[str] = []
    in_fence = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            if lines:
                break
            continue
        if in_fence:
            continue
        if not line:
            if lines:
                break
            continue
        if _NOT_PROSE_RE.match(line):
            if lines:
                break
            continue
        lines.append(line)
    paragraph = " ".join(lines)
    paragraph = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", paragraph)
    paragraph = re.sub(r"`([^`]*)`", r"\1", paragraph)
    paragraph = re.sub(r"[*_]", "", paragraph).strip()
    return summarise(paragraph, limit)


def summarise(text: str, limit: int) -> str:
    """Trim a description at a sentence, word, or hard boundary — never mid-word."""
    if len(text) <= limit:
        return text
    window = text[: limit - 1]
    sentence = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
    if sentence > limit // 2:
        return text[: sentence + 1]
    word = window.rfind(" ")
    return (text[:word] if word > 0 else window).rstrip(" ,;:") + "…"


def slugify_stem(stem: str) -> str:
    """ASCII, URL-safe slug for a document file name."""
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_stem.lower()).strip("-")
    return slug or "page"


def build_pages(
    root: Path, warnings: list[str], privacy: Privacy = NO_PRIVACY
) -> list[Page]:
    """Resolve NAV into concrete pages, then add every other document."""
    sources = discover_docs(root, privacy, warnings)
    readme = None
    readme_path = root / "README.md"
    if readme_path.is_file():
        # README is published as a page, so it passes the same privacy and symlink
        # checks as everything under docs/.
        if _is_symlinked(readme_path, root) or privacy.is_private("README.md"):
            warnings.append("README.md is not published (private by .gitignore, or a symlink)")
        else:
            readme = "README.md"
    available = set(sources) | ({readme} if readme else set())
    home_source = next((c for c in HOME_CANDIDATES if c in available), None)
    if home_source is None:
        # No curated landing page: promote the first document so the site has an index.
        home_source = sources[0] if sources else None

    pages: list[Page] = []
    seen: dict[str, Page] = {}
    used_slugs: set[str] = set()

    def add(source: str, title: str, group: str, nav: bool) -> Page:
        slug = slugify_stem(Path(source).stem)
        unique, counter = slug, 2
        while unique in used_slugs or unique in RESERVED_SLUGS:
            unique, counter = f"{slug}-{counter}", counter + 1
        used_slugs.add(unique)
        is_home = source == home_source
        page = Page(
            source=source,
            url="" if is_home else f"{unique}/",
            target="index.html" if is_home else f"{unique}/index.html",
            out_file="index.html" if is_home else f"{unique}/index.html",
            title=title,
            group=group,
            order=len(pages),
            nav=nav,
            is_home=is_home,
        )
        pages.append(page)
        seen[source] = page
        return page

    for group in NAV:
        for title, source in group["items"]:
            if source not in available:
                if (root / source).is_file():
                    warnings.append(
                        f"nav entry {title!r} ({source}) is private (.gitignore) and is not "
                        "published; --include-ignored previews it locally"
                    )
                else:
                    warnings.append(f"nav entry {title!r} points at a missing file: {source}")
                continue
            if source in seen:
                continue
            add(source, title, group["title"], nav=True)

    for source in sorted(set(sources) | ({readme} if readme else set())):
        if source in seen:
            continue
        text = (root / source).read_text(encoding="utf-8")
        add(source, first_heading(text) or Path(source).stem.replace("-", " ").title(),
            "More documents", nav=False)

    for page in pages:
        text = (root / page.source).read_text(encoding="utf-8")
        page.heading = first_heading(text) or page.title
        page.description = first_paragraph(text) or SITE_DESCRIPTION
    return pages


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

HEADING_RE = re.compile(r'<h([1-6])\b[^>]*?\bid="([^"]+)"[^>]*?>.*?</h\1>', re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


def make_markdown(
    page: Page,
    root: Path,
    pages_by_source: dict[str, str],
    unpublished: list[str],
    privacy: Privacy = NO_PRIVACY,
) -> markdown.Markdown:
    """A Markdown engine wired for this site's documents.

    ``extra`` is spelled out instead of being imported wholesale so that
    ``fenced_code`` stays out: fences are handled by `FencePreprocessor`, which
    also works inside indented callout bodies and across blank lines.
    """
    md = markdown.Markdown(
        extensions=[
            "abbr",
            "attr_list",
            "def_list",
            "footnotes",
            "md_in_html",
            "tables",
            "sane_lists",
            "admonition",
            "toc",
            "codehilite",
        ],
        extension_configs={
            "toc": {
                "permalink": "#",
                "permalink_title": "Copy a link to this section",
                "permalink_class": "headerlink",
                "toc_depth": "2-3",
            },
            "codehilite": dict(CODEHILITE_OPTIONS),
        },
        output_format="html",
    )
    md.inlinePatterns.register(KeysInlineProcessor(_KEYS_RE, md), "site_keys", 176)
    md.inlinePatterns.register(IconInlineProcessor(_MATERIAL_ICON_RE, md), "site_icons", 175)
    md.preprocessors.register(FencePreprocessor(md), "site_fences", 30)
    md.preprocessors.register(DetailsPreprocessor(md), "site_details", 27)
    md.treeprocessors.register(DetailsTreeprocessor(md), "site_details_tree", 6)
    md.treeprocessors.register(
        LinkRewriter(md, page, root, pages_by_source, unpublished, privacy), "site_links", 4
    )
    return md


def plain_text(markup: str) -> str:
    """Flatten rendered HTML to searchable text (entities decoded, tags dropped)."""
    without_scripts = re.sub(
        r"<(script|style)\b.*?</\1>", " ", markup, flags=re.DOTALL | re.IGNORECASE
    )
    return WHITESPACE_RE.sub(" ", html.unescape(TAG_RE.sub(" ", without_scripts))).strip()


def extract_sections(
    body: str, toc_tokens: Sequence[dict[str, Any]]
) -> tuple[list[Section], list[Section]]:
    """Split the rendered body into searchable sections, and build the TOC model."""
    toc: list[Section] = []

    def walk(tokens: Sequence[dict[str, Any]]) -> None:
        for token in tokens:
            toc.append(
                Section(
                    id=str(token.get("id") or ""),
                    title=plain_text(str(token.get("name") or "")),
                    level=int(token.get("level") or 2),
                )
            )
            walk(token.get("children") or [])

    walk(toc_tokens)

    sections: list[Section] = []
    matches = list(HEADING_RE.finditer(body))
    if not matches:
        # A document with no headings is still searchable as one section.
        text = plain_text(body)
        return ([Section(id="", title="Overview", level=1, text=text)] if text else []), toc
    intro = plain_text(body[: matches[0].start()])
    if intro:
        sections.append(Section(id="", title="Overview", level=1, text=intro))
    for index, match in enumerate(matches):
        heading_id = match.group(2)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        title = next((item.title for item in toc if item.id == heading_id), "")
        sections.append(
            Section(
                id=heading_id,
                title=title or plain_text(match.group(0)),
                level=int(match.group(1)),
                text=plain_text(body[match.end(): end]),
            )
        )
    return sections, toc


def render_markdown(
    page: Page,
    root: Path,
    pages_by_source: dict[str, str],
    unpublished: list[str],
    privacy: Privacy = NO_PRIVACY,
) -> None:
    text = (root / page.source).read_text(encoding="utf-8")
    md = make_markdown(page, root, pages_by_source, unpublished, privacy)
    page.body = md.convert(text)
    page.sections, page.toc = extract_sections(page.body, md.toc_tokens)


def render_nav_link(
    current: Page, page: Page, label: str | None = None, css: str = "nav-link"
) -> str:
    active = current.source == page.source
    classes = f"{css} is-active" if active else css
    current_attr = ' aria-current="page"' if active else ""
    return (
        f'<li><a class="{classes}" href="{html.escape(current.link(page), quote=True)}"'
        f'{current_attr}><span>{html.escape(label or page.title)}</span></a></li>'
    )


def render_nav(current: Page, pages: Sequence[Page]) -> str:
    """Sidebar navigation: home, the NAV groups, then everything else."""
    groups: dict[str, list[Page]] = {}
    for page in pages:
        if page.group != "More documents" and not page.is_home:
            groups.setdefault(page.group, []).append(page)

    out = ['<nav class="site-nav" aria-label="Documentation">']
    home = next((page for page in pages if page.is_home), None)
    if home is not None:
        out.append('<ul class="nav-home">')
        out.append(render_nav_link(current, home, "Home", "nav-link nav-link--home"))
        out.append("</ul>")
    for title, items in groups.items():
        out.append('<div class="nav-group">')
        out.append(f'<p class="nav-group-title">{html.escape(title)}</p><ul>')
        out.extend(render_nav_link(current, page) for page in items)
        out.append("</ul></div>")

    extras = [page for page in pages if page.group == "More documents"]
    if extras:
        out.append('<details class="nav-group nav-group--more" open>')
        out.append(f'<summary class="nav-group-title">More documents ({len(extras)})</summary><ul>')
        out.extend(render_nav_link(current, page) for page in extras)
        out.append("</ul></details>")
    out.append("</nav>")
    return "\n".join(out)


def render_toc(page: Page, css: str) -> str:
    items = [
        f'<li class="toc-level-{section.level}">'
        f'<a href="#{html.escape(section.id, quote=True)}">{html.escape(section.title)}</a></li>'
        for section in page.toc
        if section.id
    ]
    if len(items) < 2:
        return ""
    return (
        f'<nav class="{css}" aria-label="On this page">'
        '<p class="toc-title">On this page</p>'
        f'<ul>{"".join(items)}</ul></nav>'
    )


def render_pager(current: Page, pages: Sequence[Page]) -> str:
    ordered = [page for page in pages if page.nav]
    if current not in ordered:
        return ""
    index = ordered.index(current)
    parts: list[str] = []
    if index > 0:
        previous = ordered[index - 1]
        parts.append(
            f'<a class="pager-link pager-prev" rel="prev"'
            f' href="{html.escape(current.link(previous), quote=True)}">'
            f'<span class="pager-label">Previous</span>'
            f'<span class="pager-title">{html.escape(previous.title)}</span></a>'
        )
    if index + 1 < len(ordered):
        following = ordered[index + 1]
        parts.append(
            f'<a class="pager-link pager-next" rel="next"'
            f' href="{html.escape(current.link(following), quote=True)}">'
            f'<span class="pager-label">Next</span>'
            f'<span class="pager-title">{html.escape(following.title)}</span></a>'
        )
    return f'<nav class="pager" aria-label="Page navigation">{"".join(parts)}</nav>' if parts else ""


def render_breadcrumbs(page: Page) -> str:
    if page.is_home:
        return ""
    crumbs: list[str] = []
    if page.group and page.group != "More documents":
        crumbs.append(f'<span class="crumb-group">{html.escape(page.group)}</span>')
    crumbs.append(f'<span aria-current="page">{html.escape(page.heading or page.title)}</span>')
    return f'<nav class="breadcrumbs" aria-label="Breadcrumb">{" <span>/</span> ".join(crumbs)}</nav>'


def expand(template: str, tokens: dict[str, str]) -> str:
    """Replace ``{{TOKEN}}`` placeholders, failing loudly on an unknown key."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in tokens:
            raise KeyError(f"unknown template token in base.html: {key}")
        return tokens[key]

    return re.sub(r"\{\{([A-Z_]+)\}\}", replace, template)


def render_shell(
    *,
    page: Page,
    site: Site,
    body: str,
    content: str,
    description: str,
    title: str,
    heading: str,
    group: str,
    tokens: dict[str, str] | None = None,
) -> str:
    """Render one page through ``templates/base.html``."""
    template = (THEME_DIR / "templates" / "base.html").read_text(encoding="utf-8")
    values = {
        "LANG": "en",
        "SITE_NAME": SITE_NAME,
        "SITE_TAGLINE": SITE_TAGLINE,
        "REL": page.rel,
        "TITLE": html.escape(title),
        "HEADING": html.escape(heading),
        "DESCRIPTION": html.escape(description, quote=True),
        "CANONICAL": html.escape(f"{site.site_url}{page.url}", quote=True),
        "NAV": render_nav(page, site.pages),
        "BREADCRUMBS": render_breadcrumbs(page),
        "CONTENT": content,
        "TOC_DESKTOP": render_toc(page, "page-toc"),
        "TOC_MOBILE": render_toc(page, "page-toc page-toc--mobile"),
        "PAGER": render_pager(page, site.pages),
        "EDIT_URL": html.escape(page.edit_url, quote=True),
        "SOURCE_PATH": html.escape(page.source, quote=True),
        "REPO_URL": html.escape(REPO_URL, quote=True),
        "SITE_URL": html.escape(site.site_url, quote=True),
        "AUTHOR": html.escape(SITE_AUTHOR),
        "COPYRIGHT": html.escape(COPYRIGHT),
        "BODY_CLASS": body,
        "GROUP": html.escape(group),
        "ICON_SEARCH": icon_svg("search"),
        "ICON_SUN": icon_svg("sun"),
        "ICON_MOON": icon_svg("moon"),
        "ICON_MENU": icon_svg("menu"),
        "ICON_CLOSE": icon_svg("close"),
        "ICON_EXTERNAL": icon_svg("external"),
        "ICON_PENCIL": icon_svg("pencil"),
        "ICON_LIST": icon_svg("list"),
    }
    values.update(tokens or {})
    return expand(template, values)


def render_document_page(page: Page, site: Site) -> str:
    return render_shell(
        page=page,
        site=site,
        body="is-home" if page.is_home else "",
        content=page.body,
        description=page.description,
        title=f"{page.heading or page.title} · {SITE_NAME}" if not page.is_home
        else f"{SITE_NAME} — {SITE_TAGLINE}",
        heading=page.heading or page.title,
        group=page.group,
    )


def render_index_page(pages: Sequence[Page], site: Site) -> str:
    """A generated directory of every rendered document."""
    synthetic = Page(
        source="(generated)",
        url="all-pages/",
        target="all-pages/index.html",
        out_file="all-pages/index.html",
        title="All pages",
        group="Index",
        order=len(pages),
    )
    synthetic.heading = "All pages"
    synthetic.description = "Every document published on this site, in reading order."

    groups: dict[str, list[Page]] = {}
    for page in pages:
        groups.setdefault(page.group, []).append(page)

    parts = [
        '<h1 id="all-pages">All pages</h1>',
        "<p>Every document in this site, in reading order. The same list is "
        "published as <code>sitemap.xml</code> for crawlers, and the sidebar links "
        "back here from every page.</p>",
    ]
    for group, items in groups.items():
        parts.append(f'<h2 id="group-{slugify_stem(group)}">{html.escape(group)}</h2>')
        parts.append('<ul class="page-list">')
        for page in items:
            parts.append(
                f'<li><a href="{html.escape(synthetic.link(page), quote=True)}">{html.escape(page.title)}</a>'
                f'<span class="page-list-path">{html.escape(page.source)}</span>'
                f'<p class="page-list-desc">{html.escape(page.description)}</p></li>'
            )
        parts.append("</ul>")

    return render_shell(
        page=synthetic,
        site=site,
        body="is-index",
        content="\n".join(parts),
        description=synthetic.description,
        title=f"All pages · {SITE_NAME}",
        heading="All pages",
        group="Index",
        tokens={"PAGER": "", "BREADCRUMBS": ""},
    )


# --------------------------------------------------------------------------- #
# Search index, sitemap, robots
# --------------------------------------------------------------------------- #

def js_json(value: Any) -> str:
    """JSON that is also safe to embed inside a ``<script>`` element."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (
        text.replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def build_search_index(pages: Sequence[Page]) -> str:
    payload = [
        {
            "u": page.target,
            "t": page.title,
            "h": page.heading,
            "g": page.group,
            "s": [
                {"i": section.id, "t": section.title, "x": section.text[:8000]}
                for section in page.sections
                if section.text
            ],
        }
        for page in pages
    ]
    return (
        "/* Generated by tools/build_site.py — client-side search corpus. */\n"
        f"window.PACIFICA_SEARCH_INDEX={js_json(payload)};\n"
    )


def build_sitemap(pages: Sequence[Page], site: Site) -> str:
    rows = [f"  <url><loc>{html.escape(site.site_url + page.url)}</loc></url>" for page in pages]
    rows.append(f"  <url><loc>{html.escape(site.site_url)}all-pages/</loc></url>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(rows)
        + "\n</urlset>\n"
    )


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #

def default_out_dir(root: Path) -> Path:
    """``site/`` when .gitignore covers it, ``build/site/`` when it does not."""
    gitignore = root / ".gitignore"
    if gitignore.is_file():
        for raw in gitignore.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip().lstrip("/")
            if not line or line.startswith("#"):
                continue
            if line.rstrip("/").split("*")[0].rstrip("/") == "site":
                return root / "site"
    return root / "build" / "site"


def check_out_dir(out_dir: Path, root: Path, force: bool) -> Path:
    """Validate an output path; raise `SystemExit` when replacing it would be unsafe.

    Refusals: a symlinked `--out` (its target is not what the caller named), the
    repository root itself, anything inside a source tree (`docs/`, `src/`,
    `tools/`, `.github/`, `.git/`), any ancestor of the repository, and a
    non-empty directory that does not carry this generator's marker unless
    `--force` is given.  `build/site/` and `site/` are fine: they are build output.
    Nothing is deleted here.
    """
    if out_dir.is_symlink():
        raise SystemExit(f"refusing to build into {out_dir}: it is a symlink")
    resolved = out_dir.resolve()
    root_resolved = root.resolve()
    if resolved in root_resolved.parents:
        raise SystemExit(f"refusing to build into {resolved}: it contains the repository")
    if resolved == root_resolved:
        raise SystemExit(f"refusing to build into {resolved}: that is the repository root")
    for name in (DOCS_DIRNAME, "src", "tools", ".github", ".git"):
        directory = (root / name).resolve()
        if resolved == directory or resolved.is_relative_to(directory):
            raise SystemExit(f"refusing to build into {resolved}: that is a source directory")
    if resolved.exists():
        if not resolved.is_dir():
            raise SystemExit(f"refusing to overwrite {resolved}: not a directory")
        entries = list(resolved.iterdir())
        if entries and not (resolved / BUILD_MARKER).is_file() and not force:
            raise SystemExit(
                f"refusing to delete {resolved}: it does not look like a previous build "
                f"(no {BUILD_MARKER}; pass --force to overwrite it)"
            )
    return resolved


def write_build_marker(directory: Path, **extra: Any) -> None:
    """Claim a directory as this generator's output (also the build manifest)."""
    manifest: dict[str, Any] = {"generator": "tools/build_site.py"}
    manifest.update(extra)
    (directory / BUILD_MARKER).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def build(options: argparse.Namespace) -> int:
    root = Path(options.root).resolve()
    if not (root / DOCS_DIRNAME).is_dir():
        raise SystemExit(f"{root} does not look like the repository root (no {DOCS_DIRNAME}/)")

    warnings: list[str] = []
    unpublished: list[str] = []
    out_dir = Path(options.out) if options.out else default_out_dir(root)
    if not options.out:
        inside = out_dir.relative_to(root) if out_dir.is_relative_to(root) else out_dir
        warnings.append(f"no --out given: writing to {inside}")
    out_dir = check_out_dir(out_dir, root, options.force)

    privacy = privacy_rules(root, options.include_ignored, warnings)
    private_paths = privacy.paths_under(root, DOCS_DIRNAME)

    # Render into a sibling staging directory and swap it in only on success, so a
    # failed build never deletes the previous site (the marker is written first, so
    # a staging directory left behind by a crash is still replaceable).
    staging = out_dir.with_name(f"{out_dir.name}.building")
    if staging.exists():
        check_out_dir(staging, root, force=options.force)
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    write_build_marker(staging)

    try:
        site = Site(root=root, out_dir=staging, site_url=options.site_url.rstrip("/") + "/")
        site.pages = build_pages(root, warnings, privacy)
        if not site.pages:
            raise SystemExit("no documents found under docs/")

        pages_by_source = {page.source: page.target for page in site.pages}
        for page in site.pages:
            render_markdown(page, root, pages_by_source, unpublished, privacy)

        assets = staging / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        for path in sorted((THEME_DIR / "static").iterdir()):
            if path.is_file():
                shutil.copy2(path, assets / path.name)

        for page in site.pages:
            destination = staging / page.out_file
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(render_document_page(page, site), encoding="utf-8")

        (staging / "all-pages").mkdir(parents=True, exist_ok=True)
        (staging / "all-pages" / "index.html").write_text(
            render_index_page(site.pages, site), encoding="utf-8"
        )
        (assets / "search-index.js").write_text(build_search_index(site.pages), encoding="utf-8")
        (staging / "sitemap.xml").write_text(build_sitemap(site.pages, site), encoding="utf-8")
        (staging / "robots.txt").write_text(
            f"User-agent: *\nAllow: /\nSitemap: {site.site_url}sitemap.xml\n", encoding="utf-8"
        )
        (staging / ".nojekyll").write_text("", encoding="utf-8")
        write_build_marker(
            staging,
            pages=len(site.pages),
            site_url=site.site_url,
            # A count, not the names: this file is part of the published output.
            private_documents_skipped=len(private_paths),
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # Move the old output aside instead of deleting it, so a failure while the new
    # tree is put in place can still restore what was published before.
    previous = check_out_dir(out_dir, root, options.force)
    backup = out_dir.with_name(f"{out_dir.name}.previous")
    if backup.exists():
        check_out_dir(backup, root, force=options.force)
        shutil.rmtree(backup)
    if previous.exists():
        previous.rename(backup)
    try:
        staging.rename(out_dir)
    except OSError as error:
        if not out_dir.exists() and backup.exists():
            backup.rename(out_dir)
        raise SystemExit(f"could not replace {out_dir}: {error}") from error
    shutil.rmtree(backup, ignore_errors=True)

    print(f"built {len(site.pages)} pages + all-pages/ into {out_dir}")
    for page in site.pages:
        print(f"  {page.out_file:<34} {page.title}")
    if unpublished:
        warnings.append(
            f"{len(set(unpublished))} link(s) point at unpublished repository files "
            "(rewritten to GitHub)"
        )
    # Warnings are always visible: they explain missing nav entries, documents that
    # are private by .gitignore, and links that leave the published site.
    for warning in warnings:
        print(f"  ! {warning}", file=sys.stderr)
    if options.verbose:
        for entry in sorted(set(unpublished)):
            print(f"    · {entry}", file=sys.stderr)
    return 0


def privacy_rules(root: Path, include_ignored: bool, warnings: list[str]) -> Privacy:
    """Decide which documents ``.gitignore`` marks private.

    Prefers git's own semantics (an ignored *and untracked* file is private) using
    the repository's index.  When tracked status cannot be established — no ``.git``,
    a split index, index format v4 — the ignore rules are still applied, because
    publishing personal vehicle data is the worse failure; ``--include-ignored`` is
    the deliberate escape hatch.
    """
    if include_ignored:
        return Privacy()
    rules = collect_ignore_rules(root)
    if not rules:
        return Privacy()
    tracked = tracked_paths(root)
    if tracked is None:
        warnings.append(
            "could not read .git/index: documents matched by .gitignore are skipped "
            "as a precaution (use --include-ignored to publish them anyway)"
        )
        return Privacy(rules=rules, tracked=frozenset())
    return Privacy(rules=rules, tracked=tracked)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_site.py",
        description="Render docs/*.md into a static site (no MkDocs, no Node).",
    )
    parser.add_argument("--out", default=None, help="output directory (default: site/ or build/site/)")
    parser.add_argument("--root", default=str(ROOT), help="repository root (default: this repository)")
    parser.add_argument("--site-url", default=SITE_URL, help="absolute site URL used for canonical links")
    parser.add_argument("--force", action="store_true", help="overwrite a non-empty --out directory")
    parser.add_argument(
        "--include-ignored",
        action="store_true",
        help="also publish documents that .gitignore marks private (for local previews)",
    )
    parser.add_argument("--verbose", action="store_true", help="print build warnings and link notes")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    return build(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
