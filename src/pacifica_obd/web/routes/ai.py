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
    data["defaults"] = {"base_urls": dict(ai_config.DEFAULT_BASE_URLS)}
    data["cost_table_date"] = ai_config.COST_TABLE_DATE
    data["local_base_hosts"] = sorted(ai_config.LOCAL_BASE_HOSTS)
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
            "configured_provider": config.provider}


@router.get("/ai/config", summary="Current AI configuration (never a key)")
async def get_config():
    config = _load_config()
    return _public_config(config, _keystore())


@router.put("/ai/config", summary="Patch the AI configuration (secrets go elsewhere)")
async def put_config(request: Request, body: AIConfigPatch):
    store = _keystore()
    config = _load_config()
    # `exclude_unset` distinguishes "field omitted" from "explicit null": a null
    # on provider/model/base_url clears the field (so a reset is possible).
    patch = body.model_dump(exclude_unset=True)
    clearable = {"provider", "model", "base_url"}
    patch = {key: value for key, value in patch.items()
             if value is not None or key in clearable}
    try:
        updated = config.patch(patch).validate()
    except (ValueError, TypeError) as exc:
        raise errors.validation(str(exc)) from exc
    try:
        updated.save()
    except ai_keys.KeyStoreError as exc:
        raise errors.ApiError("key_store", "validation", str(exc), http_status=400) from exc
    return _public_config(_load_config(), store)


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
    except ai_keys.KeyStoreError as exc:
        raise errors.ApiError("key_store", "validation", str(exc), http_status=400) from exc
    # Never echo the key back; a masked hint only.
    return {"provider": provider, "key": store.masked(provider)}


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
        else:
            # No model-discovery API (e.g. Anthropic): a 1-token ping is the only
            # honest proof of reachability.
            await bounded(_ping(provider, model))
            check("endpoint", True, "reachable (no model list; a 1-token ping succeeded)")
    except ProviderError as exc:
        if exc.code == "bad_request" and getattr(provider, "supports_model_discovery", True):
            # The server may simply have no /models route; a 1-token ping proves
            # the chat path instead.
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
        "provider": config.provider,
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
