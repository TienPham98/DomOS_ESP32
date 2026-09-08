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
from typing import Any

import av
import edge_tts
import httpx
import speech_recognition as sr
from fastapi import WebSocket, WebSocketDisconnect
from gtts import gTTS

from config import settings
from services.app_commands import (
    APP_COMMAND_HELP,
    APP_CONFIRMATIONS,
    APP_LAUNCH_FAILED,
    claims_app_launch,
    is_unresolved_launch_request,
    requested_app,
)
from services.conversation_store import ConversationStore
from services.text_normalization import plain_speech_text

logger = logging.getLogger("domos.openrouter")

PCM_SAMPLE_RATE = 16_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2
PCM_FRAME_MS = 60
PCM_FRAME_BYTES = PCM_SAMPLE_RATE * PCM_FRAME_MS // 1000 * PCM_SAMPLE_WIDTH
VAD_ENERGY_THRESHOLD = 180
VAD_SILENCE_FRAMES = 9
VAD_MIN_SPEECH_FRAMES = 3
VAD_MAX_FRAMES = 20_000 // PCM_FRAME_MS
WAKE_MAX_FRAMES = 3_000 // PCM_FRAME_MS
VAD_CALIBRATION_FRAMES = 5
VAD_START_FRAMES = 2
VAD_NOISE_MULTIPLIER = 1.8
VAD_NOISE_MARGIN = 40
# Single-word "Hey" / "Dom" can be shorter than the old 300 ms minimum.
WAKE_MIN_SPEECH_FRAMES = 3
PROVIDER_RETRY_SECONDS = 300
NO_SPEECH_RESPONSE = "Mình chưa nghe rõ. Bạn nói lại giúp mình nhé."

SYSTEM_PROMPT = """Bạn là Dom, trợ lý giọng nói tiếng Việt của DomOS trên thiết bị ESP32-S3.
Luôn hiểu ý định và trả lời bằng tiếng Việt tự nhiên, ngắn gọn, thân thiện, phù hợp để đọc thành tiếng.
Tận dụng ngữ cảnh hội thoại để hiểu câu nói tiếp nối; không lặp lại thông tin người dùng vừa nói.
Trả lời trực tiếp trước, chỉ giải thích thêm khi hữu ích. Nếu thiếu dữ kiện quan trọng, hỏi đúng một câu ngắn.
Không dùng Markdown, tiêu đề, danh sách ký hiệu, dấu sao hoặc mô tả nội bộ như “đang gọi công cụ”.
Bạn có thể điều khiển thiết bị bằng các công cụ được cung cấp. Khi người dùng yêu cầu điều khiển,
phải gọi công cụ phù hợp và chỉ xác nhận thành công sau khi nhận kết quả công cụ. Không bịa kết quả.
Không bịa lịch thi đấu, hạn mức, trạng thái hiện tại hoặc khả năng không có trong công cụ.
Với lệnh tăng/giảm không nêu mức, dùng delta 10 hoặc -10. Chỉ trả lời nội dung cần nói."""

TOOLS = [
    {"type": "function", "function": {"name": "device.get_status", "description": "Lấy trạng thái trợ lý và âm thanh", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "speaker.set_volume", "description": "Đặt âm lượng loa từ 0 đến 100", "parameters": {"type": "object", "properties": {"volume": {"type": "integer", "minimum": 0, "maximum": 100}}, "required": ["volume"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "speaker.adjust_volume", "description": "Tăng hoặc giảm âm lượng loa theo delta", "parameters": {"type": "object", "properties": {"delta": {"type": "integer", "minimum": -100, "maximum": 100}}, "required": ["delta"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "display.adjust_brightness", "description": "Tăng hoặc giảm độ sáng màn hình theo delta", "parameters": {"type": "object", "properties": {"delta": {"type": "integer", "minimum": -100, "maximum": 100}}, "required": ["delta"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "display.set_brightness", "description": "Đặt độ sáng màn hình từ 0 đến 100", "parameters": {"type": "object", "properties": {"brightness": {"type": "integer", "minimum": 0, "maximum": 100}}, "required": ["brightness"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "app.launch", "description": "Mở ứng dụng DomOS", "parameters": {"type": "object", "properties": {"app": {"type": "string", "enum": ["wallpaper", "clock", "tracking-status", "man-utd", "codex-credit"]}}, "required": ["app"], "additionalProperties": False}}},
]


@dataclass
class VoiceRegistry:
    sessions: dict[str, VoiceSession] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def count(self) -> int:
        return len(self.sessions)

    async def add(self, session: VoiceSession) -> None:
        async with self.lock:
            self.sessions[session.session_id] = session

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
        self.send_lock = asyncio.Lock()
        self.pre_roll: deque[bytes] = deque(maxlen=8)
        self.noise_samples: deque[int] = deque(maxlen=50)
        self.start_candidate_frames = 0
        self.capture_threshold = VAD_ENERGY_THRESHOLD
        self.audio = bytearray()
        self.speech_frames = 0
        self.silence_frames = 0
        self.silence_window: deque[bool] = deque(maxlen=12)
        self.speech_started = False
        self.speech_started_at = 0.0
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
            threshold = self.vad_threshold()
            if energy < threshold:
                self.noise_samples.append(energy)
                self.start_candidate_frames = 0
                return
            self.start_candidate_frames += 1
            if self.start_candidate_frames < VAD_START_FRAMES:
                return
            self.speech_started = True
            self.speech_started_at = time.monotonic()
            self.audio.extend(b"".join(self.pre_roll))
            self.speech_frames = self.start_candidate_frames
            self.speech_energy_total = energy
            self.capture_threshold = threshold
            self.silence_frames = 0
            return
        self.audio.extend(pcm)
        # End the utterance against the calibrated room noise. A low fixed
        # release threshold made noisy rooms run every wake capture to 3 s.
        release_threshold = max(VAD_ENERGY_THRESHOLD, round(self.capture_threshold * 0.9))
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
            or time.monotonic() - self.speech_started_at >= elapsed_limit
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
            and (enough_recent_silence or reached_hard_limit)
        ):
            await self.start_pipeline()

    async def start_pipeline(self) -> None:
        min_speech_frames = (
            WAKE_MIN_SPEECH_FRAMES if self.state == "WAKE_WORD" else VAD_MIN_SPEECH_FRAMES
        )
        if self.pipeline_task is not None or self.speech_frames < min_speech_frames:
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

    async def transcribe_wake_google(self, pcm: bytes) -> list[tuple[str, str]]:
        """Bound both cloud requests; a slow secondary cannot delay activation.

        Prefer the configured language for Vietnamese command suffixes. Allow
        it a short grace period if English recognizes the wake phrase first.
        """
        async def recognize(language: str) -> tuple[str, str]:
            try:
                text = await asyncio.wait_for(
                    self._transcribe_google(pcm, language, timeout=settings.WAKE_STT_TIMEOUT_SEC),
                    timeout=settings.WAKE_STT_TIMEOUT_SEC,
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
                        wake_pcm,
                        timeout=settings.WAKE_STT_TIMEOUT_SEC,
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
        try:
            logger.info("STT device=%s text=%s", self.device_id, transcript)
            await self.send_json({"type": "stt", "text": transcript})
            history = await conversation_store.recent_context(self.device_id)
            turn_id = await conversation_store.create_turn(
                self.device_id, transcript, "pending", "pending"
            )
            answer = await self.chat(history, transcript, turn_id)
            answer = plain_speech_text(answer)
            await conversation_store.update_execution(
                turn_id, self.last_llm_provider, self.last_llm_model
            )
            await conversation_store.complete_turn(turn_id, answer)
            await self.send_json({"type": "llm", "emotion": "happy", "text": answer})
            await self.speak(answer)
        except Exception as exc:
            logger.exception("Voice pipeline failed device=%s", self.device_id)
            message = "Xin lỗi, Dom chưa xử lý được yêu cầu này. Bạn thử lại nhé."
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

    async def _llm(self, payload: dict[str, Any]) -> dict[str, Any]:
        order = [name.strip().lower() for name in settings.LLM_PROVIDER_ORDER.split(",")]
        openai_configured = "openai" in order and bool(settings.OPENAI_API_KEY)
        quota_exhausted = openai_configured and not self.provider_ready("openai-llm-quota")
        if openai_configured and not quota_exhausted:
            try:
                response = await self._openai({**payload, "model": settings.OPENAI_MODEL})
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
            response = await self._openrouter({**payload, "model": settings.OPENROUTER_MODEL})
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
            providers = ["openai", "google-web"]
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
                or (primary != "openrouter" and not settings.STT_OPENROUTER_FALLBACK)
            ):
                continue

            started = time.monotonic()
            try:
                if provider == "openai":
                    request = self._transcribe_openai(prepared, selected_language)
                elif provider == "google-web":
                    request = self._transcribe_google(
                        prepared, selected_language, timeout=timeout
                    )
                else:
                    request = self._transcribe_openrouter(prepared)
                transcript = (
                    await asyncio.wait_for(request, timeout=timeout)
                    if timeout is not None else await request
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if provider == "openai":
                    self.defer_provider(openai_cooldown_key)
                detail = str(exc) or type(exc).__name__
                logger.warning(
                    "STT provider failed device=%s provider=%s: %s",
                    self.device_id, provider, detail,
                )
                continue

            elapsed_ms = round((time.monotonic() - started) * 1000)
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
            return _clean_text(await asyncio.to_thread(
                recognizer.recognize_google,
                audio,
                language=language,
            ))
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
        return _clean_text(response.json().get("text"))

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
        return _clean_text(choices[0].get("message", {}).get("content")) if choices else ""

    async def chat(self, history: list[dict[str, str]], transcript: str, turn_id: str) -> str:
        direct = await self.try_direct_command(transcript, turn_id)
        if direct:
            self.last_llm_provider, self.last_llm_model = "device", "deterministic-command-router"
            return direct
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *[
                {**item, "content": plain_speech_text(item.get("content", ""))}
                for item in history
            ],
            {"role": "user", "content": transcript},
        ]
        for _ in range(3):
            response = await self._llm({
                "messages": messages,
                "tools": TOOLS,
                "tool_choice": "auto",
                "temperature": 0.3,
            })
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
                        result = await self.call_device_tool(name, arguments)
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
        text = transcript.casefold().strip()
        name = ""
        arguments: dict[str, Any] = {}
        success_text = ""
        number_match = re.search(r"\b(100|[1-9]?\d)\b", text)
        number = int(number_match.group(1)) if number_match else None

        app = requested_app(transcript)
        if app is not None:
            name, arguments = "app.launch", {"app": app}
            success_text = APP_CONFIRMATIONS[app]
        elif is_unresolved_launch_request(transcript):
            return APP_COMMAND_HELP
        elif "độ sáng" in text or "màn hình" in text:
            if "tăng" in text:
                delta = number or 10
            elif "giảm" in text:
                delta = -(number or 10)
            else:
                return None
            name, arguments = "display.adjust_brightness", {"delta": delta}
            success_text = f"Độ sáng đã {'tăng' if delta > 0 else 'giảm'} {abs(delta)} rồi nhé!"
        elif "âm lượng" in text or "loa" in text:
            if "tăng" in text:
                delta = number or 10
                name, arguments = "speaker.adjust_volume", {"delta": delta}
                success_text = f"Âm lượng đã tăng {delta} rồi nhé!"
            elif "giảm" in text:
                delta = -(number or 10)
                name, arguments = "speaker.adjust_volume", {"delta": delta}
                success_text = f"Âm lượng đã giảm {abs(delta)} rồi nhé!"
            elif number is not None:
                name, arguments = "speaker.set_volume", {"volume": number}
                success_text = f"Âm lượng đã được đặt ở {number} rồi nhé!"
            else:
                return None
        elif "trạng thái" in text and ("thiết bị" in text or "dom" in text):
            name, arguments, success_text = "device.get_status", {}, "Thiết bị đang hoạt động bình thường."
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

    async def speak(self, text: str) -> None:
        self.state = "SPEAKING"
        await self.send_json({"type": "tts", "state": "start"})
        try:
            sentences = [part.strip() for part in re.split(r"(?<=[.!?…])\s+|(?<=[。！？])", text) if part.strip()]
            for sentence in sentences or [text]:
                await self.send_json({"type": "tts", "state": "sentence_start", "text": sentence})
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
                for offset in range(0, len(pcm), PCM_FRAME_BYTES):
                    frame = pcm[offset: offset + PCM_FRAME_BYTES]
                    if len(frame) < PCM_FRAME_BYTES:
                        frame += bytes(PCM_FRAME_BYTES - len(frame))
                    await self.send_bytes(frame)
                    await asyncio.sleep(PCM_FRAME_MS / 1000)
        finally:
            await self.send_json({"type": "tts", "state": "stop"})


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
    gTTS(text=text, lang="vi").write_to_fp(output)
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
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        hello = json.loads(raw)
        validate_dom_hello(hello)
        await voice_registry.add(session)
        await session.send_json({
            "type": "hello", "provider": primary_llm_provider(), "transport": "websocket",
            "audio_params": {"codec": "pcm", "sample_rate": 16_000, "channels": 1, "frame_duration": 60},
            "features": {"mcp": True, "vad": True, "emotions": True, "tts_streaming": True},
        })
        await session.set_wake_word()
        logger.info("Cloud voice connected device=%s provider=%s session=%s", device_id, primary_llm_provider(), session_id)
        while True:
            raw_message = await websocket.receive()
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
        if session.pipeline_task and not session.pipeline_task.done():
            session.pipeline_task.cancel()
        if session.activation_timeout_task and not session.activation_timeout_task.done():
            session.activation_timeout_task.cancel()
        for future in session.pending_mcp.values():
            if not future.done():
                future.cancel()
        await voice_registry.remove(session_id)
        logger.info("Cloud voice disconnected device=%s", device_id)
