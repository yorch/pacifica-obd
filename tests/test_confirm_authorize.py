"""A confirmed operation is authorised at the moment of transmit, not earlier.

``consume`` burns the token when the job starts, but the job then acquires the
adapter, opens a session and reads the precondition — seconds during which the
token can expire and the adapter generation can change. An adversarial review
showed the 2E/31 and raw paths had no final gate, so a consumed token still
authorised the send after its window (the same class as the 0x27 fix).

    uv run --extra web python tests/test_confirm_authorize.py
"""
from __future__ import annotations

from pacifica_obd.web.confirm import ConfirmError, ConfirmTokenStore


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _previewed(store, *, ttl_s: float = 120.0, generation: str = "gen-1"):
    """A previewed-and-confirmed 2E write, exactly as the preview route leaves it."""
    entry = store.issue(kind="write-did", module="744", bus="can-c",
                        request="2EF19001",
                        params={"generation": generation, "did": "F190"}, ttl_s=ttl_s)
    store.consume(entry.token, kind="write-did", acknowledgement="744")
    return entry


def test_authorize_accepts_a_fresh_consumed_token():
    store = ConfirmTokenStore(clock=Clock())
    entry = _previewed(store)
    assert store.authorize(entry.token, kind="write-did", generation="gen-1").token == entry.token


def test_authorize_refuses_after_the_ttl_lapses_mid_job():
    clock = Clock()
    store = ConfirmTokenStore(clock=clock)
    entry = _previewed(store, ttl_s=120.0)
    clock.now += 121.0  # the session/setup outlived the token
    try:
        store.authorize(entry.token, kind="write-did", generation="gen-1")
    except ConfirmError as exc:
        assert exc.status == "expired", exc.status
    else:  # pragma: no cover
        raise AssertionError("an expired token authorised a transmit")


def test_authorize_refuses_a_changed_adapter_generation():
    store = ConfirmTokenStore(clock=Clock())
    entry = _previewed(store)
    try:
        store.authorize(entry.token, kind="write-did", generation="gen-2")
    except ConfirmError as exc:
        assert exc.status == "generation_mismatch", exc.status
    else:  # pragma: no cover
        raise AssertionError("a token from another connection authorised a transmit")


def test_authorize_is_single_use():
    store = ConfirmTokenStore(clock=Clock())
    entry = _previewed(store)
    assert store.authorize(entry.token, kind="write-did", generation="gen-1")
    try:
        store.authorize(entry.token, kind="write-did", generation="gen-1")
    except ConfirmError as exc:
        assert exc.status == "consumed", exc.status
    else:  # pragma: no cover
        raise AssertionError("one token authorised two transmits")


def test_authorize_refuses_a_token_that_was_never_confirmed():
    store = ConfirmTokenStore(clock=Clock())
    entry = store.issue(kind="write-did", module="744", bus="can-c", request="2EF19001",
                        params={"generation": "gen-1"})
    try:
        store.authorize(entry.token, kind="write-did", generation="gen-1")
    except ConfirmError as exc:
        assert exc.status == "unconfirmed", exc.status
    else:  # pragma: no cover
        raise AssertionError("an unconfirmed token authorised a transmit")


def test_authorize_refuses_a_token_for_another_operation():
    store = ConfirmTokenStore(clock=Clock())
    entry = _previewed(store)
    try:
        store.authorize(entry.token, kind="routine", generation="gen-1")
    except ConfirmError as exc:
        assert exc.status == "mismatch", exc.status
    else:  # pragma: no cover
        raise AssertionError("a 2E token authorised a 31 routine")


def test_authorize_refuses_an_unknown_token():
    store = ConfirmTokenStore(clock=Clock())
    try:
        store.authorize("nope", kind="write-did", generation="gen-1")
    except ConfirmError as exc:
        assert exc.status == "expired", exc.status
    else:  # pragma: no cover
        raise AssertionError("an unknown token authorised a transmit")


# --------------------------------------------------------------------------- #
# the router must actually consult the gate
# --------------------------------------------------------------------------- #
def test_the_write_route_refuses_a_token_that_lapses_during_the_job():
    """The token is valid when consumed, then lapses while the job sets up.

    This is the actual window: `consume` happens in the handler, and the job then
    acquires the adapter and opens a session before transmitting. Simulated by
    letting the clock jump immediately *after* a successful consume, so a build
    without the final gate would transmit anyway (and fail this test).
    """
    import asyncio
    import os
    import tempfile

    from httpx import ASGITransport, AsyncClient

    from pacifica_obd.web.app import create_app
    from pacifica_obd.web.settings import Settings

    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    app = create_app(Settings(demo=True, read_only=False))
    headers = {"X-Pacifica-Request": "1"}

    async def body(client):
        await client.post("/api/connect", headers=headers, json={"bus": "can-c"})
        preview = await client.post("/api/writes/write-did", headers=headers,
                                    json={"module": "744", "did": "F190", "data": "01"})
        assert preview.status_code == 200, preview.text
        payload = preview.json()
        token = payload.get("confirm_token") or payload.get("token")
        ack = payload.get("acknowledgement") or "744"

        store = app.state.confirm
        base_clock = store._clock
        original_consume = store.consume

        def consume_then_lapse(*args, **kwargs):
            entry = original_consume(*args, **kwargs)     # valid at this moment
            store._clock = lambda: base_clock() + 10_000  # the job outlives the TTL
            return entry

        store.consume = consume_then_lapse
        try:
            before = len(getattr(app.state.worker.backend, "sent", []))
            confirm = await client.post("/api/writes/write-did/confirm", headers=headers,
                                        json={"confirm_token": token,
                                              "acknowledgement": ack})
            sent = list(getattr(app.state.worker.backend, "sent", []))[before:]
        finally:
            store.consume = original_consume
            store._clock = base_clock

        assert confirm.status_code == 409, confirm.text
        assert confirm.json()["error"]["status"] == "expired", confirm.json()
        assert not any(str(c).startswith("2E") for c in sent), sent

    async def wrapper():
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://127.0.0.1:8765") as client:
            return await body(client)

    asyncio.run(wrapper())


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all confirm-authorisation checks passed")
