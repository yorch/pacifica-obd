"""Seed→key helpers: pluggable providers and a short-secret brute-forcer.

We deliberately do **not** ship manufacturer algorithms: they are
reverse-engineered and frequently GPL/licensed, so bundling them would change
this project's licence and expose us to claims. Instead:

* supply your own ``key_for(seed, **ctx) -> int`` function in a Python file and
  point ``security --provider`` at it; or
* recover a short per-ECU secret from observed ``(seed, key)`` pairs plus an
  algorithm *family* you provide (Stellantis ECUs typically use a 2-byte key),
  which is only feasible because the search space is tiny.

Recovery is *conditional*: matching pairs do not prove the algorithm family is
correct, so we require distinct observations and refuse to guess when more than
one secret fits.

Nothing here touches hardware; it is pure and unit-testable.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Callable, Iterable

# A provider maps an observed seed to the key the ECU expects.
SeedKeyProvider = Callable[..., "int | None"]
# An algorithm family maps (seed, secret) to the key for a given per-ECU secret.
AlgorithmFamily = Callable[[int, int], int]

# Practical ceiling: a 4-byte secret is 2**32 candidates (minutes, not hours).
MAX_WIDTH = 4

_HEX = re.compile(r"^[0-9A-Fa-f]+$")


class SeedKeyError(RuntimeError):
    pass


def _check_width(width: int) -> int:
    if type(width) is not int or not 1 <= width <= MAX_WIDTH:
        raise SeedKeyError(f"width must be 1..{MAX_WIDTH} bytes")
    return width


def _load_callable(path, name: str):
    file = Path(path).expanduser()
    if not file.exists():
        raise SeedKeyError(f"file not found: {file}")
    spec = importlib.util.spec_from_file_location("pacifica_seedkey_plugin", file)
    if spec is None or spec.loader is None:
        raise SeedKeyError(f"cannot load: {file}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - surface plugins as a clean error
        raise SeedKeyError(f"failed to load {file}: {exc}") from exc
    func = getattr(module, name, None)
    if not callable(func):
        raise SeedKeyError(f"{file} does not define {name}()")
    return func


def load_provider(path: str | Path) -> SeedKeyProvider:
    """Import a user-supplied file exposing ``key_for(seed, **ctx) -> int``."""
    return _load_callable(path, "key_for")


def load_algorithm(path: str | Path) -> AlgorithmFamily:
    """Import a file exposing ``algorithm(seed, secret) -> key``."""
    return _load_callable(path, "algorithm")


def parse_pairs(text: str, width: int | None = None) -> list[tuple[int, int]]:
    """Parse ``SEED:KEY[,SEED:KEY...]`` (unsigned hex) into integer pairs."""
    pairs: list[tuple[int, int]] = []
    for chunk in text.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise SeedKeyError(f"pair must be SEED:KEY (hex): {chunk!r}")
        seed_hex, key_hex = (part.strip() for part in chunk.split(":", 1))
        if not _HEX.match(seed_hex) or not _HEX.match(key_hex):
            raise SeedKeyError(f"pair is not unsigned hex: {chunk!r}")
        seed, key = int(seed_hex, 16), int(key_hex, 16)
        if width is not None:
            limit = 1 << (8 * _check_width(width))
            if seed >= limit or key >= limit:
                raise SeedKeyError(f"pair exceeds {width}-byte width: {chunk!r}")
        pairs.append((seed, key))
    if not pairs:
        raise SeedKeyError("no pairs given")
    return pairs


def find_secrets(algorithm: AlgorithmFamily, pairs: Iterable[tuple[int, int]],
                 width: int = 2) -> list[int]:
    """Return every `width`-byte secret consistent with the observations.

    Distinct observations only; stops early once two candidates are found (the
    answer is then ambiguous). An empty result means no candidate fits.
    """
    _check_width(width)
    seen: set[tuple[int, int]] = set()
    observations: list[tuple[int, int]] = []
    for pair in pairs:
        if pair not in seen:
            seen.add(pair)
            observations.append(pair)
    if not observations:
        raise SeedKeyError("no observations given")

    span = 1 << (8 * width)
    mask = span - 1
    matches: list[int] = []
    for secret in range(span):
        if all(
            type(algorithm(seed, secret)) is int and (algorithm(seed, secret) & mask) == (key & mask)
            for seed, key in observations
        ):
            matches.append(secret)
            if len(matches) > 1:
                break
    return matches


def recover_secret(algorithm: AlgorithmFamily, pairs: Iterable[tuple[int, int]],
                   width: int = 2) -> int | None:
    """Return the unique consistent secret, or None if none/multiple fit."""
    matches = find_secrets(algorithm, pairs, width=width)
    return matches[0] if len(matches) == 1 else None


class SecretProvider:
    """A SeedKeyProvider backed by a recovered per-ECU secret + algorithm family."""

    def __init__(self, algorithm: AlgorithmFamily, secret: int, width: int = 2):
        _check_width(width)
        if type(secret) is not int or not 0 <= secret < (1 << (8 * width)):
            raise SeedKeyError("secret out of range for the given width")
        self.algorithm = algorithm
        self.secret = secret
        self.width = width

    def key_for(self, seed: int, **_ctx) -> int:
        key = self.algorithm(seed, self.secret)
        if type(key) is not int or not 0 <= key < (1 << (8 * self.width)):
            raise SeedKeyError("algorithm returned an invalid key")
        return key

    @classmethod
    def from_pairs(cls, algorithm: AlgorithmFamily, pairs: Iterable[tuple[int, int]],
                   width: int = 2) -> "SecretProvider":
        matches = find_secrets(algorithm, pairs, width=width)
        if not matches:
            raise SeedKeyError("no secret consistent with the supplied pairs")
        if len(matches) > 1:
            raise SeedKeyError("ambiguous: multiple secrets fit; supply more pairs")
        return cls(algorithm, matches[0], width)
