from __future__ import annotations

import json
from typing import Any


def parse_json_object_content(content: Any) -> dict[str, Any] | None:
    """Normalize a model's text or text-block response into one JSON object."""
    if isinstance(content, dict):
        return content

    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                text_parts.append(text)
        content = "".join(text_parts)

    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None
