"""Regression checks for the "key saved but no provider selected" trap.

The owner saved an Anthropic key and the assistant still said *"No provider is
configured"* — because ``POST /api/ai/keys`` stored the key without selecting the
provider. These checks freeze the fix:

1. saving a key with **no** provider selected selects it and fills the preset
   ``model``/``base_url`` (only when they are empty);
2. saving a key while a **different** provider is configured does *not* switch it
   and returns an actionable hint;
3. ``GET /api/ai/config`` reports every state a screen must distinguish
   (``unconfigured`` / ``key_without_provider`` / ``provider_without_key`` /
   ``ready``) — including a key supplied only by the environment;
4. the response never echoes the key, and an existing model/base URL is never
   clobbered.

    uv run --extra web python tests/test_ai_provider_selection.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from httpx import ASGITransport, AsyncClient  # noqa: F401
except ImportError:  # pragma: no cover
    print("the [web] extra is required: uv run --extra web "
          "python tests/test_ai_provider_selection.py")
    raise SystemExit(2)

from pacifica_obd.web.ai import config as ai_config  # noqa: E402
from pacifica_obd.web.ai.keys import KeyStore  # noqa: E402
from pacifica_obd.web.app import create_app  # noqa: E402
from pacifica_obd.web.routes.ai import (  # noqa: E402
    AI_SETUP_STATES,
    _provider_or_400,
    _safe_reason,
)
from pacifica_obd.web.settings import Settings  # noqa: E402

BASE = "http://127.0.0.1:8765"
HEADERS = {"X-Pacifica-Request": "1"}
SECRET = "sk-ant-regression-secret-1234567890"

#: Every env name the key store / config loader honours that could leak between
#: scenarios on the developer's machine.
_AI_ENV = (
    "PACIFICA_OBD_AI_PROVIDER", "PACIFICA_OBD_AI_MODEL", "PACIFICA_OBD_AI_BASE_URL",
    "PACIFICA_OBD_AI_OFFLINE", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "GOOGLE_API_KEY", "PACIFICA_OBD_AI_KEY_ANTHROPIC", "PACIFICA_OBD_AI_KEY_OPENAI",
    "PACIFICA_OBD_AI_KEY_GEMINI",
)


def fresh_home(extra=None) -> str:
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    for key in _AI_ENV:
        os.environ.pop(key, None)
    for key, value in (extra or {}).items():
        os.environ[key] = value
    return os.environ["PACIFICA_OBD_HOME"]


def make_app():
    return create_app(Settings(demo=True))


def run_async(app, fn):
    async def wrapper():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE) as client:
            try:
                return await fn(client)
            finally:
                app.state.worker.stop()

    return asyncio.run(wrapper())


def assert_state_consistent(config: dict) -> None:
    """The new fields must not be able to disagree with ``provider`` + ``keys``."""
    state = config["state"]
    assert state in AI_SETUP_STATES, state
    saved = [pid for pid, info in config["keys"].items() if info["configured"]]
    # Both lists are built in PRESETS order from the same store, so they agree.
    assert config["saved_key_providers"] == saved, (config["saved_key_providers"], saved)
    if state == "unconfigured":
        assert not config["provider"] and not saved
        assert config["suggested_provider"] is None
    elif state == "key_without_provider":
        assert not config["provider"] and saved
        assert config["suggested_provider"] in saved
    elif state == "provider_without_key":
        assert config["provider"] and config["provider"] not in saved
        assert config["suggested_provider"] == config["provider"]
    elif state == "invalid":
        assert config["provider"] and config["state_reason"]
        assert config["suggested_provider"] == config["provider"]
    else:  # ready
        assert config["provider"]
        assert config.get("requires_key") is False or config["provider"] in saved
        assert config["suggested_provider"] is None
        assert not config["state_reason"]


def provider_preset(provider: str):
    from pacifica_obd.web.ai import presets

    return presets.preset(provider)


# --------------------------------------------------------------------------- #
# 1. a saved key selects the provider when none is configured
# --------------------------------------------------------------------------- #
def test_key_save_with_no_provider_selects_it_and_fills_defaults():
    fresh_home()
    app = make_app()

    async def body(client):
        response = await client.post("/api/ai/keys",
                                     json={"provider": "anthropic", "key": SECRET},
                                     headers=HEADERS)
        assert response.status_code == 200, response.text
        assert SECRET not in response.text                  # never echoed back
        payload = response.json()
        assert payload["provider"] == "anthropic"           # backward-compatible shape
        assert payload["key"]["configured"] is True and payload["key"]["source"] == "file"
        assert payload["selected"] is True and payload["reason"] is None

        config = (await client.get("/api/ai/config")).json()
        assert SECRET not in (await client.get("/api/ai/config")).text
        preset = provider_preset("anthropic")
        assert config["provider"] == "anthropic"
        assert config["model"] == preset.default_model
        assert config["base_url"] == preset.default_base_url
        assert config["state"] == "ready"
        assert config["saved_key_providers"] == ["anthropic"]
        assert config["suggested_provider"] is None
        # The settings/assistant screens can fill a preset from this payload
        # without a second request (used by the one-click "Use Anthropic").
        assert config["defaults"]["models"]["anthropic"] == preset.default_model
        assert config["defaults"]["base_urls"]["anthropic"] == preset.default_base_url
        assert_state_consistent(config)

        # The chat gate is passed: a provider can be built from the saved config
        # with the stored key. Constructing it opens no socket.
        ai_keys = KeyStore()
        provider = _provider_or_400(ai_config.AIConfig.load(), ai_keys)
        assert provider.id == "anthropic"

    run_async(app, body)
    fresh_home()


# --------------------------------------------------------------------------- #
# 2. a saved key never silently switches another configured provider
# --------------------------------------------------------------------------- #
def test_key_save_with_another_provider_does_not_switch_and_hints():
    fresh_home()
    app = make_app()

    async def body(client):
        await client.put("/api/ai/config",
                         json={"provider": "openai", "model": "gpt-4o-mini",
                               "base_url": "https://api.openai.com/v1"},
                         headers=HEADERS)
        await client.post("/api/ai/keys", json={"provider": "openai", "key": SECRET},
                          headers=HEADERS)
        before = (await client.get("/api/ai/config")).json()
        assert before["state"] == "ready" and before["provider"] == "openai"

        response = await client.post("/api/ai/keys",
                                    json={"provider": "gemini", "key": "AIza-other-000"},
                                    headers=HEADERS)
        assert response.status_code == 200
        payload = response.json()
        assert payload["selected"] is False
        assert payload["reason"] == "another provider is configured"
        assert payload["key"]["configured"] is True

        after = (await client.get("/api/ai/config")).json()
        assert after["provider"] == "openai"                       # not switched
        assert after["model"] == "gpt-4o-mini"                     # not clobbered
        assert after["base_url"] == "https://api.openai.com/v1"
        assert after["state"] == "ready"
        assert "gemini" in after["saved_key_providers"]
        assert_state_consistent(after)

    run_async(app, body)
    fresh_home()


# --------------------------------------------------------------------------- #
# 3. the config endpoint distinguishes all four states
# --------------------------------------------------------------------------- #
def test_config_reports_all_four_states():
    fresh_home()
    app = make_app()

    async def body(client):
        # (a) nothing configured
        config = (await client.get("/api/ai/config")).json()
        assert config["state"] == "unconfigured" and config["saved_key_providers"] == []
        assert_state_consistent(config)

        # (b) the owner's exact file: a key stored with no provider selected.
        # Written through the key store, not the HTTP route, so it stays unselected
        # (the route now selects — that is case (c) below).
        KeyStore().set("anthropic", SECRET)
        config = (await client.get("/api/ai/config")).json()
        assert config["state"] == "key_without_provider"
        assert config["provider"] is None
        assert config["saved_key_providers"] == ["anthropic"]
        assert config["suggested_provider"] == "anthropic"
        assert config["keys"]["anthropic"]["configured"] is True
        assert_state_consistent(config)

        # (c) select it via the UI's one-click action -> ready
        await client.put("/api/ai/config",
                         json={"provider": "anthropic",
                               "model": provider_preset("anthropic").default_model},
                         headers=HEADERS)
        config = (await client.get("/api/ai/config")).json()
        assert config["state"] == "ready" and config["provider"] == "anthropic"
        assert_state_consistent(config)

        # (d) a hosted provider selected with no key of its own
        await client.put("/api/ai/config",
                         json={"provider": "openai", "model": "gpt-4o-mini"},
                         headers=HEADERS)
        config = (await client.get("/api/ai/config")).json()
        assert config["state"] == "provider_without_key"
        assert config["provider"] == "openai"
        assert config["suggested_provider"] == "openai"
        assert_state_consistent(config)

    run_async(app, body)
    fresh_home()


# --------------------------------------------------------------------------- #
# 4. a key supplied only by the environment is seen and is actionable
# --------------------------------------------------------------------------- #
def test_env_only_key_is_visible_without_provider_selected():
    fresh_home({"ANTHROPIC_API_KEY": "sk-ant-from-env-9999"})
    app = make_app()

    async def body(client):
        config = (await client.get("/api/ai/config")).json()
        assert config["state"] == "key_without_provider"
        assert config["provider"] is None
        assert config["saved_key_providers"] == ["anthropic"]
        assert config["suggested_provider"] == "anthropic"
        assert config["keys"]["anthropic"] == {
            "configured": True, "hint": "…9999", "source": "env"}
        assert_state_consistent(config)

        await client.put("/api/ai/config",
                         json={"provider": "anthropic",
                               "model": provider_preset("anthropic").default_model},
                         headers=HEADERS)
        after = (await client.get("/api/ai/config")).json()
        assert after["state"] == "ready" and after["provider"] == "anthropic"

    run_async(app, body)
    fresh_home()


# --------------------------------------------------------------------------- #
# 5. a saved model / custom base URL is preserved
# --------------------------------------------------------------------------- #
def test_key_save_fills_only_empty_fields():
    fresh_home()
    app = make_app()

    async def body(client):
        # No provider, but the user already typed a model and a custom endpoint.
        await client.put("/api/ai/config",
                         json={"model": "my-own-model",
                               "base_url": "https://my-gateway.example/v1"},
                         headers=HEADERS)
        response = await client.post("/api/ai/keys",
                                     json={"provider": "anthropic", "key": SECRET},
                                     headers=HEADERS)
        assert response.json()["selected"] is True
        config = (await client.get("/api/ai/config")).json()
        assert config["provider"] == "anthropic"
        assert config["model"] == "my-own-model"                      # preserved
        assert config["base_url"] == "https://my-gateway.example/v1"  # preserved

        # A second save for the same provider keeps it selected.
        again = await client.post("/api/ai/keys",
                                  json={"provider": "anthropic", "key": "sk-ant-second-2222"},
                                  headers=HEADERS)
        assert again.json()["selected"] is True and again.json()["reason"] is None
        assert again.json()["key"]["hint"] == "…2222"

    run_async(app, body)
    fresh_home()


# --------------------------------------------------------------------------- #
# 6. a preset with no default model refuses to select (and hints why)
# --------------------------------------------------------------------------- #
def test_key_save_with_no_usable_default_does_not_store_an_invalid_config():
    fresh_home()
    app = make_app()

    async def body(client):
        # `openai_compat` has no default model: a key alone cannot make it usable.
        response = await client.post("/api/ai/keys",
                                     json={"provider": "openai_compat", "key": SECRET},
                                     headers=HEADERS)
        payload = response.json()
        assert payload["selected"] is False
        # A custom endpoint has neither a default model nor a default base URL, so
        # the reason names one of the two things still missing.
        assert payload["reason"]
        assert "model" in payload["reason"] or "base URL" in payload["reason"]
        config = (await client.get("/api/ai/config")).json()
        assert config["provider"] is None                 # nothing invalid saved
        assert config["keys"]["openai_compat"]["configured"] is True

    run_async(app, body)
    fresh_home()


# --------------------------------------------------------------------------- #
# 7. the provider id is still validated
# --------------------------------------------------------------------------- #
def test_unknown_provider_is_refused():
    fresh_home()
    app = make_app()

    async def body(client):
        response = await client.post("/api/ai/keys",
                                     json={"provider": "definitely-not-real", "key": SECRET},
                                     headers=HEADERS)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation"

    run_async(app, body)
    fresh_home()


# --------------------------------------------------------------------------- #
# 8. a bad saved configuration is reported, not called "ready"
# --------------------------------------------------------------------------- #
def test_invalid_configuration_is_reported_as_a_state():
    fresh_home()
    app = make_app()

    async def body(client):
        # A provider that needs a model, saved without one (a hand-edited file:
        # the config write route would have rejected it).
        ai_config.AIConfig(provider="vllm", model="").save()
        config = (await client.get("/api/ai/config")).json()
        assert config["state"] == "invalid"
        assert "model" in config["state_reason"]
        assert_state_consistent(config)

        # An unknown provider is likewise never "ready".
        ai_config.AIConfig(provider="not-a-provider", model="x").save()
        config = (await client.get("/api/ai/config")).json()
        assert config["state"] == "invalid" and config["state_reason"]
        assert_state_consistent(config)

    run_async(app, body)
    fresh_home()


# --------------------------------------------------------------------------- #
# 9. an environment override is never written into the file
# --------------------------------------------------------------------------- #
def test_key_save_does_not_persist_environment_overrides():
    fresh_home()
    ai_config.AIConfig(provider="ollama", model="llama3.2",
                       base_url="http://127.0.0.1:11434/v1").save()
    os.environ["PACIFICA_OBD_AI_PROVIDER"] = "openai"
    app = make_app()

    async def body(client):
        before = (await client.get("/api/ai/config")).json()
        assert before["provider"] == "openai"            # the override is effective
        response = await client.post("/api/ai/keys",
                                     json={"provider": "openai", "key": SECRET},
                                     headers=HEADERS)
        assert response.json()["selected"] is True
        # The override was NOT persisted: the file still names Ollama.
        stored = ai_config.AIConfig.load(env={})
        assert stored.provider == "ollama"
        assert stored.model == "llama3.2"
        assert stored.base_url == "http://127.0.0.1:11434/v1"
        os.environ.pop("PACIFICA_OBD_AI_PROVIDER")
        after = (await client.get("/api/ai/config")).json()
        assert after["provider"] == "ollama"              # the file's choice stands

    run_async(app, body)
    fresh_home()


# --------------------------------------------------------------------------- #
# 10. the explicit select endpoint preserves, switches and refuses correctly
# --------------------------------------------------------------------------- #
def test_select_endpoint_preserves_custom_values_and_switches_with_defaults():
    fresh_home()
    app = make_app()

    async def body(client):
        # A model and endpoint chosen before any provider: preserved, not clobbered.
        await client.put("/api/ai/config",
                         json={"model": "my-own-model",
                               "base_url": "https://my-gateway.example/v1"},
                         headers=HEADERS)
        selected = await client.post("/api/ai/select", json={"provider": "anthropic"},
                                     headers=HEADERS)
        assert selected.status_code == 200, selected.text
        config = selected.json()
        assert config["provider"] == "anthropic"
        assert config["model"] == "my-own-model"
        assert config["base_url"] == "https://my-gateway.example/v1"
        assert config["state"] == "provider_without_key"

        # Switching to another provider uses that provider's defaults (the old
        # model belonged to Anthropic) and only the id was ever sent.
        await client.post("/api/ai/keys", json={"provider": "anthropic", "key": SECRET},
                          headers=HEADERS)
        switched = await client.post("/api/ai/select", json={"provider": "openai"},
                                     headers=HEADERS)
        config = switched.json()
        assert config["provider"] == "openai"
        assert config["model"] == "gpt-4o-mini"
        assert config["base_url"] == "https://api.openai.com/v1"

        bad = await client.post("/api/ai/select", json={"provider": "nope"},
                                headers=HEADERS)
        assert bad.status_code == 400

    run_async(app, body)
    fresh_home()


# --------------------------------------------------------------------------- #
# 11. a selection reason never echoes a value (no key can leak through it)
# --------------------------------------------------------------------------- #
def test_selection_reason_never_echoes_a_value_or_key():
    secret = "sk-ant-test-secret-abcdef0123456789"
    for exc in (
        ValueError(f"invalid IPv6 URL https://[{secret}]/"),
        ValueError(f"unknown provider: {secret!r}"),
        ValueError(f"a model id is required once a provider is chosen {secret}"),
        ValueError(f"base_url must not contain credentials: {secret}"),
    ):
        reason = _safe_reason(exc)
        assert reason and reason == reason.strip()
        assert secret not in reason and "sk-" not in reason

    # And the endpoint itself returns only a fixed phrase for that case.
    fresh_home()
    app = make_app()

    async def body(client):
        response = await client.post("/api/ai/keys",
                                     json={"provider": "openai_compat", "key": SECRET},
                                     headers=HEADERS)
        assert SECRET not in response.text
        reason = response.json()["reason"]
        assert reason and SECRET not in reason

    run_async(app, body)
    fresh_home()


# --------------------------------------------------------------------------- #
# 12. PUT config does not copy environment overrides into the file
# --------------------------------------------------------------------------- #
def test_put_config_does_not_persist_environment_overrides():
    fresh_home()
    ai_config.AIConfig(provider="ollama", model="llama3.2",
                       base_url="http://127.0.0.1:11434/v1").save()
    os.environ["PACIFICA_OBD_AI_PROVIDER"] = "openai"
    os.environ["PACIFICA_OBD_AI_MODEL"] = "gpt-4o-mini"
    app = make_app()

    async def body(client):
        response = await client.put("/api/ai/config", json={"temperature": 0.5},
                                    headers=HEADERS)
        assert response.status_code == 200, response.text
        assert response.json()["provider"] == "openai"     # the override is effective
        stored = ai_config.AIConfig.load(env={})
        assert stored.provider == "ollama" and stored.model == "llama3.2"
        assert stored.base_url == "http://127.0.0.1:11434/v1"
        assert stored.temperature == 0.5                   # the patch itself was saved

    run_async(app, body)
    fresh_home()


# --------------------------------------------------------------------------- #
# 13. a switch never reuses the old provider's model or endpoint
# --------------------------------------------------------------------------- #
def test_switch_never_reuses_the_old_providers_model_or_endpoint():
    fresh_home()
    app = make_app()

    async def body(client):
        await client.put("/api/ai/config",
                         json={"provider": "openai", "model": "gpt-4o-mini",
                               "base_url": "https://api.openai.com/v1"},
                         headers=HEADERS)
        # A generic endpoint with no defaults must not inherit OpenAI's model/URL:
        # the switch is refused and nothing is written.
        response = await client.post("/api/ai/select", json={"provider": "openai_compat"},
                                     headers=HEADERS)
        assert response.status_code == 400, response.text
        stored = ai_config.AIConfig.load(env={})
        assert (stored.provider, stored.model, stored.base_url) == (
            "openai", "gpt-4o-mini", "https://api.openai.com/v1")
        # vLLM has an endpoint but no default model: the OpenAI model is dropped
        # and the missing model is reported, not silently carried across.
        response = await client.post("/api/ai/select", json={"provider": "vllm"},
                                     headers=HEADERS)
        assert response.status_code == 400
        stored = ai_config.AIConfig.load(env={})
        assert stored.provider == "openai" and stored.model == "gpt-4o-mini"

    run_async(app, body)
    fresh_home()


# --------------------------------------------------------------------------- #
# 14. a key pasted into the provider field cannot come back out
# --------------------------------------------------------------------------- #
def test_public_config_does_not_echo_a_key_used_as_a_provider_id():
    fresh_home()
    secret = "sk-ant-placed-in-the-provider-field-9999"
    ai_config.AIConfig(provider=secret, model="x").save()
    app = make_app()

    async def body(client):
        config_text = (await client.get("/api/ai/config")).text
        providers_text = (await client.get("/api/ai/providers")).text
        preview_text = (await client.get("/api/ai/what-will-be-sent",
                                          params={"message": "hi"})).text
        assert secret not in config_text
        assert secret not in providers_text
        assert secret not in preview_text
        assert (await client.get("/api/ai/config")).json()["state"] == "invalid"

    run_async(app, body)
    fresh_home()


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all provider-selection regression checks passed")
