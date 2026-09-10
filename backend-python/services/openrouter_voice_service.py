"""Dom Voice Protocol v3 using OpenRouter cloud AI and Edge cloud TTS."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import math
import re
import time
import unicodedata
import uuid
import wave
from array import array
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import av
import edge_tts
import httpx
import speech_recognition as sr
from fastapi import WebSocket, WebSocketDisconnect
from gtts import gTTS

from config import settings
from services.assistant_prompt import assistant_now, build_system_prompt
from services.app_commands import (
    APP_COMMAND_HELP,
    APP_CONFIRMATIONS,
    APP_LAUNCH_FAILED,
    claims_app_launch,
    is_unresolved_launch_request,
    requested_app,
)
from services.conversation_store import ConversationStore
from services.text_normalization import (
    PunctuationChunker,
    filter_asr_transcript,
    fold_vietnamese,
    normalize_tts_text,
    plain_speech_text,
)
from services.web_search_service import needs_web_search, web_search_service

logger = logging.getLogger("domos.openrouter")

PCM_SAMPLE_RATE = 16_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2
PCM_FRAME_MS = 60
PCM_FRAME_BYTES = PCM_SAMPLE_RATE * PCM_FRAME_MS // 1000 * PCM_SAMPLE_WIDTH
# Keep a small amount of synthesized speech ahead of the ESP32 playback clock.
# Sending exactly one frame every 60 ms leaves no margin for Internet jitter and
# makes I2S run dry.  Five frames is 300 ms, small enough for PSRAM while large
# enough to absorb ordinary cloud/Wi-Fi scheduling delays.
TTS_JITTER_BUFFER_FRAMES = 5
TTS_STOP_GRACE_FRAMES = 1
VAD_ENERGY_THRESHOLD = 180
VAD_SILENCE_FRAMES = 9
VAD_MIN_SPEECH_FRAMES = 3
VAD_MAX_FRAMES = 20_000 // PCM_FRAME_MS
WAKE_MAX_FRAMES = 3_000 // PCM_FRAME_MS
VAD_CALIBRATION_FRAMES = 5
VAD_START_FRAMES = 2
VAD_PRE_ROLL_FRAMES = 24  # 1.44 s; preserves softly spoken Vietnamese sentence starts
VAD_MAX_PAUSE_SEC = 2.0
LISTENING_VAD_MAX_THRESHOLD = 450
WAKE_VAD_MAX_THRESHOLD = 700
VAD_NOISE_MULTIPLIER = 1.8
VAD_NOISE_MARGIN = 40
# Single-word "Hey" / "Dom" can be shorter than the old 300 ms minimum.
WAKE_MIN_SPEECH_FRAMES = 3
PROVIDER_RETRY_SECONDS = 300
NO_SPEECH_RESPONSE = "Mình chưa nghe rõ. Bạn nói lại giúp mình nhé."

TOOLS = [
    {"type": "function", "function": {"name": "device.get_status", "description": "Lấy trạng thái trợ lý và âm thanh", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "speaker.set_volume", "description": "Đặt âm lượng loa từ 0 đến 100", "parameters": {"type": "object", "properties": {"volume": {"type": "integer", "minimum": 0, "maximum": 100}}, "required": ["volume"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "speaker.adjust_volume", "description": "Tăng hoặc giảm âm lượng loa theo delta", "parameters": {"type": "object", "properties": {"delta": {"type": "integer", "minimum": -100, "maximum": 100}}, "required": ["delta"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "display.adjust_brightness", "description": "Tăng hoặc giảm độ sáng màn hình theo delta", "parameters": {"type": "object", "properties": {"delta": {"type": "integer", "minimum": -100, "maximum": 100}}, "required": ["delta"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "display.set_brightness", "description": "Đặt độ sáng màn hình từ 0 đến 100", "parameters": {"type": "object", "properties": {"brightness": {"type": "integer", "minimum": 0, "maximum": 100}}, "required": ["brightness"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "app.launch", "description": "Mở ứng dụng DomOS", "parameters": {"type": "object", "properties": {"app": {"type": "string", "enum": ["wallpaper", "clock", "tracking-status", "man-utd", "codex-credit"]}}, "required": ["app"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "web_search", "description": "Tìm kiếm web cho tin tức, thời tiết, giá cả, thể thao và dữ liệu thời gian thực", "parameters": {"type": "object", "properties": {"query": {"type": "string", "minLength": 2}}, "required": ["query"], "additionalProperties": False}}},
]


@dataclass
class VoiceRegistry:
    sessions: dict[str, VoiceSession] = field(default_factory=dict)
    provider_backoff: dict[str, dict[str, float]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def count(self) -> int:
        return len(self.sessions)

    async def add(self, session: VoiceSession) -> None:
        stale_sessions: list[VoiceSession] = []
        async with self.lock:
            # Northflank may rotate a WebSocket connection while the process
            # remains alive. Preserve provider quota cooldowns across that
            # reconnect so every new session does not repeat a slow 429 call.
            session.provider_retry_after = self.provider_backoff.setdefault(
                session.device_id.casefold(), {}
            )
            wanted = session.device_id.casefold()
            for session_id, active in tuple(self.sessions.items()):
                if session_id != session.session_id and active.device_id.casefold() == wanted:
                    stale_sessions.append(active)
                    self.sessions.pop(session_id, None)
            self.sessions[session.session_id] = session

        # A proxy reset can leave the old ASGI receive pending even though the
        # ESP32 has already opened a replacement socket. Remove it from routing
        # immediately and wake any dashboard request so it can retry the new
        # session instead of waiting on a ghost connection.
        for stale in stale_sessions:
            for future in stale.pending_mcp.values():
                if not future.done():
                    future.cancel()
            with contextlib.suppress(Exception):
                await stale.websocket.close(code=1012, reason="Superseded by reconnect")

    async def remove(self, session_id: str) -> None:
        async with self.lock:
            self.sessions.pop(session_id, None)

    async def get(self, device_id: str | None = None) -> VoiceSession | None:
        async with self.lock:
            candidates = reversed(tuple(self.sessions.values()))
            if device_id:
                wanted = device_id.casefold()
                return next(
                    (session for session in candidates if session.device_id.casefold() == wanted),
                    None,
                )
            return next(candidates, None)


voice_registry = VoiceRegistry()
conversation_store = ConversationStore(settings.CONVERSATION_DB_PATH)


def primary_llm_provider() -> str:
    for provider in settings.LLM_PROVIDER_ORDER.split(","):
        name = provider.strip().lower()
        if name == "openai" and settings.OPENAI_API_KEY:
            return "openai"
        if name == "openrouter" and settings.OPENROUTER_API_KEY:
            return "openrouter"
    return "unconfigured"


def primary_llm_model() -> str:
    return settings.OPENAI_MODEL if primary_llm_provider() == "openai" else settings.OPENROUTER_MODEL


def primary_stt_provider() -> str:
    """Keep OpenAI first whenever its credential is available.

    This deliberately overrides stale deployment values such as
    ``STT_PROVIDER=google-web`` without mutating or re-exporting cloud secrets.
    """
    return "openai" if settings.OPENAI_API_KEY else settings.STT_PROVIDER.strip().lower()


def primary_wake_stt_provider() -> str:
    return "configured" if settings.OPENAI_API_KEY else settings.WAKE_STT_PROVIDER


def effective_wake_stt_timeout() -> float:
    # OpenAI transcription can take longer than the stale 3-second deployment
    # value under cold-start/network jitter. Eight seconds remains bounded.
    return max(8.0, settings.WAKE_STT_TIMEOUT_SEC) if settings.OPENAI_API_KEY else settings.WAKE_STT_TIMEOUT_SEC


def validate_dom_hello(message: dict[str, Any]) -> None:
    expected = {"codec": "pcm", "sample_rate": 16_000, "channels": 1, "frame_duration": 60}
    audio = message.get("audio_params")
    if message.get("type") != "hello" or message.get("version") != 3:
        raise ValueError("Expected Dom Voice Protocol v3 hello")
    if not isinstance(audio, dict) or any(audio.get(key) != value for key, value in expected.items()):
        raise ValueError("Unsupported Dom PCM audio parameters")


def pcm_rms(pcm: bytes) -> int:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return 0
    return int(math.sqrt(sum(sample * sample for sample in samples) / len(samples)))


def pcm_signal_rms(pcm: bytes) -> int:
    """Return AC energy, excluding the microphone's DC offset."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return 0
    mean = sum(samples) / len(samples)
    return int(math.sqrt(sum((sample - mean) ** 2 for sample in samples) / len(samples)))


def pcm_to_wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(PCM_CHANNELS)
        wav.setsampwidth(PCM_SAMPLE_WIDTH)
        wav.setframerate(PCM_SAMPLE_RATE)
        wav.writeframes(pcm)
    return output.getvalue()


def normalize_speech_pcm(pcm: bytes, target_peak: int = 12_000) -> bytes:
    """Remove DC offset, normalize speech and add context padding for cloud STT."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return pcm
    mean = round(sum(samples) / len(samples))
    samples = array("h", (sample - mean for sample in samples))
    peak = max(abs(sample) for sample in samples)
    if peak == 0:
        return pcm
    gain = min(12.0, max(1.0, target_peak / peak))
    if gain > 1.0:
        samples = array("h", (
            max(-32_768, min(32_767, round(sample * gain))) for sample in samples
        ))
    padding = bytes(PCM_SAMPLE_RATE * PCM_SAMPLE_WIDTH // 4)  # 250 ms
    return padding + samples.tobytes() + padding


def normalize_wake_pcm(pcm: bytes, target_peak: int = 12_000) -> bytes:
    """Backward-compatible wake phrase normalization helper."""
    return normalize_speech_pcm(pcm, target_peak)


def _clean_text(value: Any) -> str:
    if isinstance(value, list):
        value = " ".join(str(part.get("text", "")) for part in value if isinstance(part, dict))
    text = str(value or "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^(bản ghi|transcript|transcription)\s*:\s*", "", text.strip(), flags=re.IGNORECASE)
    return text.strip().strip('"“”')


class OpenAIAPIError(RuntimeError):
    def __init__(self, operation: str, status_code: int, error_type: str,
                 error_code: str, message: str, request_id: str = "") -> None:
        super().__init__(
            f"OpenAI {operation} HTTP {status_code}: "
            f"{error_type or error_code or 'unknown'}: {message or 'request failed'}"
        )
        self.status_code = status_code
        self.error_type = error_type
        self.error_code = error_code
        self.request_id = request_id


def _openai_api_error(response: httpx.Response, operation: str) -> OpenAIAPIError:
    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        error = {}
    return OpenAIAPIError(
        operation=operation,
        status_code=response.status_code,
        error_type=str(error.get("type") or ""),
        error_code=str(error.get("code") or ""),
        message=str(error.get("message") or "request failed"),
        request_id=response.headers.get("x-request-id", ""),
    )


def is_openai_credit_exhausted(error: BaseException) -> bool:
    if not isinstance(error, OpenAIAPIError) or error.status_code != 429:
        return False
    machine_code = " ".join((error.error_type, error.error_code)).casefold()
    message = str(error).casefold()
    return "insufficient_quota" in machine_code or any(
        phrase in message for phrase in (
            "no credits remaining", "credit balance", "billing quota", "add credits",
        )
    )


def _tool_succeeded(result: Any) -> bool:
    return isinstance(result, dict) and bool(result) and not (
        result.get("isError") or result.get("error")
    )


def _tool_result_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    parts = result.get("content") or []
    if isinstance(parts, list):
        return " ".join(
            str(part.get("text") or "") for part in parts if isinstance(part, dict)
        ).strip()
    return str(result.get("text") or "").strip()


def _tool_result_object(result: Any) -> dict[str, Any]:
    text = _tool_result_text(result)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _wake_signature_text(text: str) -> str:
    value = unicodedata.normalize("NFKD", text.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("đ", "d")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def matches_device_wake_signature(english: str, vietnamese: str) -> bool:
    """Accept only strong bilingual evidence for device-specific STT misses.

    The paired variants below come from this ES3C28P microphone and the
    owner's pronunciation. Neither generic phrase is accepted on its own;
    both recognizers must produce a known pair for the same audio capture.
    """
    en = _wake_signature_text(english)
    vi = _wake_signature_text(vietnamese)
    english_variants = {"hey don", "hey dong", "hey dome", "hey tom"}
    vietnamese_variants = {
        "hay dom", "hey dom", "hay dong", "hey dong",
    }
    if en in english_variants and vi in vietnamese_variants:
        return True
    learned_pairs = {
        # Captured repeatedly on 2026-09-05 while the owner said "Hey Dom".
        ("how you doing", "huy dong"),
        ("how you doing", "hinh dong"),
        ("how you doing", "hanh dong"),
        ("how you doing", "hello"),
        ("are you down", "huy tam"),
        # Google vi/en results for a clean synthesized "Hey Dom" sample.
        ("hazel", "heyzo"),
    }
    return (en, vi) in learned_pairs


def split_wake_word(text: str) -> tuple[bool, str]:
    normalized_chars: list[str] = []
    original_end_offsets: list[int] = []
    for index, original in enumerate(text.casefold()):
        decomposed = unicodedata.normalize("NFKD", original)
        for char in decomposed:
            if not unicodedata.combining(char):
                normalized_chars.append("d" if char == "đ" else char)
                original_end_offsets.append(index + 1)
    normalized = "".join(normalized_chars)
    # Always-on listening must not treat generic greetings (Hello), "huy động"
    # or English pronouns (he does) as wake words. Keep brand variants narrow.
    # Match the full phrase first so "Hey Dom <command>" strips both words.
    patterns = (
        r"^\s*(?:hey|hay|hai|hei)\s+(?:dom|dome|don|dong)(?!['’][a-z])\b",
        r"^\s*(?:hey|dom)(?!['’][a-z])\b",
    )
    match = next((candidate for pattern in patterns
                  if (candidate := re.search(pattern, normalized))), None)
    if not match:
        return False, ""
    original_end = original_end_offsets[match.end() - 1]
    remainder = text[original_end:].lstrip(" ,.!?:;-–—")
    return True, remainder


def resolve_wake_transcripts(
    transcripts: list[tuple[str, str]], preferred_language: str,
) -> tuple[str, str, bool, str]:
    """Select a wake match while preserving the best diagnostic transcript."""
    for language, transcript in transcripts:
        matched, command = split_wake_word(transcript)
        if matched:
            return language, transcript, True, command

    by_language = dict(transcripts)
    if matches_device_wake_signature(
        by_language.get("en-US", ""), by_language.get(preferred_language, "")
    ):
        diagnostic = " / ".join(text for _, text in transcripts if text)
        return "bilingual-signature", diagnostic, True, ""

    diagnostic = next(((language, text) for language, text in transcripts if text), None)
    if diagnostic is not None:
        return diagnostic[0], diagnostic[1], False, ""
    if transcripts:
        return transcripts[0][0], "", False, ""
    return "", "", False, ""


class VoiceSession:
    def __init__(self, websocket: WebSocket, device_id: str, session_id: str) -> None:
        self.websocket = websocket
        self.device_id = device_id
        self.session_id = session_id
        self.state = "IDLE"
        self.local_vad = False  # Explicitly negotiated; old firmware keeps RMS VAD.
        self.send_lock = asyncio.Lock()
        self.pre_roll: deque[bytes] = deque(maxlen=VAD_PRE_ROLL_FRAMES)
        self.noise_samples: deque[int] = deque(maxlen=50)
        self.start_candidate_frames = 0
        self.capture_threshold = VAD_ENERGY_THRESHOLD
        self.audio = bytearray()
        self.speech_frames = 0
        self.silence_frames = 0
        self.silence_window: deque[bool] = deque(maxlen=12)
        self.speech_started = False
        self.speech_started_at = 0.0
        self.last_strong_voice_at = 0.0
        self.max_energy = 0
        self.speech_energy_total = 0
        self.pipeline_task: asyncio.Task[None] | None = None
        self.activation_timeout_task: asyncio.Task[None] | None = None
        self.pending_mcp: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.next_request_id = 1
        self.provider_retry_after: dict[str, float] = {}
        self.last_llm_provider = "pending"
        self.last_llm_model = "pending"

    def provider_ready(self, provider: str) -> bool:
        return time.monotonic() >= self.provider_retry_after.get(provider, 0.0)

    def defer_provider(self, provider: str) -> None:
        self.provider_retry_after[provider] = time.monotonic() + PROVIDER_RETRY_SECONDS

    async def send_json(self, message: dict[str, Any]) -> None:
        message.setdefault("session_id", self.session_id)
        async with self.send_lock:
            await self.websocket.send_text(json.dumps(message, ensure_ascii=False))

    async def send_bytes(self, data: bytes) -> None:
        async with self.send_lock:
            await self.websocket.send_bytes(data)

    def reset_capture(self) -> None:
        self.pre_roll.clear()
        self.audio.clear()
        self.speech_frames = 0
        self.silence_frames = 0
        self.silence_window.clear()
        self.speech_started = False
        self.speech_started_at = 0.0
        self.last_strong_voice_at = 0.0
        self.max_energy = 0
        self.speech_energy_total = 0
        self.start_candidate_frames = 0
        self.capture_threshold = VAD_ENERGY_THRESHOLD

    def vad_threshold(self) -> int:
        if len(self.noise_samples) < VAD_CALIBRATION_FRAMES:
            return VAD_ENERGY_THRESHOLD
        ordered = sorted(self.noise_samples)
        # A lower quartile remains stable when an occasional voice frame enters
        # the rolling noise sample.
        noise_floor = ordered[len(ordered) // 4]
        return max(
            VAD_ENERGY_THRESHOLD,
            round(noise_floor * VAD_NOISE_MULTIPLIER + VAD_NOISE_MARGIN),
        )

    async def set_wake_word(self, notify_board: bool = False) -> None:
        current = asyncio.current_task()
        if self.activation_timeout_task and self.activation_timeout_task is not current:
            self.activation_timeout_task.cancel()
        self.activation_timeout_task = None
        self.state = "WAKE_WORD"
        self.reset_capture()
        if notify_board:
            await self.send_json({"type": "listen", "state": "wake"})
        await self.send_json({"type": "llm", "emotion": "idle", "text": ""})

    async def set_listening(self, notify_board: bool = False, *, source: str | None = None) -> None:
        if self.activation_timeout_task:
            self.activation_timeout_task.cancel()
        self.state = "LISTENING"
        self.reset_capture()
        # A tap or accepted wake word is an explicit start-of-utterance signal.
        # Capture from this point instead of waiting for VAD onset, which can
        # otherwise lose quiet Vietnamese words at the beginning of a command.
        self.speech_started = True
        self.speech_started_at = time.monotonic()
        self.capture_threshold = VAD_ENERGY_THRESHOLD
        if notify_board:
            message = {"type": "listen", "state": "start"}
            if source is not None:
                message["source"] = source
            await self.send_json(message)
        await self.send_json({"type": "llm", "emotion": "listening", "text": "Mình đang nghe đây..."})
        self.activation_timeout_task = asyncio.create_task(
            self.expire_activation(), name=f"wake-timeout-{self.session_id}"
        )

    async def expire_activation(self) -> None:
        try:
            await asyncio.sleep(settings.VOICE_SESSION_TIMEOUT_SEC)
            if self.state == "LISTENING":
                logger.info("Listening activation expired device=%s", self.device_id)
                await self.set_wake_word(notify_board=True)
        except asyncio.CancelledError:
            pass

    async def consume_audio(self, pcm: bytes) -> None:
        if self.state not in {"WAKE_WORD", "LISTENING"} or self.pipeline_task is not None:
            return
        energy = pcm_signal_rms(pcm)
        self.max_energy = max(self.max_energy, energy)
        if not self.speech_started:
            self.pre_roll.append(pcm)
            if len(self.noise_samples) < VAD_CALIBRATION_FRAMES:
                self.noise_samples.append(energy)
                return
            threshold = min(
                self.vad_threshold(),
                WAKE_VAD_MAX_THRESHOLD
                if self.state == "WAKE_WORD"
                else LISTENING_VAD_MAX_THRESHOLD,
            )
            if energy < threshold:
                self.noise_samples.append(energy)
                self.start_candidate_frames = 0
                return
            self.start_candidate_frames += 1
            if self.start_candidate_frames < VAD_START_FRAMES:
                return
            self.speech_started = True
            self.speech_started_at = time.monotonic()
            self.last_strong_voice_at = self.speech_started_at
            self.audio.extend(b"".join(self.pre_roll))
            self.speech_frames = self.start_candidate_frames
            self.speech_energy_total = energy
            self.capture_threshold = threshold
            self.silence_frames = 0
            return
        self.audio.extend(pcm)
        # End the utterance against the calibrated room noise. A low fixed
        # release threshold made noisy rooms run every wake capture to 3 s.
        now = time.monotonic()
        release_threshold = max(VAD_ENERGY_THRESHOLD, round(self.capture_threshold * 0.9))
        # A calibrated room normally ends via the 9-frame silence window.
        # If startup/background noise remains above that release threshold,
        # still end after two seconds without a clearly voiced frame.
        strong_voice_threshold = max(
            self.capture_threshold,
            min(round(self.max_energy * 0.65), round(self.capture_threshold * 1.5)),
        )
        if energy >= strong_voice_threshold:
            self.last_strong_voice_at = now
        if energy >= release_threshold:
            self.speech_frames += 1
            self.speech_energy_total += energy
            self.silence_frames = 0
            self.silence_window.append(False)
        else:
            self.silence_frames += 1
            self.silence_window.append(True)
        frame_limit = WAKE_MAX_FRAMES if self.state == "WAKE_WORD" else VAD_MAX_FRAMES
        elapsed_limit = frame_limit * PCM_FRAME_MS / 1000
        enough_recent_silence = (
            len(self.silence_window) >= VAD_SILENCE_FRAMES
            and sum(self.silence_window) >= VAD_SILENCE_FRAMES
        )
        reached_hard_limit = (
            len(self.audio) >= frame_limit * PCM_FRAME_BYTES
            or now - self.speech_started_at >= elapsed_limit
        )
        reached_max_pause = (
            self.state == "LISTENING"
            and self.last_strong_voice_at > 0
            and now - self.last_strong_voice_at >= VAD_MAX_PAUSE_SEC
        )
        min_speech_frames = (
            WAKE_MIN_SPEECH_FRAMES if self.state == "WAKE_WORD" else VAD_MIN_SPEECH_FRAMES
        )
        if reached_hard_limit and self.speech_frames < min_speech_frames:
            logger.debug(
                "Discarding short VAD event device=%s speech_frames=%d required=%d",
                self.device_id, self.speech_frames, min_speech_frames,
            )
            self.reset_capture()
            return
        if (
            self.speech_frames >= min_speech_frames
            and (reached_hard_limit or (
                not (self.local_vad and self.state == "LISTENING")
                and (enough_recent_silence or reached_max_pause)
            ))
        ):
            await self.start_pipeline()

    async def start_pipeline(self) -> None:
        min_speech_frames = (
            WAKE_MIN_SPEECH_FRAMES if self.state == "WAKE_WORD" else VAD_MIN_SPEECH_FRAMES
        )
        # A negotiated neural VAD can detect quiet speech below the legacy RMS
        # floor. Do not discard its explicit stop solely on an energy counter.
        enough_speech = (len(self.audio) >= 3 * PCM_FRAME_BYTES
                         if self.local_vad and self.state == "LISTENING"
                         else self.speech_frames >= min_speech_frames)
        if self.pipeline_task is not None or not enough_speech:
            return
        pcm = bytes(self.audio)
        wake_check = self.state == "WAKE_WORD"
        if wake_check:
            logger.info(
                "Wake audio device=%s frames=%d speech_frames=%d peak_rms=%d avg_speech_rms=%d",
                self.device_id,
                len(pcm) // PCM_FRAME_BYTES,
                self.speech_frames,
                self.max_energy,
                self.speech_energy_total // max(1, self.speech_frames),
            )
        else:
            logger.info(
                "Command speech ended device=%s frames=%d speech_frames=%d peak_rms=%d",
                self.device_id,
                len(pcm) // PCM_FRAME_BYTES,
                self.speech_frames,
                self.max_energy,
            )
        if self.activation_timeout_task:
            self.activation_timeout_task.cancel()
            self.activation_timeout_task = None
        self.state = "PROCESSING"
        self.audio.clear()
        if not wake_check:
            await self.send_json({"type": "listen", "state": "processing"})
        coroutine = self.run_wake_check(pcm) if wake_check else self.run_pipeline(pcm)
        self.pipeline_task = asyncio.create_task(coroutine, name=f"voice-{self.session_id}")
        self.pipeline_task.add_done_callback(lambda _: setattr(self, "pipeline_task", None))

    async def abort(self) -> None:
        task = self.pipeline_task
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.send_json({"type": "tts", "state": "stop"})
        await self.set_wake_word(notify_board=True)

    async def transcribe_wake_google(
        self,
        pcm: bytes,
        *,
        timeout: float | None = None,
    ) -> list[tuple[str, str]]:
        """Bound both cloud requests; a slow secondary cannot delay activation.

        Prefer the configured language for Vietnamese command suffixes. Allow
        it a short grace period if English recognizes the wake phrase first.
        """
        provider_timeout = timeout or settings.WAKE_STT_TIMEOUT_SEC

        async def recognize(language: str) -> tuple[str, str]:
            try:
                text = await asyncio.wait_for(
                    self._transcribe_google(pcm, language, timeout=provider_timeout),
                    timeout=provider_timeout,
                )
                return language, text
            except Exception as exc:
                logger.debug("Wake STT unavailable language=%s: %s", language, exc)
                return language, ""

        languages = list(dict.fromkeys((settings.STT_LANGUAGE, "en-US")))
        pending = {asyncio.create_task(recognize(language)) for language in languages}
        transcripts: list[tuple[str, str]] = []
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                transcripts.extend(task.result() for task in done)
                matched = [item for item in transcripts if split_wake_word(item[1])[0]]
                if matched:
                    if pending and not any(lang == settings.STT_LANGUAGE for lang, _ in transcripts):
                        done, pending = await asyncio.wait(pending, timeout=0.15)
                        transcripts.extend(task.result() for task in done)
                    break
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        return sorted(transcripts, key=lambda item: item[0] != settings.STT_LANGUAGE)

    def should_use_wake_fallback(self) -> bool:
        """Avoid sending short background noises to paid cloud STT providers."""
        return (
            self.speech_frames >= settings.WAKE_STT_FALLBACK_MIN_SPEECH_FRAMES
            and self.max_energy >= settings.WAKE_STT_FALLBACK_MIN_PEAK_RMS
        )

    async def transcribe_wake_fallback(self, pcm: bytes) -> list[tuple[str, str]]:
        """Try independent cloud STT providers without failing the wake loop."""
        transcripts: list[tuple[str, str]] = []

        if (
            settings.WAKE_STT_OPENAI_FALLBACK
            and settings.OPENAI_API_KEY
            and self.provider_ready("openai-wake-stt")
        ):
            try:
                logger.info("Wake STT fallback device=%s provider=openai", self.device_id)
                transcript = await asyncio.wait_for(
                    self._transcribe_openai(pcm, settings.STT_LANGUAGE),
                    timeout=settings.WAKE_STT_TIMEOUT_SEC,
                )
                transcripts.append(("openai", transcript))
                if split_wake_word(transcript)[0]:
                    return transcripts
            except Exception as exc:
                self.defer_provider("openai-wake-stt")
                logger.warning(
                    "Wake STT fallback unavailable device=%s provider=openai: %s",
                    self.device_id, exc,
                )

        if settings.STT_OPENROUTER_FALLBACK and settings.OPENROUTER_API_KEY:
            try:
                logger.info("Wake STT fallback device=%s provider=openrouter", self.device_id)
                transcript = await asyncio.wait_for(
                    self._transcribe_openrouter(pcm),
                    timeout=settings.WAKE_STT_TIMEOUT_SEC,
                )
                transcripts.append(("openrouter-audio", transcript))
            except Exception as exc:
                logger.warning(
                    "Wake STT fallback unavailable device=%s provider=openrouter: %s",
                    self.device_id, exc,
                )
        return transcripts

    async def run_wake_check(self, pcm: bytes) -> None:
        started = time.monotonic()
        try:
            wake_pcm = normalize_wake_pcm(pcm)
            wake_provider = primary_wake_stt_provider()
            if wake_provider == "google-web":
                transcripts = await self.transcribe_wake_google(wake_pcm)
            else:
                transcripts = [(
                    settings.STT_LANGUAGE,
                    await self.transcribe(
                        pcm,
                        timeout=effective_wake_stt_timeout(),
                    ),
                )]

            language, transcript, matched, command = resolve_wake_transcripts(
                transcripts, settings.STT_LANGUAGE
            )
            if (
                not matched
                and wake_provider == "google-web"
                and self.should_use_wake_fallback()
            ):
                transcripts.extend(await self.transcribe_wake_fallback(wake_pcm))
                language, transcript, matched, command = resolve_wake_transcripts(
                    transcripts, settings.STT_LANGUAGE
                )
            if not matched:
                logger.info(
                    "Wake phrase rejected device=%s transcripts=%r",
                    self.device_id, transcripts,
                )
                await self.set_wake_word()
                return
            logger.info(
                "Wake phrase accepted device=%s language=%s transcript=%r candidates=%r stt_ms=%d",
                self.device_id, language, transcript, transcripts, round((time.monotonic() - started) * 1000),
            )
            if command:
                await self.send_json({"type": "listen", "state": "processing", "source": "wake_word"})
                await self.process_transcript(command)
                await self.set_wake_word(notify_board=True)
            else:
                await self.set_listening(notify_board=True, source="wake_word")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Wake-word check failed device=%s: %s", self.device_id, exc)
            with contextlib.suppress(Exception):
                await self.set_wake_word()

    async def run_pipeline(self, pcm: bytes) -> None:
        try:
            await self.send_json({"type": "llm", "emotion": "thinking", "text": "Đang nhận diện..."})
            transcript = await self.transcribe(pcm)
            if not transcript:
                logger.info("VAD event contained no recognizable speech device=%s", self.device_id)
                await self.send_json({
                    "type": "llm",
                    "emotion": "sad",
                    "text": NO_SPEECH_RESPONSE,
                })
                await self.speak(NO_SPEECH_RESPONSE)
                return
            await self.process_transcript(transcript)
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(Exception):
                await self.set_wake_word(notify_board=True)

    async def process_transcript(self, transcript: str) -> None:
        turn_id = ""
        speech_queue: asyncio.Queue[str | None] = asyncio.Queue()
        chunker = PunctuationChunker()
        streamed_chunks = 0

        async def queue_delta(delta: str) -> None:
            nonlocal streamed_chunks
            for raw_chunk in chunker.feed(delta):
                chunk = normalize_tts_text(raw_chunk)
                if chunk:
                    streamed_chunks += 1
                    await speech_queue.put(chunk)

        speech_task = asyncio.create_task(
            self.speak_stream(speech_queue), name=f"tts-stream-{self.session_id}"
        )
        try:
            logger.info("STT device=%s text=%s", self.device_id, transcript)
            await self.send_json({"type": "stt", "text": transcript})
            history = await conversation_store.recent_context(self.device_id)
            turn_id = await conversation_store.create_turn(
                self.device_id, transcript, "pending", "pending"
            )
            answer = await self.chat(history, transcript, turn_id, on_text_delta=queue_delta)
            tail = normalize_tts_text(chunker.flush())
            if tail:
                streamed_chunks += 1
                await speech_queue.put(tail)
            await speech_queue.put(None)
            answer = normalize_tts_text(answer)
            await conversation_store.update_execution(
                turn_id, self.last_llm_provider, self.last_llm_model
            )
            await conversation_store.complete_turn(turn_id, answer)
            await self.send_json({"type": "llm", "emotion": "happy", "text": answer})
            streamed = await speech_task
            if not streamed or streamed_chunks == 0:
                await self.speak(answer)
        except asyncio.CancelledError:
            speech_task.cancel()
            await asyncio.gather(speech_task, return_exceptions=True)
            raise
        except Exception as exc:
            if not speech_task.done():
                await speech_queue.put(None)
            await asyncio.gather(speech_task, return_exceptions=True)
            logger.exception("Voice pipeline failed device=%s", self.device_id)
            message = normalize_tts_text("Xin lỗi, Dom chưa xử lý được yêu cầu này. Bạn thử lại nhé.")
            if turn_id:
                await conversation_store.complete_turn(turn_id, message)
            with contextlib.suppress(Exception):
                await self.send_json({"type": "llm", "emotion": "sad", "text": message})
                await self.speak(message)
            logger.warning("Pipeline reason: %s", exc)

    async def _openrouter(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not settings.OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        headers = {
            "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": settings.OPENROUTER_HTTP_REFERER,
            "X-Title": "DomOS",
        }
        async with httpx.AsyncClient(timeout=settings.OPENROUTER_TIMEOUT_SEC) as client:
            response = await client.post(
                f"{settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
        if response.is_error:
            try:
                error = response.json().get("error") or {}
                detail = f"{error.get('code', 'unknown')}: {error.get('message', 'request failed')}"
            except (ValueError, AttributeError):
                detail = "request failed"
            raise RuntimeError(f"OpenRouter HTTP {response.status_code}: {detail}")
        return response.json()

    async def _openai(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        headers = {
            "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=settings.OPENAI_TIMEOUT_SEC) as client:
            response = await client.post(
                f"{settings.OPENAI_BASE_URL.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
        if response.is_error:
            raise _openai_api_error(response, "chat")
        return response.json()

    async def _stream_completion(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
        provider: str,
        on_text_delta: Callable[[str], Awaitable[None]],
    ) -> dict[str, Any]:
        """Reassemble an OpenAI-compatible SSE stream, including tool calls."""
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", url, headers=headers, json={**payload, "stream": True}
            ) as response:
                if response.is_error:
                    await response.aread()
                    if provider == "openai":
                        raise _openai_api_error(response, "chat")
                    try:
                        error = response.json().get("error") or {}
                        detail = f"{error.get('code', 'unknown')}: {error.get('message', 'request failed')}"
                    except (ValueError, AttributeError):
                        detail = "request failed"
                    raise RuntimeError(f"OpenRouter HTTP {response.status_code}: {detail}")

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        logger.debug("Ignoring malformed %s SSE event", provider)
                        continue
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or {}
                    text_delta = delta.get("content")
                    if isinstance(text_delta, str) and text_delta:
                        content_parts.append(text_delta)
                        await on_text_delta(text_delta)
                    for raw_call in delta.get("tool_calls") or []:
                        index = int(raw_call.get("index", 0))
                        call = tool_calls.setdefault(index, {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if raw_call.get("id"):
                            call["id"] += str(raw_call["id"])
                        if raw_call.get("type"):
                            call["type"] = raw_call["type"]
                        function = raw_call.get("function") or {}
                        call["function"]["name"] += str(function.get("name") or "")
                        call["function"]["arguments"] += str(function.get("arguments") or "")

        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if tool_calls:
            message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
        return {"choices": [{"message": message, "finish_reason": finish_reason}]}

    async def _openai_stream(
        self, payload: dict[str, Any], on_text_delta: Callable[[str], Awaitable[None]]
    ) -> dict[str, Any]:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        return await self._stream_completion(
            url=f"{settings.OPENAI_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout=settings.OPENAI_TIMEOUT_SEC,
            provider="openai",
            on_text_delta=on_text_delta,
        )

    async def _openrouter_stream(
        self, payload: dict[str, Any], on_text_delta: Callable[[str], Awaitable[None]]
    ) -> dict[str, Any]:
        if not settings.OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        return await self._stream_completion(
            url=f"{settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": settings.OPENROUTER_HTTP_REFERER,
                "X-Title": "DomOS",
            },
            payload=payload,
            timeout=settings.OPENROUTER_TIMEOUT_SEC,
            provider="openrouter",
            on_text_delta=on_text_delta,
        )

    async def _llm(
        self,
        payload: dict[str, Any],
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        order = [name.strip().lower() for name in settings.LLM_PROVIDER_ORDER.split(",")]
        openai_configured = "openai" in order and bool(settings.OPENAI_API_KEY)
        quota_exhausted = openai_configured and not self.provider_ready("openai-llm-quota")
        if openai_configured and not quota_exhausted:
            try:
                request = {**payload, "model": settings.OPENAI_MODEL}
                response = (
                    await self._openai_stream(request, on_text_delta)
                    if on_text_delta is not None and settings.LLM_STREAMING_ENABLED
                    else await self._openai(request)
                )
                self.last_llm_provider, self.last_llm_model = "openai", settings.OPENAI_MODEL
                return response
            except Exception as exc:
                if not is_openai_credit_exhausted(exc):
                    # Rate limits, authentication, timeouts and server/network
                    # errors are not proof that account credit is exhausted.
                    raise
                quota_exhausted = True
                self.defer_provider("openai-llm-quota")
                logger.warning(
                    "OpenAI LLM quota exhausted; using OpenRouter for %ds: %s",
                    PROVIDER_RETRY_SECONDS, exc,
                )
        if (quota_exhausted or not openai_configured) and "openrouter" in order:
            if not settings.OPENROUTER_API_KEY:
                raise RuntimeError("OPENROUTER_API_KEY is not configured")
            request = {**payload, "model": settings.OPENROUTER_MODEL}
            response = (
                await self._openrouter_stream(request, on_text_delta)
                if on_text_delta is not None and settings.LLM_STREAMING_ENABLED
                else await self._openrouter(request)
            )
            self.last_llm_provider, self.last_llm_model = "openrouter", settings.OPENROUTER_MODEL
            return response
        raise RuntimeError("No configured LLM provider is available")

    async def transcribe(
        self,
        pcm: bytes,
        language: str | None = None,
        *,
        timeout: float | None = None,
    ) -> str:
        """Transcribe with the configured provider first and bounded fallbacks.

        Northflank uses OpenAI as the primary provider. An empty transcript is
        treated as a miss, not a success, so Google can still recover the turn.
        OpenRouter Audio remains opt-in unless it is explicitly the primary.
        """
        prepared = normalize_speech_pcm(pcm)
        selected_language = language or settings.STT_LANGUAGE
        primary = primary_stt_provider()
        openai_cooldown_key = "openai-wake-stt" if timeout is not None else "openai-stt"
        if primary == "openai":
            providers = ["openai", "google-web", "openrouter"]
        elif primary == "google-web":
            providers = ["google-web", "openai"]
        else:
            providers = ["openrouter", "openai", "google-web"]
        if settings.STT_OPENROUTER_FALLBACK and "openrouter" not in providers:
            providers.append("openrouter")

        for provider in providers:
            if provider == "openai" and (
                not settings.OPENAI_API_KEY or not self.provider_ready(openai_cooldown_key)
            ):
                continue
            if provider == "openrouter" and (
                not settings.OPENROUTER_API_KEY
                or not self.provider_ready("openrouter-stt")
                or (
                    primary != "openrouter"
                    and not settings.STT_OPENROUTER_FALLBACK
                    and self.provider_ready("openai-stt-quota")
                )
            ):
                continue

            started = time.monotonic()
            try:
                if provider == "openai":
                    request = self._transcribe_openai(prepared, selected_language)
                elif provider == "google-web":
                    if timeout is None:
                        request = self._transcribe_google(prepared, selected_language)
                    else:
                        async def recognize_wake_bilingually() -> str:
                            candidates = await self.transcribe_wake_google(
                                prepared,
                                timeout=timeout,
                            )
                            matched_language, matched_text, matched, _ = resolve_wake_transcripts(
                                candidates,
                                selected_language,
                            )
                            if matched:
                                # Preserve direct transcripts (including a
                                # command suffix). A learned bilingual pair is
                                # canonicalized because wrapping it in one
                                # transcript would otherwise lose the pairing.
                                return "Hey Dom" if matched_language == "bilingual-signature" else matched_text
                            # A non-wake diagnostic such as "noise" must not
                            # block the next quota-approved fallback provider.
                            return ""

                        request = recognize_wake_bilingually()
                else:
                    request = self._transcribe_openrouter(prepared)
                transcript = (
                    await asyncio.wait_for(request, timeout=timeout)
                    if timeout is not None and provider != "google-web"
                    else await request
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if provider == "openai":
                    self.defer_provider(openai_cooldown_key)
                    if is_openai_credit_exhausted(exc):
                        self.defer_provider("openai-stt-quota")
                        logger.warning(
                            "OpenAI STT quota exhausted; enabling OpenRouter Audio fallback for %ds",
                            PROVIDER_RETRY_SECONDS,
                        )
                elif provider == "openrouter":
                    self.defer_provider("openrouter-stt")
                detail = str(exc) or type(exc).__name__
                logger.warning(
                    "STT provider failed device=%s provider=%s: %s",
                    self.device_id, provider, detail,
                )
                continue

            elapsed_ms = round((time.monotonic() - started) * 1000)
            raw_transcript = transcript
            transcript = filter_asr_transcript(transcript)
            if raw_transcript and not transcript:
                logger.info(
                    "ASR hallucination dropped device=%s provider=%s text=%r",
                    self.device_id, provider, raw_transcript,
                )
            if transcript:
                logger.info(
                    "STT provider selected device=%s provider=%s latency_ms=%d",
                    self.device_id, provider, elapsed_ms,
                )
                return transcript
            logger.info(
                "STT provider returned empty device=%s provider=%s latency_ms=%d",
                self.device_id, provider, elapsed_ms,
            )
        return ""

    async def _transcribe_google(self, pcm: bytes, language: str, *, timeout: float | None = None) -> str:
        audio = sr.AudioData(pcm, PCM_SAMPLE_RATE, PCM_SAMPLE_WIDTH)
        recognizer = sr.Recognizer()
        if timeout is not None:
            recognizer.operation_timeout = timeout
        try:
            return filter_asr_transcript(_clean_text(await asyncio.to_thread(
                recognizer.recognize_google,
                audio,
                language=language,
            )))
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as exc:
            raise RuntimeError(f"Google STT unavailable: {exc}") from exc

    async def _transcribe_openai(self, pcm: bytes, language: str) -> str:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        # The Audio API expects ISO-639-1 language codes rather than locales.
        language_code = language.split("-", 1)[0].lower()
        headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}
        files = {"file": ("speech.wav", pcm_to_wav(pcm), "audio/wav")}
        data = {
            "model": settings.OPENAI_STT_MODEL,
            "language": language_code,
            "response_format": "json",
            "prompt": "Câu nói có thể bắt đầu bằng wake word 'Hey Dom', sau đó là lệnh tiếng Việt.",
        }
        async with httpx.AsyncClient(timeout=settings.OPENAI_TIMEOUT_SEC) as client:
            response = await client.post(
                f"{settings.OPENAI_BASE_URL.rstrip('/')}/audio/transcriptions",
                headers=headers,
                files=files,
                data=data,
            )
        if response.is_error:
            raise _openai_api_error(response, "STT")
        return filter_asr_transcript(_clean_text(response.json().get("text")))

    async def _transcribe_openrouter(self, pcm: bytes) -> str:
        audio = base64.b64encode(pcm_to_wav(pcm)).decode("ascii")
        response = await self._openrouter({
            "model": settings.OPENROUTER_AUDIO_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Chép lại chính xác lời nói trong âm thanh. Ưu tiên tiếng Việt. Chỉ trả về nguyên văn, không giải thích."},
                {"type": "input_audio", "input_audio": {"data": audio, "format": "wav"}},
            ]}],
            "temperature": 0,
        })
        choices = response.get("choices") or []
        return filter_asr_transcript(
            _clean_text(choices[0].get("message", {}).get("content"))
        ) if choices else ""

    async def chat(
        self,
        history: list[dict[str, str]],
        transcript: str,
        turn_id: str,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        direct = await self.try_direct_command(transcript, turn_id)
        if direct:
            self.last_llm_provider, self.last_llm_model = "device", "deterministic-command-router"
            return direct
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt()},
            *[
                {**item, "content": plain_speech_text(item.get("content", ""))}
                for item in history
            ],
            {"role": "user", "content": transcript},
        ]
        force_search = needs_web_search(transcript)
        for round_index in range(3):
            payload = {
                "messages": messages,
                "tools": TOOLS,
                "tool_choice": (
                    {"type": "function", "function": {"name": "web_search"}}
                    if force_search and round_index == 0 else "auto"
                ),
                "temperature": 0.3,
            }
            response = (
                await self._llm(payload, on_text_delta=on_text_delta)
                if on_text_delta is not None else await self._llm(payload)
            )
            choices = response.get("choices") or []
            if not choices:
                raise RuntimeError("LLM returned no answer")
            message = choices[0].get("message") or {}
            calls = message.get("tool_calls") or []
            if not calls:
                answer = plain_speech_text(message.get("content"))
                if claims_app_launch(answer):
                    logger.warning("Suppressed app-launch confirmation without a device tool result")
                    return APP_LAUNCH_FAILED
                if answer:
                    return answer
                raise RuntimeError("LLM returned an empty answer")
            messages.append({
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": calls,
            })
            app_answer = None
            for call in calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                except (json.JSONDecodeError, ValueError) as exc:
                    arguments = {}
                    result: Any = {"error": str(exc)}
                    status = "error"
                    duration_ms = 0
                else:
                    started = time.monotonic()
                    try:
                        result = (
                            await web_search_service.search(str(arguments.get("query") or transcript))
                            if name == "web_search"
                            else await self.call_device_tool(name, arguments)
                        )
                        status = "success" if _tool_succeeded(result) else "error"
                    except Exception as exc:
                        result = {"error": str(exc)}
                        status = "error"
                    duration_ms = round((time.monotonic() - started) * 1000)
                await conversation_store.add_tool_trace(
                    turn_id, name, arguments, result, duration_ms, status
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps(result, ensure_ascii=False),
                })
                if name == "app.launch":
                    app_answer = (
                        APP_CONFIRMATIONS.get(arguments.get("app"), APP_LAUNCH_FAILED)
                        if status == "success" else APP_LAUNCH_FAILED
                    )
            # Opening an app is a terminal UI action. Its confirmation comes
            # from the MCP result, not another model-generated success claim.
            if app_answer is not None:
                return app_answer
        raise RuntimeError("Too many tool-call rounds")

    async def try_direct_command(self, transcript: str, turn_id: str) -> str | None:
        """Execute unambiguous Vietnamese controls deterministically.

        This is command parsing, not local AI inference. It keeps hardware
        controls reliable when the free router selects a model with weak tool
        calling support.
        """
        text = fold_vietnamese(transcript)
        name = ""
        arguments: dict[str, Any] = {}
        success_text = ""
        response_kind = ""
        number_match = re.search(r"\b(100|[1-9]?\d)\b", text)
        number = int(number_match.group(1)) if number_match else None

        app = requested_app(transcript)
        if app is not None:
            name, arguments = "app.launch", {"app": app}
            success_text = APP_CONFIRMATIONS[app]
        elif is_unresolved_launch_request(transcript):
            return APP_COMMAND_HELP
        elif re.search(r"\b(may|bao nhieu) gio\b", text):
            current = assistant_now()
            return f"Bây giờ là {current.hour} giờ {current.minute:02d} phút."
        elif re.search(r"\b(ngay may|hom nay ngay|thu may)\b", text):
            current = assistant_now()
            return f"Hôm nay là ngày {current.day} tháng {current.month} năm {current.year}."
        elif "man hinh" in text and re.search(r"\b(tat|dong)\b", text):
            name, arguments = "display.set_brightness", {"brightness": 0}
            success_text = "Màn hình đã tắt."
        elif "man hinh" in text and re.search(r"\b(bat|mo)\b", text):
            name, arguments = "display.set_brightness", {"brightness": number or 80}
            success_text = "Màn hình đã bật."
        elif "do sang" in text or "man hinh" in text:
            if "tang" in text:
                delta = number or 10
            elif "giam" in text:
                delta = -(number or 10)
            elif number is not None:
                name, arguments = "display.set_brightness", {"brightness": number}
                success_text = f"Độ sáng đã được đặt ở {number}."
                delta = 0
            else:
                return None
            if not name:
                name, arguments = "display.adjust_brightness", {"delta": delta}
                success_text = f"Độ sáng đã {'tăng' if delta > 0 else 'giảm'} {abs(delta)}."
                response_kind = "brightness"
        elif ("am luong" in text or "loa" in text) and re.search(r"\b(tat|mute)\b", text):
            name, arguments = "speaker.set_volume", {"volume": 0}
            success_text = "Loa đã tắt."
        elif "am luong" in text or "loa" in text:
            if "tang" in text:
                delta = number or 10
                name, arguments = "speaker.adjust_volume", {"delta": delta}
                success_text = f"Âm lượng đã tăng {delta}."
                response_kind = "volume"
            elif "giam" in text:
                delta = -(number or 10)
                name, arguments = "speaker.adjust_volume", {"delta": delta}
                success_text = f"Âm lượng đã giảm {abs(delta)}."
                response_kind = "volume"
            elif number is not None:
                name, arguments = "speaker.set_volume", {"volume": number}
                success_text = f"Âm lượng đã được đặt ở {number}."
            else:
                return None
        elif "pin" in text:
            name, arguments = "device.get_status", {}
            response_kind = "battery"
        elif "wifi" in text or "mang" in text:
            name, arguments = "device.get_status", {}
            response_kind = "wifi"
        elif "trang thai" in text and ("thiet bi" in text or "dom" in text):
            name, arguments = "device.get_status", {}
            response_kind = "status"
        else:
            return None

        started = time.monotonic()
        try:
            result = await self.call_device_tool(name, arguments)
            status = "success" if _tool_succeeded(result) else "error"
        except Exception as exc:
            result = {"error": str(exc), "isError": True}
            status = "error"
        duration_ms = round((time.monotonic() - started) * 1000)
        await conversation_store.add_tool_trace(
            turn_id, name, arguments, result, duration_ms, status
        )
        if status == "error":
            return "Dom chưa điều khiển được thiết bị. Bạn thử lại nhé."
        result_text = _tool_result_text(result)
        status_data = _tool_result_object(result)
        if response_kind == "volume":
            match = re.search(r"volume set to (\d+)", result_text, flags=re.IGNORECASE)
            if match:
                return f"Âm lượng hiện tại là {match.group(1)}."
        if response_kind == "brightness":
            match = re.search(r"brightness set to (\d+)", result_text, flags=re.IGNORECASE)
            if match:
                return f"Độ sáng hiện tại là {match.group(1)}."
        if response_kind == "battery":
            battery = status_data.get("battery") or status_data.get("battery_percent")
            return (
                f"Pin còn {battery} phần trăm."
                if battery is not None else "Thiết bị chưa cung cấp thông tin mức pin."
            )
        if response_kind == "wifi":
            return "Kết nối oai-phai và máy chủ đang hoạt động."
        if response_kind == "status":
            volume = status_data.get("volume")
            brightness = status_data.get("brightness")
            if volume is not None and brightness is not None:
                return f"Thiết bị đang hoạt động, âm lượng {volume}, độ sáng {brightness}."
            return "Thiết bị đang hoạt động bình thường."
        return success_text

    async def call_device_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_request_id
        self.next_request_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending_mcp[request_id] = future
        try:
            await self.send_json({
                "type": "mcp",
                "payload": {
                    "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            })
            return await asyncio.wait_for(future, timeout=5)
        finally:
            self.pending_mcp.pop(request_id, None)

    def resolve_mcp(self, payload: dict[str, Any]) -> None:
        request_id = payload.get("id")
        future = self.pending_mcp.get(request_id)
        if future and not future.done():
            future.set_result(payload.get("result") or {"error": payload.get("error"), "isError": True})

    async def _synthesize_sentence_pcm(self, sentence: str) -> tuple[str, bytes] | None:
        sentence = normalize_tts_text(sentence)
        if not sentence:
            return None
        if settings.TTS_PROVIDER == "google":
            mp3 = await asyncio.to_thread(_google_synthesize, sentence)
        else:
            try:
                mp3 = await asyncio.wait_for(
                    _edge_synthesize(sentence), timeout=settings.TTS_TIMEOUT_SEC
                )
            except (asyncio.TimeoutError, OSError, edge_tts.exceptions.NoAudioReceived):
                logger.warning("Edge TTS unavailable; using Google TTS fallback")
                mp3 = await asyncio.to_thread(_google_synthesize, sentence)
        pcm = await asyncio.to_thread(_decode_mp3, mp3)
        return sentence, pcm

    async def _send_synthesized_sentence(
        self,
        sentence: str,
        pcm: bytes,
        previous_playback_deadline: float | None = None,
    ) -> float:
        await self.send_json({"type": "tts", "state": "sentence_start", "text": sentence})
        frame_count = max(1, math.ceil(len(pcm) / PCM_FRAME_BYTES))
        stream_started_at = time.monotonic()
        # Only the first clause (or a clause after a real synthesis gap) needs
        # a burst. Consecutive clauses already have audio queued; bursting each
        # one would eventually overflow the finite ESP32 queue.
        playback_starts_at = max(previous_playback_deadline or 0.0, stream_started_at)
        for frame_index, offset in enumerate(range(0, len(pcm), PCM_FRAME_BYTES)):
            # Prime the device queue in a short burst, then pace against a
            # monotonic clock.  A deadline-based loop does not accumulate the
            # event-loop drift that repeated sleep(0.06) calls introduce.
            send_at = playback_starts_at + (
                frame_index - TTS_JITTER_BUFFER_FRAMES + 1
            ) * PCM_FRAME_MS / 1000
            delay = send_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            frame = pcm[offset: offset + PCM_FRAME_BYTES]
            if len(frame) < PCM_FRAME_BYTES:
                frame += bytes(PCM_FRAME_BYTES - len(frame))
            await self.send_bytes(frame)

        return playback_starts_at + frame_count * PCM_FRAME_MS / 1000

    async def _wait_for_tts_tail(self, playback_deadline: float | None) -> None:
        if playback_deadline is None:
            return
        # Wait only after the final clause. Waiting after every clause drains
        # the jitter queue and inserts an audible hole between sentences.
        stop_deadline = playback_deadline + TTS_STOP_GRACE_FRAMES * PCM_FRAME_MS / 1000
        delay = stop_deadline - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _speak_sentence(
        self, sentence: str, previous_playback_deadline: float | None = None
    ) -> float | None:
        synthesized = await self._synthesize_sentence_pcm(sentence)
        if synthesized is not None:
            return await self._send_synthesized_sentence(
                *synthesized, previous_playback_deadline
            )
        return None

    async def speak_stream(self, queue: asyncio.Queue[str | None]) -> bool:
        """Synthesize the next clause while the current clause is playing."""
        ready: asyncio.Queue[tuple[str, bytes] | Exception | None] = asyncio.Queue(maxsize=2)

        async def synthesize_ahead() -> None:
            try:
                while True:
                    sentence = await queue.get()
                    if sentence is None:
                        await ready.put(None)
                        return
                    audio = await self._synthesize_sentence_pcm(sentence)
                    if audio is not None:
                        await ready.put(audio)
            except Exception as exc:
                await ready.put(exc)

        producer = asyncio.create_task(synthesize_ahead(), name=f"tts-producer-{self.session_id}")
        started = False
        try:
            playback_deadline: float | None = None
            while True:
                synthesized = await ready.get()
                if synthesized is None:
                    break
                if isinstance(synthesized, Exception):
                    raise synthesized
                if not started:
                    self.state = "SPEAKING"
                    await self.send_json({"type": "tts", "state": "start"})
                    started = True
                playback_deadline = await self._send_synthesized_sentence(
                    *synthesized, playback_deadline
                )
            await self._wait_for_tts_tail(playback_deadline)
            return started
        finally:
            # Covers cancellation before the first PCM, provider errors and
            # disconnects as well as normal playback. No orphan producer can
            # continue generating a cancelled answer into an abandoned queue.
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            if started:
                await self.send_json({"type": "tts", "state": "stop"})

    async def speak(self, text: str) -> None:
        normalized = normalize_tts_text(text)
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        for sentence in re.split(r"(?<=[.!?])\s+|(?<=[。！？])", normalized):
            if sentence.strip():
                queue.put_nowait(sentence.strip())
        queue.put_nowait(None)
        await self.speak_stream(queue)


async def _edge_synthesize(text: str) -> bytes:
    mp3 = bytearray()
    async for chunk in edge_tts.Communicate(text, settings.TTS_VOICE).stream():
        if chunk["type"] == "audio":
            mp3.extend(chunk["data"])
    if not mp3:
        raise edge_tts.exceptions.NoAudioReceived("No audio received")
    return bytes(mp3)


def _google_synthesize(text: str) -> bytes:
    output = io.BytesIO()
    gTTS(text=text, lang="vi", timeout=settings.TTS_TIMEOUT_SEC).write_to_fp(output)
    return output.getvalue()


def _decode_mp3(mp3: bytes) -> bytes:
    output = bytearray()
    with av.open(io.BytesIO(mp3), mode="r") as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=PCM_SAMPLE_RATE)
        for decoded in container.decode(audio=0):
            frames = resampler.resample(decoded) or []
            if not isinstance(frames, list):
                frames = [frames]
            for frame in frames:
                output.extend(bytes(frame.planes[0])[: frame.samples * PCM_SAMPLE_WIDTH])
        # Resampling holds a filter tail; omitting flush loses final samples.
        for frame in resampler.resample(None) or []:
            output.extend(bytes(frame.planes[0])[: frame.samples * PCM_SAMPLE_WIDTH])
    return bytes(output)


async def handle_openrouter_voice(websocket: WebSocket) -> None:
    await websocket.accept()
    authorization = websocket.headers.get("authorization", "")
    if settings.VOICE_AUTH_TOKEN and authorization != f"Bearer {settings.VOICE_AUTH_TOKEN}":
        await websocket.close(code=1008, reason="Unauthorized")
        return
    device_id = websocket.headers.get("device-id", "ES3C28P")
    session_id = str(uuid.uuid4())
    session = VoiceSession(websocket, device_id, session_id)
    heartbeat_task: asyncio.Task[None] | None = None
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        hello = json.loads(raw)
        validate_dom_hello(hello)
        features = hello.get("features")
        session.local_vad = isinstance(features, dict) and features.get("local_vad") is True
        await voice_registry.add(session)
        await session.send_json({
            "type": "hello", "provider": primary_llm_provider(), "transport": "websocket",
            "audio_params": {"codec": "pcm", "sample_rate": 16_000, "channels": 1, "frame_duration": 60},
            "features": {"mcp": True, "vad": True, "local_vad": session.local_vad,
                         "emotions": True, "tts_streaming": True},
        })
        await session.set_wake_word()
        heartbeat_task = asyncio.create_task(
            voice_heartbeat_loop(session), name=f"voice-heartbeat-{session_id}"
        )
        logger.info("Cloud voice connected device=%s provider=%s session=%s", device_id, primary_llm_provider(), session_id)
        while True:
            receive_task = asyncio.create_task(websocket.receive())
            done, _ = await asyncio.wait(
                (receive_task, heartbeat_task), return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat_task in done:
                receive_task.cancel()
                await asyncio.gather(receive_task, return_exceptions=True)
                await heartbeat_task
                raise RuntimeError("Voice heartbeat task stopped unexpectedly")
            raw_message = receive_task.result()
            if raw_message.get("type") == "websocket.disconnect":
                break
            if raw_message.get("bytes") is not None:
                await session.consume_audio(raw_message["bytes"])
                continue
            text = raw_message.get("text")
            if not text:
                continue
            message = json.loads(text)
            message_type = message.get("type")
            if message_type == "listen" and message.get("state") == "start":
                if session.pipeline_task is None:
                    if message.get("mode") == "wake":
                        await session.set_wake_word()
                    else:
                        await session.set_listening()
            elif message_type == "listen" and message.get("state") == "stop":
                await session.start_pipeline()
            elif message_type == "abort":
                await session.abort()
            elif message_type == "mcp" and isinstance(message.get("payload"), dict):
                session.resolve_mcp(message["payload"])
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Voice protocol error device=%s: %s", device_id, exc)
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=1002, reason=str(exc)[:120])
    except Exception:
        logger.exception("Voice session failed device=%s", device_id)
    finally:
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if session.pipeline_task and not session.pipeline_task.done():
            session.pipeline_task.cancel()
        if session.activation_timeout_task and not session.activation_timeout_task.done():
            session.activation_timeout_task.cancel()
        for future in session.pending_mcp.values():
            if not future.done():
                future.cancel()
        await voice_registry.remove(session_id)
        logger.info("Cloud voice disconnected device=%s", device_id)


async def voice_heartbeat_loop(
    session: VoiceSession,
    interval_sec: float | None = None,
) -> None:
    """Send application heartbeats without cancelling the ASGI receive call.

    Incoming PCM is continuous while wake detection is armed, so heartbeat
    scheduling must be independent of the receive loop. A failed write bubbles
    into the session handler and removes the stale registry entry immediately.
    """
    interval = interval_sec if interval_sec is not None else settings.VOICE_HEARTBEAT_INTERVAL_SEC
    interval = max(float(interval), 0.01)
    while True:
        await asyncio.sleep(interval)
        await session.send_json({"type": "ping", "timestamp": int(time.time())})
