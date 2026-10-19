"""Design-consistency guards for the build-free front end.

    uv run python tests/test_design_consistency.py

This is the mechanical half of the UI review: it freezes the design system's
invariants so the next drift is caught by a command instead of by eye. It needs
no browser, no Node and no new dependency — it reads the shipped CSS and ES
modules as text.

What is enforced
----------------
1. **Token resolution.** Every `var(--token)` used anywhere in the CSS or the
   modules resolves to a custom property defined in `app.css` (`:root` or
   `[data-theme="dark"]`) or in `print.css` (the paper re-map). A `var()` with a
   silent fallback to a made-up name is how a palette quietly forks.
2. **No colour literals outside the tokens.** A hex/rgb/hsl colour is allowed
   only on a custom-property definition line — in `app.css` (section 1, the
   palette) or in `print.css` (which re-points the same names for paper). A view
   or a module must not introduce its own hue.
3. **Semantic contrast.** The foreground/background *pairs that carry meaning*
   are extracted from the token blocks and measured with the WCAG 2.x formula:
   >= 4.5:1 for text, >= 3:1 for marks, lines, icons and control borders, in both
   themes. The whole table is printed so the numbers can be checked by hand.
   Deliberately excluded pairs are listed in `DECORATIVE_PAIRS` with a reason
   each: they are tint or hairline only, and the information they sit behind is
   carried by a passing pair (the word on a pill, the major tick on a gauge).
4. **Component reuse.** Every view imports the shared primitives and hand-rolls
   none of them: no `h("div.card")`, no `card__body`/`card__head` markup, no
   hand-assembled `pill` class. `BESPOKE_VIEWS` documents any view allowed to
   skip the primitives it genuinely does not need.
5. **Spacing and radius discipline.** Literal `px` in a spacing property
   (padding/margin/gap) and any `border-radius` outside the token scale must be
   on the documented allowlist; inline `style` objects may not set spacing at
   all. The allowlist is small on purpose — a new ad-hoc gap fails here.
"""
from __future__ import annotations

import glob
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parents[1] / "src" / "pacifica_obd" / "web" / "static"
APP_CSS = ROOT / "css" / "app.css"
PRINT_CSS = ROOT / "css" / "print.css"
COMPONENTS_JS = ROOT / "js" / "components.js"

VIEW_FILES = sorted((ROOT / "js" / "views").glob("*.js"))

# `vendor/` is third-party and out of scope for the design system.
def css_files() -> list[Path]:
    return sorted((ROOT / "css").glob("*.css"))


def js_files() -> list[Path]:
    return sorted(p for p in (ROOT / "js").rglob("*.js") if "vendor" not in p.parts)


# --------------------------------------------------------------- 1. tokens --

def _token_block(text: str, selector: str) -> dict[str, str]:
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", text, re.S)
    if not match:
        raise AssertionError(f"no {selector!r} block found")
    return {name: value.strip()
            for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", match.group(1))}


def themes() -> tuple[dict[str, str], dict[str, str]]:
    app = APP_CSS.read_text(encoding="utf-8")
    return _token_block(app, ":root"), _token_block(app, '[data-theme="dark"]')


def defined_tokens() -> set[str]:
    """Every token name the app defines for the screen or for paper."""
    light, dark = themes()
    print_ = _token_block(PRINT_CSS.read_text(encoding="utf-8"), ':root, [data-theme="dark"]')
    return set(light) | set(dark) | set(print_)


def test_every_css_variable_resolves():
    """A `var(--x)` with no definition is a silently broken colour or size."""
    known = defined_tokens()
    # `var(--name)` / `var(--name, fallback)` — a template-built name such as
    # `var(--series-${i})` deliberately does not match, because the palette must
    # be referenced literally for this check to mean anything.
    pattern = re.compile(r"var\(\s*(--[\w-]+)\s*[,)]")
    missing: list[str] = []
    for path in sorted(set(css_files()) | set(js_files())):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in pattern.findall(line):
                if name not in known:
                    missing.append(f"{path.relative_to(ROOT)}:{number}: var({name}) is not defined")
    assert not missing, "unresolved custom properties:\n  " + "\n  ".join(missing)
    print(f"    {len(known)} tokens defined; every var() reference resolves")


# ------------------------------------------------------------- 2. colours --

# Only machine-readable colour literals are checked. Named colours are not, so a
# prose "grey hairline" in a hint string is not a false positive.
COLOUR_LITERAL = re.compile(
    r"#[0-9a-fA-F]{3,8}(?![0-9a-zA-Z_-])"
    r"|\brgba?\s*\("
    r"|\bhsla?\s*\(",
)
# A line that defines a custom property: the palette lives here and nowhere else.
TOKEN_DEFINITION = re.compile(r"^\s*--[\w-]+\s*:")


def test_no_colour_literals_outside_the_tokens():
    offenders: list[str] = []
    for path in css_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if TOKEN_DEFINITION.match(line):
                continue
            for hit in COLOUR_LITERAL.findall(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {hit} in {line.strip()[:70]!r}")
    for path in js_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for hit in COLOUR_LITERAL.findall(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {hit} in {line.strip()[:70]!r}")
    assert not offenders, (
        "a colour literal outside the token definitions (move it to app.css "
        "section 1, or to print.css if it is paper):\n  " + "\n  ".join(offenders)
    )
    print("    no colour literal outside a token definition (app.css or print.css)")


# ------------------------------------------------------------ 3. contrast --

def _resolve(theme: dict[str, str], name: str) -> str:
    """Follow `var(--other)` references so a token alias still measures."""
    seen: set[str] = set()
    value = theme.get(name, "")
    while isinstance(value, str) and value.startswith("var("):
        inner = re.match(r"var\(\s*(--[\w-]+)\s*\)", value)
        if not inner or inner.group(1) in seen:
            raise AssertionError(f"cannot resolve {name} -> {value!r}")
        seen.add(inner.group(1))
        value = theme.get(inner.group(1), "")
    if not value.startswith("#"):
        raise AssertionError(f"{name} is not a hex colour on this theme: {value!r}")
    return value


def _channel(value: float) -> float:
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    text = hex_colour.lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    red, green, blue = (int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast(foreground: str, background: str) -> float:
    first, second = luminance(foreground), luminance(background)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


# (foreground, background, kind) — the pairs whose contrast carries information.
TEXT_PAIRS: list[tuple[str, str]] = [
    *[(fg, bg) for fg in ("--text", "--text-2", "--text-muted")
      for bg in ("--canvas", "--panel", "--panel-soft", "--sunken", "--raised")],
    *[(f"--{ramp}-fg", f"--{ramp}-bg") for ramp in
      ("ok", "warn", "danger", "info", "idle", "untested")],
    *[(f"--{ramp}-fg", "--panel") for ramp in
      ("ok", "warn", "danger", "info", "idle", "untested")],
    ("--accent", "--accent-soft"), ("--accent", "--panel"),
    ("--on-accent", "--accent"), ("--on-danger", "--danger-solid"),
    ("--text-inverse", "--inverse"), ("--chart-axis", "--panel"),
]

MARK_PAIRS: list[tuple[str, str]] = [
    *[(f"--{ramp}-solid", "--panel") for ramp in
      ("ok", "warn", "danger", "info", "idle", "untested")],
    ("--accent", "--panel"),
    ("--border-input", "--panel"),      # the boundary of a form control
    ("--chart-crosshair", "--panel"),
    ("--gauge-value", "--panel"), ("--gauge-redline", "--panel"),
    ("--gauge-tick-major", "--panel"), ("--gauge-stale", "--panel"),
    ("--signal", "--panel"),
    # The arcs and bar fills are drawn *on* the gauge track, so the pair that
    # actually has to read is the mark against the track, not against the panel.
    ("--gauge-value", "--gauge-track"), ("--gauge-stale", "--gauge-track"),
    ("--gauge-crit", "--gauge-track"), ("--warn-solid", "--gauge-track"),
    ("--accent", "--gauge-track"),
    *[(f"--series-{index}", "--panel") for index in range(1, 9)],
]

# Measured and printed, but not asserted: these are tint, hairline or inactive
# chrome, and each sits behind a pair that does pass.
DECORATIVE_PAIRS: dict[tuple[str, str], str] = {
    ("--text-disabled", "--panel"): "inactive control; WCAG 1.4.3 exempts disabled UI",
    ("--text-disabled", "--canvas"): "inactive control; WCAG 1.4.3 exempts disabled UI",
    ("--border-subtle", "--panel"): "hairline divider, never the only signal",
    ("--border", "--panel"): "hairline divider, never the only signal",
    ("--border-strong", "--panel"): "hairline on a control whose label carries it at 17:1",
    ("--chart-grid", "--panel"): "chart gridline; the axis labels and marks carry it",
    ("--gauge-track", "--panel"): "gauge track behind the value arc",
    ("--gauge-tick", "--panel"): "minor tick; the major ticks carry the scale",
    ("--gauge-warn-zone", "--panel"): "zone fill; the warn mark on the outer ring carries it",
    ("--gauge-redline-zone", "--panel"): "zone fill; the redline mark on the outer ring carries it",
}
DECORATIVE_PAIRS.update({
    (f"--{ramp}-line", "--panel"): "pill tint border; the pill's word carries it"
    for ramp in ("ok", "warn", "danger", "info", "idle", "untested")
})


def _pairs_for(theme: dict[str, str]) -> list[tuple[str, str, float, float, str]]:
    rows = []
    for foreground, background in TEXT_PAIRS:
        rows.append((foreground, background,
                     contrast(_resolve(theme, foreground), _resolve(theme, background)), 4.5, "text"))
    for foreground, background in MARK_PAIRS:
        rows.append((foreground, background,
                     contrast(_resolve(theme, foreground), _resolve(theme, background)), 3.0, "mark"))
    return rows


def test_semantic_contrast_in_both_themes():
    light, dark = themes()
    failures: list[str] = []
    for name, theme in (("light", light), ("dark", dark)):
        print(f"    -- {name} --")
        for foreground, background, ratio, threshold, kind in _pairs_for(theme):
            verdict = "ok" if ratio >= threshold else "FAIL"
            print(f"    {verdict:4} {ratio:6.2f}:1  {foreground:<22} on {background:<14} ({kind} >= {threshold})")
            if ratio < threshold:
                failures.append(
                    f"{name}: {foreground} on {background} = {ratio:.2f}:1, need {threshold}:1 ({kind})")
        for (foreground, background), reason in DECORATIVE_PAIRS.items():
            ratio = contrast(_resolve(theme, foreground), _resolve(theme, background))
            print(f"    note {ratio:6.2f}:1  {foreground:<22} on {background:<14} (decorative: {reason})")
    # Show the arithmetic for a representative pair so the numbers can be checked.
    print("    arithmetic: relative luminance L = 0.2126R + 0.7152G + 0.0722B over")
    print("    linearised sRGB channels; ratio = (Lmax + 0.05) / (Lmin + 0.05).")
    print(f"    example: light --text {_resolve(light, '--text')} on --panel "
          f"{_resolve(light, '--panel')} = "
          f"{contrast(_resolve(light, '--text'), _resolve(light, '--panel')):.2f}:1")
    print(f"    example: dark  --accent {_resolve(dark, '--accent')} on --accent-soft "
          f"{_resolve(dark, '--accent-soft')} = "
          f"{contrast(_resolve(dark, '--accent'), _resolve(dark, '--accent-soft')):.2f}:1")
    assert not failures, "contrast below threshold:\n  " + "\n  ".join(failures)


# --------------------------------------------------------- 4. composition --

# Views that may legitimately not use the panel primitives. Empty today: every
# screen renders panels, tables or empty/error states through components.js. A
# genuinely bespoke screen (a full-bleed canvas, say) would be named here with
# the reason it is exempt.
BESPOKE_VIEWS: dict[str, str] = {}


def test_every_view_uses_the_shared_primitives():
    problems: list[str] = []
    for path in VIEW_FILES:
        name = path.name
        source = path.read_text(encoding="utf-8")
        import_block = re.search(r'import \{([^}]*)\} from "\.\./components\.js";', source, re.S)
        if not import_block:
            problems.append(f"{name}: no import from components.js")
            continue
        imported = {token.strip() for token in re.split(r"[,\n]", import_block.group(1)) if token.strip()}
        if "card" not in imported and name not in BESPOKE_VIEWS:
            problems.append(f"{name}: does not use card()")
        if not imported & {"dataTable", "emptyState", "errorState", "loadingState", "feed"}:
            problems.append(f"{name}: uses none of dataTable/emptyState/errorState/loadingState/feed")
        # Rendered prefix: the modules a view actually imports from components.js.
        body = source[import_block.end():]
        for needle, what in (
            ('h("div.card"', "a card built by hand instead of card()"),
            ('h("section.card"', "a card built by hand instead of card()"),
            ("card__body", "card body markup built by hand instead of card()"),
            ("card__head", "card head markup built by hand instead of card()"),
            ('class: "pill', "a pill built by hand instead of pill()/tonePill()"),
            ("class: `pill", "a pill built by hand instead of pill()/tonePill()"),
        ):
            if needle in body:
                problems.append(f"{name}: {what}")
        if '"pad"' in body or "'pad'" in body:
            problems.append(f"{name}: pads a card body by hand (.pad inside .card__body already pads)")
    assert not problems, "component drift:\n  " + "\n  ".join(problems)
    print(f"    {len(VIEW_FILES)} views; all use card() plus a shared state primitive, none hand-rolled")


# ---------------------------------------------- 5. spacing / radius rules --

SPACING_PROPERTY = re.compile(
    r"^\s*(padding|padding-[a-z]+|margin|margin-[a-z]+|gap|row-gap|column-gap)\s*:\s*(.*?);")
LITERAL_PX = re.compile(r"-?\d+(?:\.\d+)?px")

# Literal px still allowed in a spacing property, with the reason it is not a
# token. Keep this tiny: a new entry means someone introduced a one-off gap.
ALLOWED_SPACING_PX: dict[str, str] = {
    "-1px": "the 1px overlap that makes a tab underline sit on the border it marks",
    "2px": "the inset of a sub-control inside its own 2px-padded track",
}
SPACING_ALLOWED_TOTAL = 4

RADIUS_TOKEN = re.compile(r"var\(--radius-[\w-]+\)")
INLINE_STYLE_BLOCK = re.compile(r"style:\s*\{([^}]*)\}", re.S)


def test_spacing_and_radius_stay_on_the_scale():
    counted: list[str] = []
    for path in css_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            spacing = SPACING_PROPERTY.match(line)
            if spacing:
                for literal in LITERAL_PX.findall(spacing.group(2)):
                    counted.append(f"{path.relative_to(ROOT)}:{number}: {spacing.group(1)}: {literal}"
                                   f"  ({ALLOWED_SPACING_PX.get(literal, 'NOT ALLOWED')})")
            radius = re.match(r"^\s*border-radius\s*:\s*(.*?);", line)
            if radius and LITERAL_PX.search(radius.group(1)) and not RADIUS_TOKEN.search(radius.group(1)):
                counted.append(f"{path.relative_to(ROOT)}:{number}: border-radius: {radius.group(1)}"
                               "  (NOT ALLOWED: use a --radius-* token)")

    offenders = [entry for entry in counted if "NOT ALLOWED" in entry]
    # Inline styles may not set spacing at all: that is what a class is for.
    for path in js_files():
        source = path.read_text(encoding="utf-8")
        for match in INLINE_STYLE_BLOCK.finditer(source):
            number = source[:match.start()].count("\n") + 1
            for key in re.findall(r"(margin[A-Za-z]*|padding[A-Za-z]*|gap|rowGap|columnGap)\s*:", match.group(1)):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: inline style sets {key} — use a class")

    assert not offenders, "off-scale spacing/radius:\n  " + "\n  ".join(offenders)
    assert len(counted) <= SPACING_ALLOWED_TOTAL, (
        f"{len(counted)} literal px in spacing properties, allowed {SPACING_ALLOWED_TOTAL}:\n  "
        + "\n  ".join(counted))
    print(f"    {len(counted)} documented literal px in spacing properties "
          f"(limit {SPACING_ALLOWED_TOTAL}); every border-radius uses a token")
    for entry in counted:
        print(f"      {entry}")


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            print(f"{name}")
            func()
            print(f"ok  {name}")
    print("all design-consistency checks passed")
