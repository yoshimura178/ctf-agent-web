"""Chat web model-spec helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.config import Settings


DEFAULT_MODELS: list[str] = [
    "chatweb/chatgpt/o3-medium",
    "chatweb/chatgpt/gpt-5.5-high",
    "chatweb/chatgpt/gpt-5.4-high",
]

CONTEXT_WINDOWS: dict[str, int] = {
    "o3-medium": 1_000_000,
    "gpt-5.5-high": 1_000_000,
    "gpt-5.4-high": 1_000_000,
}

VISION_MODELS: set[str] = {
    "o3-medium",
    "gpt-5.5-high",
    "gpt-5.4-high",
}


def model_id_from_spec(spec: str) -> str:
    parts = spec.split("/")
    if len(parts) >= 3 and parts[0] == "chatweb":
        return parts[2]
    return spec


def provider_from_spec(spec: str) -> str:
    return spec.split("/", 1)[0]


def web_provider_from_spec(spec: str) -> str:
    parts = spec.split("/")
    if len(parts) >= 3 and parts[0] == "chatweb":
        provider = parts[1]
        if provider == "chatgpt":
            return provider
    raise ValueError(f"Invalid chatweb model spec: {spec}")


def enabled_default_models(settings: Settings) -> list[str]:
    enabled = {
        "chatweb/chatgpt/o3-medium": getattr(settings, "enable_chatgpt_o3_medium", True),
        "chatweb/chatgpt/gpt-5.5-high": getattr(settings, "enable_chatgpt_gpt55_high", True),
        "chatweb/chatgpt/gpt-5.4-high": getattr(settings, "enable_chatgpt_gpt54_high", True),
    }
    return [spec for spec in DEFAULT_MODELS if enabled.get(spec, True)]


def supports_vision(spec: str) -> bool:
    return model_id_from_spec(spec) in VISION_MODELS


def context_window(spec: str) -> int:
    return CONTEXT_WINDOWS.get(model_id_from_spec(spec), 200_000)
