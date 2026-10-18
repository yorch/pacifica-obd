"""AI configuration: provider, model, privacy toggles and cost caps (design §5.5).

Owner decision §9.3 governs the defaults:

* **no provider configured** out of the box (with a one-click local shortcut);
* "send bus data to a remote model" **off**;
* VIN redaction **on**;
* conversation persistence **off**;
* reads are **cache-first**; live bus reads need an explicit setting.

The cost table is a small, dated estimate. It is never presented as a bill.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, replace

__all__ = ["AIConfig", "estimate_cost", "COST_TABLE_DATE", "PROVIDER_LABELS", "LOCAL_BASE_HOSTS"]

#: Everything the shipped UI shows about providers stays vendor-neutral.
PROVIDER_LABELS = {
    "openai_compat": "Compatible chat API (hosted or local)",
    "anthropic": "Native messages API",
    "fake": "Deterministic offline provider (testing)",
}

#: Default endpoints for the two shapes. Deliberately example.invalid so nothing
#: is ever contacted by accident; the operator sets a real base URL.
DEFAULT_BASE_URLS = {
    "openai_compat": "https://api.example.invalid/v1",
    "anthropic": "https://api.example.invalid",
}

#: base_url hosts treated as local (no bus data leaves the machine).
LOCAL_BASE_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"})

COST_TABLE_DATE = "2026-01"

#: model substring -> (USD per 1M input, USD per 1M output). Unknown → 0 with the
#: UI showing "estimate unavailable" rather than a made-up number.
_COST_TABLE = {
    "gpt-": (2.50, 10.00),
    "o4": (2.50, 10.00),
    "claude": (3.00, 15.00),
    "gemini": (1.25, 5.00),
    "llama": (0.0, 0.0),
    "qwen": (0.0, 0.0),
    "mistral": (0.0, 0.0),
    "fake": (0.0, 0.0),
}

_ENV = {
    "provider": "PACIFICA_OBD_AI_PROVIDER",
    "model": "PACIFICA_OBD_AI_MODEL",
    "base_url": "PACIFICA_OBD_AI_BASE_URL",
}


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class AIConfig:
    provider: str | None = None
    model: str = ""
    base_url: str | None = None
    temperature: float = 0.2
    send_bus_data_to_remote: bool = False
    redact_vin: bool = True
    allow_live_bus: bool = False
    persist_conversations: bool = False
    force_local_only: bool = False
    max_tokens_per_conversation: int = 120000
    max_requests_per_minute: int = 20
    max_output_tokens_per_request: int = 2048
    max_live_reads_per_turn: int = 5
    keys: dict = field(default_factory=dict)

    # -- validation --------------------------------------------------------
    def validate(self) -> "AIConfig":
        if self.provider is not None and self.provider not in PROVIDER_LABELS:
            raise ValueError(f"unknown provider: {self.provider!r}")
        if self.base_url is not None:
            if not str(self.base_url).startswith(("http://", "https://")):
                raise ValueError("base_url must be an http(s) URL")
            from urllib.parse import urlparse

            parsed = urlparse(str(self.base_url))
            if parsed.scheme == "http" and (parsed.hostname or "") not in LOCAL_BASE_HOSTS:
                raise ValueError(
                    "an http:// base_url is only allowed for a loopback host; "
                    "use https:// for a remote endpoint"
                )
        if self.provider and self.provider != "fake" and not self.model:
            raise ValueError("a model id is required once a provider is chosen")
        if not 0.0 <= float(self.temperature) <= 2.0:
            raise ValueError("temperature must be between 0 and 2")
        for name in ("max_tokens_per_conversation", "max_requests_per_minute",
                     "max_live_reads_per_turn", "max_output_tokens_per_request"):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        return self

    def url(self) -> str | None:
        if self.provider == "fake":
            return None
        return self.base_url or DEFAULT_BASE_URLS.get(self.provider or "")

    def is_local(self) -> bool:
        """True when traffic stays on this machine (a local server or the fake)."""
        if self.provider == "fake":
            return True
        url = self.url() or ""
        try:
            from urllib.parse import urlparse

            host = urlparse(url).hostname or ""
        except ValueError:
            return False
        return host in LOCAL_BASE_HOSTS

    # -- serialisation -----------------------------------------------------
    def to_dict(self, *, include_keys: bool = True) -> dict:
        data = asdict(self)
        if not include_keys:
            data.pop("keys", None)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AIConfig":
        allowed = {key: value for key, value in (data or {}).items()
                   if key in cls.__dataclass_fields__}
        if "keys" in allowed and not isinstance(allowed["keys"], dict):
            allowed.pop("keys")
        return cls(**allowed)

    def patch(self, patch: dict) -> "AIConfig":
        allowed = {key: value for key, value in (patch or {}).items()
                   if key in self.__dataclass_fields__ and key != "keys"}
        return replace(self, **{k: v for k, v in allowed.items() if v is not None})

    # -- env + file --------------------------------------------------------
    @classmethod
    def load(cls, path=None, *, env=None) -> "AIConfig":
        from pacifica_obd.web.ai.keys import read_json_file

        env = os.environ if env is None else env
        raw = {}
        try:
            raw = read_json_file(path)
        except Exception:  # noqa: BLE001 - a broken file must not crash the server
            raw = {}
        data = {key: value for key, value in raw.items() if key != "keys"}
        data.pop("keys", None)
        config = cls.from_dict(data)
        for field_name, env_name in _ENV.items():
            value = env.get(env_name)
            if value:
                setattr(config, field_name, value)
        if _as_bool(env.get("PACIFICA_OBD_AI_OFFLINE")):
            config.provider = "fake"
            config.model = config.model or "fake-1"
        return config

    def save(self, path=None) -> None:
        from pacifica_obd.web.ai.keys import read_json_file, write_json_file

        try:
            raw = read_json_file(path)
        except Exception:  # noqa: BLE001
            raw = {}
        data = self.to_dict(include_keys=False)
        data["keys"] = raw.get("keys") or self.keys or {}
        write_json_file(data, path)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Approximate USD from a small dated table; 0.0 when the model is unknown."""
    lowered = (model or "").lower()
    rates = None
    for key, value in _COST_TABLE.items():
        if key in lowered:
            rates = value
            break
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    return round(input_tokens / 1_000_000 * in_rate + output_tokens / 1_000_000 * out_rate, 6)
