"""Redaction, untrusted-data fencing and the injection heuristic (design §5.8).

Bus data is chosen by an ECU and is therefore **untrusted input**. This module:

* strips/escapes control characters and caps field length and total size, so a
  hostile DID payload cannot blow up the context or smuggle a fence break;
* wraps data in ``<untrusted source=... tool=...>`` … ``</untrusted>`` with every
  ``<``/``>`` in the payload escaped as ``\\u003c``/``\\u003e``, so the payload
  cannot close the fence;
* redacts the VIN to ``<VIN:redacted>`` (with a stable pseudonym for continuity)
  when redaction is on;
* flags instruction-like text conservatively (it never blocks, it informs).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re

__all__ = [
    "FIELD_LIMIT",
    "TOTAL_LIMIT",
    "redact_vin",
    "redact_vin_text",
    "pseudonym",
    "sanitize",
    "fence",
    "looks_like_injection",
    "redact_secrets",
]

FIELD_LIMIT = 4096
TOTAL_LIMIT = 65536

_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts)", re.I),
    re.compile(r"\b(system|developer)\s*:", re.I),
    re.compile(r"you\s+(must|should|shall)\b", re.I),
    re.compile(r"\b(assistant|tool_use|tool_call)\s*:", re.I),
    re.compile(r"</?untrusted", re.I),
    re.compile(r"\b(execute|run|call)\s+(the\s+)?(tool|command|function)\b", re.I),
    re.compile(r"\bnew\s+instructions\b", re.I),
)

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _salt() -> str:
    """A per-install salt so the VIN pseudonym is not a brute-forceable hash.

    Read from ``PACIFICA_OBD_AI_PSEUDONYM_SALT`` or a 0600 ``ai-salt`` file under
    the tool's config directory; created on first use.
    """
    env = os.environ.get("PACIFICA_OBD_AI_PSEUDONYM_SALT")
    if env:
        return env
    try:
        from pacifica_obd.web.ai.keys import config_dir

        path = config_dir() / "ai-salt"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        import secrets

        value = secrets.token_hex(16)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(value, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return value
    except Exception:  # noqa: BLE001 - a missing salt must not break redaction
        return ""


_SALT: str | None = None


def pseudonym(vin: str) -> str:
    """A stable, keyed, non-reversible stand-in so a conversation stays coherent."""
    global _SALT
    if _SALT is None:
        _SALT = _salt()
    digest = hmac.new((_SALT or "pacifica").encode("utf-8"),
                      str(vin).encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:10]
    return f"VIN-{digest}"


def redact_vin(text, vin: str | None = None, *, enabled: bool = True) -> str:
    """Replace the VIN with ``<VIN:redacted>`` (or a pseudonym) when enabled."""
    value = "" if text is None else str(text)
    if not enabled or not vin:
        return value
    return value.replace(str(vin), f"<VIN:redacted {pseudonym(vin)}>")


_VIN_LIKE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")


def redact_vin_text(text, *, enabled: bool = True) -> str:
    """Redact any VIN-*shaped* token in free text (the user's own message too)."""
    value = "" if text is None else str(text)
    if not enabled:
        return value
    return _VIN_LIKE.sub(lambda match: f"<VIN:redacted {pseudonym(match.group())}>", value)


def redact_secrets(text, redactor=None) -> str:
    if redactor is None:
        return "" if text is None else str(text)
    return redactor(text)


def sanitize(value, *, limit: int = FIELD_LIMIT) -> str:
    """Escape control characters and a fence-breaking ``<``/``>``; cap the length."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True,
                                                           default=str, sort_keys=True)
    text = _CONTROL.sub(lambda m: "\\x%02x" % ord(m.group()), text)
    text = text.replace("\\", "\\\\").replace("<", "\\u003c").replace(">", "\\u003e")
    if len(text) > limit:
        text = text[:limit] + f"…[truncated {len(text) - limit} chars]"
    return text


def fence(source: str, tool: str, value, *, nonce: str | None = None,
          limit: int = FIELD_LIMIT) -> str:
    """Serialise ``value`` as inert, fenced untrusted data."""
    payload = sanitize(value, limit=limit)
    if len(payload) > TOTAL_LIMIT:
        payload = payload[:TOTAL_LIMIT] + "…[truncated]"
    marker = f' nonce="{sanitize(nonce, limit=64)}"' if nonce else ""
    return (f'<untrusted source="{sanitize(source, limit=64)}" '
            f'tool="{sanitize(tool, limit=64)}"{marker}>\n{payload}\n</untrusted>')


def looks_like_injection(value) -> bool:
    """A conservative heuristic; used to warn, never to block."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)
