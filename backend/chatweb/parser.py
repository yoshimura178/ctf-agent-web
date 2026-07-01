"""Chat web response parser and prompt helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal


ChatWebProvider = Literal["chatgpt"]


@dataclass
class ChatWebAction:
    action: str
    tool: str = ""
    args: dict[str, Any] | None = None
    flag: str = ""
    method: str = ""
    message: str = ""


IMAGE_ANALYSIS_TEMPLATE = """\
Analyze the attached CTF challenge image.

File: {filename}
Media type: {media_type}

Look for flags, hidden text, steganography clues, metadata clues, QR/barcodes,
visual anomalies, transparency/channel issues, and any encoding hints. If you
find a candidate flag, respond using the required JSON final action. Otherwise,
respond with one JSON tool action for the next concrete analysis step.
"""


def build_image_analysis_prompt(filename: str, media_type: str) -> str:
    return IMAGE_ANALYSIS_TEMPLATE.format(filename=filename, media_type=media_type)


def _strip_code_fence(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if m:
        return m.group(1)
    return text.strip()


def parse_chatweb_response(text: str) -> ChatWebAction:
    """Parse the strict JSON protocol expected from chat web responses."""
    raw = _strip_code_fence(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        flag_match = re.search(r"\bFLAG:\s*(\S+)", text)
        if flag_match:
            return ChatWebAction(action="final", flag=flag_match.group(1), method="FLAG line")
        raise ValueError(
            "Chat web response must be JSON with action=tool/final/message, or a FLAG line."
        ) from e

    if not isinstance(data, dict):
        raise ValueError("Chat web response JSON must be an object.")

    action = str(data.get("action") or data.get("type") or "").lower()
    if action == "tool":
        tool = str(data.get("tool") or data.get("name") or "")
        args = data.get("args") or data.get("arguments") or {}
        if not tool:
            raise ValueError("Tool response missing 'tool'.")
        if not isinstance(args, dict):
            raise ValueError("Tool response 'args' must be an object.")
        return ChatWebAction(action="tool", tool=tool, args=args)

    if action in {"final", "flag_found"}:
        flag = str(data.get("flag") or "")
        if not flag:
            raise ValueError("Final response missing 'flag'.")
        return ChatWebAction(
            action="final",
            flag=flag,
            method=str(data.get("method") or data.get("summary") or "chatweb final"),
        )

    if action == "message":
        return ChatWebAction(action="message", message=str(data.get("message") or ""))

    raise ValueError("Unknown chat web action. Expected 'tool', 'final', or 'message'.")
