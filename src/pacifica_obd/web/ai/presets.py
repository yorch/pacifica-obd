"""Provider presets — everything a setup form needs, in one place (design §5.3).

The rest of the AI layer is vendor-neutral: it knows two wire *formats*
(``openai_compat`` and ``anthropic``) and the offline ``fake``. A **preset** is
what a person actually picks when they configure the assistant — OpenAI,
Anthropic, Google Gemini, Ollama, LM Studio, a self-hosted vLLM, or a generic
OpenAI-compatible endpoint — and it maps straight onto one of those formats.

A preset carries exactly what a settings form must render and what the backend
must validate:

* ``label``            — the human name shown in the picker;
* ``format``           — which adapter speaks to it;
* ``default_base_url`` — a real, working endpoint (empty ⇒ the user must supply
  one, e.g. the generic compatible entry);
* ``requires_key``     — whether a key is *mandatory* (hosted vendors) or merely
  optional (local servers and most proxies accept none);
* ``local``            — a hint that the default endpoint stays on this machine;
* ``default_model``    — a sensible model to prefill;
* ``example_models``   — a few ids the UI can offer;
* ``key_env``          — operator env var names honoured for this provider, in
  addition to the generic ``PACIFICA_OBD_AI_KEY_<ID>``.

Locality of a *configured* endpoint is always recomputed from its host (see
:meth:`pacifica_obd.web.ai.config.AIConfig.is_local`); ``local`` here is only the
preset's default so a loopback Ollama is understood before the first request.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "ProviderPreset",
    "PRESETS",
    "preset",
    "preset_ids",
    "preset_label",
    "default_base_url",
    "preset_key_env",
    "PROVIDER_LABELS",
]

OPENAI_COMPAT_FORMAT = "openai_compat"
ANTHROPIC_FORMAT = "anthropic"
FAKE_FORMAT = "fake"

FORMAT_LABELS = {
    OPENAI_COMPAT_FORMAT: "OpenAI-compatible chat API",
    ANTHROPIC_FORMAT: "Anthropic Messages API",
    FAKE_FORMAT: "Deterministic offline provider",
}


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    label: str
    format: str
    default_base_url: str = ""
    requires_key: bool = False
    local: bool = False
    default_model: str = ""
    example_models: tuple[str, ...] = field(default=())
    notes: str = ""
    key_env: tuple[str, ...] = field(default=())
    key_hint: str = ""

    @property
    def format_label(self) -> str:
        return FORMAT_LABELS.get(self.format, self.format)

    def as_dict(self) -> dict:
        """The shape ``GET /api/ai/providers`` returns (never contains a key)."""
        return {
            "id": self.id,
            "label": self.label,
            "format": self.format,
            "format_label": self.format_label,
            "default_base_url": self.default_base_url,
            "requires_key": self.requires_key,
            "local": self.local,
            "default_model": self.default_model,
            "example_models": list(self.example_models),
            "notes": self.notes,
            "key_hint": self.key_hint,
            "key_env": list(self.key_env),
        }


#: Ordered so the picker is stable: hosted vendors, then local servers, then the
#: generic escape hatch, then the offline test provider.
PRESETS: dict[str, ProviderPreset] = {
    p.id: p
    for p in (
        ProviderPreset(
            id="openai",
            label="OpenAI",
            format=OPENAI_COMPAT_FORMAT,
            default_base_url="https://api.openai.com/v1",
            requires_key=True,
            default_model="gpt-4o-mini",
            example_models=("gpt-4o-mini", "gpt-4o", "gpt-4.1", "o4-mini"),
            notes="Paste an OpenAI API key. The key is stored 0600 on this machine.",
            key_env=("OPENAI_API_KEY",),
            key_hint="sk-…",
        ),
        ProviderPreset(
            id="anthropic",
            label="Anthropic (Claude)",
            format=ANTHROPIC_FORMAT,
            default_base_url="https://api.anthropic.com",
            requires_key=True,
            # Verified against GET /v1/models for a live key (the 3.5-era ids are
            # retired and made "Test connection" fail with a bare bad_request).
            default_model="claude-sonnet-5-5",
            example_models=("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5",
                            "claude-opus-4-8", "claude-sonnet-4-6"),
            notes="Native Messages API. Paste an Anthropic API key.",
            key_env=("ANTHROPIC_API_KEY",),
            key_hint="sk-ant-…",
        ),
        ProviderPreset(
            id="gemini",
            label="Google Gemini",
            format=OPENAI_COMPAT_FORMAT,
            default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            requires_key=True,
            default_model="gemini-2.0-flash",
            example_models=("gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-pro"),
            notes="Google's OpenAI-compatible endpoint. Paste a Gemini API key.",
            key_env=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            key_hint="AIza…",
        ),
        ProviderPreset(
            id="ollama",
            label="Ollama (local)",
            format=OPENAI_COMPAT_FORMAT,
            default_base_url="http://127.0.0.1:11434/v1",
            requires_key=False,
            local=True,
            default_model="llama3.2",
            example_models=("llama3.2", "qwen2.5", "mistral", "phi4"),
            notes="Runs on this machine (default port 11434). No key needed.",
        ),
        ProviderPreset(
            id="lmstudio",
            label="LM Studio (local)",
            format=OPENAI_COMPAT_FORMAT,
            default_base_url="http://127.0.0.1:1234/v1",
            requires_key=False,
            local=True,
            default_model="local-model",
            example_models=("local-model",),
            notes="LM Studio's local server (default port 1234). No key needed.",
        ),
        ProviderPreset(
            id="vllm",
            label="vLLM (self-hosted)",
            format=OPENAI_COMPAT_FORMAT,
            default_base_url="http://127.0.0.1:8000/v1",
            requires_key=False,
            local=True,
            default_model="",
            example_models=("your-served-model",),
            notes="A self-hosted vLLM server. Use its public https URL if it is "
                  "not on this machine; a key is optional.",
        ),
        ProviderPreset(
            id="openai_compat",
            label="OpenAI-compatible endpoint (custom)",
            format=OPENAI_COMPAT_FORMAT,
            default_base_url="",
            requires_key=False,
            default_model="",
            notes="Any server that speaks /v1/chat/completions. You must supply "
                  "the base URL (and a key if it needs one).",
        ),
        ProviderPreset(
            id="fake",
            label="Offline deterministic (testing)",
            format=FAKE_FORMAT,
            requires_key=False,
            local=True,
            default_model="fake-1",
            example_models=("fake-1",),
            notes="Never contacts a network. Used by tests and offline demos.",
        ),
    )
}


def preset(provider_id: str | None) -> ProviderPreset | None:
    if not provider_id:
        return None
    return PRESETS.get(str(provider_id).strip())


def preset_ids() -> tuple[str, ...]:
    return tuple(PRESETS)


def preset_label(provider_id: str | None) -> str | None:
    found = preset(provider_id)
    return found.label if found else None


def default_base_url(provider_id: str | None) -> str:
    found = preset(provider_id)
    return found.default_base_url if found else ""


def preset_key_env(provider_id: str | None) -> tuple[str, ...]:
    """Env var names honoured for ``provider_id`` (generic first, then vendor)."""
    found = preset(provider_id)
    if found is None:
        return ()
    generic = ("PACIFICA_OBD_AI_KEY_" + found.id.upper(),)
    extra = tuple(name for name in found.key_env if name not in generic)
    return generic + extra


#: Kept for callers that only wanted id → label (routes, app).
PROVIDER_LABELS: dict[str, str] = {pid: p.label for pid, p in PRESETS.items()}
