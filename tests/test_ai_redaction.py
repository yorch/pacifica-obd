"""Egress redaction regressions (codex adversarial review, stage 2).

Each test here is a concrete leak the review demonstrated, so a regression fails
loudly rather than quietly shipping a key or the VIN to a remote model, a log or
the browser.

    uv run --extra web python tests/test_ai_redaction.py
"""
from __future__ import annotations

import asyncio
import io
import logging

from pacifica_obd.web.ai import redact
from pacifica_obd.web.ai.keys import MASK, _RedactingFilter, set_logging_literals
from pacifica_obd.web.ai.providers import ProviderError, map_http_error

VIN = "1C2C7GBG9MR123456"
VIN_HEX = VIN.encode().hex().upper()
# An unusual-shaped key: it does not match any sk-/Bearer pattern, so only the
# literal value can catch it.
ODD_KEY = "customer-secret-12345"


def test_provider_error_body_never_becomes_the_message():
    """A 500 body quoting the credential and the VIN must not reach the client."""
    body = (f'{{"error":"upstream rejected credential {ODD_KEY}; '
            f'request VIN {VIN}"}}')
    for status in (400, 404, 429, 500, 503, 418, 422):
        err = map_http_error(status, body)
        assert ODD_KEY not in err.message, (status, err.message)
        assert VIN not in err.message, (status, err.message)
        assert "upstream rejected" not in err.message.lower()
    # …but the class of error is still useful
    assert map_http_error(429, body).code == "rate_limit"
    assert map_http_error(401, body).code == "auth"
    assert map_http_error(500, body).code == "unavailable"


def test_scrub_removes_literals_the_ascii_vin_and_the_hex_vin():
    text = f"key {ODD_KEY} vin {VIN} hex {VIN_HEX} raw-sk {MASK}"
    out = redact.scrub(text, keys=(ODD_KEY,), vin=VIN)
    assert ODD_KEY not in out
    assert VIN not in out
    assert VIN_HEX not in out, "the F190 hex encoding is a reversible VIN"
    assert VIN_HEX.lower() not in out
    # the redactor never depends on the caller knowing the VIN
    assert VIN not in redact.scrub(f"vin {VIN}")
    assert VIN_HEX not in redact.scrub(f"hex {VIN_HEX}")


def test_hex_redaction_does_not_eat_ordinary_hex():
    """Only a hex run that decodes to a VIN-shaped token may be replaced."""
    for innocent in ("010C", "62F190", "1234567890ABCDEF", "00" * 12):
        assert redact.scrub(innocent) == innocent


def test_stream_redactor_catches_a_vin_split_across_chunks():
    redactor = redact.StreamRedactor(lambda s: redact.scrub(s, vin=VIN))
    chunks = ["here it is: 1C2C7GBG9", "MR123456", " and some prose afterwards"]
    out = "".join(redactor.feed(c) for c in chunks) + redactor.flush()
    assert VIN not in out
    assert "some prose afterwards" in out, out
    # a key split the same way is caught too
    r2 = redact.StreamRedactor(lambda s: redact.scrub(s, keys=(ODD_KEY,)))
    out2 = "".join(r2.feed(c) for c in ["key=customer", "-secret-12345", " tail"]) + r2.flush()
    assert ODD_KEY not in out2


def test_logging_filter_masks_configured_literals_including_args_and_exc():
    set_logging_literals([ODD_KEY])
    filt = _RedactingFilter()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(filt)
    logger = logging.getLogger("pacifica_obd.test.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    logger.info("calling provider with %s", ODD_KEY)
    try:
        raise RuntimeError(f"upstream said {ODD_KEY} for {VIN}")
    except RuntimeError:
        logger.exception("request failed")
    text = stream.getvalue()
    assert ODD_KEY not in text, text
    assert VIN not in text, text


def test_chat_engine_scrubs_provider_and_internal_errors():
    """A provider error object carrying a secret must not reach the SSE stream."""
    from pacifica_obd.web.ai import chat as chat_mod
    from pacifica_obd.web.ai.config import AIConfig

    class LeakyProvider:
        id = "fake"
        label = "fake"

        async def stream(self, request):
            raise ProviderError("unavailable", f"upstream body: {ODD_KEY} / {VIN}")
            yield  # pragma: no cover - keeps this an async generator

    engine = chat_mod.ChatEngine(LeakyProvider(), config=AIConfig(provider="fake",
                                                                 model="fake-1"))
    engine._keys = (ODD_KEY,)          # as if configured in the key store

    async def drain():
        return [event async for event in engine.stream(messages=[])]

    events = asyncio.run(drain())
    blob = repr(events)
    assert ODD_KEY not in blob, blob
    assert VIN not in blob, blob
    assert any(e["event"] == "error" for e in events), events


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all AI redaction checks passed")
