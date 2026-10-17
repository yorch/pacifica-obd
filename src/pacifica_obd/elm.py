"""Minimal raw ELM327 / STN11xx transport over a serial port.

This deliberately bypasses python-OBD: it talks to the adapter at the command
level so we can issue arbitrary AT/ST commands and raw OBD/UDS requests
(e.g. ``ATSH 7E0`` + ``22F190``). It is the foundation for manufacturer-specific
(UDS) work described in docs/enhanced-diagnostics.md.
"""

from __future__ import annotations

import time

import serial


class Elm327Error(RuntimeError):
    pass


class Elm327:
    """A thin, synchronous ELM327/STN command channel.

    Responses are configured with spaces ON (``ATS1``) so the header and data
    bytes are unambiguously separable by callers.
    """

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0, init: bool = True):
        self.port = port
        self.timeout = timeout
        try:
            self.ser = serial.Serial(port, baudrate=baudrate, timeout=0.1, write_timeout=2.0)
        except serial.SerialException as exc:  # noqa: TRY003
            raise Elm327Error(f"cannot open {port}: {exc}") from exc
        if init:
            try:
                self._init()
            except Exception:
                # never leak an open port if initialization fails
                self.close()
                raise

    # -- low level ---------------------------------------------------------
    def _init(self) -> None:
        self.cmd("ATZ", wait=2.0)   # reset
        self.cmd("ATE0")            # echo off
        self.cmd("ATL0")            # linefeeds off
        self.cmd("ATS1")            # spaces ON -> "7E8 06 50 03 ..." is parseable

    def cmd(self, command: str, wait: float | None = None) -> list[str]:
        """Send one command and return its response lines (prompt stripped)."""
        command = command.strip()
        try:
            self.ser.reset_input_buffer()
            self.ser.write((command + "\r").encode("ascii"))
            self.ser.flush()
        except (serial.SerialException, OSError) as exc:
            raise Elm327Error(f"serial write failed: {exc}") from exc

        deadline = time.monotonic() + (wait if wait is not None else self.timeout)
        buf = bytearray()
        seen_prompt = False
        while time.monotonic() < deadline:
            try:
                chunk = self.ser.read(256)
            except (serial.SerialException, OSError) as exc:
                raise Elm327Error(f"serial read failed: {exc}") from exc
            if not chunk:
                continue
            buf += chunk
            if b">" in buf:
                seen_prompt = True
                break

        if not seen_prompt:
            # Timed out without a prompt. Drain whatever is still arriving so an
            # in-flight response is not mis-attributed to the next command.
            time.sleep(0.05)
            try:
                buf += self.ser.read(256)
            except Exception:  # noqa: BLE001
                pass

        text = bytes(buf).decode("ascii", errors="replace")
        text = text.split(">")[0]  # drop the prompt and anything after it

        lines: list[str] = []
        for raw in text.replace("\r", "\n").split("\n"):
            line = raw.strip().lstrip(">").strip()
            if not line:
                continue
            if line.upper() == command.upper():  # drop echo if it slipped through
                continue
            lines.append(line)
        return lines

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "Elm327":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
