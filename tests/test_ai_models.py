"""Cached model listings: freshness, provenance, invalidation, fail-soft.

A model list costs a network round trip, so it is fetched only when asked for,
cached in the 0600 config against the exact provider + endpoint it came from, and
invalidated when the provider, endpoint or key changes. When listing is
unavailable the entry says why, and the preset's suggestions are still returned —
the model field must never be empty.

    uv run --extra web python tests/test_ai_models.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import stat

from pacifica_obd.web.ai import models_cache


def _home(tmp: str) -> None:
    os.environ["PACIFICA_OBD_HOME"] = tmp


def test_store_and_read_round_trip_with_0600(tmp="/tmp", **_):
    import tempfile

    home = tempfile.mkdtemp()
    _home(home)
    entry = models_cache.store("openai", "https://api.openai.com/v1/",
                               models=[{"id": "gpt-4o-mini", "name": "4o mini"},
                                       {"id": "gpt-4o"}])
    assert entry["supported"] is True and entry["reason"] is None
    assert [m["id"] for m in entry["models"]] == ["gpt-4o-mini", "gpt-4o"]
    # the trailing slash must not create a second entry
    assert models_cache.read_entry("openai", "https://api.openai.com/v1") is not None
    path = os.path.join(home, "ai.json")
    assert os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, "config is not 0600"
    # the cache lives in the same file the keys do, and does not disturb them
    raw = json.load(open(path))
    assert "model_cache" in raw


def test_a_different_endpoint_is_a_different_listing(tmp="/tmp", **_):
    import tempfile

    _home(tempfile.mkdtemp())
    models_cache.store("openai_compat", "http://127.0.0.1:8000/v1",
                       models=[{"id": "served-model"}])
    assert models_cache.read_entry("openai_compat", "http://127.0.0.1:8000/v1")
    assert models_cache.read_entry("openai_compat", "http://127.0.0.1:9999/v1") is None
    assert models_cache.read_entry("openai", "http://127.0.0.1:8000/v1") is None


def test_clear_by_provider_and_everything():
    import tempfile

    _home(tempfile.mkdtemp())
    models_cache.store("openai", "https://api.openai.com/v1", models=[{"id": "a"}])
    models_cache.store("openai", "https://proxy/v1", models=[{"id": "b"}])
    models_cache.store("ollama", "http://127.0.0.1:11434/v1", models=[{"id": "c"}])
    assert models_cache.clear("openai") == 2
    assert models_cache.read_entry("openai", "https://api.openai.com/v1") is None
    assert models_cache.read_entry("openai", "https://proxy/v1") is None
    assert models_cache.read_entry("ollama", "http://127.0.0.1:11434/v1") is not None
    assert models_cache.clear() == 1
    assert models_cache.read_entry("ollama", "http://127.0.0.1:11434/v1") is None


def test_an_unsupported_listing_remembers_why():
    import tempfile

    _home(tempfile.mkdtemp())
    entry = models_cache.store("anthropic", "https://api.anthropic.com", models=[],
                               supported=False, reason="no model-listing endpoint")
    assert entry["supported"] is False and entry["reason"]
    read = models_cache.read_entry("anthropic", "https://api.anthropic.com")
    assert read["supported"] is False and "endpoint" in read["reason"]


def test_staleness_uses_the_recorded_timestamp():
    import tempfile

    _home(tempfile.mkdtemp())
    entry = models_cache.store("openai", "https://api.openai.com/v1", models=[{"id": "a"}])
    assert models_cache.is_stale(entry) is False
    # an entry from two days ago is stale under the 24 h TTL
    old = dict(entry, checked_at=(dt.datetime.now(dt.timezone.utc)
                                  - dt.timedelta(days=2)).isoformat(timespec="seconds"))
    assert models_cache.is_stale(old) is True
    assert models_cache.is_stale(None) is True
    assert models_cache.is_stale({"checked_at": "not a date"}) is True
    assert models_cache.is_stale({"checked_at": (dt.datetime.now(dt.timezone.utc)
                                                 .isoformat(timespec="seconds"))}) is False


def test_a_corrupt_cache_never_raises_and_is_never_clobbered():
    import tempfile
    import pathlib

    home = tempfile.mkdtemp()
    _home(home)
    cache = pathlib.Path(home) / "ai.json"
    cache.write_text("{ not json", encoding="utf-8")
    os.chmod(cache, 0o600)
    # reads degrade to "no entry"; a write must not blow up either…
    assert models_cache.read_entry("openai", None) is None
    entry = models_cache.store("openai", None, models=[{"id": "x"}])
    assert entry["models"][0]["id"] == "x"
    # …and it must not overwrite a config it could not read, because that file may
    # still hold keys the user cannot recover any other way.
    assert cache.read_text(encoding="utf-8") == "{ not json"
    assert models_cache.clear("openai") == 0


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all AI model-cache checks passed")
