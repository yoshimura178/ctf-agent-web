"""Chat web Chrome client package.

Public imports are kept stable:

    from backend.chatweb import WebChatClient, parse_chatweb_response
"""

from backend.chatweb.base import WebChatClient
from backend.chatweb.parser import (
    IMAGE_ANALYSIS_TEMPLATE,
    ChatWebAction,
    ChatWebProvider,
    build_image_analysis_prompt,
    parse_chatweb_response,
)

__all__ = [
    "ChatWebAction",
    "ChatWebProvider",
    "IMAGE_ANALYSIS_TEMPLATE",
    "WebChatClient",
    "build_image_analysis_prompt",
    "parse_chatweb_response",
]
