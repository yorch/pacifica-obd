"""Static-asset checks for the build-free front end.

    uv run python tests/test_web_static.py

These are cheap guards that catch the class of mistake the UI is prone to: a
typo'd ES module (which only shows up as an empty screen in a browser), a page
that references a missing asset, markup injected from data, or a vendored file
losing its licence text. No browser or Node is required — Node is used when it is
present for a real parse.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STATIC = Path(__file__).resolve().parents[1] / "src" / "pacifica_obd" / "web" / "static"

# Everything index.html pulls in, plus every module reachable from app.js.
REQUIRED = [
    "index.html",
    "css/app.css",
    "css/print.css",
    "vendor/uplot.min.js",
    "vendor/uplot.min.css",
    "vendor/uPlot.LICENSE.txt",
    "js/app.js",
    "js/api.js",
    "js/stream.js",
    "js/dom.js",
    "js/router.js",
    "js/format.js",
    "js/components.js",
    "js/charts.js",
    "js/views/dashboard.js",
    "js/views/live.js",
    "js/views/modules.js",
    "js/views/dids.js",
    "js/views/capture.js",
    "js/views/db.js",
    "js/views/reports.js",
    "js/views/settings.js",
    "js/views/help.js",
]

# Classes whose whole point is that no key/secret or real vehicle data is in the repo.
FORBIDDEN_SNIPPETS = ["sk-", "AIza", "innerHTML", "insertAdjacentHTML", "document.write"]


def js_files() -> list[Path]:
    return sorted(STATIC.rglob("*.js"))


def test_every_required_asset_exists_and_is_not_empty():
    missing = [name for name in REQUIRED if not (STATIC / name).exists()]
    assert not missing, missing
    for name in REQUIRED:
        assert (STATIC / name).stat().st_size > 0, name


def test_index_html_wires_up_the_shell():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for needle in ("/css/app.css", "/css/print.css", "/vendor/uplot.min.js",
                   '/js/app.js', 'id="view"', 'id="rail"', 'id="toasts"'):
        assert needle in html, needle
    assert 'type="module"' in html
    # No inline script/style: the CSP is strict (script-src 'self').
    assert "<script>" not in html
    assert " style=" not in html


def test_no_markup_injection_helpers_are_used():
    for path in [*js_files(), STATIC / "index.html"]:
        text = path.read_text(encoding="utf-8")
        if path.name == "dom.js":
            # dom.js documents the rule and throws on `html:`; it must not call it.
            assert "innerHTML" in text
            assert "node.innerHTML" not in text
            continue
        for snippet in FORBIDDEN_SNIPPETS:
            assert snippet not in text, f"{path.name} contains {snippet!r}"


def test_vendored_uplot_keeps_its_licence_and_is_mit():
    licence = (STATIC / "vendor" / "uPlot.LICENSE.txt").read_text(encoding="utf-8")
    assert "MIT License" in licence and "Leon Sorokin" in licence
    header = (STATIC / "vendor" / "uplot.min.js").read_text(encoding="utf-8")[:200]
    assert "uPlot" in header
    assert "leeoniya/uPlot" in header


def test_every_module_parses():
    """A real ESM parse when Node is available, so a typo cannot ship."""
    node = shutil.which("node")
    files = js_files()
    if node is None:
        # Fallback: a bracket-balance sweep that ignores strings and comments.
        for path in files:
            assert _balanced(path.read_text(encoding="utf-8")), path.name
        print("    (node not found: used the bracket-balance fallback)")
        return
    for path in files:
        source = path.read_text(encoding="utf-8")
        # `--input-type=module` is essential: a plain `node --check file.js` parses
        # as CommonJS and silently accepts broken ES-module syntax.
        result = subprocess.run([node, "--check", "--input-type=module"],
                                input=source, capture_output=True, text=True)
        assert result.returncode == 0, f"{path.name}: {result.stderr.strip()[:400]}"


def _balanced(source: str) -> bool:
    """Rough bracket balance, skipping comments and string/template literals."""
    stack: list[str] = []
    index = 0
    length = len(source)
    pairs = {")": "(", "]": "[", "}": "{"}
    while index < length:
        char = source[index]
        if char == "/" and index + 1 < length and source[index + 1] == "/":
            index = source.find("\n", index)
            if index == -1:
                break
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "*":
            index = source.find("*/", index + 2)
            if index == -1:
                break
            index += 2
            continue
        if char in ("'", '"', "`"):
            quote = char
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack[-1] != pairs[char]:
                return False
            stack.pop()
        index += 1
    return not stack


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all static-asset checks passed")
