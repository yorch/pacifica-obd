# SITE.md — how the documentation site is built

The published site (<https://yorch.github.io/pacifica-obd/>) is a **hand-built
static site**. MkDocs Material was dropped: `tools/build_site.py` renders the
Markdown in `docs/` (plus the repository `README.md`) into plain HTML, and
`.github/workflows/pages.yml` publishes it to GitHub Pages.

The Markdown stays the source of truth. Nothing on the site is edited by hand.

## Build it

```sh
uv run --with-requirements requirements-docs.txt python tools/build_site.py --out /tmp/psite
python -m http.server -d /tmp/psite 8000        # optional: preview at :8000
```

`--out` is the only argument you normally need. The default output directory is
`site/` when `.gitignore` covers it and `build/site/` when it does not — this
repository's `.gitignore` deliberately does not list `site/`, so a bare
`python tools/build_site.py` writes to `build/site/` and never leaves a
committable artifact behind. CI passes `--out site` because a workflow artifact
is never committed.

| Flag | Purpose |
|---|---|
| `--out DIR` | output directory (default `site/` or `build/site/`) |
| `--site-url URL` | absolute URL used for canonical links and `sitemap.xml` |
| `--include-ignored` | also publish documents that `.gitignore` marks private |
| `--force` | overwrite a non-empty `--out` directory that is not a previous build |
| `--verbose` | print build warnings and the links rewritten to GitHub |

Dependencies are only `markdown` and `pygments`, both in
`requirements-docs.txt` (kept out of `pyproject.toml` so the published tool
grows no documentation dependency). There is no Node, Jekyll or MkDocs step.

## Layout

```
tools/build_site.py              the generator: nav, rendering, search index, sitemap
tools/site_theme/templates/base.html   the page shell ({{TOKEN}} placeholders)
tools/site_theme/static/site.css       the whole design (light + dark)
tools/site_theme/static/site.js        theme toggle, search, copy buttons, scroll-spy
tools/site_theme/static/favicon.svg
requirements-docs.txt            markdown + pygments
.github/workflows/pages.yml      build + deploy to GitHub Pages
```

Generated output (never committed): every document as `<slug>/index.html`, the
home page as `index.html`, plus `all-pages/`, `assets/`, `sitemap.xml`,
`robots.txt`, `.nojekyll` and `.site-build.json` (the build manifest, which is
also the marker that allows a rebuild to replace a previous output directory).

Every internal link points at an explicit `index.html`, so the site works when it
is opened straight from disk as well as from GitHub Pages. Canonical URLs and
`sitemap.xml` keep the shorter directory form.

## Adding a document

1. Write `docs/your-page.md`. It is picked up automatically: anything under
   `docs/` is rendered, whether or not it is in the navigation.
2. To control its position and title, add it to `NAV` in `tools/build_site.py`.
   A nav entry whose file does not exist yet is skipped with a warning, so a
   navigation entry can be committed before the document.

`docs/examples/` and `docs/stylesheets/` are skipped (they are not prose).

## What the design includes

Sticky top bar and sidebar (drawer on mobile), a hero and feature-card grid on
the home page, syntax-highlighted code blocks with copy buttons, styled
scrollable tables, `!!!`/`???` callouts, `:material-…:` icons, `++key++`
shortcuts, `{.md-button}` buttons, per-page contents with scroll-spy, heading
anchor links, an “Edit this page on GitHub” link, previous/next paging, a
persisted dark/light toggle, and client-side search over a generated index
(`assets/search-index.js`, no server, works from `file://`).

Everything is self-contained: no CDN, no web fonts, no runtime network calls.

## Documents that must not be published

A document is skipped when **both** are true: `.gitignore` covers it and git does
not track it. That mirrors git's own semantics (an ignored pattern can overlap
tracked pages — `docs/vehicle-*.md` matches both the private
`docs/vehicle-assessment.md` and the published `docs/vehicle-support.md`) and it
keeps personal vehicle data off a public site. `--include-ignored` previews such
a document locally, and the build warns whenever it skips one.

When tracked status cannot be established (no `.git`, a split index, index
format v4) the ignore rules are still applied: publishing what the repository
marks private is the worse failure, and `--include-ignored` is the way to say
otherwise. The build is not dependent on `git`: the index is read directly, and
if it cannot be read the build says so.

Documents reached through a symlink are never published, so a link named like a
public page cannot smuggle in a private or out-of-repository file.

## Known limitations

- **Raw HTML in a document passes through**, exactly as it did under MkDocs
  Material: `docs/index.md` relies on it for the hero and the feature-card grids.
  Anyone who can write to `docs/` can therefore add markup to a page, which is
  the same trust boundary as the rest of the repository.
- `docs/stylesheets/extra.css` (MkDocs Material polish) is no longer used; it is
  left in place because the Markdown in `docs/` belongs to other changes.
- Links from a document to a file outside `docs/` (for example `../AGENTS.md` or
  `../src/pacifica_obd/cli.py`) are rewritten to GitHub `blob`/`tree` links, and
  local images are rewritten to `raw.githubusercontent.com`, so that nothing is
  copied out of the repository. Run with `--verbose` to list them.
- There is no custom `404.html`: GitHub Pages serves its own.
- Tabbed content (`=== "Tab"` from pymdownx) is not implemented; `!!!`, `???`,
  icons, keys, buttons, tables, footnotes, abbreviations and code fences are.
