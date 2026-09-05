"""Normalize model output for a small screen and spoken playback."""

from __future__ import annotations

import html
import re
from typing import Any


def plain_speech_text(value: Any) -> str:
    """Convert common Markdown/HTML output to readable plain text.

    This is intentionally deterministic: prompts reduce Markdown generation,
    while this boundary guarantees that formatting tokens never reach SQLite,
    the ESP32 display, or TTS.
    """
    if isinstance(value, list):
        value = " ".join(
            str(part.get("text", "")) for part in value if isinstance(part, dict)
        )
    text = html.unescape(str(value or ""))
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^(bản ghi|transcript|transcription)\s*:\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"!\[([^]]*)]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", text)
    text = re.sub(r"<https?://[^>]+>", "", text)
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)

    lines: list[str] = []
    in_code_fence = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if re.match(r"^\s*(```|~~~)", line):
            in_code_fence = not in_code_fence
            continue
        if re.fullmatch(r"(?:[-*_]\s*){3,}", line):
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"^\s*>+\s?", "", line)
        line = re.sub(r"^\s*(?:[-+*]|\d+[.)])\s+", "", line)
        line = re.sub(r"\*{1,3}([^*\n]+?)\*{1,3}", r"\1", line)
        line = re.sub(r"_{1,3}([^_\n]+?)_{1,3}", r"\1", line)
        line = re.sub(r"~~([^~\n]+?)~~", r"\1", line)
        line = line.replace("`", "").replace("*", "")
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
        elif lines and not in_code_fence and lines[-1] != "":
            lines.append("")
    return "\n".join(lines).strip().strip('"“”')
