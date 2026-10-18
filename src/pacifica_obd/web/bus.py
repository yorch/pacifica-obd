"""Single-owner adapter access for the web UI.

FastAPI is async; the adapter is a blocking serial device that exactly **one**
thread may use. This module provides:

* :class:`BusWorker` — one background thread, one FIFO job queue, cooperative
  cancellation via a ``threading.Event`` per job. HTTP handlers never touch the
  adapter: they submit a job and ``await`` its future (or subscribe to its event
  stream). A long-running job (a live watch/monitor) *holds the bus*, so a second
  bus request is refused with :class:`BusBusy` → HTTP 409 ``bus_busy`` — the same
  "one owner at a time" rule the TUI enforces.
* :class:`StreamChannel` — a tiny fan-out for SSE: the worker thread publishes
  events, each browser subscriber gets an ``asyncio.Queue`` fed through the loop.
* Adapter backends: :class:`SerialBackend` (the real thing) plus
  :class:`FakeBackend` / :class:`FakeAdapter` / :class:`FakeOBD` used by the
  standalone tests and by ``--demo``.

Nothing here imports FastAPI, so it is unit-testable on its own.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from pacifica_obd.adapter import AdapterError, ElmAdapter, find_port
from pacifica_obd.elm import Elm327Error
from pacifica_obd.profiles import response_id as _profile_response_id

__all__ = [
    "BusBusy", "JobCancelled", "JobContext", "Job", "StreamChannel", "BusWorker",
    "SerialBackend", "FakeAdapter", "FakeOBD", "FakeBackend", "Backend",
    "list_serial_ports", "GENERIC_PROTOCOL",
]


class BusBusy(RuntimeError):
    """Another job owns the adapter (HTTP 409 ``bus_busy``)."""


class JobCancelled(RuntimeError):
    """The job was cancelled before it could start."""


# Bound the tracked-job table (finished jobs only) and the pending FIFO, so a
# burst of requests cannot grow memory without limit while one job runs slowly.
MAX_TRACKED_JOBS = 200
MAX_QUEUED_JOBS = 32

# Generic OBD-II on this vehicle is ISO 15765-4 CAN 11-bit/500k (= STN preset 33,
# ELM protocol 6). Naming it explicitly makes python-OBD send ATTP6 (temporary)
# rather than ATSP0 (which persists the protocol to EEPROM).
GENERIC_PROTOCOL: str | None = "6"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------- #
# SSE fan-out
# --------------------------------------------------------------------------- #
class StreamChannel:
    """A thread-safe event fan-out for one job's SSE subscribers.

    Events carry a monotonic ``seq`` so a reconnecting client can resume from
    ``Last-Event-ID``; a bounded history makes that replay cheap.
    """

    def __init__(self, history: int = 1000):
        self._lock = threading.Lock()
        self._subs: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = {}
        self._history: deque[tuple[int, str, dict]] = deque(maxlen=history)
        self._seq = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)

    def subscribe(self, loop: asyncio.AbstractEventLoop, last_event_id: int | None = None):
        """Register ``loop`` for events; returns ``(subscription_id, queue)``."""
        q: asyncio.Queue = asyncio.Queue(maxsize=4096)
        with self._lock:
            if self._closed:
                # One consistent terminal event, delivered immediately.
                q.put_nowait((self._seq, "closed", {"reason": "closed"}))
                return -1, q
            sid = id(q)
            self._subs[sid] = (loop, q)
            if last_event_id is not None:
                for seq, event, data in self._history:
                    if seq > last_event_id:
                        q.put_nowait((seq, event, data))
        return sid, q

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)

    def publish(self, event: str, data: dict) -> None:
        """Publish from the worker thread (or any thread)."""
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._history.append((seq, event, data))
            subs = list(self._subs.values())
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(self._put, q, seq, event, data)
            except RuntimeError:  # loop already closed; drop the subscriber
                pass

    @staticmethod
    def _put(q: asyncio.Queue, seq: int, event: str, data: dict) -> None:
        """Enqueue, evicting the oldest item if the subscriber has fallen behind.

        A terminal event (`done`/`closed`) must never be dropped just because a
        slow reader filled the queue: the stream would then never end.
        """
        try:
            q.put_nowait((seq, event, data))
            return
        except asyncio.QueueFull:
            pass
        try:
            q.get_nowait()  # drop the oldest sample
        except asyncio.QueueEmpty:
            pass
        try:
            q.put_nowait((seq, event, data))
        except asyncio.QueueFull:
            pass

    def close(self, reason: str = "done") -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._seq += 1
            seq = self._seq
            subs = list(self._subs.values())
            self._subs.clear()
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(self._put, q, seq, "closed", {"reason": reason})
            except RuntimeError:
                pass


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
@dataclass
class JobContext:
    """Handed to a job body: the adapter, the cancel flag, and progress helpers."""

    worker: "BusWorker"
    job: "Job"

    @property
    def cancel(self) -> threading.Event:
        return self.job.cancel

    def cancelled(self) -> bool:
        return self.job.cancel.is_set()

    def uds(self):
        """The raw ELM/UDS adapter (owns the port while called)."""
        return self.worker.backend.uds()

    def obd(self):
        """The python-OBD connection (owns the port while called)."""
        return self.worker.backend.obd()

    @property
    def store(self):
        return self.worker.store()

    def open_session(self, command: str, args: dict | None = None):
        """Start a persisted session for this job; returns its id or None."""
        store = self.worker.store()
        if store is None:
            return None
        try:
            return store.start_session(command, args or {}, getattr(self.worker.backend, "port", None))
        except Exception:  # noqa: BLE001
            return None

    def close_session(self, session_id, status: str = "ok", vin: str | None = None) -> None:
        store = self.worker.store()
        if store is None or session_id is None:
            return
        try:
            store.finish_session(session_id, status, vin)
        except Exception:  # noqa: BLE001
            pass

    def progress(self, **fields) -> None:
        self.job.progress.update(fields)
        self.job.publish("progress", {**self.job.progress, "job": self.job.id})

    def emit(self, event: str, data: dict) -> None:
        self.job.publish(event, data)

    def log(self, message: str) -> None:
        self.job.publish("log", {"message": message, "ts": _now()})


@dataclass
class Job:
    id: str
    name: str
    cancel: threading.Event
    future: "Any"
    body: Callable[["JobContext"], Any] | None = None
    holds_bus: bool = False
    state: str = "queued"          # queued|running|done|error|cancelled
    progress: dict = field(default_factory=dict)
    result: Any = None
    error: dict | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    channel: StreamChannel = field(default_factory=StreamChannel)

    def publish(self, event: str, data: dict) -> None:
        if not self.channel.closed:
            self.channel.publish(event, data)

    @property
    def duration_ms(self) -> int | None:
        if self.started is None:
            return None
        end = self.finished if self.finished is not None else time.time()
        return int((end - self.started) * 1000)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "holds_bus": self.holds_bus,
            "progress": dict(self.progress),
            "duration_ms": self.duration_ms,
            "error": self.error,
            "subscribers": self.channel.subscribers,
        }


def _describe_error(exc: BaseException) -> dict:
    if isinstance(exc, AdapterError):
        return {"code": "adapter", "kind": "adapter", "message": str(exc)}
    if isinstance(exc, Elm327Error):
        return {"code": "elm", "kind": "adapter", "message": str(exc)}
    if isinstance(exc, ValueError):
        return {"code": "domain", "kind": "domain", "message": str(exc)}
    return {"code": "internal", "kind": "internal", "message": str(exc)}


# --------------------------------------------------------------------------- #
# adapter backends
# --------------------------------------------------------------------------- #
class Backend(Protocol):
    """What a :class:`BusWorker` needs from an adapter owner."""

    def uds(self): ...
    def obd(self): ...
    def close(self) -> None: ...
    def info(self) -> dict: ...
    @property
    def attached(self) -> bool: ...


class SerialBackend:
    """The real backend: an ``ElmAdapter`` and/or a python-OBD connection.

    macOS ``/dev/cu.*`` is exclusive, so only one of the two is ever open. The
    worker thread is the only caller, so switching between them is safe.
    """

    def __init__(self, port: str | None = None, baudrate: int = 115200,
                 timeout: float = 2.0, check_voltage: bool = False,
                 protocol: str | None = GENERIC_PROTOCOL):
        self.requested_port = port
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.check_voltage = check_voltage
        self.protocol = protocol
        self._elm: ElmAdapter | None = None
        self._conn = None
        self._last_info: dict | None = None

    @property
    def attached(self) -> bool:
        return self._elm is not None or self._conn is not None

    def _resolve_port(self) -> str:
        resolved = find_port(self.requested_port) if self.requested_port is None else self.requested_port
        if not resolved:
            raise AdapterError(
                "no serial port found (is the vLinker FS plugged in? check `ls /dev/cu.usb*`)"
            )
        return resolved

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def _close_elm(self) -> None:
        if self._elm is not None:
            try:
                self._elm.close()
            except Exception:  # noqa: BLE001
                pass
            self._elm = None

    def uds(self) -> ElmAdapter:
        if self._elm is None:
            self._close_conn()
            elm = ElmAdapter(port=self.requested_port, baudrate=self.baudrate,
                             timeout=self.timeout)
            self._elm = elm
            self.port = elm.port
        return self._elm

    def obd(self):
        if self._conn is None:
            self._close_elm()
            import obd

            port = self._resolve_port()
            try:
                # An explicit protocol makes python-OBD use ATTP (temporary),
                # never ATSP (which saves the protocol to the adapter's EEPROM).
                # AGENTS.md: "select protocols with STP (ST) / ATTP (AT) ... and
                # avoid ATSP writes".
                self._conn = obd.OBD(portstr=port, baudrate=self.baudrate, fast=True,
                                     timeout=self.timeout, protocol=self.protocol,
                                     check_voltage=self.check_voltage)
            except Exception as exc:  # noqa: BLE001 - surface any driver error clearly
                self._conn = None
                raise AdapterError(f"could not open {port}: {exc}") from exc
            self.port = port
        return self._conn

    def info(self) -> dict:
        from pacifica_obd.web import service

        data: dict = {
            "port": getattr(self.port, "port", None) or self.port,
            "attached": self.attached,
            "protocol": None,
            "status": None,
            "adapter": None,
            "voltage": None,
            "vin": None,
        }
        if self._conn is not None:
            try:
                data.update(service.obd_info(self._conn))
            except Exception as exc:  # noqa: BLE001
                data["status"] = f"error: {exc}"
            data["attached"] = True
        elif self._elm is not None:
            try:
                lines = self._elm.cmd("ATI")
                data["adapter"] = lines[0].strip() if lines else None
                data["port"] = self._elm.port
                data["status"] = "connected"
            except Exception as exc:  # noqa: BLE001
                data["status"] = f"error: {exc}"
            data["attached"] = True
        self._last_info = data
        return data

    def cached_info(self) -> dict:
        """Last known adapter info — never touches the adapter."""
        if self._last_info is not None:
            return dict(self._last_info)
        return {"port": getattr(self.port, "port", None) or self.port,
                "attached": self.attached}

    def close(self) -> None:
        self._close_elm()
        self._close_conn()


def list_serial_ports() -> list[str]:
    """Enumerate candidate serial devices *without opening them*.

    ``obd.scan_serial()`` probes every device with ``serial.Serial(...)``, which
    would reopen the adapter this app is already using. ``list_ports.comports()``
    is purely passive, exactly like the TUI's port list.
    """
    try:
        from serial.tools import list_ports

        devices = [info.device for info in list_ports.comports() if info.device]
    except Exception:  # noqa: BLE001 - enumeration is best effort
        return []
    names = set(devices)
    for device in list(names):  # prefer callout (cu.*) names over dial-in (tty.*)
        if "/dev/tty." in device:
            callout = device.replace("/dev/tty.", "/dev/cu.", 1)
            if callout in names:
                names.discard(device)

    def rank(device: str) -> tuple[int, str]:
        low = device.lower()
        preferred = any(key in low for key in ("usbserial", "vlinker", "ftdi", "usbmodem"))
        return (0 if preferred else 1, device)

    return sorted(names, key=rank)


# --------------------------------------------------------------------------- #
# the worker
# --------------------------------------------------------------------------- #
class BusWorker:
    """One thread, one job at a time, cooperative cancellation.

    ``submit`` returns a :class:`Job` immediately; the caller awaits
    ``job.future``. Jobs run FIFO. A job that ``holds_bus`` (a live stream) makes
    every later bus request fail with :class:`BusBusy` instead of queueing behind
    an indefinite stream, which is what a user expects from "Stop" + "Scan".
    """

    def __init__(self, backend: Backend, *, bus: str = "can-c",
                 store_factory=None):
        self.backend = backend
        self.bus = bus
        self.store_factory = store_factory
        self._store = None
        self._store_ready = False
        self._queue: "queue.Queue[Job | None]" = queue.Queue()
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._active: Job | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Start (or restart) the worker.

        Restart is only allowed once the previous thread has actually finished;
        a dead thread is reaped here and any stale stop sentinel is discarded so
        a fresh worker cannot consume it and exit immediately.
        """
        with self._lock:
            if self._thread is not None:
                if self._thread.is_alive():
                    return
                self._thread.join(timeout=0)
                self._thread = None
            self._drain_sentinels()
            self._stopping.clear()
            self._thread = threading.Thread(target=self._run, name="pacifica-bus", daemon=True)
            self._thread.start()

    def _drain_sentinels(self) -> None:
        """Drop stop sentinels left over from an earlier stop()."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                self._queue.put(item)  # preserve real work; sentinels are always last
                return

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the worker to finish. The *worker* closes the adapter when it does."""
        already_stopping = self._stopping.is_set()
        self._stopping.set()
        self.cancel_all()
        if not already_stopping:
            self._queue.put(None)  # exactly one sentinel per shutdown
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if not thread.is_alive():
                with self._lock:
                    if self._thread is thread:
                        self._thread = None
                self._drain_sentinels()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- jobs --------------------------------------------------------------
    def submit(self, fn: Callable[[JobContext], Any], *, name: str = "job",
               exclusive: bool = False, holds_bus: bool = False,
               job_id: str | None = None) -> Job:
        import concurrent.futures

        with self._lock:
            if self._stopping.is_set():
                raise BusBusy("the bus worker is shutting down")
            if self._active is not None and self._active.holds_bus:
                raise BusBusy(
                    f"the adapter is busy with '{self._active.name}'; stop it first"
                )
            if exclusive and (self._active is not None or not self._queue.empty()):
                raise BusBusy("another bus operation is queued or running")
            if not exclusive and self._queue.qsize() >= MAX_QUEUED_JOBS:
                raise BusBusy(f"too many queued bus operations ({MAX_QUEUED_JOBS})")
            job = Job(
                id=job_id or uuid.uuid4().hex[:12],
                name=name,
                cancel=threading.Event(),
                future=concurrent.futures.Future(),
                body=fn,
                holds_bus=holds_bus,
            )
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune()
            self._queue.put(job)
        return job

    def _prune(self) -> None:
        """Bound the job table with *finished* jobs only.

        Evicting a queued/running job would remove the only handle a client has
        to cancel it (and ``cancel_all`` could no longer find it), so unfinished
        work is always retained.
        """
        if len(self._order) <= MAX_TRACKED_JOBS:
            return
        finished = [
            jid for jid in self._order
            if (job := self._jobs.get(jid)) is not None
            and job.state in ("done", "error", "cancelled")
        ]
        for jid in finished[:len(self._order) - MAX_TRACKED_JOBS]:
            self._jobs.pop(jid, None)
            self._order.remove(jid)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def store(self):
        """The writable store for recording (opened once, on the worker thread)."""
        if not self._store_ready:
            self._store_ready = True
            try:
                self._store = self.store_factory() if self.store_factory else None
            except Exception:  # noqa: BLE001 - persistence never breaks a job
                self._store = None
        return self._store

    @property
    def active(self) -> Job | None:
        return self._active

    @property
    def busy(self) -> bool:
        return self._active is not None

    def jobs(self) -> list[Job]:
        with self._lock:
            return [self._jobs[jid] for jid in self._order if jid in self._jobs]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        job.cancel.set()
        if job.state == "queued":
            job.state = "cancelled"
            job.channel.close("cancelled")
            if not job.future.done():
                job.future.set_exception(JobCancelled("cancelled before start"))
        return True

    def cancel_active(self) -> bool:
        if self._active is None:
            return False
        self._active.cancel.set()
        return True

    def cancel_all(self) -> None:
        for job in self.jobs():
            if job.state in ("queued", "running"):
                self.cancel(job.id)

    def status(self) -> dict:
        active = self._active
        with self._lock:
            queued = self._queue.qsize()
        return {
            "busy": active is not None,
            "holder": active.name if active is not None else None,
            "holder_id": active.id if active is not None else None,
            "queued": queued,
            "bus": self.bus,
            "attached": bool(getattr(self.backend, "attached", False)),
        }

    # -- thread ------------------------------------------------------------
    def _run(self) -> None:
        try:
            while not self._stopping.is_set():
                job = self._queue.get()
                if job is None:
                    break
                if job.cancel.is_set():
                    job.state = "cancelled"
                    job.channel.close("cancelled")
                    if not job.future.done():
                        job.future.set_exception(JobCancelled("cancelled before start"))
                    continue
                with self._lock:
                    self._active = job
                job.state = "running"
                job.started = time.time()
                job.publish("status", {"state": "running", "job": job.id, "name": job.name})
                try:
                    result = self._invoke(job)
                    job.result = result
                    job.state = "cancelled" if job.cancel.is_set() else "done"
                    if not job.future.done():
                        job.future.set_result(result)
                except JobCancelled:
                    job.state = "cancelled"
                    if not job.future.done():
                        job.future.set_exception(JobCancelled("cancelled"))
                except BaseException as exc:  # noqa: BLE001 - report, never kill the thread
                    job.state = "cancelled" if job.cancel.is_set() else "error"
                    job.error = _describe_error(exc)
                    job.publish("error", job.error)
                    if not job.future.done():
                        job.future.set_exception(exc)
                finally:
                    job.finished = time.time()
                    job.publish("status", {"state": job.state, "job": job.id,
                                           "duration_ms": job.duration_ms})
                    if job.state != "cancelled":
                        job.publish("done", {"state": job.state})
                    job.channel.close(job.state)
                    with self._lock:
                        self._active = None
        finally:
            # The worker owns the port for its whole life, so it is also the only
            # place the adapter is closed — even if stop() gave up waiting.
            with self._lock:
                self._active = None
            try:
                self.backend.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass

    def _invoke(self, job: Job) -> Any:
        if job.body is None:
            raise JobCancelled("job has no body")
        return job.body(JobContext(self, job))


# --------------------------------------------------------------------------- #
# fakes (tests + --demo)
# --------------------------------------------------------------------------- #
class _FakeQuantity:
    """A pint-Quantity lookalike so ``to_jsonable`` produces ``{value, unit}``."""

    def __init__(self, magnitude: float, units: str):
        self.magnitude = magnitude
        self.units = units

    def __str__(self) -> str:
        return f"{self.magnitude:g} {self.units}"

    def __repr__(self) -> str:
        return str(self)


class _FakeResp:
    def __init__(self, value=None):
        self.value = value

    def is_null(self) -> bool:
        return self.value is None


class _FakeCommand:
    def __init__(self, name: str, command: str, desc: str = ""):
        self.name = name
        self.command = command.encode("ascii")
        self.desc = desc


def _isotp_lines(response_id: str, payload_hex: str) -> list[str]:
    """Encode a UDS payload as spaced ELM lines, multi-frame when > 7 bytes."""
    payload = bytes.fromhex(payload_hex)
    if len(payload) <= 7:
        frame = ("%02X" % len(payload)) + payload.hex().upper()
        return [f"{response_id} " + " ".join(frame[i:i + 2] for i in range(0, len(frame), 2))]
    length = len(payload)
    first = bytes([0x10 | ((length >> 8) & 0x0F), length & 0xFF]) + payload[:6]
    lines = [f"{response_id} " + " ".join("%02X" % b for b in first)]
    rest = payload[6:]
    seq = 1
    for i in range(0, len(rest), 7):
        chunk = bytes([0x20 | (seq & 0x0F)]) + rest[i:i + 7]
        lines.append(f"{response_id} " + " ".join("%02X" % b for b in chunk))
        seq += 1
    return lines


# A deliberately fake VIN (never a real vehicle). AGENTS.md: no real VINs.
FAKE_VIN = "2C4RC1BG7MR000001"


class FakeAdapter:
    """Scripted ELM/UDS responder for tests and demo mode.

    Answers AT configuration with ``OK``, replays synthetic frames for a fixed
    set of modules, and records every command it receives in ``self.commands``
    so a test can assert addressing/flow-control behaviour.
    """

    def __init__(self, *, modules: dict[str, int] | None = None,
                 vin: str = FAKE_VIN,
                 dtcs: dict[str, str] | None = None,
                 dids: dict[str, dict[int, str]] | None = None,
                 refuse: set[str] | None = None,
                 refuse_filter: bool = False,
                 adapter_id: str = "STN1170 v4.3.2"):
        # modules: request id -> response id
        self.modules = modules or {"7E0": 0x7E8, "7E1": 0x7E9, "7E2": 0x7EA, "7E7": 0x7EF}
        self.vin = vin
        # dtcs: request id -> "5902FF" payload (without header), or a negative hex
        self.dtcs = dtcs or {
            "7E0": "5902FF03000008",   # P0300 (confirmed) on the PCM
            "7E1": "5902FF",           # TCM answered: no codes
            "7E2": "7F1911",           # HCP refused the service
            "7E7": "5902FF",
        }
        self.dids = dids or {}
        self.refuse = refuse or set()
        self.refuse_filter = refuse_filter
        self.adapter_id = adapter_id
        self.commands: list[str] = []
        self.header: str | None = None
        self.filter: str | None = None
        self.flow_control = False
        self.closed = False

    # -- helpers -----------------------------------------------------------
    def _response_id(self, header: str) -> str | None:
        if header in self.modules:
            return "%03X" % self.modules[header]
        try:
            return "%03X" % _profile_response_id(int(header, 16))
        except (ValueError, TypeError):
            return None  # unmapped (e.g. broadcast 7DF): no responder to speak for

    def _probe_response(self, header: str, command: str) -> list[str] | None:
        if header not in self.modules:
            return ["NO DATA"]  # only the configured ECUs answer
        response = self._response_id(header)
        if response is None or header in self.refuse:
            return ["NO DATA"]
        if command == "1003":
            return _isotp_lines(response, "5003003201F4")
        if command == "22F100":
            return _isotp_lines(response, "62F10000C6")
        if command == "22F190":
            return _isotp_lines(response, "62F190" + self.vin.encode("ascii").hex().upper())
        if command.startswith("22F1"):
            did = command[2:6]
            name = {"F186": "5003", "F187": "PART1234", "F188": "SW0001",
                    "F189": "1.0.2", "F18A": "SUP00C6", "F18C": "SER00042",
                    "F190": self.vin, "F191": "HW0007", "F100": "00C6"}.get(did)
            if name is None:
                return _isotp_lines(response, "7F2231")
            return _isotp_lines(response, "62" + did + name.encode("ascii").hex().upper())
        if command == "1902FF":
            payload = self.dtcs.get(header)
            if payload is None:
                return ["NO DATA"]
            if payload.startswith("7F"):
                return _isotp_lines(response, payload)
            # records are 4 bytes each after the availability mask
            return _isotp_lines(response, payload)
        return None

    def _custom_did(self, header: str, command: str) -> list[str] | None:
        if header not in self.modules:
            return None
        table = self.dids.get(header)
        if not table:
            return None
        if command.startswith("22") and len(command) >= 6:
            did = int(command[2:6], 16)
            if did in table:
                response = self._response_id(header)
                if response is None:
                    return None
                return _isotp_lines(response, table[did])
        return None

    # -- transport ---------------------------------------------------------
    def cmd(self, command: str, wait: float | None = None) -> list[str]:
        command = command.strip().upper()
        self.commands.append(command)
        if command.startswith("ATSH"):
            self.header = command.split()[-1][-3:]
            return ["OK"]
        if command.startswith("ATCRA"):
            if self.refuse_filter:
                return ["?"]  # this adapter rejects the filter: caller must cope
            parts = command.split()
            self.filter = parts[-1][-3:] if len(parts) > 1 else None
            return ["OK"]
        if command == "ATAR":
            self.filter = None
            return ["OK"]
        if command.startswith("ATFC") or command.startswith("ATCFC"):
            self.flow_control = True
            return ["OK"]
        if command == "ATI":
            return [self.adapter_id]
        if command in ("NO DATA", "CAN ERROR"):
            return [command]
        if command.startswith("AT") or command.startswith("ST"):
            return ["OK"]
        if self.header is None:
            return ["NO DATA"]
        lines = self._probe_response(self.header, command)
        if lines is None:
            lines = self._custom_did(self.header, command)
        if lines is None:
            lines = ["NO DATA"]
        if self.filter is not None:
            expected = self._response_id(self.header)
            if expected is not None:
                lines = [line for line in lines if line.startswith(expected)]
        return lines

    def close(self) -> None:
        self.closed = True


class FakeOBD:
    """python-OBD lookalike for tests and demo mode."""

    def __init__(self, *, vin: str = FAKE_VIN):
        self.vin = vin
        self.closed = False
        self._connected = True
        self._tick = 0
        self.supported_commands = [
            _FakeCommand("RPM", "010C", "Engine RPM"),
            _FakeCommand("SPEED", "010D", "Vehicle Speed"),
            _FakeCommand("COOLANT_TEMP", "0105", "Engine Coolant Temperature"),
            _FakeCommand("ENGINE_LOAD", "0104", "Calculated Engine Load"),
            _FakeCommand("THROTTLE_POS", "0111", "Throttle Position"),
            _FakeCommand("INTAKE_TEMP", "010F", "Intake Air Temperature"),
            _FakeCommand("MAF", "0110", "Mass Air Flow"),
            _FakeCommand("FUEL_LEVEL", "012F", "Fuel Level"),
            _FakeCommand("CONTROL_MODULE_VOLTAGE", "0142", "Control Module Voltage"),
            _FakeCommand("ELM_VOLTAGE", "ATRV", "Adapter Voltage"),
            # python-OBD seeds supported_commands with every base command, so a
            # mode-04 write is in there; the fake mirrors that so a regression is
            # caught rather than hidden.
            _FakeCommand("CLEAR_DTC", "04", "Clear DTCs and freeze frame (WRITE)"),
        ]
        self.queried: list[str] = []
        self.interface = _FakeInterface()

    def port_name(self) -> str:
        return "/dev/cu.fake"

    def protocol_name(self) -> str:
        return "6"

    def status(self):
        return "Car connected" if self._connected else "Not connected"

    def is_connected(self) -> bool:
        return self._connected

    def query(self, command):
        name = getattr(command, "name", str(command))
        self.queried.append(name)
        # A slow waveform so the demo's live chart is not a flat line.
        import math

        self._tick += 1
        phase = self._tick / 12.0
        table = {
            "RPM": _FakeQuantity(round(820 + 240 * math.sin(phase), 1), "rpm"),
            "SPEED": _FakeQuantity(round(max(0.0, 45 + 45 * math.sin(phase / 2)), 1), "km/h"),
            "COOLANT_TEMP": _FakeQuantity(round(88 + 5 * math.sin(phase / 6), 1), "degC"),
            "ENGINE_LOAD": _FakeQuantity(round(34 + 12 * math.sin(phase * 0.8), 1), "percent"),
            "THROTTLE_POS": _FakeQuantity(round(15 + 6 * math.sin(phase * 0.8), 1), "percent"),
            "INTAKE_TEMP": _FakeQuantity(round(25 + 2 * math.sin(phase / 8), 1), "degC"),
            "MAF": _FakeQuantity(round(3.4 + 0.9 * math.sin(phase), 2), "g/s"),
            "FUEL_LEVEL": _FakeQuantity(62.0, "percent"),
            "CONTROL_MODULE_VOLTAGE": _FakeQuantity(round(14.1 + 0.12 * math.sin(phase / 2), 2), "V"),
            "ELM_VOLTAGE": _FakeQuantity(12.4, "V"),
            "ELM_VERSION": "STN1170 v4.3.2",
            "VIN": self.vin,
            "GET_DTC": [("P0300", "Random/Multiple Cylinder Misfire Detected")],
            "GET_CURRENT_DTC": [],
            "GET_PERMANENT_DTC": [],
            "STATUS": _FakeStatus(),
            "FREEZE_DTC": None,
        }
        return _FakeResp(table.get(name))

    def close(self) -> None:
        self.closed = True


class _FakeInterface:
    """Only what ``service.obd_freeze`` needs (mode-02 raw requests)."""

    def send_and_parse(self, request: str):
        return []


class _FakeStatus:
    """Shaped like python-OBD's ``Status``: scalar fields + monitor objects."""

    def __init__(self):
        self.ignition_type = "spark"
        self.MIL = False
        self.DTC_count = 1
        self.misfire = _FakeMonitor(True, True)
        self.components = _FakeMonitor(True, False)
        self.fuel_system = _FakeMonitor(False, False)


class _FakeMonitor:
    def __init__(self, available: bool, complete: bool):
        self.available = available
        self.complete = complete


class FakeBackend:
    """A backend made of the fakes above; usable for tests and ``--demo``."""

    def __init__(self, adapter: FakeAdapter | None = None, conn: FakeOBD | None = None):
        self._adapter = adapter or FakeAdapter()
        self._conn = conn or FakeOBD(vin=self._adapter.vin)
        self._info = {
            "port": "/dev/cu.fake",
            "status": "Car connected",
            "protocol": "6",
            "adapter": self._adapter.adapter_id,
            "voltage": {"value": 12.4, "unit": "V"},
            "vin": self._adapter.vin,
        }

    @property
    def attached(self) -> bool:
        return True

    def uds(self):
        return self._adapter

    def obd(self):
        return self._conn

    def info(self) -> dict:
        return dict(self._info)

    cached_info = info

    def close(self) -> None:
        self._adapter.close()
        self._conn.close()
