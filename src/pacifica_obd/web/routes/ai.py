"""AI routes: config, keys, the read-only tool surface and streaming chat (§5).

Privacy defaults come straight from the owner decision §9.3: no provider out of
the box, remote bus data **off**, VIN redaction **on**, conversation persistence
**off**, cache-first reads. A remote provider with consent off simply receives no
vehicle context (the plain chat still works); a *task* that is defined by vehicle
data refuses with 403 instead of silently sending the user's car to the cloud.

The assistant has no write route: it cannot reach ``/api/security`` or
``/api/writes``, its tool registry is read-only (asserted by a test), and every
tool result is fenced as untrusted before it reaches a model.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from pacifica_obd.store import Store
from pacifica_obd.web import deps, errors
from pacifica_obd.web.ai import backend as ai_backend
from pacifica_obd.web.ai import config as ai_config
from pacifica_obd.web.ai import keys as ai_keys
from pacifica_obd.web.ai import models_cache as ai_models
from pacifica_obd.web.ai import presets as ai_presets
from pacifica_obd.web.ai import prompts as ai_prompts
from pacifica_obd.web.ai import tools as ai_tools
from pacifica_obd.web.ai.chat import ChatEngine, RateLimiter
from pacifica_obd.web.ai.keys import redact_text
from pacifica_obd.web.ai.providers import (
    Message,
    ProviderError,
    build_provider,
    effective_model,
)
from pacifica_obd.web.ai.redact import redact_vin_text
from pacifica_obd.web.schemas import (
    AIChatRequest,
    AIConfigPatch,
    AIKeyRequest,
    AISelectRequest,
    AITaskRequest,
    AITestRequest,
)

router = APIRouter()

TASKS = ("explain", "diagnose", "report", "compare", "pretrip")

#: How long the connection test may take, and how many models it will name.
TEST_TIMEOUT_S = 8.0
TEST_MODEL_LIMIT = 50


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _keystore() -> ai_keys.KeyStore:
    return ai_keys.KeyStore()


def _load_config() -> ai_config.AIConfig:
    return ai_config.AIConfig.load()


def _validated_config() -> ai_config.AIConfig:
    config = ai_config.AIConfig.load()
    try:
        return config.validate()
    except ValueError as exc:
        raise errors.validation(str(exc)) from exc


def _rate_limiter(request: Request, config) -> RateLimiter:
    limiter = getattr(request.app.state, "ai_rate_limiter", None)
    if limiter is None or limiter.per_minute != int(config.max_requests_per_minute):
        limiter = RateLimiter(config.max_requests_per_minute)
        request.app.state.ai_rate_limiter = limiter
    return limiter


def _provider_or_400(config, store: ai_keys.KeyStore):
    if not config.provider:
        raise errors.ApiError("no_provider", "validation",
                              "no AI provider is configured; open Settings → AI",
                              http_status=400)
    found = ai_presets.preset(config.provider)
    key = store.get(config.provider)
    # A key is only mandatory for a preset that declares one (the hosted
    # vendors). A local server or a custom proxy may legitimately accept none;
    # in that case a 401 from the server becomes an actionable `auth` error.
    if found is not None and found.requires_key and not key:
        raise errors.ApiError(
            "no_key", "validation",
            f"no API key is configured for {found.label}",
            http_status=400,
            detail={"hint": "POST /api/ai/keys", "provider": config.provider})
    try:
        provider = build_provider(config, key=key)
    except ProviderError as exc:
        raise errors.ApiError(exc.code, "validation", redact_text(exc.message),
                              http_status=400) from exc
    return provider


#: The setup states a screen must tell apart (design §5.5). They are the whole
#: point of the audit that produced them: "a key is saved" and "a provider is
#: configured" are **different** facts, and only a provider can serve a chat.
#:
#: ``unconfigured``         — no provider and no key saved: nothing to use yet;
#: ``key_without_provider`` — a key exists (file *or* environment) but no provider
#:                            is selected, so the key is present and useless;
#: ``provider_without_key`` — a provider is selected that requires a key and none
#:                            is saved for it;
#: ``ready``                — a provider is selected, its key situation is complete
#:                            and the configuration validates;
#: ``invalid``              — a provider is selected but the saved configuration
#:                            cannot be used (unknown provider, missing model, bad
#:                            base URL); ``state_reason`` says which in prose.
AI_SETUP_STATES = ("unconfigured", "key_without_provider",
                   "provider_without_key", "ready", "invalid")


def _safe_reason(exc: Exception) -> str:
    """A human reason that never echoes a configured value — or a key.

    Validation messages are normally built by Web-Framework code and can include
    a URL or a free-text field; a key can end up in either. The key store's
    redactor would catch a key-shaped string, but a proxy token in a query string
    has no key shape. These fixed phrases say what to fix without repeating any
    value from the request or the file.
    """
    text = str(exc)
    if "model" in text:
        return "a model id is required before this provider can be used"
    if "base_url" in text:
        return "the base URL is not valid for this provider"
    if "provider" in text:
        return "the selected provider is not available"
    if "temperature" in text:
        return "the temperature setting is out of range"
    for name in ("max_tokens_per_conversation", "max_requests_per_minute",
                 "max_output_tokens_per_request", "max_live_reads_per_turn"):
        if name in text:
            return f"{name} is out of range"
    return "the saved configuration needs attention"


def _setup_state(config, store: ai_keys.KeyStore) -> dict:
    """State the UI needs, computed from the *same* store the payload reports.

    A key can be present with no provider selected — the owner's trap — and that
    key can come from the environment rather than the file, so the state is
    derived from :meth:`KeyStore.masked` (which checks both) rather than from the
    file alone. ``saved_key_providers`` lets a screen name every key it holds, and
    ``suggested_provider`` is the id to offer a one-click "Use <label>" action for
    (the saved-key provider, or the selected provider that is missing its key).
    """
    saved = [pid for pid in ai_presets.PRESETS if store.masked(pid)["configured"]]
    if not config.provider:
        if saved:
            return {"state": "key_without_provider", "saved_key_providers": saved,
                    "suggested_provider": saved[0], "state_reason": None}
        return {"state": "unconfigured", "saved_key_providers": [],
                "suggested_provider": None, "state_reason": None}
    found = ai_presets.preset(config.provider)
    requires = bool(found.requires_key) if found else False
    if requires and not store.get(config.provider):
        return {"state": "provider_without_key", "saved_key_providers": saved,
                "suggested_provider": config.provider, "state_reason": None}
    if found is None:
        return {"state": "invalid", "saved_key_providers": saved,
                "suggested_provider": config.provider,
                "state_reason": "the selected provider is not available"}
    try:
        # The config endpoint is read-only, but "ready" must mean *usable*: a
        # provider with no model (vllm/openai_compat) or a bad cap is not ready.
        config.validate()
    except (ValueError, TypeError) as exc:
        return {"state": "invalid", "saved_key_providers": saved,
                "suggested_provider": config.provider,
                "state_reason": _safe_reason(exc)}
    return {"state": "ready", "saved_key_providers": saved,
            "suggested_provider": None, "state_reason": None}


def _public_config(config, store: ai_keys.KeyStore) -> dict:
    redact = store.redactor()
    data = config.to_dict(include_keys=False)
    # A URL can carry a query-string credential; scrub the configured literals
    # before echoing any of it back to the browser. The raw `model` is scrubbed
    # too: a user can paste a key into any free-text field.
    data["model"] = redact(config.model) if config.model else config.model
    data["base_url"] = redact(config.base_url) if config.base_url else config.base_url
    data["url"] = redact(config.url()) if config.url() else config.url()
    data["is_local"] = config.is_local()
    data["effective_model"] = redact(config.effective_model())
    data["effective_base_url"] = redact(config.url()) if config.url() else None
    data["format"] = config.format()
    data["requires_key"] = config.requires_key()
    data["preset"] = config.preset().as_dict() if config.preset() else None
    data["keys"] = {name: store.masked(name) for name in ai_presets.PRESETS}
    data["provider_labels"] = dict(ai_presets.PROVIDER_LABELS)
    data["defaults"] = {"base_urls": dict(ai_config.DEFAULT_BASE_URLS),
                        "models": {pid: preset.default_model
                                   for pid, preset in ai_presets.PRESETS.items()}}
    data["cost_table_date"] = ai_config.COST_TABLE_DATE
    data["local_base_hosts"] = sorted(ai_config.LOCAL_BASE_HOSTS)
    data.update(_setup_state(config, store))
    # A provider id is normally a small known token, but a hand-edited file or an
    # environment override can put anything there. Scrub it like every other
    # externally sourced string so a key pasted into the wrong field cannot come
    # back out of this read-only endpoint.
    if data.get("provider"):
        data["provider"] = redact(data["provider"])
    if data.get("suggested_provider"):
        data["suggested_provider"] = redact(data["suggested_provider"])
    return data


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _runner(request: Request):
    worker = deps.get_worker(request)

    async def run(name, fn):
        return await deps.run_job(worker, name, fn, exclusive=True, timeout_s=120.0)

    return worker, run


# --------------------------------------------------------------------------- #
# config + keys
# --------------------------------------------------------------------------- #
@router.get("/ai/providers", summary="Provider registry with presets (no network, no keys)")
async def providers():
    store = _keystore()
    config = _load_config()
    redact = store.redactor()
    rows = []
    for provider_id, found in ai_presets.PRESETS.items():
        masked = store.masked(provider_id)
        selected = provider_id == config.provider
        rows.append({
            **found.as_dict(),
            "selected": selected,
            "key": masked,
            "available": bool(masked["configured"]) or found.format == "fake"
                         or (selected and config.is_local()) or not found.requires_key,
        })
    return {"providers": rows, "cost_table_date": ai_config.COST_TABLE_DATE,
            "configured_provider": redact(config.provider) if config.provider else config.provider}


@router.get("/ai/config", summary="Current AI configuration (never a key)")
async def get_config():
    config = _load_config()
    return _public_config(config, _keystore())


@router.put("/ai/config", summary="Patch the AI configuration (secrets go elsewhere)")
async def put_config(request: Request, body: AIConfigPatch):
    store = _keystore()
    effective = _load_config()
    # `exclude_unset` distinguishes "field omitted" from "explicit null": a null
    # on provider/model/base_url clears the field (so a reset is possible).
    patch = body.model_dump(exclude_unset=True)
    clearable = {"provider", "model", "base_url"}
    patch = {key: value for key, value in patch.items()
             if value is not None or key in clearable}
    try:
        # Validate the *effective* view (file + environment): that is what a chat
        # would use, and the environment may satisfy a field the file leaves empty.
        updated = effective.patch(patch).validate()
    except (ValueError, TypeError) as exc:
        raise errors.validation(str(exc)) from exc
    try:
        # Persist the patch onto the *file-only* config: an environment override in
        # effect for this process must never be copied into the file by an
        # unrelated save.
        _persisted_config().patch(patch).save()
    except ai_keys.KeyStoreError as exc:
        raise errors.ApiError("key_store", "validation", str(exc), http_status=400) from exc
    # A model list belongs to one provider + endpoint; if either moved, the cached
    # list is no longer about the configuration being used.
    if "provider" in patch or "base_url" in patch:
        ai_models.clear(effective.provider or updated.provider)
    return _public_config(_load_config(), store)


def _persisted_config() -> ai_config.AIConfig:
    """The configuration as stored on disk, without environment overrides.

    Environment values are runtime overrides. A write path must never copy them
    into the file just because they were in effect for this process: saving a key
    while ``PACIFICA_OBD_AI_PROVIDER`` was set for a one-off run must not make
    that provider permanent.
    """
    return ai_config.AIConfig.load(env={})


def _fill_preset_defaults(config, preset) -> ai_config.AIConfig:
    """Fill ``model``/``base_url`` from the preset only where they are empty."""
    filled: dict = {}
    if not (config.model or "").strip() and preset and preset.default_model:
        filled["model"] = preset.default_model
    if not (config.base_url or "").strip() and preset and preset.default_base_url:
        filled["base_url"] = preset.default_base_url
    return config.patch(filled) if filled else config


def _needs_base_url(config, preset) -> bool:
    """True when a non-fake preset has no default endpoint and none is set.

    ``AIConfig.validate`` does not require a base URL (a local adapter can have a
    built-in fallback), but a *selection* of a generic endpoint with no URL would
    leave a key pointed at a placeholder. The selection paths refuse it; the
    settings form already enforces the same rule.
    """
    return (preset is not None and preset.format != ai_presets.FAKE_FORMAT
            and not preset.default_base_url
            and not (config.base_url or "").strip())


def _select_provider_for_key(provider: str) -> dict:
    """Make a freshly saved key usable, without ever switching a chosen provider.

    Saving a key for a provider must be enough to *use* that provider: when no
    provider is configured yet, the saved provider is selected and any empty
    ``model``/``base_url`` is filled from the preset defaults. When a **different**
    provider is already configured the selection is left untouched and the caller
    gets a hint to offer in the UI ("another provider is configured") — a silent
    switch would edit a config the user set on another screen.

    Decisions use the *effective* configuration (file + environment) but writes
    target the **file** configuration only. Only empty fields are filled, so a
    saved model or a custom base URL is never clobbered. Returns
    ``{"selected": bool, "reason": str | None}`` and never raises: a preset with
    no default model (a custom endpoint) still needs one, so the call reports that
    instead of storing a configuration the config endpoint would reject.
    """
    effective = _load_config()
    if effective.provider and effective.provider != provider:
        return {"selected": False, "reason": "another provider is configured"}
    preset = ai_presets.preset(provider)
    saved = _persisted_config()
    if saved.provider and saved.provider != provider:
        # The file names another provider; an environment override is what makes
        # this key usable right now. The key works, but do not rewrite the choice
        # the file already holds.
        return {"selected": True, "reason": None}
    candidate = saved if saved.provider else saved.patch({"provider": provider})
    candidate = _fill_preset_defaults(candidate, preset)
    if _needs_base_url(candidate, preset):
        return {"selected": False, "reason": "a base URL is required for this provider"}
    try:
        candidate.validate()
    except (ValueError, TypeError) as exc:
        return {"selected": False, "reason": _safe_reason(exc)}
    if candidate != saved:
        try:
            candidate.save()
        except ai_keys.KeyStoreError as exc:
            return {"selected": False, "reason": _safe_reason(exc)}
    return {"selected": True, "reason": None}


@router.post("/ai/select", summary="Select a provider (fills empty preset defaults)")
async def select_provider(body: AISelectRequest):
    """Select a provider explicitly — the UI's "Use <provider>" action.

    Unlike saving a key, this *is* the user asking to switch, so the persisted
    provider becomes ``body.provider``. ``model``/``base_url`` are preserved when
    they already belong to this provider; on a switch the new preset's defaults
    replace **both** (including an empty default, so the old provider's model or
    endpoint is never reused). A first selection preserves values already typed
    and fills only the gaps. Only the file is written: environment overrides are
    runtime values, never persisted. The full public config is returned so the
    caller can re-render in one round trip.
    """
    provider = body.provider.strip()
    if provider not in ai_presets.PRESETS:
        raise errors.validation(f"unknown provider: {provider!r}")
    preset = ai_presets.preset(provider)
    saved = _persisted_config()
    candidate = saved.patch({"provider": provider})
    if saved.provider is None:
        # Nothing was selected before: a model/base_url already in the file was
        # typed for this pending choice, so preserve it and fill only the gaps.
        candidate = _fill_preset_defaults(candidate, preset)
    elif saved.provider != provider:
        # Switching: both fields belonged to the old provider, so replace both
        # with the new preset's defaults — even when they are empty.
        candidate = candidate.patch({"model": preset.default_model or "",
                                     "base_url": preset.default_base_url or None})
    else:
        candidate = _fill_preset_defaults(candidate, preset)
    if _needs_base_url(candidate, preset):
        raise errors.validation("a base URL is required for this provider")
    try:
        updated = candidate.validate()
    except (ValueError, TypeError) as exc:
        raise errors.validation(_safe_reason(exc)) from exc
    try:
        updated.save()
    except ai_keys.KeyStoreError as exc:
        raise errors.ApiError("key_store", "validation", _safe_reason(exc),
                              http_status=400) from exc
    if saved.provider != updated.provider or saved.base_url != updated.base_url:
        ai_models.clear(updated.provider)
    return _public_config(_load_config(), _keystore())


@router.post("/ai/keys", summary="Set a provider key (write-only; returns a hint)")
async def set_key(body: AIKeyRequest):
    provider = body.provider.strip()
    if provider not in ai_presets.PRESETS:
        raise errors.validation(f"unknown provider: {provider!r}")
    store = _keystore()
    try:
        store.set(provider, body.key)
        # A fresh key must be masked by the log filter immediately, not at the
        # next restart.
        store.refresh_logging_literals()
        ai_models.clear(provider)
    except ai_keys.KeyStoreError as exc:
        raise errors.ApiError("key_store", "validation", str(exc), http_status=400) from exc
    selection = _select_provider_for_key(provider)
    # Never echo the key back; a masked hint only. `selected`/`reason` are additive
    # (the response keeps `provider` and `key`), so an older client still works and
    # a newer one can offer "Use <label> instead" when the switch was refused.
    return {"provider": provider, "key": store.masked(provider),
            "selected": selection["selected"], "reason": selection["reason"]}


@router.delete("/ai/keys/{provider}", summary="Remove a provider key")
async def delete_key(provider: str):
    if provider not in ai_presets.PRESETS:
        raise errors.validation(f"unknown provider: {provider!r}")
    store = _keystore()
    removed = store.delete(provider)
    try:
        store.refresh_logging_literals()
    except Exception:  # noqa: BLE001 - logging is best-effort
        pass
    ai_models.clear(provider)
    return {"provider": provider, "removed": removed}


@router.post("/ai/test", summary="Validate a configuration without starting a chat")
async def test_connection(body: AITestRequest):
    """Check the (possibly unsaved) configuration, and optionally probe it.

    Never returns a provider's response body, never logs the key, and never
    spends more than a handful of tokens: the probe lists models, or — when the
    endpoint has no model list — sends a single ``max_tokens=1`` ping.
    """
    base = _load_config()
    overrides = body.model_dump(exclude_unset=True, exclude={"probe"})
    overrides = {key: value for key, value in overrides.items()
                 if value is not None or key in ("provider", "model", "base_url")}
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "", **extra) -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, **extra})
        return bool(ok)

    try:
        config = base.patch(overrides).validate()
    except (ValueError, TypeError) as exc:
        return {"ok": False, "checks": checks, "models": [],
                "error": {"code": "validation", "message": str(exc)}}

    found = config.preset()
    store = _keystore()

    # A stored key must never be sent to an endpoint the user has not saved. The
    # test is "would this send my key?" — so an unsaved base URL is refused rather
    # than probed, and the UI is told to save the change first. Without this the
    # endpoint is a credential-exfiltration primitive: the caller chooses the URL
    # and the server attaches the key (adversarial review, highest-risk finding).
    if config.provider and store.get(config.provider) and base.url() != config.url():
        # Scrub before building the response: the caller-supplied URL can carry a
        # credential in a query string, and this path used to echo it verbatim
        # (the redactor was not created until further down).
        redact_early = store.redactor()
        return {
            "ok": False,
            "checks": checks + [{
                "name": "endpoint", "ok": False,
                "detail": "the base URL differs from the saved one and a key is "
                          "stored; save it before testing so the key is only sent "
                          "to the endpoint you configured",
            }],
            "models": [],
            "provider": config.provider,
            "base_url": redact_early(config.url()),
            "error": {"code": "endpoint_not_saved",
                      "message": "save the base URL before testing with a stored key"},
        }

    redact = store.redactor()
    key = store.get(config.provider) if config.provider else None
    result = {
        "ok": True,
        "provider": config.provider,
        "label": found.label if found else None,
        "format": config.format(),
        "local": config.is_local(),
        "model": redact(effective_model(config)),
        "base_url": redact(config.url()) if config.url() else None,
        "requires_key": config.requires_key(),
        "key": store.masked(config.provider) if config.provider else None,
        "probed": bool(body.probe),
        "checks": checks,
        "models": [],
        "error": None,
    }

    if not config.provider:
        check("configuration", False, "no provider is selected")
        result["ok"] = False
        return result
    check("configuration", True,
          f"{found.label} · {config.format()} · {'local' if config.is_local() else 'remote'}")

    if config.requires_key() and not key:
        check("key", False, f"no API key is configured for {found.label}")
        result["ok"] = False
        return result
    if key:
        masked = store.masked(config.provider)
        check("key", True, f"configured ({masked.get('source')}, {masked.get('hint')})")
    else:
        check("key", True, "not required for this provider")

    if not body.probe:
        return result

    def fail(check_name: str, code: str, message: str) -> None:
        check(check_name, False, message, code=code)
        result["ok"] = False
        result["error"] = {"code": code, "message": message}

    try:
        provider = build_provider(config, key=key)
    except ProviderError as exc:
        fail("provider", exc.code, exc.message)
        return result

    # One overall budget for the whole probe, so discovery + ping cannot sum to
    # double the advertised timeout.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TEST_TIMEOUT_S

    async def bounded(coro):
        remaining = max(0.1, deadline - loop.time())
        return await asyncio.wait_for(coro, remaining)

    model = effective_model(config)

    def redact_models(models) -> list[str]:
        return [redact(info.id) for info in models][:TEST_MODEL_LIMIT]

    try:
        if getattr(provider, "supports_model_discovery", True):
            models = await bounded(provider.list_models())
            result["models"] = redact_models(models)
            check("endpoint", True, f"reachable; {len(models)} model(s)")
            # Remember the listing (and the endpoint it came from) so the settings
            # screen can offer it without another round trip.
            ai_models.store(config.provider, config.url(),
                            models=[{"id": info.id, "name": getattr(info, "name", None)}
                                    for info in models],
                            supported=True)
        else:
            # No model-discovery API (e.g. Anthropic native): a 1-token ping is the
            # only honest proof of reachability.
            ai_models.store(config.provider, config.url(), models=[],
                            supported=False,
                            reason="this provider has no model-listing endpoint")
            await bounded(_ping(provider, model))
            check("endpoint", True, "reachable (no model list; a 1-token ping succeeded)")
    except ProviderError as exc:
        if exc.code == "bad_request" and getattr(provider, "supports_model_discovery", True):
            # The server may simply have no /models route; a 1-token ping proves
            # the chat path instead.
            ai_models.store(config.provider, config.url(), models=[],
                            supported=False,
                            reason="this endpoint does not advertise a model list")
            try:
                await bounded(_ping(provider, model))
                check("endpoint", True,
                      "reachable (no model list; a 1-token ping succeeded)")
            except ProviderError as ping_exc:
                fail("endpoint", ping_exc.code, ping_exc.message)
            except asyncio.TimeoutError:
                fail("endpoint", "timeout", "the provider did not answer in time")
            except Exception:  # noqa: BLE001 - never echo a fragment of the response
                fail("endpoint", "internal", "the provider test failed")
        else:
            fail("endpoint", exc.code, exc.message)
    except asyncio.TimeoutError:
        fail("endpoint", "timeout", "the provider did not answer in time")
    except Exception:  # noqa: BLE001 - never leak a raw traceback or a body fragment
        fail("endpoint", "internal", "the provider test failed")
    finally:
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001
            pass
    return result


async def _ping(provider, model: str) -> None:
    """One cheap request that proves the chat path works (bounded by the caller).

    Consumes the stream to a terminal event: a provider that yields one token and
    then errors must not be reported as reachable.
    """
    from pacifica_obd.web.ai.providers import ChatRequest, Done

    request = ChatRequest(model=model, system="Reply with a single character.",
                          messages=[Message(role="user", content="ping")],
                          temperature=0.0, max_output_tokens=1)
    async for event in provider.stream(request):
        if isinstance(event, ProviderError):
            raise event
        if isinstance(event, Done):
            if event.reason in ("error", "cancelled", "budget", "rate_limit"):
                raise ProviderError("unavailable",
                                    "the provider did not complete the probe")
            return
    raise ProviderError("bad_request", "the provider did not complete a stream")


@router.get("/ai/models", summary="Model list for the configured (or a candidate) provider")
async def list_models(provider: str | None = None, base_url: str | None = None,
                      refresh: bool = False):
    """Return the discovered model list, the preset's suggestions, and freshness.

    Cached by default (no network): a listing costs a round trip, so it is fetched
    only when asked for. ``refresh=1`` re-probes the provider — which needs a saved
    configuration and, for a hosted provider, a key — and stores the result. When
    listing is unavailable the entry explains why, and the suggestions are returned
    regardless, so the model field is never empty.
    """
    config = _validated_config()
    store = _keystore()

    wanted_provider = (provider or config.provider or "").strip().lower() or None
    if wanted_provider is not None and wanted_provider not in ai_presets.PRESETS:
        raise errors.validation(f"unknown provider: {wanted_provider!r}")
    preset = ai_presets.preset(wanted_provider)
    wanted_url = (base_url or (config.url() if wanted_provider == config.provider else None)
                  or (preset.default_base_url if preset else None))

    entry = ai_models.read_entry(wanted_provider, wanted_url)
    payload = {
        "provider": wanted_provider,
        "label": preset.label if preset else None,
        "base_url": wanted_url,
        "suggested": list(preset.example_models) if preset else [],
        "discovered": [],
        "supported": None,
        "reason": None,
        "checked_at": None,
        "stale": True,
        "source": "none",
    }

    if refresh:
        if wanted_provider is None:
            raise errors.validation("choose a provider before refreshing its model list")
        if wanted_provider != config.provider:
            # Probing a provider that is not the saved configuration would need a
            # key we do not have (keys are per provider, but the endpoint must be
            # the saved one to be meaningful).
            raise errors.validation(
                "refresh the list for the configured provider, or save the change first"
            )
        key = store.get(config.provider)
        if preset is not None and preset.requires_key and not key:
            raise errors.ApiError("no_key", "validation",
                                 f"no API key is configured for {preset.label}",
                                 http_status=400)
        models, supported, reason = await _discover_models(config)
        entry = ai_models.store(
            config.provider, config.url(),
            models=[{"id": info.id, "name": getattr(info, "name", None)}
                    for info in (models or [])],
            supported=supported, reason=reason)
        payload["source"] = "live"

    if entry is not None:
        payload["discovered"] = [
            {"id": item.get("id"), "name": item.get("name")}
            for item in (entry.get("models") or [])
        ]
        payload["supported"] = entry.get("supported")
        payload["reason"] = entry.get("reason")
        payload["checked_at"] = entry.get("checked_at")
        if payload["source"] != "live":
            payload["source"] = "cache"
    payload["stale"] = ai_models.is_stale(entry)
    return payload


async def _discover_models(config) -> tuple[list | None, bool, str | None]:
    """One bounded model listing. Returns ``(models, supported, reason)``.

    Never raises for a provider-side failure: the caller wants to say *why* there
    is no list, not to fail the request.
    """
    key = _keystore().get(config.provider)
    try:
        provider = build_provider(config, key=key)
    except ProviderError as exc:
        return None, False, exc.message
    try:
        if not getattr(provider, "supports_model_discovery", True):
            return None, False, "this provider has no model-listing endpoint"
        try:
            models = await asyncio.wait_for(provider.list_models(), TEST_TIMEOUT_S)
            return list(models), True, None
        except ProviderError as exc:
            if exc.code == "bad_request":
                return None, False, "this endpoint does not advertise a model list"
            return None, False, exc.message
        except asyncio.TimeoutError:
            return None, False, "the provider did not answer in time"
        except Exception:  # noqa: BLE001 - never echo a body fragment
            return None, False, "the model listing failed"
    finally:
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001
            pass


@router.get("/ai/conversations", summary="Stored conversations (opt-in)")
async def conversations(limit: int = 50):
    def load():
        store = Store()
        try:
            if not store.available:
                return {"available": False, "conversations": []}
            return {"available": True, "conversations": store.ai_list_conversations(limit)}
        finally:
            store.close()

    return await asyncio.to_thread(load)


@router.get("/ai/conversations/{conversation_id}", summary="One stored conversation")
async def conversation(conversation_id: str):
    def load():
        store = Store()
        try:
            if not store.available:
                raise errors.ApiError("db_unavailable", "adapter", "no local database found",
                                      http_status=503)
            found = store.ai_get_conversation(conversation_id)
            if found is None:
                raise errors.not_found(f"no conversation {conversation_id}",
                                       code="conversation_not_found")
            return found
        finally:
            store.close()

    return await asyncio.to_thread(load)


# --------------------------------------------------------------------------- #
# "what will be sent"
# --------------------------------------------------------------------------- #
def _preview(config, message: str) -> dict:
    redacted, _ = _prepare_user_message(config, message, None)
    redact = ai_keys.KeyStore().redactor()
    system = ai_prompts.SYSTEM_PROMPT
    return {
        # A provider id is normally a known token, but a key can be pasted into
        # any free-text field; scrub it like the model and URL below.
        "provider": redact(config.provider) if config.provider else config.provider,
        "provider_label": ai_presets.preset_label(config.provider),
        "model": redact(effective_model(config)),
        "base_url": redact(config.url()) if config.url() and not config.is_local() else None,
        "local": config.is_local(),
        "redact_vin": config.redact_vin,
        "send_bus_data_to_remote": config.send_bus_data_to_remote,
        "system": system,
        "user": redacted,
        "tools": ai_tools.tools_json(),
        "note": "tool results are fetched later and are fenced as untrusted vehicle data",
    }


@router.get("/ai/what-will-be-sent", summary="Exactly what egresses on the next request")
async def what_will_be_sent(request: Request, message: str = "", task: str | None = None):
    config = _load_config()
    preview = _preview(config, message)
    allow_bus = config.is_local() or config.send_bus_data_to_remote
    pack = None
    if task and task in TASKS and allow_bus:
        worker = deps.get_worker(request)
        cache = ai_backend.CacheToolBackend(config, worker=worker)
        envelope = _Envelope(None, None, None, None)
        try:
            pack, _ids, _meta = await asyncio.to_thread(_gather_context, cache, task,
                                                        envelope, engine_nonce())
        except Exception:  # noqa: BLE001 - a preview must never fail hard
            pack = None
    preview["context_pack"] = pack
    preview["context_withheld"] = bool(task) and not allow_bus
    return preview


@router.delete("/ai/conversations", summary="Delete all stored conversations")
async def delete_conversations():
    def clear():
        store = Store()
        try:
            if store.available:
                store.ai_delete_conversations()
            return {"deleted": True}
        finally:
            store.close()

    return await asyncio.to_thread(clear)


# --------------------------------------------------------------------------- #
# chat + tasks (SSE)
# --------------------------------------------------------------------------- #
def _prepare_user_message(config, message: str, vin: str | None):
    from pacifica_obd.web.ai.keys import KeyStore

    redactor = KeyStore().redactor()
    redacted = redactor(redact_vin_text(message, enabled=bool(config.redact_vin)))
    return redacted, vin


def _gather_context(cache, task: str | None, body,
                    nonce: str | None = None) -> tuple[str, list[str], dict]:
    """Gather a deterministic evidence pack (code, not model).

    Every stored read is scoped to **one** session so unrelated historical scans
    (or another vehicle) cannot be presented as facts about the car in question.
    The scope defaults to the most recent session; a task may name one.
    """
    items: list[dict] = []
    meta: dict = {}

    def add(eid, label, tool, data, source=None):
        spec = ai_tools.TOOLS.get(tool)
        items.append({"id": eid, "label": label, "tool": tool, "data": data,
                      "source": source or (spec.source if spec else "local-db")})

    sessions = cache.call("list_sessions", {"limit": 1})
    rows = list(sessions.get("sessions") or [])
    requested = getattr(body, "session_id", None)
    scope_id = requested or (rows[0].get("id") if rows else None)
    scope = {"session_id": int(scope_id)} if scope_id else {}
    meta["session_id"] = scope.get("session_id")
    # Pin the cache backend to this session so every tool it serves (including
    # vehicle context and discovery) stays in scope.
    cache.session_id = scope.get("session_id")

    add("E1", "Adapter/connection status", "get_adapter_status",
        cache.call("get_adapter_status", {}))
    add("E2", "Vehicle context", "get_vehicle_context",
        cache.call("get_vehicle_context", {}))
    if scope_id:
        known = next((row for row in rows if row.get("id") == scope_id), None)
        payload = {"session_id": int(scope_id),
                   "note": "the stored reads below are scoped to this session only"}
        if known is not None:
            payload["row"] = known
        else:
            payload["requested"] = True
        add("E3", "Evidence scope (one stored session)", "list_sessions", payload)
    else:
        add("E3", "Evidence scope", "list_sessions",
            {"note": "no stored session found; vehicle reads below are absent"})

    context = getattr(body, "context", None) or {}
    selected = context.get("selection") if isinstance(context, dict) else None
    if selected:
        add("E4", "User-selected item", "explain_selection", selected, source="local-db")

    needs_vehicle = task in ("diagnose", "report", "pretrip", "compare", None)
    dtcs: list[dict] = []
    if needs_vehicle or task == "explain":
        generic = cache.call("read_generic_dtcs", dict(scope))
        dtcs = list(generic.get("dtcs") or [])
        add("E5", "Generic DTCs (scoped)", "read_generic_dtcs", generic)

    if task in ("diagnose", "pretrip"):
        add("E6", "Per-module DTCs (scoped)", "read_module_dtcs",
            cache.call("read_module_dtcs", dict(scope)))
        add("E7", "Readiness (stored, may be absent)", "read_readiness",
            cache.call("read_readiness", {}))

    if task == "report" and getattr(body, "session_id", None):
        add("E8", "Session", "get_session",
            cache.call("get_session", {"session_id": int(body.session_id)}))
    if task == "compare" and getattr(body, "a", None) and getattr(body, "b", None):
        add("E6", "Session diff", "compare_sessions",
            cache.call("compare_sessions", {"a": int(body.a), "b": int(body.b)}))

    if task == "pretrip":
        coverage = cache.session_coverage(scope.get("session_id"))
        meta["coverage"] = coverage
        verdict = _pretrip_verdict(dtcs, coverage=coverage)
        meta["verdict"] = verdict
        add("E9", "Deterministic pre-trip verdict", "pretrip_verdict", verdict,
            source="local-db")

    flags = ai_prompts.safety_flags(dtcs)
    meta["safety_flags"] = flags
    if flags:
        add("E10", "Deterministic safety flags", "safety_flags", {"flags": flags},
            source="local-db")

    text, ids = ai_prompts.render_evidence(items, nonce=nonce)
    if flags:
        text = "SAFETY FLAGS (computed in code; do not change):\n" + "\n".join(
            f"- {flag}" for flag in flags) + "\n\n" + text
    return text, ids, meta


def _pretrip_verdict(dtcs, *, coverage: dict | None = None) -> dict:
    flags = ai_prompts.safety_flags(dtcs)
    codes = [str(dtc.get("code") or "") for dtc in dtcs or []]
    hard = [flag for flag in flags if flag.startswith(("High-voltage", "Fault in the hybrid"))
            or ": restraint" in flag or ": brake" in flag]
    # A session id alone is not a scan: the session has to have recorded some
    # diagnostic data (DTCs, discovery or DID/identification reads) before a
    # "READY" is honest. A bare command session (e.g. `ports`) is not coverage.
    has_scan = bool(coverage and coverage.get("diagnostic"))
    if not has_scan:
        return {"verdict": "INSUFFICIENT DATA", "dtc_count": len(codes), "codes": codes,
                "reasons": ["no diagnostic read is recorded in the scoped session; "
                            "run a scan before a pre-trip check"],
                "coverage": coverage or {},
                "computed_by": "deterministic rule table"}
    if hard:
        verdict = "DO NOT DRIVE"
    elif codes:
        verdict = "ATTENTION"
    else:
        verdict = "READY"
    return {"verdict": verdict, "dtc_count": len(codes), "codes": codes,
            "reasons": hard or ([f"{len(codes)} stored code(s)"] if codes
                                else ["no stored codes in the scoped evidence pack"]),
            "coverage": coverage or {},
            "computed_by": "deterministic rule table"}


@router.post("/ai/chat", summary="Stream an assistant turn (SSE; no write capability)")
async def chat(request: Request, body: AIChatRequest, collect: int = Query(default=0)):
    return await _chat_stream(request, message=body.message, tools=body.tools,
                              task=body.task, context=body.context,
                              conversation_id=body.conversation_id, session_id=None,
                              a=None, b=None, collect=bool(collect))


@router.post("/ai/tasks/{name}", summary="Run a task prompt (SSE)")
async def task(request: Request, name: str, body: AITaskRequest,
               collect: int = Query(default=0)):
    if name not in TASKS:
        raise errors.not_found(f"unknown task: {name}", code="unknown_task")
    message = body.message or ai_prompts.TASK_PROMPTS.get(name, "")
    return await _chat_stream(request, message=message, tools=body.tools, task=name,
                              context=None, conversation_id=body.conversation_id,
                              session_id=body.session_id, a=body.a, b=body.b,
                              collect=bool(collect))


class _Envelope:
    def __init__(self, context, session_id, a, b):
        self.context = context
        self.session_id = session_id
        self.a = a
        self.b = b


async def _chat_stream(request: Request, *, message, tools, task, context,
                       conversation_id, session_id, a, b, collect: bool = False):
    config = _validated_config()
    if config.force_local_only and not config.is_local():
        raise errors.ApiError("local_only", "refused",
                              "local-only mode is on: a remote provider cannot be used",
                              http_status=403)
    allow_bus = config.is_local() or config.send_bus_data_to_remote
    # Bus-data consent gates *everything*, including tool-calling: a cache-first
    # tool still returns stored vehicle data, so with a remote provider and consent
    # off the tool loop must be refused, not merely stripped of its evidence pack.
    if (task or context or tools) and not allow_bus:
        raise errors.ApiError(
            "remote_context_not_allowed", "refused",
            "this request needs the vehicle's data, and sending bus data to a remote "
            "model is off",
            http_status=403,
            detail={"hint": "enable it in Settings → AI, or use a local model"},
        )
    store = _keystore()
    redactor = store.redactor()
    provider = _provider_or_400(config, store)
    limiter = _rate_limiter(request, config)
    worker, run = _runner(request)

    user_text = redactor(redact_vin_text(message, enabled=bool(config.redact_vin)))
    messages = [Message(role="user", content=user_text)]
    cancel = asyncio.Event()
    conversation = conversation_id or uuid.uuid4().hex
    prior_tokens = int(getattr(request.app.state, "ai_tokens", {}).get(conversation, 0))

    async def turn():
        pack, ids = None, []
        try:
            scope_id = None
            if allow_bus:
                cache = ai_backend.CacheToolBackend(config, worker=worker)
                envelope = _Envelope(context, session_id, a, b)
                pack, ids, _meta = await asyncio.to_thread(
                    _gather_context, cache, task, envelope, engine_nonce())
                scope_id = _meta.get("session_id")
            # The tool backend is only wired when bus data is allowed; with consent
            # off it is None, so the engine cannot dispatch even if asked. It is
            # pinned to the task's session so a tool call cannot widen the scope.
            backend = (ai_backend.WebToolBackend(worker, config, runner=run,
                                                 session_id=scope_id)
                       if allow_bus else None)
            engine = ChatEngine(provider, config=config, backend=backend,
                                tools_enabled=bool(tools) and allow_bus,
                                rate_limiter=limiter,
                                live_reads=config.max_live_reads_per_turn,
                                prior_tokens=prior_tokens)
            yield "meta", {"provider": provider.id, "model": redactor(effective_model(config)),
                           "local": config.is_local(),
                           "context": "included" if pack else "withheld",
                           "tools": bool(tools) and allow_bus,
                           "task": task,
                           "conversation_id": conversation,
                           "redact_vin": bool(config.redact_vin),
                           "cost_estimate": True}
            answer = []
            async for event in engine.stream(
                    messages=messages, context_pack=pack, evidence_ids=ids, cancel=cancel,
                    max_output_tokens=config.max_output_tokens_per_request):
                if await request.is_disconnected():
                    cancel.set()
                if event["event"] == "token":
                    answer.append(event["data"].get("text", ""))
                if event["event"] == "done":
                    event["data"]["conversation_id"] = conversation
                    event["data"]["invalid_citations"] = []
                yield event["event"], event["data"]
            snapshot = engine.meter.snapshot()
            request.app.state.ai_tokens = getattr(request.app.state, "ai_tokens", {})
            request.app.state.ai_tokens[conversation] = snapshot["total_tokens"]
            await asyncio.to_thread(_record, config, provider.id, snapshot, conversation,
                                    user_text, redactor("".join(answer)),
                                    redactor(effective_model(config)))
        except asyncio.CancelledError:
            cancel.set()
            raise
        except ProviderError as exc:
            yield "error", {**exc.as_dict(), "message": redactor(exc.message)}
            yield "done", {"reason": "error", "conversation_id": conversation}
        except Exception as exc:  # noqa: BLE001 - never leak a raw traceback
            yield "error", {"code": "internal", "message": redactor(str(exc))[:300],
                            "retry_after_s": None}
            yield "done", {"reason": "error", "conversation_id": conversation}
        finally:
            await provider.aclose()

    if collect:
        events = [{"event": event, "data": data} async for event, data in turn()]
        text = "".join(item["data"].get("text", "") for item in events
                       if item["event"] == "token")
        return JSONResponse({"events": events, "answer": text,
                             "conversation_id": conversation})

    async def sse():
        async for event, data in turn():
            yield _sse(event, data)

    return StreamingResponse(sse(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


def engine_nonce() -> str:
    import secrets

    return secrets.token_hex(6)


def _record(config, provider_id: str, usage: dict, conversation_id: str,
            user_text: str, answer: str, model: str | None = None) -> None:
    """Best-effort usage + opt-in conversation persistence; never raises.

    ``model`` is pre-redacted by the caller so a credential pasted into the model
    field is never written to the diagnostic store.
    """
    try:
        store = Store()
    except Exception:  # noqa: BLE001
        return
    try:
        if not store.available:
            return
        store.ai_record_usage(provider_id, model if model is not None else config.model,
                              usage.get("input_tokens", 0),
                              usage.get("output_tokens", 0),
                              usage.get("usd_estimate", 0.0))
        if config.persist_conversations:
            store.ai_save_conversation(conversation_id, [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": answer},
            ], title=user_text[:60])
    except Exception:  # noqa: BLE001
        pass
    finally:
        store.close()
