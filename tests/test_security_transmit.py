"""The key is authorised at the moment of transmit, not seconds earlier.

Codex (adversarial review) showed a window: ``consume`` returns a positive
decision, the caller then configures the bus and opens a diagnostic session, and
during those seconds the lease can expire or be superseded — yet the key still
went out. ``SecurityLeaseStore.authorize`` closes that window, and
``send_security_key_guarded`` calls it immediately before the write. These tests
pin both halves.

    uv run python tests/test_security_transmit.py
"""
from __future__ import annotations

from unittest import mock

from pacifica_obd import security as policy
from pacifica_obd.web import service

MODULE, LEVEL, BUS, GEN = "7E0", 1, "can-c", "gen-1"


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _consumed(store, *, ttl_s: float = 30.0):
    """Issue a seed and consume its lease, as the confirm step does."""
    lease = store.issue(MODULE, LEVEL, "A1B2C3D4", bus=BUS, generation=GEN,
                        key_hex="E0E2DC58", ttl_s=ttl_s)
    decision = store.consume(lease.token, module=MODULE, level=LEVEL, bus=BUS,
                             generation=GEN)
    assert decision.send, decision
    return lease


def test_authorize_accepts_a_freshly_consumed_lease():
    store = policy.SecurityLeaseStore(clock=Clock())
    lease = _consumed(store)
    decision = store.authorize(lease.token, module=MODULE, level=LEVEL, bus=BUS,
                               generation=GEN)
    assert decision.send is True, decision
    assert decision.status == "ok"


def test_authorize_refuses_after_the_ttl_elapses_between_consume_and_transmit():
    clock = Clock()
    store = policy.SecurityLeaseStore(clock=clock)
    lease = _consumed(store, ttl_s=30.0)
    clock.now += 31.0  # opening the session outlived the lease
    decision = store.authorize(lease.token, module=MODULE, level=LEVEL, bus=BUS,
                               generation=GEN)
    assert decision.send is False
    assert decision.status == "expired", decision


def test_authorize_refuses_a_lease_superseded_by_a_newer_seed_request():
    store = policy.SecurityLeaseStore(clock=Clock())
    lease = _consumed(store)
    store.invalidate(MODULE, LEVEL, BUS)  # someone asked for a new seed
    decision = store.authorize(lease.token, module=MODULE, level=LEVEL, bus=BUS,
                               generation=GEN)
    assert decision.send is False
    assert decision.status == "superseded", decision


def test_authorize_refuses_a_changed_adapter_generation():
    store = policy.SecurityLeaseStore(clock=Clock())
    lease = _consumed(store)
    decision = store.authorize(lease.token, module=MODULE, level=LEVEL, bus=BUS,
                               generation="gen-2")
    assert decision.send is False
    assert decision.status == "generation_mismatch", decision


def test_authorize_refuses_a_different_target():
    store = policy.SecurityLeaseStore(clock=Clock())
    lease = _consumed(store)
    decision = store.authorize(lease.token, module="744", level=LEVEL, bus=BUS,
                               generation=GEN)
    assert decision.send is False
    assert decision.status == "mismatch", decision


def test_authorize_refuses_a_lease_that_was_never_confirmed():
    store = policy.SecurityLeaseStore(clock=Clock())
    lease = store.issue(MODULE, LEVEL, "A1B2C3D4", bus=BUS, generation=GEN)
    decision = store.authorize(lease.token, module=MODULE, level=LEVEL, bus=BUS,
                               generation=GEN)
    assert decision.send is False
    assert decision.status == "not_consumed", decision


def test_authorize_is_single_use():
    store = policy.SecurityLeaseStore(clock=Clock())
    lease = _consumed(store)
    assert store.authorize(lease.token, module=MODULE, level=LEVEL, bus=BUS,
                           generation=GEN).send is True
    again = store.authorize(lease.token, module=MODULE, level=LEVEL, bus=BUS,
                            generation=GEN)
    assert again.send is False
    assert again.status == "consumed", again


def test_guard_calls_authorize_before_it_writes_anything():
    """The refusal must happen with the session already open but before the write."""
    order: list[str] = []

    def refusing():
        order.append("authorize")
        return policy.SendKeyDecision(False, "expired", "the held seed expired")

    with mock.patch.object(service, "require_extended_session",
                           return_value=("7E0", "7E8")), \
            mock.patch.object(service, "uds_send_security_key") as transmit, \
            mock.patch.object(service, "_ensure_live"):
        try:
            service.send_security_key_guarded(
                object(), bus=BUS, module=MODULE, level=LEVEL, key_hex="E0E2DC58",
                authorize=refusing)
        except policy.SendRefused as exc:
            order.append("refused")
            assert "expired" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("a refused authorisation must not transmit")
    assert order == ["authorize", "refused"]
    transmit.assert_not_called()


def test_guard_transmits_only_after_a_positive_authorisation():
    order: list[str] = []

    def allowing():
        order.append("authorize")
        return policy.SendKeyDecision(True, "ok", "authorised at transmit",
                                      seed="A1B2C3D4")

    def fake_transmit(adapter, level, key_hex, **kwargs):
        order.append("transmit")
        return {"status": "ok", "unlocked": True}

    with mock.patch.object(service, "require_extended_session",
                           return_value=("7E0", "7E8")), \
            mock.patch.object(service, "uds_send_security_key",
                              side_effect=fake_transmit), \
            mock.patch.object(service, "_ensure_live"):
        result = service.send_security_key_guarded(
            object(), bus=BUS, module=MODULE, level=LEVEL, key_hex="E0E2DC58",
            authorize=allowing)
    assert order == ["authorize", "transmit"]
    assert result["sent"] is True
    assert result["request"] == "2702E0E2DC58"


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all pre-transmit authorisation checks passed")
