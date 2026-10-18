"""Pydantic request models.

Deliberately thin: field *types* are validated here (and a validation failure is
mapped to the 400 ``validation`` envelope), while the domain rules — CAN IDs,
DIDs, hex payloads, timeouts — are validated by the shared service layer so the
web and the CLI reject exactly the same strings with the same messages.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ConnectRequest(BaseModel):
    port: str | None = None
    baud: int = 115200
    timeout_s: float = 2.0
    check_voltage: bool = False


class BusRequest(BaseModel):
    bus: Literal["can-c", "can-ihs", "both"]


class DiscoveryRequest(BaseModel):
    bus: Literal["can-c", "can-ihs"] = "can-c"
    start: str | int = "700"
    end: str | int = "7FF"
    timeout_ms: int = 100
    identify: bool = False


class DtcAllRequest(BaseModel):
    bus: Literal["can-c", "can-ihs", "both"] = "can-c"
    start: str | int = "700"
    end: str | int = "7FF"
    module: str | int | None = None
    timeout_ms: int = 100


class ReadDidRequest(BaseModel):
    module: str | int
    bus: Literal["can-c", "can-ihs"] = "can-c"
    dids: list[str | int] = Field(default_factory=list)
    timeout_ms: int = 100


class ModuleInfoRequest(BaseModel):
    module: str | int | None = None
    bus: Literal["can-c", "can-ihs", "both"] = "can-c"
    start: str | int = "700"
    end: str | int = "7FF"
    timeout_ms: int = 100


class CaptureRequest(BaseModel):
    bus: Literal["can-c", "can-ihs", "both"] = "both"
    start: str | int = "700"
    end: str | int = "7FF"
    timeout_ms: int = 100
    vin: str | None = None


class WatchStartRequest(BaseModel):
    source: Literal["pid", "did"] = "pid"
    values: list[str] = Field(default_factory=list)
    module: str | int | None = None
    bus: Literal["can-c", "can-ihs"] = "can-c"
    start: str | int = "700"
    end: str | int = "7FF"
    timeout_ms: int = 100
    interval_ms: int = 1000
    store: bool = False


class MonitorStartRequest(BaseModel):
    module: str | int | None = None
    bus: Literal["can-c", "can-ihs"] = "can-c"
    start: str | int = "700"
    end: str | int = "7FF"
    dids: list[str | int] = Field(default_factory=list)
    timeout_ms: int = 100
    interval_ms: int = 1000
    store: bool = False


class StopRequest(BaseModel):
    stream_id: str


class ReportDumpRequest(BaseModel):
    bus: Literal["can-c", "can-ihs", "both"] = "both"
    start: str | int = "700"
    end: str | int = "7FF"
    timeout_ms: int = 100
    formats: list[str] = Field(default_factory=lambda: ["json", "xml", "html"])
    generic: bool = True
    uds: bool = True
    vin: str | None = None


class ReportFromSessionRequest(BaseModel):
    session_id: int
    formats: list[str] = Field(default_factory=lambda: ["json", "html"])


class CompareRequest(BaseModel):
    a: int
    b: int
