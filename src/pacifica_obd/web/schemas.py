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


# -- HV battery screen (design §3.6) --------------------------------------- #
class BatteryPreset(BaseModel):
    """A DID map for one pack. Domain rules (DID hex, count, scale) are enforced
    by ``routes/battery.py`` so the web and any future caller reject the same
    strings with the same messages. ``cells_did: None`` clears the cell map."""

    cells_did: str | None = None
    first_cell_index: int = 1
    count: int = 0
    scale: float = 1.0
    pack_voltage_did: str | None = None
    pack_voltage_scale: float = 0.1
    soc_did: str | None = None
    soc_scale: float = 0.5
    temp_did: str | None = None
    temp_scale: float = 1.0
    current_did: str | None = None
    current_scale: float = 0.1
    module: str = "7E7"
    soc_module: str = "7E2"
    source: str | None = None


class BatteryReadRequest(BaseModel):
    bus: Literal["can-c", "can-ihs"] = "can-c"
    timeout_ms: int = 100
    preset: BatteryPreset | None = None
    store: bool = False


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


# -- raw console (design §3.14) -------------------------------------------- #
class RawCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=256)
    timeout_ms: int = 100


class RawConfirmRequest(BaseModel):
    """Only the token and the typed acknowledgement (the exact command bytes)."""

    confirm_token: str = Field(min_length=8, max_length=256)
    acknowledgement: str = Field(min_length=1, max_length=256)


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


class AITestRequest(BaseModel):
    """Test a configuration without starting a chat.

    The three optional fields are transient overrides (so the form can test an
    unsaved provider/model/base URL). ``probe=False`` validates the settings
    only and never opens a socket.
    """

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    probe: bool = True


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

