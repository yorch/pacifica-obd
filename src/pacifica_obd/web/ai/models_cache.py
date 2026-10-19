"""Discovered model lists, cached in the 0600 config file — never polled.

Listing a provider's models costs a network round trip (and a hosted provider may
rate-limit it), so the list is fetched **only when asked for** and kept with the
timestamp and the endpoint it came from. The cache key is ``provider +
effective base URL``: a list fetched from one endpoint, or with one key, must
never be offered for another.

The entry also records when listing is *not* available, so the settings screen can
explain why instead of showing an empty list.
"""

from __future__ import annotations

import datetime as dt

from pacifica_obd.web.ai.keys import KeyStoreError, read_json_file, write_json_file

__all__ = [
    "CACHE_FIELD",
    "DEFAULT_TTL_S",
    "cache_key",
    "read_entry",
    "store",
    "clear",
    "is_stale",
]

#: The field inside the AI config file that holds the cache.
CACHE_FIELD = "model_cache"

#: A day is long enough that a list feels live, short enough that a retired model
#: does not linger for months.
DEFAULT_TTL_S = 24 * 3600

#: Cap the cache so a provider with a huge catalogue cannot grow the file forever.
MAX_MODELS = 500


def cache_key(provider: str | None, base_url: str | None) -> str:
    """Identify a listing by provider *and* endpoint (key-independent)."""
    return "%s|%s" % ((provider or "").strip().lower(),
                      (base_url or "").strip().rstrip("/"))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _read_cache() -> dict:
    try:
        data = read_json_file()
    except KeyStoreError:
        return {}
    cache = data.get(CACHE_FIELD)
    return cache if isinstance(cache, dict) else {}


def read_entry(provider: str | None, base_url: str | None) -> dict | None:
    """The cached entry for this provider/endpoint, or ``None``."""
    entry = _read_cache().get(cache_key(provider, base_url))
    return entry if isinstance(entry, dict) else None


def store(provider: str | None, base_url: str | None, *, models=None,
          supported: bool = True, reason: str | None = None) -> dict:
    """Record the outcome of a listing (a success *or* a reason it is unavailable).

    Best-effort: a cache that cannot be written must never break the request that
    asked for the list.
    """
    entry = {
        "provider": (provider or "").strip().lower(),
        "base_url": (base_url or "").strip().rstrip("/"),
        "checked_at": _now().isoformat(timespec="seconds"),
        "supported": bool(supported),
        "reason": reason,
        "models": [
            {"id": str(item.get("id") or ""), "name": item.get("name") or None}
            for item in (models or [])[:MAX_MODELS]
            if str(item.get("id") or "").strip()
        ],
    }
    try:
        data = read_json_file()
        cache = data.get(CACHE_FIELD)
        if not isinstance(cache, dict):
            cache = {}
        cache[cache_key(provider, base_url)] = entry
        data[CACHE_FIELD] = cache
        write_json_file(data)
    except (KeyStoreError, OSError, ValueError):
        pass
    return entry


def clear(provider: str | None = None, base_url: str | None = None) -> int:
    """Drop cached lists.

    With no arguments, everything. With a ``provider``, every endpoint for that
    provider — which is what a key change or a provider change needs, because a
    list obtained with the old credential or endpoint is no longer trustworthy.
    With both, exactly one entry.
    """
    try:
        data = read_json_file()
    except KeyStoreError:
        return 0
    cache = data.get(CACHE_FIELD)
    if not isinstance(cache, dict) or not cache:
        return 0
    if provider is None and base_url is None:
        removed = len(cache)
        data[CACHE_FIELD] = {}
    elif base_url is None:
        prefix = (provider or "").strip().lower() + "|"
        removed = len([k for k in cache if k.startswith(prefix)])
        for key in [k for k in cache if k.startswith(prefix)]:
            cache.pop(key, None)
        data[CACHE_FIELD] = cache
    else:
        key = cache_key(provider, base_url)
        removed = 1 if key in cache else 0
        cache.pop(key, None)
        data[CACHE_FIELD] = cache
    if removed:
        try:
            write_json_file(data)
        except (KeyStoreError, OSError, ValueError):
            pass
    return removed


def is_stale(entry: dict | None, *, ttl_s: float = DEFAULT_TTL_S,
             now: dt.datetime | None = None) -> bool:
    """True when an entry is missing or older than the TTL."""
    if not isinstance(entry, dict):
        return True
    stamp = entry.get("checked_at")
    if not isinstance(stamp, str):
        return True
    try:
        checked = dt.datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=dt.timezone.utc)
    return ((now or _now()) - checked).total_seconds() > ttl_s
