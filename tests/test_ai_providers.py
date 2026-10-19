"""Provider-preset, config-validation, adapter-shape and connection-test checks.

Covers the setup ergonomics the brief demands — can a user pick each provider and
paste a key without reading the source — plus the review fixes: https/loopback
base-URL rules, key refusals, config bounds, robust SSE parsing, and the
``POST /api/ai/test`` endpoint (against the offline fake provider).

    uv run --extra web python tests/test_ai_providers.py

No network and no key are needed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import httpx  # noqa: F401
    from httpx import ASGITransport, AsyncClient
except ImportError:  # pragma: no cover
    print("the [web] extra is required: uv run --extra web python tests/test_ai_providers.py")
    raise SystemExit(2)

from pacifica_obd.web import errors  # noqa: E402
from pacifica_obd.web.ai import config as ai_config  # noqa: E402
from pacifica_obd.web.ai import keys as ai_keys  # noqa: E402
from pacifica_obd.web.ai import presets as ai_presets  # noqa: E402
from pacifica_obd.web.ai import redact as ai_redact  # noqa: E402
from pacifica_obd.web.ai.providers import (  # noqa: E402
    ChatRequest,
    Done,
    Message,
    ProviderError,
    Token,
    ToolSpec,
    build_provider,
)
from pacifica_obd.web.routes.ai import _provider_or_400  # noqa: E402

HEADERS = {"X-Pacifica-Request": "1"}
HOSTED = {"openai", "anthropic", "gemini"}
LOCAL = {"ollama", "lmstudio", "vllm", "fake"}


def collect(provider, req):
    async def run():
        return [event async for event in provider.stream(req)]

    return asyncio.run(run())


def sse(*chunks: dict) -> bytes:
    return ("\n\n".join(f"data: {json.dumps(chunk)}" for chunk in chunks)
            + "\n\ndata: [DONE]\n\n").encode()


# --------------------------------------------------------------------------- #
# presets
# --------------------------------------------------------------------------- #
def test_preset_registry_covers_every_named_provider():
    expected = {"openai", "anthropic", "gemini", "ollama", "lmstudio", "vllm",
                "openai_compat", "fake"}
    assert set(ai_presets.PRESETS) == expected, set(ai_presets.PRESETS) ^ expected
    for pid, found in ai_presets.PRESETS.items():
        assert found.label and found.format in ("openai_compat", "anthropic", "fake"), pid
        url = found.default_base_url
        assert url == "" or url.startswith(("http://", "https://")), (pid, url)
        if pid in HOSTED:
            assert found.requires_key is True and found.local is False, pid
            assert found.default_model, pid
        if pid in LOCAL:
            assert found.requires_key is False and found.local is True, pid
    # The generic endpoint must be the one the user has to point somewhere.
    assert ai_presets.PRESETS["openai_compat"].default_base_url == ""
    # Every preset round-trips through the API shape.
    for pid in ai_presets.PRESETS:
        payload = ai_presets.PRESETS[pid].as_dict()
        assert payload["id"] == pid and "notes" in payload


def test_each_preset_builds_the_right_adapter():
    for pid, found in ai_presets.PRESETS.items():
        overrides = {}
        if found.default_base_url == "":
            overrides["base_url"] = "https://api.example.invalid/v1"
        if not found.default_model:
            overrides["model"] = "test-model"
        config = ai_config.AIConfig(provider=pid, **overrides).validate()
        built = build_provider(config, key="sk-test-key-123456")
        assert built.id == pid, (pid, getattr(built, "id", None))
        expected_type = {"fake": "FakeProvider",
                         "anthropic": "AnthropicProvider"}.get(found.format,
                                                               "OpenAICompatProvider")
        assert type(built).__name__ == expected_type, (pid, type(built).__name__)


def test_key_only_setup_for_a_hosted_provider():
    """Pick 'openai' and paste a key: no model or base URL needed."""
    config = ai_config.AIConfig(provider="openai", redact_vin=True).validate()
    assert config.effective_model() == "gpt-4o-mini"
    assert config.url() == "https://api.openai.com/v1"
    assert config.requires_key() is True and config.is_local() is False
    # A local server needs neither a key nor an explicit URL.
    local = ai_config.AIConfig(provider="ollama").validate()
    assert local.url() == "http://127.0.0.1:11434/v1" and local.is_local() is True


# --------------------------------------------------------------------------- #
# base-URL and config validation
# --------------------------------------------------------------------------- #
def test_base_url_rules():
    def accepts(**kw):
        return ai_config.AIConfig(provider="openai_compat", model="m", **kw).validate()

    accepts(base_url="https://api.example.com/v1")
    accepts(base_url="http://127.0.0.1:11434/v1")
    accepts(base_url="http://localhost:1234/v1")
    for bad in ("http://api.example.com/v1",   # plaintext remote
                "https://",                    # no host
                "ftp://api.example.com/v1",    # wrong scheme
                "https://user:pw@proxy.example/v1"):  # embedded credential
        try:
            accepts(base_url=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"base_url {bad!r} was accepted")


def test_config_bounds_and_types():
    def bad(**kw):
        try:
            ai_config.AIConfig(provider="fake", model="fake-1", **kw).validate()
        except ValueError:
            return
        raise AssertionError(f"{kw} was accepted")

    bad(max_output_tokens_per_request=10 ** 9)
    bad(max_requests_per_minute=0)
    bad(max_tokens_per_conversation=10)
    bad(temperature=5.0)
    # Zero live reads is a legitimate "never read the bus" choice.
    ai_config.AIConfig(provider="fake", model="fake-1",
                       max_live_reads_per_turn=0).validate()
    # A stringly-typed saved bool is coerced, not treated as truthy.
    coerced = ai_config.AIConfig.from_dict(
        {"provider": "openai", "model": "m",
         "send_bus_data_to_remote": "false"}).validate()
    assert coerced.send_bus_data_to_remote is False
    # A null temperature is a ValueError (a 400), never a raw TypeError (a 500).
    try:
        ai_config.AIConfig.from_dict(
            {"provider": "fake", "model": "fake-1", "temperature": None}).validate()
    except ValueError:
        pass
    else:
        raise AssertionError("temperature=None did not raise ValueError")


def test_patch_clears_on_explicit_null_and_keeps_omitted_fields():
    config = ai_config.AIConfig(provider="openai", model="m",
                                base_url="https://api.openai.com/v1",
                                send_bus_data_to_remote=True)
    cleared = config.patch({"provider": None, "base_url": None})
    assert cleared.provider is None and cleared.base_url is None
    assert cleared.model == "m" and cleared.send_bus_data_to_remote is True


# --------------------------------------------------------------------------- #
# key handling
# --------------------------------------------------------------------------- #
def test_missing_and_blank_key_refusals():
    home = tempfile.mkdtemp()
    store = ai_keys.KeyStore(Path(home) / "ai.json", env={})
    # A hosted provider with no key is refused with an actionable code.
    try:
        _provider_or_400(ai_config.AIConfig(provider="openai", model="gpt-x"), store)
    except errors.ApiError as exc:
        assert exc.code == "no_key" and exc.http_status == 400
    else:
        raise AssertionError("a keyless hosted provider was built")
    # A local server and a custom endpoint may legitimately need no key.
    assert _provider_or_400(ai_config.AIConfig(provider="ollama"), store) is not None
    custom = ai_config.AIConfig(provider="openai_compat", model="m",
                                base_url="https://vllm.example/v1")
    assert _provider_or_400(custom, store) is not None
    # With a key, the hosted provider builds.
    store.set("openai", "sk-openai-1234567890")
    assert _provider_or_400(ai_config.AIConfig(provider="openai", model="gpt-x"),
                            store) is not None


def test_key_set_replace_delete_mask_roundtrip():
    home = tempfile.mkdtemp()
    path = Path(home) / "ai.json"
    store = ai_keys.KeyStore(path, env={})
    store.set("openai", "sk-first-1234567890")
    assert store.get("openai") == "sk-first-1234567890"
    assert store.masked("openai") == {"configured": True, "hint": "…7890", "source": "file"}
    store.set("openai", "sk-second-abcdefghij")           # replace
    assert store.get("openai") == "sk-second-abcdefghij"
    assert store.masked("openai")["hint"].endswith("ghij")
    # A fresh store sees the replaced value (nothing cached across instances).
    assert ai_keys.KeyStore(path, env={}).get("openai") == "sk-second-abcdefghij"
    assert store.delete("openai") is True
    assert store.masked("openai")["configured"] is False
    assert store.delete("openai") is False                 # idempotent
    assert ai_keys.KeyStore(path, env={}).get("openai") is None


def test_vendor_env_var_names_are_honoured():
    home = tempfile.mkdtemp()
    path = Path(home) / "ai.json"
    assert ai_keys.KeyStore(path, env={"OPENAI_API_KEY": "sk-env-openai"}).get(
        "openai") == "sk-env-openai"
    assert ai_keys.KeyStore(path, env={"ANTHROPIC_API_KEY": "sk-ant-env"}).get(
        "anthropic") == "sk-ant-env"
    assert ai_keys.KeyStore(path, env={"GEMINI_API_KEY": "AIza-env"}).get(
        "gemini") == "AIza-env"
    # The historical alias still resolves for the generic compatible provider.
    assert ai_keys.KeyStore(path, env={"PACIFICA_OBD_AI_KEY_OPENAI": "sk-old"}).get(
        "openai_compat") == "sk-old"


def test_logging_literals_refresh_and_retain_retired_keys():
    home = tempfile.mkdtemp()
    store = ai_keys.KeyStore(Path(home) / "ai.json", env={})
    store.set("openai", "corporate-secret-abcdef")
    store.refresh_logging_literals()
    assert "corporate-secret-abcdef" in ai_keys._LITERALS
    store.delete("openai")
    store.refresh_logging_literals()
    # A retired key stays masked so an in-flight request cannot leak it.
    assert "corporate-secret-abcdef" in ai_keys._LITERALS


def test_logging_filter_scrubs_nested_dict_arguments():
    import io
    import logging

    ai_keys.set_logging_literals(["corporate-secret-abcdef"])
    ai_keys.install_logging_filter()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(ai_keys._FILTER)
    logger = logging.getLogger("pacifica_obd.test.nested")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        logger.info("payload=%s", {"outer": {"credential": "corporate-secret-abcdef"}})
    finally:
        logger.handlers = []
    assert "corporate-secret-abcdef" not in stream.getvalue(), stream.getvalue()


def test_recursive_scrub_covers_dict_keys_and_nesting():
    key = "corporate-secret-abcdef"
    value = {"ok": [{"nested": {key: f"x {key} y"}}], key: key}
    out = ai_redact.scrub_obj(value, keys=(key,))
    assert key not in json.dumps(out)
    # a pathological structure does not raise
    deep = current = {}
    for _ in range(50):
        current["child"] = {}
        current = current["child"]
    current["leaf"] = key
    assert key not in json.dumps(ai_redact.scrub_obj(deep, keys=(key,)))


def test_stream_redactor_never_splits_a_literal_with_whitespace():
    key = "alpha beta gamma"
    redactor = ai_redact.StreamRedactor(lambda s: ai_redact.scrub(s, keys=(key,)),
                                        literals=(key,))
    chunks = ["alpha beta ", "g", "amma" + "x" * 40, " tail"]
    out = "".join(redactor.feed(c) for c in chunks) + redactor.flush()
    assert key not in out, out
    assert "tail" in out


def test_stream_redactor_never_releases_a_partial_literal_when_they_overlap():
    literals = ("abcdef ghij", "12345abcdef")
    scrub = lambda s: ai_redact.scrub(s, keys=literals)  # noqa: E731
    redactor = ai_redact.StreamRedactor(scrub, literals=literals)
    buffer = "12345abcdef ghij" + "x" * 24
    # `feed` must not release a prefix of either literal while more may follow.
    assert redactor.feed(buffer) == ""
    flushed = redactor.flush()
    assert "12345abcdef" not in flushed and "abcdef ghij" not in flushed


def test_over_depth_scrub_uses_a_marker_not_escaped_serialization():
    key = 'secret"phrase'
    value = {"leaf": key}
    for _ in range(10):
        value = {"nest": value}
    out = json.dumps(ai_redact.scrub_obj(value, keys=(key,)))
    assert key not in out
    assert 'secret\\"' not in out  # JSON escaping must not resurrect the literal


def test_logging_filter_preserves_positional_tuple_args():
    import io
    import logging

    ai_keys.set_logging_literals(["unusual-secret-abcdef"])
    ai_keys.install_logging_filter()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(ai_keys._FILTER)
    logger = logging.getLogger("pacifica_obd.test.positional")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        # Must not raise "not enough arguments for format string".
        logger.info("a=%s b=%s", "one", {"token": "unusual-secret-abcdef"})
    finally:
        logger.handlers = []
    assert "unusual-secret-abcdef" not in stream.getvalue(), stream.getvalue()
    assert "a=one b=" in stream.getvalue()


def test_alternative_numeric_link_local_forms_are_rejected():
    for bad in ("https://0xa9fea9fe", "https://2851995648", "https://169.254.43518",
                "https://0251.0376.0251.0376", "https://[::ffff:169.254.169.254]"):
        try:
            ai_config.AIConfig(provider="openai_compat", model="m",
                               base_url=bad).validate()
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} was accepted")
    # A normal host is unaffected.
    ai_config.AIConfig(provider="openai_compat", model="m",
                       base_url="https://api.openai.com/v1").validate()


def test_probe_ping_requires_a_terminal_event():
    from pacifica_obd.web.ai.providers import Done, Token, Usage
    from pacifica_obd.web.routes.ai import _ping

    class YieldsThenErrors:
        async def stream(self, request):
            yield Token("x")
            raise ProviderError("unavailable", "boom")

    class EmptyStream:
        async def stream(self, request):
            return
            yield  # pragma: no cover

    class DoneError:
        async def stream(self, request):
            yield Usage()
            yield Done(reason="error")

    class YieldsProviderError:
        async def stream(self, request):
            yield ProviderError("unavailable", "boom")

    class Completes:
        async def stream(self, request):
            yield Usage()
            yield Done(reason="stop")

    for provider in (YieldsThenErrors(), EmptyStream(), DoneError(), YieldsProviderError()):
        try:
            asyncio.run(_ping(provider, "m"))
        except ProviderError:
            pass
        else:
            raise AssertionError("an incomplete probe was reported as reachable")
    asyncio.run(_ping(Completes(), "m"))  # does not raise


def test_sse_line_cap_rejects_an_oversized_line():
    long_line = ("data: " + "x" * 200 + "\n\n").encode()
    provider = build_provider(
        ai_config.AIConfig(provider="openai_compat", model="m",
                           base_url="https://api.example.invalid/v1"),
        key="sk-x-1234567890",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=long_line)))
    # Force a tiny line cap so the guard is what rejects it.
    async def run():
        from pacifica_obd.web.ai.providers import iter_sse_data

        response = httpx.Response(200, content=long_line)
        async for _data in iter_sse_data(response, max_line=16):
            pass

    try:
        asyncio.run(run())
    except ProviderError as exc:
        assert exc.code == "unavailable"
    else:
        raise AssertionError("an oversized SSE line was accepted")


def test_link_local_and_metadata_hosts_are_rejected():
    for bad in ("https://169.254.169.254/latest", "http://169.254.169.254/v1"):
        try:
            ai_config.AIConfig(provider="openai_compat", model="m",
                               base_url=bad).validate()
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} was accepted")


def test_probe_model_listing_is_bounded_and_field_safe():
    # A model row whose context_length is junk must not raise (and must not echo it).
    body = json.dumps({"data": [{"id": "m", "context_length": "odd-secret-value"}]}).encode()
    provider = build_provider(
        ai_config.AIConfig(provider="openai_compat", model="m",
                           base_url="https://api.example.invalid/v1"),
        key="sk-x-1234567890",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    models = asyncio.run(provider.list_models())
    assert models and models[0].id == "m" and models[0].context == 0

    # A response that is not a model list is a typed error, not a crash.
    provider = build_provider(
        ai_config.AIConfig(provider="openai_compat", model="m",
                           base_url="https://api.example.invalid/v1"),
        key="sk-x-1234567890",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"<html>nope</html>")))
    try:
        asyncio.run(provider.list_models())
    except ProviderError as exc:
        assert exc.code == "bad_request"
    else:
        raise AssertionError("a non-JSON model list was accepted")


def test_anthropic_reports_no_model_discovery():
    provider = build_provider(ai_config.AIConfig(provider="anthropic", model="claude-x"),
                              key="sk-ant-x-123456")
    # A static model suggestion is not proof of reachability; the probe must ping.
    assert getattr(provider, "supports_model_discovery", True) is False
    provider2 = build_provider(ai_config.AIConfig(provider="openai", model="gpt-4o-mini"),
                               key="sk-x-1234567890")
    assert getattr(provider2, "supports_model_discovery", False) is True


def test_public_config_redacts_url_credentials():
    from pacifica_obd.web.routes.ai import _public_config

    home = tempfile.mkdtemp()
    store = ai_keys.KeyStore(Path(home) / "ai.json", env={})
    store.set("openai_compat", "corporate-secret-abcdef")
    config = ai_config.AIConfig(
        provider="openai_compat", model="m",
        base_url="https://proxy.example/v1?token=corporate-secret-abcdef").validate()
    data = _public_config(config, store)
    assert "corporate-secret-abcdef" not in json.dumps(data)
    assert data["effective_model"] == "m"


def test_pretrip_requires_recorded_coverage():
    from pacifica_obd.web.routes.ai import _pretrip_verdict

    empty = _pretrip_verdict([], coverage={"diagnostic": False})
    assert empty["verdict"] == "INSUFFICIENT DATA"
    assert _pretrip_verdict([], coverage={"diagnostic": True})["verdict"] == "READY"
    assert _pretrip_verdict([{"code": "P0300", "module": "7E0"}],
                            coverage={"diagnostic": True})["verdict"] == "ATTENTION"
    assert _pretrip_verdict([{"code": "P0AA6", "module": "BPCM"}],
                            coverage={"diagnostic": True})["verdict"] == "DO NOT DRIVE"


# --------------------------------------------------------------------------- #
# adapter request shape (stubbed transport)
# --------------------------------------------------------------------------- #
def _capturing_transport(handler, seen):
    def wrapped(request):
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped)


def test_openai_request_shape_and_tools():
    seen: list = []
    provider = build_provider(
        ai_config.AIConfig(provider="openai_compat", model="m",
                           base_url="https://api.openai.com/v1"),
        key="sk-shape-1234567890",
        transport=_capturing_transport(
            lambda request: httpx.Response(200, content=sse(
                {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})),
            seen))
    tool = ToolSpec(name="read_generic_dtcs", description="dtcs",
                    parameters={"type": "object", "properties": {}})
    collect(provider, ChatRequest(model="m", system="SYS",
                                  messages=[Message(role="user", content="hi")],
                                  tools=[tool]))
    request = seen[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-shape-1234567890"
    body = json.loads(request.content)
    assert body["model"] == "m" and body["stream"] is True
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "read_generic_dtcs"


def test_anthropic_request_shape_and_tools():
    seen: list = []
    body = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"ok"}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":2}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    ).encode()
    provider = build_provider(
        ai_config.AIConfig(provider="anthropic", model="claude-x",
                           base_url="https://api.anthropic.com"),
        key="sk-ant-shape-123456",
        transport=_capturing_transport(
            lambda request: httpx.Response(200, content=body), seen))
    tool = ToolSpec(name="read_vin", description="vin",
                    parameters={"type": "object", "properties": {}})
    events = collect(provider, ChatRequest(model="claude-x", system="SYS",
                                           messages=[Message(role="user", content="hi")],
                                           tools=[tool]))
    request = seen[0]
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant-shape-123456"
    assert request.headers["anthropic-version"]
    payload = json.loads(request.content)
    assert payload["system"][0]["text"] == "SYS"
    assert payload["tools"][0]["input_schema"]["type"] == "object"
    assert "".join(e.text for e in events if isinstance(e, Token)) == "ok"
    assert isinstance(events[-1], Done) and events[-1].reason == "end_turn"


def test_openai_tool_result_message_maps_to_tool_role():
    seen: list = []
    provider = build_provider(
        ai_config.AIConfig(provider="openai_compat", model="m",
                           base_url="https://api.example.invalid/v1"),
        key="sk-x-1234567890",
        transport=_capturing_transport(
            lambda request: httpx.Response(200, content=sse(
                {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})),
            seen))
    collect(provider, ChatRequest(model="m", system="s", messages=[
        Message(role="user", content="go"),
        Message(role="assistant", content="", tool_calls=(
            {"id": "c1", "name": "read_vin", "args": {}},)),
        Message(role="tool", content="<untrusted>…</untrusted>",
                tool_call_id="c1", name="read_vin"),
    ]))
    messages = json.loads(seen[0].content)["messages"]
    assert messages[0]["role"] == "system"
    assert messages[2]["tool_calls"][0]["type"] == "function"
    assert messages[3]["role"] == "tool" and messages[3]["tool_call_id"] == "c1"


# --------------------------------------------------------------------------- #
# streaming robustness (the review's "reports success on a broken stream")
# --------------------------------------------------------------------------- #
def test_a_non_sse_body_is_reported_not_treated_as_success():
    provider = build_provider(
        ai_config.AIConfig(provider="openai_compat", model="m",
                           base_url="https://proxy.example/v1"),
        key="sk-x-1234567890",
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/html"},
            content=b"<html>Corporate proxy sign-in</html>")))
    try:
        collect(provider, ChatRequest(model="m", system="s",
                                      messages=[Message(role="user", content="x")]))
    except ProviderError as exc:
        assert exc.code in ("bad_request", "unavailable")
        assert "sign-in" not in exc.message
    else:
        raise AssertionError("a non-SSE 200 was reported as success")


def test_an_in_band_error_event_raises():
    body = b'data: {"error":{"code":"context_length_exceeded"}}\n\n'
    provider = build_provider(
        ai_config.AIConfig(provider="openai_compat", model="m",
                           base_url="https://api.example.invalid/v1"),
        key="sk-x-1234567890",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body)))
    try:
        collect(provider, ChatRequest(model="m", system="s",
                                      messages=[Message(role="user", content="x")]))
    except ProviderError as exc:
        assert exc.code == "context_length"
    else:
        raise AssertionError("an in-band error event was ignored")


def test_multiline_sse_data_is_assembled():
    body = ('data: {"choices":[\n'
            'data: {"delta":{"content":"hello"},"finish_reason":"stop"}]}\n'
            '\n'
            'data: [DONE]\n\n').encode()
    provider = build_provider(
        ai_config.AIConfig(provider="openai_compat", model="m",
                           base_url="https://api.example.invalid/v1"),
        key="sk-x-1234567890",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    events = collect(provider, ChatRequest(model="m", system="s",
                                           messages=[Message(role="user", content="x")]))
    assert "".join(e.text for e in events if isinstance(e, Token)) == "hello"


def test_a_transient_5xx_is_retried_once():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="upstream hiccup")
        return httpx.Response(200, content=sse(
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    provider = build_provider(
        ai_config.AIConfig(provider="openai_compat", model="m",
                           base_url="https://api.example.invalid/v1"),
        key="sk-x-1234567890", transport=httpx.MockTransport(handler))
    events = collect(provider, ChatRequest(model="m", system="s",
                                           messages=[Message(role="user", content="x")]))
    assert calls["n"] == 2
    assert "".join(e.text for e in events if isinstance(e, Token)) == "ok"


def test_a_truncated_tool_call_is_not_run():
    # Tool-call arguments never complete and the stream ends without a terminal
    # event: the half-parsed call must not be finalised.
    body = (b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
            b'"function":{"name":"read_generic_dtcs","arguments":"{\\"source\\":"}}]},'
            b'"finish_reason":null}]}\n\n')
    provider = build_provider(
        ai_config.AIConfig(provider="openai_compat", model="m",
                           base_url="https://api.example.invalid/v1"),
        key="sk-x-1234567890",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    events = collect(provider, ChatRequest(model="m", system="s",
                                           messages=[Message(role="user", content="x")]))
    done = events[-1]
    assert isinstance(done, Done) and done.tool_calls == ()


# --------------------------------------------------------------------------- #
# the connection-test endpoint, over real HTTP against the fake provider
# --------------------------------------------------------------------------- #
def _isolated_env():
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    os.environ["PACIFICA_OBD_AI_PROVIDER"] = "fake"
    os.environ["PACIFICA_OBD_AI_MODEL"] = "fake-1"


def _clear_ai_env():
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    for name in ("PACIFICA_OBD_AI_PROVIDER", "PACIFICA_OBD_AI_MODEL",
                 "PACIFICA_OBD_AI_BASE_URL", "PACIFICA_OBD_AI_OFFLINE"):
        os.environ.pop(name, None)


def _run(app, fn):
    async def wrapper():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            try:
                return await fn(client)
            finally:
                app.state.worker.stop()

    return asyncio.run(wrapper())


def test_test_endpoint_and_providers_route_over_http():
    from pacifica_obd.web.app import create_app
    from pacifica_obd.web.settings import Settings

    _isolated_env()
    app = create_app(Settings(demo=True))

    async def body(client):
        # The provider list carries everything a form needs.
        rows = (await client.get("/api/ai/providers")).json()["providers"]
        by_id = {row["id"]: row for row in rows}
        assert {"openai", "anthropic", "gemini", "ollama", "lmstudio", "vllm",
                "openai_compat", "fake"} <= set(by_id)
        for row in rows:
            for field in ("id", "label", "format", "default_base_url", "requires_key",
                          "local", "default_model", "example_models", "notes"):
                assert field in row, (row["id"], field)
        assert by_id["openai"]["requires_key"] is True
        assert by_id["ollama"]["requires_key"] is False

        # The test endpoint proves the configuration without a chat.
        result = (await client.post("/api/ai/test", json={}, headers=HEADERS)).json()
        assert result["ok"] is True
        assert result["provider"] == "fake" and result["local"] is True
        assert result["model"] == "fake-1"
        assert "fake-1" in result["models"]
        assert any(check["name"] == "configuration" and check["ok"] for check in result["checks"])

        # A config-only test never opens a socket and still succeeds.
        quick = (await client.post("/api/ai/test", json={"probe": False},
                                   headers=HEADERS)).json()
        assert quick["ok"] is True and quick["probed"] is False

        # The endpoint is CSRF-gated like the other state-spending routes.
        assert (await client.post("/api/ai/test", json={})).status_code == 403

        # An invalid transient override is a structured 200 with ok=false.
        bad = (await client.post("/api/ai/test",
                                 json={"base_url": "http://api.example.com/v1"},
                                 headers=HEADERS)).json()
        assert bad["ok"] is False and bad["error"]["code"] == "validation"

    _run(app, body)


def test_config_route_round_trips_a_transient_provider():
    from pacifica_obd.web.app import create_app
    from pacifica_obd.web.settings import Settings

    _clear_ai_env()
    app = create_app(Settings(demo=True))

    async def body(client):
        # Switch to a hosted preset with only a key-less patch — the model and URL
        # come from the preset.
        response = await client.put("/api/ai/config", json={"provider": "openai"},
                                    headers=HEADERS)
        assert response.status_code == 200, response.text
        config = response.json()
        assert config["provider"] == "openai"
        assert config["effective_model"] == "gpt-4o-mini"
        assert config["effective_base_url"] == "https://api.openai.com/v1"
        assert config["requires_key"] is True
        # An explicit null clears the provider again.
        cleared = await client.put("/api/ai/config", json={"provider": None},
                                   headers=HEADERS)
        assert cleared.json()["provider"] is None

    _run(app, body)


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all AI provider checks passed")
