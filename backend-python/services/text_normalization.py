"""Normalize model output for a small screen and spoken playback."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any


ASR_HALLUCINATION_PHRASES = {
    "cam on cac ban da theo doi",
    "cam on ban da theo doi",
    "hay dang ky kenh",
    "nho dang ky kenh",
    "subtitles by",
    "subtitle by",
    "thanks for watching",
    "thank you for watching",
}


def fold_vietnamese(value: str) -> str:
    """Return a lowercase, accent-free form suitable for intent matching."""
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", normalized.replace("đ", "d")).strip()


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


def filter_asr_transcript(value: Any) -> str:
    """Drop common Whisper/Google noise hallucinations before intent routing.

    Short real commands such as "mở", "Hey" and "Dom" remain valid. The
    minimum-length rule therefore rejects only empty/single-character noise
    and known filler utterances rather than all short speech.
    """
    text = plain_speech_text(value).strip()
    folded = fold_vietnamese(text)
    if len(folded) < 2 or folded in {"a", "ah", "um", "uh", "u", "ờ", "ừ", "ừm"}:
        return ""
    if any(phrase in folded for phrase in ASR_HALLUCINATION_PHRASES):
        return ""
    words = folded.split()
    if len(words) >= 4 and len(set(words)) == 1:
        return ""
    return text


def normalize_tts_text(value: Any) -> str:
    """Normalize text at the final boundary shared by LCD, storage and TTS."""
    text = plain_speech_text(value)
    text = re.sub(r"\bv(\d+(?:\.\d+){1,})\b", lambda match: (
        "phiên bản " + " chấm ".join(match.group(1).split("."))
    ), text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(\d{1,2})\s*[hH]\s*(\d{1,2})\b",
        r"\1 giờ \2 phút",
        text,
    )
    text = re.sub(r"(-?\d+(?:[,.]\d+)?)\s*°\s*[cC]\b", r"\1 độ C", text)
    text = re.sub(r"(-?\d+(?:[,.]\d+)?)\s*%", r"\1 phần trăm", text)
    text = re.sub(r"(?<![a-z])km\s*/\s*h\b", " ki-lô-mét trên giờ", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*[wW]\b", " oát", text)
    text = re.sub(r"\b(\d+(?:[,.]\d+)?)\s*[kK]\b", r"\1 nghìn", text)

    pronunciations = {
        r"\bwifi\b": "oai-phai",
        r"\bwi[ -]?fi\b": "oai-phai",
        r"\breset\b": "ri-xét",
        r"\bbluetooth\b": "blu-tút",
    }
    for pattern, replacement in pronunciations.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    text = text.replace("...", ".").replace("…", ".")
    text = re.sub(r"[\[\]{}()\"“”„‟«»‹›]", "", text)
    text = "".join(
        char for char in text
        if not (unicodedata.category(char) in {"So", "Sk"} and char not in {"°"})
    )
    text = re.sub(r"\s*\n+\s*", ". ", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    text = re.sub(r"([,.!?]){2,}", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n'’")
    return text


class PunctuationChunker:
    """Collect streamed tokens and release complete spoken clauses."""

    _BOUNDARY = re.compile(r"(?<!\d)([,.!?])(?!\d)\s+")

    def __init__(self) -> None:
        self.buffer = ""

    def feed(self, delta: str) -> list[str]:
        self.buffer += delta
        chunks: list[str] = []
        while match := self._BOUNDARY.search(self.buffer):
            end = match.start(1) + 1
            chunk = self.buffer[:end].strip()
            self.buffer = self.buffer[match.end():]
            if chunk:
                chunks.append(chunk)
        return chunks

    def flush(self) -> str:
        remainder = self.buffer.strip()
        self.buffer = ""
        return remainder
