"""Web server settings.

The single most important field is :attr:`read_only`: in stage 1 it is a
constant property with no setter, and the app registers **no write routes at
all**, so "read-only" is structural rather than a default a request could flip.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# Loopback hosts that are acceptable in a Host header (DNS-rebinding defence).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


@dataclass
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str | None = None
    demo: bool = False
    allow_remote: bool = False
    bus: str = "can-c"
    port_name: str | None = None
    baud: int = 115200
    timeout_s: float = 2.0
    check_voltage: bool = False
    # Explicit ELM protocol for the generic OBD-II pass, so python-OBD uses
    # ATTP (temporary) instead of ATSP (saved to EEPROM). 6 = ISO 15765-4 CAN
    # 11/500, matching the repo's can-c assumption.
    generic_protocol: str | None = "6"
    log_level: str = "info"
    open_browser: bool = False
    # Stage 2: the write surface is off unless the operator starts the server
    # with --allow-writes. `--read-only` (or the default) removes every write
    # route from the app entirely; the AI layer never has one either way.
    read_only: bool = True
    # Owner decision §9.2: held-seed is the default. This opt-in is for modules
    # whose seed is known to be stable and whose bad-attempt counter is not a
    # concern; it is documented with that caveat in Settings → Safety.
    strict_seed_reread: bool = False

    @property
    def auth_required(self) -> bool:
        return bool(self.token)

    @property
    def write_enabled(self) -> bool:
        return not self.read_only

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    def local_origins(self) -> set[str]:
        return {
            f"http://{host}:{self.port}" for host in LOOPBACK_HOSTS if host != "[::1]"
        } | {f"http://[::1]:{self.port}"}


def resolve_token(explicit: str | bool | None) -> str | None:
    """Resolve ``--token``: a value, a generated one, or the env override.

    ``True`` (bare ``--token``) generates a random 32-byte URL-safe token.
    ``PACIFICA_OBD_WEB_TOKEN`` wins when no flag is given.
    """
    if explicit is True:
        return secrets.token_urlsafe(24)
    if isinstance(explicit, str) and explicit:
        return explicit
    env = os.environ.get("PACIFICA_OBD_WEB_TOKEN")
    return env or None
