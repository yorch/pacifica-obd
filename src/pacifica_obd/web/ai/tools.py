"""The read-only AI tool registry (design §5.7).

The model may only name a tool from :data:`TOOLS`; the dispatcher validates the
name and the arguments and calls a :class:`ToolBackend`. It holds **no reference
to any write service** — there is no ``clear_dtc``, ``write_did``, ``routine``,
``security_*``, ``send_key`` or ``raw`` tool, and no code path here can reach the
confirm-token store or the ``security``/``writes`` routers. A unit test
enumerates the registry and fails if any tool is not on this allowlist.

Bus-touching tools are **cache-first**: ``source`` defaults to ``"cache"`` and a
``"live"`` read is only permitted when the operator has enabled it, the bus is
idle and the per-turn live-read cap has not been reached (enforced by the
backend/chat loop).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ToolSpec", "ToolError", "TOOLS", "TOOL_NAMES", "FORBIDDEN_NAMES",
    "ToolBackend", "dispatch", "validate_args", "summarize", "specs", "tools_json",
]

BusSource = ("cache", "live")

BUS_SOURCE = {
    "type": "string", "enum": list(BusSource), "default": "cache",
    "description": "read stored data (default) or the vehicle bus (requires the setting)",
}
BUS = {"type": "string", "enum": ["can-c", "can-ihs", "both"], "default": "can-c"}
MODULE = {"type": "string", "description": "module request id, e.g. 7E0"}
TIMEOUT = {"type": "integer", "minimum": 1, "maximum": 65535}

#: Explicitly absent. Any of these appearing in the registry is a test failure.
FORBIDDEN_NAMES = frozenset({
    "clear_dtc", "cleardtc", "write_did", "writedid", "routine", "routine_control",
    "security", "security_access", "security_unlock", "send_key", "unlock", "raw",
    "raw_command", "dtc_clear", "write", "erase", "adaptations_write", "coding",
})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    source: str = "local-db"       # "local-db" | "vehicle-bus" | "dataset" | "data-dir"
    vehicle_write: bool = False
    side_effect: str | None = None
    read_only: bool = True


def _obj(properties: dict, required=(), **extra) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False, **extra}


def _build() -> dict[str, ToolSpec]:
    specs = [
        ToolSpec("get_adapter_status", "Adapter/port/protocol/bus/voltage and connection state.",
                 _obj({}), source="local-db"),
        ToolSpec("read_vin", "Read the vehicle identification number (or <redacted>).",
                 _obj({"redact": {"type": "boolean", "default": True}}), source="vehicle-bus"),
        ToolSpec("read_generic_dtcs", "Stored, pending and permanent OBD-II DTCs.",
                 _obj({"source": BUS_SOURCE}), source="vehicle-bus"),
        ToolSpec("read_readiness", "OBD-II readiness/I-M monitors and the ready verdict.",
                 _obj({"source": BUS_SOURCE}), source="vehicle-bus"),
        ToolSpec("read_freeze_frame", "The freeze-frame snapshot behind the first stored DTC.",
                 _obj({"source": BUS_SOURCE}), source="vehicle-bus"),
        ToolSpec("read_live_pids", "Current value of named OBD-II PIDs.",
                 _obj({"pids": {"type": "array", "items": {"type": "string"},
                                "minItems": 1, "maxItems": 24}, "source": BUS_SOURCE},
                      ["pids"]), source="vehicle-bus"),
        ToolSpec("discover_modules", "Probe a CAN bus for responding UDS modules.",
                 _obj({"bus": BUS, "start": MODULE, "end": MODULE, "identify": {"type": "boolean"},
                       "source": BUS_SOURCE}), source="vehicle-bus"),
        ToolSpec("read_module_dtcs", "Read UDS 0x19 DTCs from all modules, or one module.",
                 _obj({"bus": BUS, "start": MODULE, "end": MODULE, "module": MODULE,
                       "source": BUS_SOURCE}), source="vehicle-bus"),
        ToolSpec("read_did", "Read data identifiers (0x22) from one module.",
                 _obj({"module": MODULE, "bus": BUS,
                       "dids": {"type": "array", "items": {"type": "string"},
                                "minItems": 1, "maxItems": 32}, "source": BUS_SOURCE},
                      ["module", "dids"]), source="vehicle-bus"),
        ToolSpec("get_module_identification", "The identification block F186-F191 for a module.",
                 _obj({"module": MODULE, "bus": BUS, "source": BUS_SOURCE}), source="vehicle-bus"),
        ToolSpec("get_module_map", "The bundled (and optional external) module address map.",
                 _obj({}), source="dataset"),
        ToolSpec("get_vehicle_context", "Year/make/model/powertrain and the detection basis; "
                                        "VIN only when privacy settings allow it.",
                 _obj({}), source="local-db"),
        ToolSpec("list_dataset_modules", "The bundled dataset's module rows.",
                 _obj({}), source="dataset"),
        ToolSpec("get_dataset_dids", "The bundled dataset's named data items for an ECU.",
                 _obj({"ecu": {"type": "string"}}, ["ecu"]), source="dataset"),
        ToolSpec("explain_dtc_code", "The bundled plain-language description of a DTC code.",
                 _obj({"code": {"type": "string", "pattern": "^[PBCU][0-9A-Fa-f]{4}$"}},
                      ["code"]), source="dataset"),
        ToolSpec("list_sessions", "Stored diagnostic sessions.",
                 _obj({"vin": {"type": "string"}, "limit": {"type": "integer",
                                                            "minimum": 1, "maximum": 200}}),
                 source="local-db"),
        ToolSpec("get_session", "One stored session with its modules, DTCs and readings.",
                 _obj({"session_id": {"type": "integer", "minimum": 1}}, ["session_id"]),
                 source="local-db"),
        ToolSpec("search_db", "Read-only, structured query over an allowlisted table.",
                 _obj({"table": {"type": "string", "enum": ["vehicles", "sessions", "events",
                                                            "modules", "dtcs", "readings",
                                                            "samples"]},
                       "filters": {"type": "object"}, "limit": {"type": "integer",
                                                                "minimum": 1, "maximum": 500},
                       "offset": {"type": "integer", "minimum": 0}}, ["table"]),
                 source="local-db"),
        ToolSpec("get_samples", "Stored samples (the recording buffer).",
                 _obj({"vin": {"type": "string"}, "module": MODULE,
                       "did": {"type": "string"}, "from": {"type": "string"},
                       "to": {"type": "string"},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 5000}}),
                 source="local-db"),
        ToolSpec("get_sample_series", "A downsampled, chart-ready sample series.",
                 _obj({"vin": {"type": "string"}, "module": MODULE, "did": {"type": "string"},
                       "from": {"type": "string"}, "to": {"type": "string"}},
                      ["vin", "module", "did"]), source="local-db"),
        ToolSpec("compare_sessions", "A structural diff of two stored sessions.",
                 _obj({"a": {"type": "integer", "minimum": 1}, "b": {"type": "integer",
                                                                     "minimum": 1}},
                      ["a", "b"]), source="local-db"),
        ToolSpec("build_report", "Build report artifact(s) **in the local data directory only** "
                                 "(disabled by default; never touches the vehicle).",
                 _obj({"formats": {"type": "array", "items": {"type": "string",
                                                              "enum": ["json", "xml", "html"]},
                                   "maxItems": 3}, "vin": {"type": "string"}}),
                 source="data-dir", side_effect="data-dir"),
    ]
    return {spec.name: spec for spec in specs}


TOOLS: dict[str, ToolSpec] = _build()
TOOL_NAMES = tuple(TOOLS)


def specs() -> list[ToolSpec]:
    return list(TOOLS.values())


def tools_json() -> list[dict]:
    return [spec.as_json() if hasattr(spec, "as_json") else
            {"name": spec.name, "description": spec.description,
             "parameters": spec.parameters} for spec in TOOLS.values()]


@runtime_checkable
class ToolBackend(Protocol):
    """What a dispatcher needs. Implemented by the web layer over the service."""

    def call(self, name: str, args: dict) -> Any: ...


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# validation (small, explicit; no external jsonschema dependency)
# --------------------------------------------------------------------------- #
def _validate_value(field: str, schema: dict, value) -> None:
    kind = schema.get("type")
    if kind == "string":
        if not isinstance(value, str):
            raise ToolError("bad_args", f"{field} must be a string")
        if "enum" in schema and value not in schema["enum"]:
            raise ToolError("bad_args", f"{field} must be one of {schema['enum']}")
        import re

        if "pattern" in schema and not re.match(schema["pattern"], value):
            raise ToolError("bad_args", f"{field} does not match {schema['pattern']}")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError("bad_args", f"{field} must be an integer")
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError("bad_args", f"{field} is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError("bad_args", f"{field} is above the maximum")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ToolError("bad_args", f"{field} must be a boolean")
    elif kind == "array":
        if not isinstance(value, list):
            raise ToolError("bad_args", f"{field} must be an array")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolError("bad_args", f"{field} has too many items")
        item_schema = schema.get("items") or {}
        for index, item in enumerate(value):
            _validate_value(f"{field}[{index}]", item_schema, item)
    elif kind == "object":
        if not isinstance(value, dict):
            raise ToolError("bad_args", f"{field} must be an object")


def validate_args(spec: ToolSpec, args: dict) -> dict:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("bad_args", "arguments must be an object")
    schema = spec.parameters
    props = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        unknown = set(args) - set(props)
        if unknown:
            raise ToolError("bad_args", f"unknown argument(s): {', '.join(sorted(unknown))}")
    for name in schema.get("required") or []:
        if name not in args:
            raise ToolError("bad_args", f"missing required argument: {name}")
    for name, value in args.items():
        _validate_value(name, props.get(name) or {}, value)
    return args


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
def summary_source(spec: ToolSpec) -> str:
    return "vehicle-bus" if spec.source == "vehicle-bus" else spec.source


def dispatch(name: str, args: dict, backend: ToolBackend) -> dict:
    """Validate and run one tool; return ``{untrusted, source, data, summary}``.

    Never raises for a domain failure — a tool that fails returns a small,
    fenced error object so the model can recover without the loop dying.
    """
    spec = TOOLS.get(name)
    if spec is None:
        return {"error": f"unknown tool: {name}", "untrusted": False, "source": "registry"}
    try:
        validate_args(spec, args or {})
    except ToolError as exc:
        return {"error": exc.message, "untrusted": False, "source": "registry"}
    try:
        data = backend.call(name, dict(args or {}))
    except Exception as exc:  # noqa: BLE001 - report, never crash the loop
        from pacifica_obd.web.ai.keys import redact_text

        return {"error": redact_text(str(exc))[:400], "untrusted": True,
                "source": summary_source(spec)}
    return {
        "untrusted": True,
        "source": summary_source(spec),
        "tool": name,
        "data": data,
        "summary": summarize(name, data),
    }


def summarize(name: str, data) -> str:
    """One line the tool timeline (and the model) can read cheaply."""
    if isinstance(data, dict):
        for key in ("summary", "status", "total_dtcs", "total", "count"):
            if key in data:
                return f"{name}: {key}={data[key]}"
        return f"{name}: {len(data)} field(s)"
    if isinstance(data, list):
        return f"{name}: {len(data)} item(s)"
    return f"{name}: {str(data)[:80]}"
