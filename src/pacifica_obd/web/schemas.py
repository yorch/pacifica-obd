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


# -- guarded surfaces ------------------------------------------------------ #
class SecurityTargetRequest(BaseModel):
    module: str | int
    level: int = 1
    device_id: str | int | None = None
    variant_id: str | int | None = None
    supplier: str | None = None


class SecuritySeedRequest(BaseModel):
    module: str | int
    bus: Literal["can-c", "can-ihs"] = "can-c"
    level: int = 1
    timeout_ms: int = 100


class SecurityPreviewRequest(SecuritySeedRequest):
    device_id: str | int | None = None
    variant_id: str | int | None = None
    supplier: str | None = None


class SecurityUnlockRequest(BaseModel):
    confirm_token: str = Field(min_length=8, max_length=256)
    acknowledgement: str = Field(min_length=1, max_length=32)
    timeout_ms: int = 100


class WriteDidPreviewRequest(BaseModel):
    module: str | int
    bus: Literal["can-c", "can-ihs"] = "can-c"
    did: str | int
    data: str
    timeout_ms: int = 100


class RoutinePreviewRequest(BaseModel):
    module: str | int
    bus: Literal["can-c", "can-ihs"] = "can-c"
    rid: str | int
    sub: int = 1
    data: str = ""
    timeout_ms: int = 100


class WriteConfirmRequest(BaseModel):
    """Only the token and the typed acknowledgement; never raw parameters."""

    confirm_token: str = Field(min_length=8, max_length=256)
    acknowledgement: str = Field(min_length=1, max_length=32)


# -- AI -------------------------------------------------------------------- #
class AIConfigPatch(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    temperature: float | None = None
    send_bus_data_to_remote: bool | None = None
    redact_vin: bool | None = None
    allow_live_bus: bool | None = None
    persist_conversations: bool | None = None
    force_local_only: bool | None = None
    max_tokens_per_conversation: int | None = None
    max_requests_per_minute: int | None = None
    max_output_tokens_per_request: int | None = None
    max_live_reads_per_turn: int | None = None


class AIKeyRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    key: str = Field(min_length=1, max_length=4096)


class AIChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=16000)
    conversation_id: str | None = None
    tools: bool = False
    task: str | None = None
    context: dict | None = None


class AITaskRequest(BaseModel):
    message: str = Field(default="", max_length=16000)
    conversation_id: str | None = None
    tools: bool = False
    session_id: int | None = None
    a: int | None = None
    b: int | None = None

