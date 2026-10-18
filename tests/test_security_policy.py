"""Security-access policy checks: the held-seed lease and the SendKey decision.

    uv run python tests/test_security_policy.py

These are the owner-decision rules from ``docs/web-ui.md`` §9.2: one seed, held
under a 30 s lease bound to (module, level, bus), and **no second seed request**
unless the opt-in strict mode is on.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pacifica_obd.security import (  # noqa: E402
    SecurityLeaseStore,
    key_request,
    normalize_seed,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_a_held_seed_is_sendable_once():
    clock = Clock()
    store = SecurityLeaseStore(ttl_s=30.0, clock=clock)
    lease = store.issue("744", 1, "A1B2C3D4", bus="can-c")
    assert lease.module == "744" and lease.level == 1
    called = []

    def read_seed():
        called.append(True)
        return "A1B2C3D4"

    decision = store.consume(lease.token, module="0x744", level=1, bus="can-c",
                             strict_seed_reread=False, read_seed=read_seed)
    assert decision.send and decision.status == "ok"
    assert decision.seed == "A1B2C3D4"
    assert decision.re_read is False
    # DEFAULT POLICY: the reader is supplied but must never be called.
    assert called == [], "the default path re-requested a seed"
    assert key_request(1, "deadbeef") == "2702DEADBEEF"


def test_lease_expires_after_its_ttl():
    clock = Clock()
    store = SecurityLeaseStore(ttl_s=30.0, clock=clock)
    lease = store.issue("744", 1, "A1B2C3D4", bus="can-c")
    clock.advance(29.9)
    assert store.peek(lease.token) is not None
    clock.advance(0.2)
    assert store.peek(lease.token) is None
    decision = store.consume(lease.token, module="744", level=1, bus="can-c")
    assert not decision.send and decision.status == "expired"


def test_a_second_attempt_cannot_reuse_a_consumed_lease():
    store = SecurityLeaseStore()
    lease = store.issue("744", 1, "A1B2C3D4", bus="can-c")
    first = store.consume(lease.token, module="744", level=1, bus="can-c")
    assert first.send
    second = store.consume(lease.token, module="744", level=1, bus="can-c")
    assert not second.send and second.status == "consumed"


def test_mismatched_module_level_or_bus_is_refused_and_not_burned():
    store = SecurityLeaseStore()
    lease = store.issue("744", 1, "A1B2C3D4", bus="can-c")
    for kwargs in (
        {"module": "745", "level": 1, "bus": "can-c"},
        {"module": "744", "level": 3, "bus": "can-c"},
        {"module": "744", "level": 1, "bus": "can-ihs"},
    ):
        decision = store.consume(lease.token, **kwargs)
        assert not decision.send and decision.status == "mismatch", kwargs
    # A user typo must not destroy the still-valid lease.
    good = store.consume(lease.token, module="744", level=1, bus="can-c")
    assert good.send


def test_strict_mode_re_reads_and_refuses_on_a_changed_seed():
    store = SecurityLeaseStore()
    lease = store.issue("744", 1, "A1B2C3D4", bus="can-c")
    decision = store.consume(lease.token, module="744", level=1, bus="can-c",
                             strict_seed_reread=True,
                             read_seed=lambda: "0BADF00D")
    assert not decision.send
    assert decision.status == "seed_changed"
    assert decision.re_read is True
    assert decision.seed == "0BADF00D"


def test_strict_mode_sends_when_the_seed_is_unchanged():
    store = SecurityLeaseStore()
    lease = store.issue("744", 1, "A1B2C3D4", bus="can-c")
    decision = store.consume(lease.token, module="744", level=1, bus="can-c",
                             strict_seed_reread=True,
                             read_seed=lambda: "a1b2c3d4")
    assert decision.send and decision.re_read is True


def test_strict_mode_without_a_reader_sends_nothing():
    store = SecurityLeaseStore()
    lease = store.issue("744", 1, "A1B2C3D4", bus="can-c")
    decision = store.consume(lease.token, module="744", level=1, bus="can-c",
                             strict_seed_reread=True)
    assert not decision.send and decision.status == "strict_unavailable"


def test_generation_change_invalidates_a_lease():
    store = SecurityLeaseStore()
    lease = store.issue("744", 1, "A1B2C3D4", bus="can-c", generation="adapter-1")
    decision = store.consume(lease.token, module="744", level=1, bus="can-c",
                             generation="adapter-2")
    assert not decision.send and decision.status == "generation_mismatch"
    assert store.consume(lease.token, module="744", level=1, bus="can-c",
                         generation="adapter-1").send


def test_a_newer_seed_request_supersedes_an_older_lease():
    store = SecurityLeaseStore()
    first = store.issue("744", 1, "A1B2C3D4", bus="can-c")
    second = store.issue("744", 1, "DEADBEEF", bus="can-c")
    decision = store.consume(first.token, module="744", level=1, bus="can-c")
    assert not decision.send and decision.status == "superseded"
    assert store.consume(second.token, module="744", level=1, bus="can-c").send
    # An explicit invalidation (a plain seed read) supersedes too.
    third = store.issue("744", 1, "CAFEBABE", bus="can-c")
    store.invalidate("744", 1, "can-c")
    assert store.consume(third.token, module="744", level=1,
                         bus="can-c").status == "superseded"


def test_seed_normalisation_and_invalid_levels():
    assert normalize_seed(b"\xA1\xB2") == "A1B2"
    assert normalize_seed("0xa1b2") == "A1B2"
    store = SecurityLeaseStore()
    for level in (0, 2, 0x7E):
        try:
            store.issue("744", level, "A1B2C3D4")
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted an invalid security level {level}")


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all security policy checks passed")
