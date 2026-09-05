"""Read and cache Codex plan usage without exposing Codex credentials to devices."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any
from zoneinfo import ZoneInfo

from config import settings


logger = logging.getLogger("domos.codex_usage")


class CodexUsageError(RuntimeError):
    """Raised when neither live nor cached Codex usage is available."""


class CodexUsageService:
    def __init__(self) -> None:
        self._cache_path = Path(settings.CODEX_USAGE_CACHE_PATH)
        self._lock = asyncio.Lock()

    async def get_usage(self, force: bool = False) -> dict[str, Any]:
        """Return fresh local CLI usage when possible, otherwise cached usage."""
        async with self._lock:
            cached = self._read_cache()
            cache_age = self._cache_age_seconds(cached)
            should_refresh = settings.CODEX_USAGE_LOCAL_ENABLED and (
                force
                or cached is None
                or cache_age >= settings.CODEX_USAGE_REFRESH_SECONDS
            )
            if should_refresh:
                try:
                    snapshot = await self.collect_local()
                    self._write_cache(snapshot)
                    return snapshot
                except (CodexUsageError, OSError, asyncio.TimeoutError) as exc:
                    logger.warning("Unable to refresh Codex usage: %s", exc)

            if cached is None:
                raise CodexUsageError("Codex usage has not been synchronized")
            result = deepcopy(cached)
            result["stale"] = cache_age >= settings.CODEX_USAGE_STALE_SECONDS
            return result

    async def collect_local(self) -> dict[str, Any]:
        """Query the authenticated local Codex app-server over JSONL stdio."""
        try:
            process = await asyncio.create_subprocess_exec(
                settings.CODEX_CLI_PATH,
                "app-server",
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise CodexUsageError("Codex CLI is unavailable") from exc

        try:
            await self._send_request(
                process,
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "domos-codex-usage",
                            "version": "1.0.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                },
            )
            await self._read_response(process, 1)
            await self._send_request(
                process,
                {"id": 2, "method": "account/rateLimits/read"},
            )
            response = await self._read_response(process, 2)
            if "error" in response:
                raise CodexUsageError(str(response["error"]))
            return self.normalize(response.get("result") or {})
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    async def store_synced(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Validate and store a normalized snapshot uploaded by a local collector."""
        normalized = self._validate_normalized(snapshot)
        async with self._lock:
            self._write_cache(normalized)
        return normalized

    @staticmethod
    async def _send_request(
        process: asyncio.subprocess.Process, request: dict[str, Any]
    ) -> None:
        if process.stdin is None:
            raise CodexUsageError("Codex CLI stdin is unavailable")
        process.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
        await process.stdin.drain()

    @staticmethod
    async def _read_response(
        process: asyncio.subprocess.Process, request_id: int
    ) -> dict[str, Any]:
        if process.stdout is None:
            raise CodexUsageError("Codex CLI stdout is unavailable")

        async def read_lines() -> dict[str, Any]:
            while True:
                line = await process.stdout.readline()
                if not line:
                    stderr = b""
                    if process.stderr is not None:
                        stderr = await process.stderr.read()
                    detail = stderr.decode(errors="replace").strip()
                    raise CodexUsageError(detail or "Codex CLI closed unexpectedly")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == request_id:
                    return message

        return await asyncio.wait_for(
            read_lines(), timeout=settings.CODEX_USAGE_COLLECT_TIMEOUT_SECONDS
        )

    @staticmethod
    def normalize(result: dict[str, Any]) -> dict[str, Any]:
        limits_by_id = result.get("rateLimitsByLimitId") or {}
        limits = limits_by_id.get("codex") or result.get("rateLimits") or {}
        windows = [limits.get("primary"), limits.get("secondary")]
        windows = [window for window in windows if isinstance(window, dict)]

        def find_window(duration: int, fallback_index: int) -> dict[str, Any]:
            for window in windows:
                if window.get("windowDurationMins") == duration:
                    return window
            return windows[fallback_index] if len(windows) > fallback_index else {}

        five_hour = CodexUsageService._normalize_window(find_window(300, 0))
        weekly = CodexUsageService._normalize_window(find_window(10080, 1))

        reset_summary = result.get("rateLimitResetCredits") or {}
        credits = reset_summary.get("credits") or []
        available = next(
            (
                credit
                for credit in credits
                if isinstance(credit, dict) and credit.get("status") == "available"
            ),
            None,
        )
        expires_at = available.get("expiresAt") if available else None
        now = int(time.time())
        return {
            "source": "codex-app-server",
            "plan": limits.get("planType") or "unknown",
            "five_hour": five_hour,
            "weekly": weekly,
            "full_reset": {
                "available": bool(available),
                "title": (
                    available.get("title")
                    if available
                    else "Full reset (Weekly + 5 hr)"
                ),
                "expires_at": expires_at,
                "expires_label": CodexUsageService._format_epoch(expires_at, True),
            },
            "updated_at": now,
            "updated_label": CodexUsageService._format_epoch(now),
            "timezone": settings.CODEX_USAGE_TIMEZONE,
            "stale": False,
        }

    @staticmethod
    def _normalize_window(window: dict[str, Any]) -> dict[str, Any]:
        used = max(0, min(100, int(window.get("usedPercent", 0))))
        resets_at = window.get("resetsAt")
        return {
            "used_percent": used,
            "remaining_percent": 100 - used,
            "resets_at": resets_at,
            "resets_label": CodexUsageService._format_epoch(resets_at),
        }

    @staticmethod
    def _format_epoch(value: Any, include_zone: bool = False) -> str:
        if not isinstance(value, int):
            return "--"
        try:
            zone = ZoneInfo(settings.CODEX_USAGE_TIMEZONE)
        except (KeyError, ValueError):
            zone = timezone.utc
        rendered = datetime.fromtimestamp(value, zone).strftime("%d/%m %H:%M")
        if include_zone:
            offset = datetime.fromtimestamp(value, zone).strftime("%z")
            if len(offset) == 5:
                offset = f"GMT{offset[:3]}:{offset[3:]}"
            rendered = f"{rendered} {offset}"
        return rendered

    def _read_cache(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            return self._validate_normalized(data)
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _write_cache(self, snapshot: dict[str, Any]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self._cache_path)

    @staticmethod
    def _cache_age_seconds(snapshot: dict[str, Any] | None) -> int:
        if snapshot is None:
            return 2**31 - 1
        return max(0, int(time.time()) - int(snapshot.get("updated_at", 0)))

    @staticmethod
    def _validate_normalized(snapshot: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(snapshot, dict):
            raise ValueError("Codex usage snapshot must be an object")
        result = deepcopy(snapshot)
        for key in ("five_hour", "weekly"):
            window = result.get(key)
            if not isinstance(window, dict):
                raise ValueError(f"Missing {key} usage window")
            remaining = int(window.get("remaining_percent", -1))
            if remaining < 0 or remaining > 100:
                raise ValueError(f"Invalid {key} remaining percentage")
        if not isinstance(result.get("full_reset"), dict):
            raise ValueError("Missing full_reset status")
        result["updated_at"] = int(result.get("updated_at", time.time()))
        result["stale"] = False
        return result


codex_usage_service = CodexUsageService()
