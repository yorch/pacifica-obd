"""Key storage, redaction and egress review (design §5.4).

Rules this module exists to enforce:

* keys live in ``$PACIFICA_OBD_HOME/ai.json`` (else ``$XDG_CONFIG_HOME`` /
  ``~/.config``), created **0600** with a 0700 parent; a looser file is refused
  rather than silently used;
* environment variables win over the file, for CI and one-off runs;
* a key is **never** logged, returned by the API, put in a report or written to
  the diagnostic store. A logging filter rewrites anything that looks like a key
  before it reaches a handler;
* the redactor is applied to provider errors and to the "what will be sent" view.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
from pathlib import Path

__all__ = [
    "KeyStoreError",
    "KeyStore",
    "config_dir",
    "config_path",
    "read_json_file",
    "write_json_file",
    "redact_text",
    "install_logging_filter",
    "MASK",
]

MASK = "sk-…redacted"

_KEY_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)\b(?:api[-_]?key|authorization|x-api-key)\b\s*[:=]\s*[\"']?([^\s\"',;]+)"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
)

_ENV_KEYS = {
    "openai_compat": ("PACIFICA_OBD_AI_KEY_OPENAI_COMPAT", "PACIFICA_OBD_AI_KEY_OPENAI"),
    "anthropic": ("PACIFICA_OBD_AI_KEY_ANTHROPIC",),
    "fake": ("PACIFICA_OBD_AI_KEY_FAKE",),
}


class KeyStoreError(RuntimeError):
    """The key file is missing, insecure, or malformed."""


# --------------------------------------------------------------------------- #
# paths + file IO
# --------------------------------------------------------------------------- #
def config_dir() -> Path:
    home = os.environ.get("PACIFICA_OBD_HOME")
    if home:
        return Path(home).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "pacifica-obd"
    return Path.home() / ".config" / "pacifica-obd"


def config_path() -> Path:
    return config_dir() / "ai.json"


def _check_permissions(path: Path) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        raise KeyStoreError(
            f"{path} is readable by other users (mode {mode:03o}); run `chmod 600 {path}`"
        )


def read_json_file(path: Path | None = None) -> dict:
    """Read the config JSON, enforcing 0600. Missing file → ``{}``."""
    path = Path(path) if path else config_path()
    if not path.exists():
        return {}
    _check_permissions(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as exc:
        raise KeyStoreError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise KeyStoreError(f"{path} does not contain a JSON object")
    return data


def write_json_file(data: dict, path: Path | None = None) -> Path:
    """Atomically write the config JSON with mode 0600 (parent 0700)."""
    path = Path(path) if path else config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".ai-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            try:
                os.unlink(temp)
            except OSError:
                pass
    return path


# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #
def redact_text(text, extra_keys=()) -> str:
    """Mask key-shaped material in ``text``; the configured literals always win."""
    if text is None:
        return ""
    out = str(text)
    for key in extra_keys:
        if key and len(str(key)) >= 6:
            out = out.replace(str(key), MASK)
    for pattern in _KEY_PATTERNS:
        out = pattern.sub(MASK, out)
    return out


_LITERALS: tuple[str, ...] = ()


def set_logging_literals(values) -> None:
    """Register the configured key values so the log filter masks them too.

    A key with an unusual shape (a passphrase, a proxy token) does not match the
    key-shape patterns, so only the literal value can catch it.
    """
    global _LITERALS
    _LITERALS = tuple(sorted({str(v) for v in values if v}, key=len, reverse=True))


def _scrub(value: str) -> str:
    """Mask key literals *and* VIN-shaped text (including its hex form) in logs."""
    try:
        from pacifica_obd.web.ai import redact

        return redact.scrub(value, keys=_LITERALS)
    except Exception:  # noqa: BLE001 - logging must never raise
        return redact_text(value, _LITERALS)


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _scrub(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: _scrub(v) if isinstance(v, str) else v
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(_scrub(a) if isinstance(a, str) else a
                                        for a in record.args)
            # An exception's text is formatted separately from msg/args: the
            # traceback comes from exc_info, which no formatter scrubs. Format it
            # here, scrub it, and hand the clean text on (exc_text wins over
            # exc_info in logging.Formatter).
            if record.exc_info:
                record.exc_text = _scrub(
                    logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
            elif isinstance(record.exc_text, str):
                record.exc_text = _scrub(record.exc_text)
            if isinstance(record.stack_info, str):
                record.stack_info = _scrub(record.stack_info)
        except Exception:  # noqa: BLE001 - logging must never raise
            pass
        return True


_FILTER: _RedactingFilter | None = None


def install_logging_filter(store: "KeyStore | None" = None) -> logging.Filter:
    """Attach the redacting filter to the root logger (and its handlers) once.

    The root logger alone is not enough: a record logged by a child logger still
    propagates its *original* args to the handlers' formatters, so the filter is
    added to handlers too (idempotent).
    """
    global _FILTER
    if _FILTER is None:
        _FILTER = _RedactingFilter()
    try:
        key_store = store if store is not None else KeyStore()
        set_logging_literals(key_store.literals())
    except Exception:  # noqa: BLE001 - an unreadable key file must not break boot
        pass
    root = logging.getLogger()
    if _FILTER not in root.filters:
        root.addFilter(_FILTER)
    for handler in root.handlers:
        if _FILTER not in handler.filters:
            handler.addFilter(_FILTER)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore",
                 "pacifica_obd"):
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
    return _FILTER


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
class KeyStore:
    """Keys from the environment or the 0600 config file, never both leaked."""

    def __init__(self, path: Path | str | None = None, *, env: dict | None = None):
        self.path = Path(path) if path else config_path()
        self._env = os.environ if env is None else env
        self._load()

    def _load(self) -> None:
        raw = read_json_file(self.path)
        keys = raw.get("keys") or {}
        self._keys = {str(k): str(v) for k, v in keys.items() if isinstance(v, str)}

    # -- access ------------------------------------------------------------
    def get(self, provider: str) -> str | None:
        for name in _ENV_KEYS.get(provider, ()):
            value = self._env.get(name)
            if value:
                return value
        return self._keys.get(provider)

    def set(self, provider: str, key: str) -> None:
        if not provider or not key:
            raise KeyStoreError("provider and key are required")
        self._keys[provider] = str(key)
        self._persist()

    def delete(self, provider: str) -> bool:
        existed = provider in self._keys
        self._keys.pop(provider, None)
        self._persist()
        return existed

    def configured(self, provider: str) -> bool:
        return bool(self.get(provider))

    def masked(self, provider: str) -> dict:
        """A hint only: ``{"configured": true, "hint": "…f3a2", "source": "file|env"}``."""
        for name in _ENV_KEYS.get(provider, ()):
            if self._env.get(name):
                return {"configured": True, "hint": _hint(self._env[name]), "source": "env"}
        value = self._keys.get(provider)
        if not value:
            return {"configured": False, "hint": None, "source": None}
        return {"configured": True, "hint": _hint(value), "source": "file"}

    def literals(self) -> tuple[str, ...]:
        """Every configured key value (file *and* environment), for redaction."""
        values = [v for v in self._keys.values() if v]
        for names in _ENV_KEYS.values():
            for name in names:
                value = self._env.get(name)
                if value:
                    values.append(value)
        # longest first, so a key that contains another is masked whole
        return tuple(sorted(set(values), key=len, reverse=True))

    def redactor(self):
        """A ``str -> str`` that also masks this store's literal keys."""
        literals = self.literals()

        def _redact(value):
            return redact_text(value, literals)

        return _redact

    # -- persistence -------------------------------------------------------
    def _persist(self) -> None:
        raw = {}
        if self.path.exists():
            # Read strictly: never overwrite a config we could not read (a bad
            # read must not silently drop other fields or the keys themselves).
            raw = read_json_file(self.path)
        raw["keys"] = self._keys
        write_json_file(raw, self.path)


def _hint(value: str) -> str:
    value = str(value)
    if len(value) <= 4:
        return "…"
    return "…" + value[-4:]
