"""BusWorker checks: one owner, FIFO serialization, cooperative cancellation,
stream fan-out, and the read-path safety rules of the fake adapter.

    uv run python tests/test_web_bus.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pacifica_obd.web.bus import (  # noqa: E402
    BusBusy, BusWorker, FakeAdapter, FakeBackend, StreamChannel,
)


def test_jobs_run_one_at_a_time_and_never_interleave():
    events: list[str] = []

    def make(tag: str, delay: float):
        def body(ctx):
            events.append(f"{tag}-start")
            for _ in range(int(delay / 0.005)):
                time.sleep(0.005)
            events.append(f"{tag}-end")
            return tag
        return body

    worker = BusWorker(FakeBackend())
    worker.start()
    try:
        first = worker.submit(make("A", 0.12), name="A")
        second = worker.submit(make("B", 0.02), name="B")
        third = worker.submit(make("C", 0.02), name="C")
        assert first.future.result(timeout=5) == "A"
        assert second.future.result(timeout=5) == "B"
        assert third.future.result(timeout=5) == "C"
        assert events == ["A-start", "A-end", "B-start", "B-end", "C-start", "C-end"], events
    finally:
        worker.stop()


def test_a_bus_holding_stream_refuses_other_jobs_with_bus_busy():
    worker = BusWorker(FakeBackend())
    worker.start()
    try:
        stream = worker.submit(lambda ctx: time.sleep(0.35) or "stream", name="watch",
                               holds_bus=True, exclusive=True)
        time.sleep(0.05)
        for kwargs in ({}, {"exclusive": True}):
            try:
                worker.submit(lambda ctx: "x", name="scan", **kwargs)
                raise AssertionError("a second bus operation was allowed while a stream held the bus")
            except BusBusy as exc:
                assert "watch" in str(exc)
        assert stream.future.result(timeout=5) == "stream"
        # Once the holder is gone, work flows again.
        assert worker.submit(lambda ctx: "after", name="after").future.result(timeout=5) == "after"
    finally:
        worker.stop()


def test_cancellation_stops_a_running_job_and_keeps_the_queue_moving():
    seen = {"ticks": 0, "cancelled": False}

    def long_job(ctx):
        while not ctx.cancelled():
            seen["ticks"] += 1
            time.sleep(0.01)
        seen["cancelled"] = True
        return "stopped"

    worker = BusWorker(FakeBackend())
    worker.start()
    try:
        job = worker.submit(long_job, name="long")
        queued = worker.submit(lambda ctx: "next", name="next")
        time.sleep(0.05)
        assert worker.cancel(job.id) is True
        assert job.future.result(timeout=5) == "stopped"
        assert seen["cancelled"] is True
        assert job.state == "cancelled"
        assert queued.future.result(timeout=5) == "next"
    finally:
        worker.stop()


def test_a_queued_job_cancelled_before_it_runs_never_starts():
    ran = []

    def body(ctx):
        ran.append(True)
        return "ran"

    worker = BusWorker(FakeBackend())
    worker.start()
    try:
        blocker = worker.submit(lambda ctx: time.sleep(0.2) or "blocker", name="blocker")
        queued = worker.submit(body, name="queued")
        worker.cancel(queued.id)
        blocker.future.result(timeout=5)
        time.sleep(0.05)
        assert ran == []
        assert queued.state == "cancelled"
    finally:
        worker.stop()


def test_job_errors_are_reported_and_the_worker_survives():
    from pacifica_obd.adapter import AdapterError

    def boom(ctx):
        raise AdapterError("no serial port found")

    worker = BusWorker(FakeBackend())
    worker.start()
    try:
        job = worker.submit(boom, name="boom")
        try:
            job.future.result(timeout=5)
            raise AssertionError("the failure was swallowed")
        except AdapterError:
            pass
        assert job.state == "error"
        assert job.error["kind"] == "adapter"
        assert worker.submit(lambda ctx: "ok", name="ok").future.result(timeout=5) == "ok"
    finally:
        worker.stop()


def test_status_and_progress_are_visible():
    worker = BusWorker(FakeBackend())
    worker.start()
    try:
        def body(ctx):
            ctx.progress(found=3, request_id="7E2")
            return "done"

        job = worker.submit(body, name="scan")
        assert job.future.result(timeout=5) == "done"
        assert job.progress == {"found": 3, "request_id": "7E2"}
        snapshot = job.snapshot()
        assert snapshot["state"] == "done" and snapshot["name"] == "scan"
        status = worker.status()
        assert status["busy"] is False and status["queued"] == 0
    finally:
        worker.stop()


def test_backend_switches_between_uds_and_generic_ownership():
    backend = FakeBackend()
    worker = BusWorker(backend)
    worker.start()
    try:
        def body(ctx):
            uds = ctx.uds()
            assert hasattr(uds, "cmd")
            obd = ctx.obd()
            assert hasattr(obd, "query")
            return "ok"

        assert worker.submit(body, name="switch").future.result(timeout=5) == "ok"
    finally:
        worker.stop()


def test_session_recording_uses_the_store_once():
    opened = []

    def factory():
        opened.append(True)
        return None

    worker = BusWorker(FakeBackend(), store_factory=factory)
    worker.start()
    try:
        def body(ctx):
            ctx.open_session("dtc-all", {"bus": "can-c"})
            ctx.open_session("dtc-all", {})
            return ctx.store

        assert worker.submit(body, name="session").future.result(timeout=5) is None
        assert len(opened) == 1  # the factory is consulted once, from the worker thread
    finally:
        worker.stop()


def test_stream_channel_fans_out_and_replays_a_reconnect():
    async def main():
        channel = StreamChannel()
        loop = asyncio.get_running_loop()
        first_id, first = channel.subscribe(loop)
        channel.publish("sample", {"seq": 1})
        channel.publish("sample", {"seq": 2})
        received = []
        for _ in range(2):
            _seq, event, data = await asyncio.wait_for(first.get(), 1)
            received.append((event, data["seq"]))
        assert received == [("sample", 1), ("sample", 2)], received
        channel.unsubscribe(first_id)

        # A late subscriber asking from id 1 gets only what it missed.
        _late_id, late = channel.subscribe(loop, last_event_id=1)
        _seq, event, data = await asyncio.wait_for(late.get(), 1)
        assert (event, data["seq"]) == ("sample", 2)
        channel.close("done")
        _seq, event, data = await asyncio.wait_for(late.get(), 1)
        assert event == "closed"

    asyncio.run(main())


def test_stream_channel_subscribers_are_counted():
    async def main():
        channel = StreamChannel()
        loop = asyncio.get_running_loop()
        sid, _queue = channel.subscribe(loop)
        assert channel.subscribers == 1
        channel.unsubscribe(sid)
        assert channel.subscribers == 0

    asyncio.run(main())


def test_job_table_is_bounded_without_evicting_unfinished_work():
    worker = BusWorker(FakeBackend())
    worker.start()
    try:
        for _ in range(260):
            worker.submit(lambda ctx: "quick", name="quick").future.result(timeout=10)
        tracked = worker.jobs()
        assert len(tracked) <= 200, len(tracked)
        assert tracked[-1].name == "quick"

        # A queued job is never pruned while it is still pending: it stays
        # cancellable.
        blocker = worker.submit(lambda ctx: time.sleep(0.3) or "blocker", name="blocker")
        queued = [worker.submit(lambda ctx: "queued", name=f"q{i}") for i in range(5)]
        assert all(worker.get(job.id) is not None for job in queued)
        assert worker.cancel(queued[0].id) is True
        blocker.future.result(timeout=5)
    finally:
        worker.stop()


def test_stop_closes_the_backend_from_the_worker_and_refuses_new_work():
    backend = FakeBackend()
    worker = BusWorker(backend)
    worker.start()
    started = []

    def slow(ctx):
        started.append(True)
        for _ in range(200):
            if ctx.cancelled():
                return "stopped"
            time.sleep(0.01)
        return "ran"

    worker.submit(slow, name="slow")
    deadline = time.time() + 5
    while not started and time.time() < deadline:
        time.sleep(0.01)
    worker.stop(timeout=0.05)          # gives up joining; must not close the port
    try:
        worker.submit(lambda ctx: "nope", name="after-stop")
        raise AssertionError("submit was allowed during shutdown")
    except BusBusy:
        pass
    deadline = time.time() + 5
    while worker.running and time.time() < deadline:
        time.sleep(0.05)
    assert worker.running is False
    assert backend._adapter.closed is True, "the worker must close the adapter on its way out"


def test_a_subscriber_that_falls_behind_still_gets_the_terminal_event():
    async def main():
        channel = StreamChannel()
        loop = asyncio.get_running_loop()
        _sid, queue = channel.subscribe(loop)
        for index in range(6000):        # more than the queue can hold
            channel.publish("sample", {"seq": index})
        channel.close("done")
        await asyncio.sleep(0.1)         # let the loop run the scheduled puts
        terminal = None
        drained = 0
        while not queue.empty():
            _seq, event, data = queue.get_nowait()
            drained += 1
            if event in ("closed", "done"):
                terminal = event
        assert drained < 6000, "the queue was unbounded"
        assert terminal in ("closed", "done"), "the terminal event was dropped"

    asyncio.run(main())


def test_a_subscriber_after_completion_is_closed_immediately():
    async def main():
        channel = StreamChannel()
        channel.close("done")
        _sid, queue = channel.subscribe(asyncio.get_running_loop())
        _seq, event, _data = await asyncio.wait_for(queue.get(), 1)
        assert event == "closed"

    asyncio.run(main())


def test_the_generic_pass_uses_a_temporary_protocol_selection():
    """python-OBD must be told a protocol, so it sends ATTP (temporary) and not
    ATSP (which persists the protocol to the adapter's EEPROM)."""
    from pacifica_obd.web.bus import GENERIC_PROTOCOL
    from pacifica_obd.web.deps import build_backend
    from pacifica_obd.web.settings import Settings

    assert GENERIC_PROTOCOL == "6"
    backend = build_backend(Settings())
    assert backend.protocol == "6"
    assert backend.protocol is not None


def test_fake_adapter_answers_only_configured_modules_and_never_leaks():
    adapter = FakeAdapter()
    adapter.cmd("ATSH 700")
    adapter.cmd("ATCRA 480")
    # An unmapped module must never answer, and nothing may be attributed to it.
    lines = adapter.cmd("1003", wait=0.01)
    assert not any(line.startswith("480") for line in lines), lines
    adapter.cmd("ATSH 7E0")
    adapter.cmd("ATCRA 7E8")
    lines = adapter.cmd("22F190", wait=0.01)
    assert lines and all(line.startswith("7E8") for line in lines)
    adapter.cmd("ATSH 7DF")  # broadcast: unmapped, must not raise
    lines = adapter.cmd("1003", wait=0.01)
    assert not any(line.startswith("480") for line in lines), lines


def test_start_stop_start_is_clean_and_leaves_no_stale_sentinel():
    backend = FakeBackend()
    worker = BusWorker(backend)
    worker.start()
    assert worker.submit(lambda ctx: "one", name="one").future.result(timeout=5) == "one"
    worker.stop()
    assert worker.running is False
    # Restart: the worker must consume real work, not an old stop sentinel.
    worker.start()
    try:
        assert worker.submit(lambda ctx: "two", name="two").future.result(timeout=5) == "two"
    finally:
        worker.stop()
    # A second stop with nothing running is harmless.
    worker.stop()


def test_stop_is_idempotent_and_closes_the_backend():
    backend = FakeBackend()
    worker = BusWorker(backend)
    worker.start()
    worker.stop()
    worker.stop()
    assert backend._adapter.closed is True
    assert worker.running is False


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all web bus checks passed")
