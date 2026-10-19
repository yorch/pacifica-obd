"""AI configuration: provider, model, privacy toggles and cost caps (design §5.5).

Owner decision §9.3 governs the defaults:

* **no provider configured** out of the box (with a one-click local shortcut);
* "send bus data to a remote model" **off**;
* VIN redaction **on**;
* conversation persistence **off**;
* reads are **cache-first**; live bus reads need an explicit setting.

Validation is deliberately strict and shared by the file, the environment and
the HTTP patch: a remote endpoint must be ``https://``, an ``http://`` endpoint
must be loopback, URL userinfo is refused (a credential belongs in the key
store), unknown providers are refused, and every numeric cap has a sane range.

The cost table is a small, dated estimate. It is never presented as a bill.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, replace
from urllib.parse import urlparse

from pacifica_obd.web.ai import presets as _presets

__all__ = ["AIConfig", "estimate_cost", "COST_TABLE_DATE", "PROVIDER_LABELS",
           "DEFAULT_BASE_URLS", "LOCAL_BASE_HOSTS"]

#: id -> label, derived from the preset registry so there is one source of truth.
PROVIDER_LABELS = dict(_presets.PROVIDER_LABELS)

#: provider id -> preset default endpoint (empty string = none baked in).
DEFAULT_BASE_URLS = {pid: p.default_base_url for pid, p in _presets.PRESETS.items()}

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

#: (min, max) for every numeric cap. A bound exists so a bad saved file or a
#: stray form value cannot ask for a billion output tokens.
_BOUNDS = {
    "max_tokens_per_conversation": (256, 10_000_000),
    "max_requests_per_minute": (1, 600),
    "max_output_tokens_per_request": (1, 131_072),
    "max_live_reads_per_turn": (0, 50),
}

#: ``None`` is meaningful for these: an explicit null clears the field.
_CLEARABLE = ("provider", "model", "base_url")


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _bool_field(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"{name} must be a boolean")


def _is_link_local(host: str | None) -> bool:
    """True for a link-local address (the cloud-metadata range).

    Normalises the alternative spellings a resolver accepts (integer, hex, octal,
    shorthand and IPv4-mapped IPv6) so they cannot slip past the check.
    """
    if not host:
        return False
    import ipaddress
    import socket

    candidates = []
    text = str(host).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        candidates.append(ipaddress.ip_address(socket.inet_aton(text)))
    except (OSError, ValueError):
        pass
    try:
        address = ipaddress.ip_address(socket.inet_pton(socket.AF_INET6, text))
        candidates.append(address)
        if address.ipv4_mapped is not None:
            candidates.append(address.ipv4_mapped)
    except (OSError, ValueError):
        pass
    try:
        candidates.append(ipaddress.ip_address(text))
    except ValueError:
        pass
    return any(address.is_link_local for address in candidates)


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

    # -- presets -----------------------------------------------------------
    def preset(self):
        return _presets.preset(self.provider)

    def effective_model(self) -> str:
        """The model the adapters will actually send (config wins, then preset)."""
        model = (self.model or "").strip()
        if model:
            return model
        found = self.preset()
        return found.default_model if found else ""

    def format(self) -> str:
        found = self.preset()
        return found.format if found else ""

    def requires_key(self) -> bool:
        found = self.preset()
        return bool(found.requires_key) if found else False

    # -- validation --------------------------------------------------------
    def normalize(self) -> "AIConfig":
        """Coerce field types from a file/env so validation sees real types.

        Raises :class:`ValueError` (never ``TypeError``) so the HTTP layer maps a
        bad saved value to a documented 400 rather than a 500.
        """
        if self.provider is not None:
            self.provider = str(self.provider).strip() or None
        if self.base_url is not None:
            self.base_url = str(self.base_url).strip() or None
        self.model = "" if self.model is None else str(self.model).strip()
        for name in ("send_bus_data_to_remote", "redact_vin", "allow_live_bus",
                     "persist_conversations", "force_local_only"):
            setattr(self, name, _bool_field(getattr(self, name), name))
        try:
            self.temperature = float(self.temperature)
        except (TypeError, ValueError) as exc:
            raise ValueError("temperature must be a number") from exc
        for name in _BOUNDS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                try:
                    value = int(str(value))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{name} must be an integer") from exc
            setattr(self, name, int(value))
        return self

    def validate(self) -> "AIConfig":
        self.normalize()
        if self.provider is not None and self.provider not in PROVIDER_LABELS:
            raise ValueError(f"unknown provider: {self.provider!r}")
        if self.base_url is not None:
            parsed = urlparse(str(self.base_url))
            if parsed.scheme not in ("http", "https"):
                raise ValueError("base_url must be an http(s) URL")
            if not parsed.hostname:
                raise ValueError("base_url must include a host name")
            if parsed.username or parsed.password:
                raise ValueError(
                    "base_url must not contain credentials; use the key field instead"
                )
            if parsed.scheme == "http" and parsed.hostname not in LOCAL_BASE_HOSTS:
                raise ValueError(
                    "an http:// base_url is only allowed for a loopback host; "
                    "use https:// for a remote endpoint"
                )
            if _is_link_local(parsed.hostname):
                raise ValueError(
                    "a link-local/metadata address is not a valid AI endpoint"
                )
        if self.provider and self.provider != "fake" and not self.effective_model():
            raise ValueError("a model id is required once a provider is chosen")
        if not 0.0 <= float(self.temperature) <= 2.0:
            raise ValueError("temperature must be between 0 and 2")
        for name, (low, high) in _BOUNDS.items():
            value = int(getattr(self, name))
            if value < low or value > high:
                raise ValueError(f"{name} must be between {low} and {high}")
        return self

    def url(self) -> str | None:
        found = self.preset()
        if found is not None and found.format == _presets.FAKE_FORMAT:
            return None
        if self.base_url:
            return self.base_url
        return _presets.default_base_url(self.provider) or None

    def is_local(self) -> bool:
        """True when traffic stays on this machine (a local server or the fake)."""
        found = self.preset()
        if found is not None and found.format == _presets.FAKE_FORMAT:
            return True
        url = self.url() or ""
        if not url:
            # No endpoint yet: fall back to the preset hint.
            return bool(found.local) if found else False
        try:
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
        """Apply a partial update.

        A key that is **present** is applied even when its value is ``None`` (so
        an explicit null can clear a field); a key that is absent is left alone.
        ``keys`` is never patched here — secrets go through the key store.
        """
        allowed = {key: value for key, value in (patch or {}).items()
                   if key in self.__dataclass_fields__ and key != "keys"}
        return replace(self, **allowed)

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
        try:
            config = cls.from_dict(data).normalize()
        except ValueError:
            # A single bad field must not make the whole file unreadable; keep
            # the raw value so validate() can report it with a clear message.
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
