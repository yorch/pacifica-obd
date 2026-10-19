"""The vendor-neutral AI layer (design §5).

Nothing outside ``web/ai`` needs to know which wire format a provider speaks:
:class:`~pacifica_obd.web.ai.providers.Provider` translates a neutral
:class:`ChatRequest` into one async stream of neutral events.
"""

from __future__ import annotations

from pacifica_obd.web.ai.config import AIConfig, estimate_cost
from pacifica_obd.web.ai.presets import PRESETS, ProviderPreset, preset, preset_ids
from pacifica_obd.web.ai.providers import (
    ChatRequest,
    Done,
    Event,
    Message,
    ModelInfo,
    Provider,
    ProviderError,
    Token,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    build_provider,
    effective_model,
    PROVIDERS,
)

__all__ = [
    "AIConfig", "ChatRequest", "Done", "Event", "Message", "ModelInfo", "Provider",
    "ProviderError", "Token", "ToolCall", "ToolResult", "ToolSpec", "Usage",
    "build_provider", "PROVIDERS", "estimate_cost", "PRESETS", "ProviderPreset",
    "preset", "preset_ids", "effective_model",
]
