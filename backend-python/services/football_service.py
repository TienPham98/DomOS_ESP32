"""Manchester United fixture data and ESP32-friendly background rendering."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
import logging
from pathlib import Path
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from PIL import Image

from config import settings


logger = logging.getLogger("domos.football")
UPCOMING_STATUSES = {"SCHEDULED", "TIMED", "IN_PLAY", "PAUSED"}


class FootballService:
    """Fetches fixtures server-side and keeps a persistent last-known-good cache."""

    def __init__(self) -> None:
        cache_path = Path(settings.FOOTBALL_CACHE_PATH)
        if not cache_path.is_absolute():
            cache_path = Path(__file__).resolve().parent.parent / cache_path
        self._cache_path = cache_path
        self._background_path = cache_path.with_name("manchester_united_background_v2.jpg")
        self._cached_schedule: dict[str, Any] | None = None
        self._schedule_cached_at = 0.0
        self._lock = asyncio.Lock()

    async def get_schedule(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if (
            not force
            and self._cached_schedule is not None
            and now - self._schedule_cached_at < settings.FOOTBALL_CACHE_TTL_SECONDS
        ):
            return self._cached_schedule

        async with self._lock:
            now = time.monotonic()
            if (
                not force
                and self._cached_schedule is not None
                and now - self._schedule_cached_at < settings.FOOTBALL_CACHE_TTL_SECONDS
            ):
                return self._cached_schedule

            try:
                schedule = await self._fetch_schedule()
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                self._cache_path.write_text(
                    json.dumps(schedule, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                self._cached_schedule = schedule
                self._schedule_cached_at = time.monotonic()
                return schedule
            except (httpx.HTTPError, ValueError, OSError, ZoneInfoNotFoundError) as exc:
                logger.warning("Football schedule refresh failed: %s", exc)
                stale = self._load_disk_cache()
                if stale is None:
                    raise RuntimeError("Football schedule is currently unavailable") from exc
                stale["stale"] = True
                stale["error"] = "Using the last successful daily update"
                self._cached_schedule = stale
                self._schedule_cached_at = time.monotonic()
                return stale

    async def get_background_jpeg(self) -> bytes:
        if self._background_path.exists():
            return self._background_path.read_bytes()
        if not settings.MANCHESTER_UNITED_BADGE_URL:
            raise RuntimeError("MANCHESTER_UNITED_BADGE_URL is not configured")

        async with self._lock:
            if self._background_path.exists():
                return self._background_path.read_bytes()
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.get(settings.MANCHESTER_UNITED_BADGE_URL)
                response.raise_for_status()
            rendered = self._render_background(response.content)
            self._background_path.parent.mkdir(parents=True, exist_ok=True)
            self._background_path.write_bytes(rendered)
            return rendered

    async def _fetch_schedule(self) -> dict[str, Any]:
        if not settings.FOOTBALL_DATA_API_KEY:
            raise ValueError("FOOTBALL_DATA_API_KEY is not configured")
        if not settings.FOOTBALL_DATA_BASE_URL:
            raise ValueError("FOOTBALL_DATA_BASE_URL is not configured")

        today = datetime.now(UTC).date()
        params = {
            "dateFrom": today.isoformat(),
            "dateTo": (today + timedelta(days=120)).isoformat(),
            "limit": 30,
        }
        url = (
            f"{settings.FOOTBALL_DATA_BASE_URL.rstrip('/')}"
            f"/teams/{settings.FOOTBALL_TEAM_ID}/matches"
        )
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                url,
                params=params,
                headers={"X-Auth-Token": settings.FOOTBALL_DATA_API_KEY},
            )
            response.raise_for_status()
            payload = response.json()

        matches = [
            item
            for item in payload.get("matches", [])
            if item.get("status") in UPCOMING_STATUSES and item.get("utcDate")
        ]
        matches.sort(key=lambda item: item["utcDate"])
        if not matches:
            raise ValueError("Provider returned no upcoming Manchester United match")

        next_match = matches[0]
        kickoff_utc = datetime.fromisoformat(next_match["utcDate"].replace("Z", "+00:00"))
        kickoff_local = kickoff_utc.astimezone(ZoneInfo(settings.FOOTBALL_TIMEZONE))
        home = next_match.get("homeTeam", {}).get("shortName") or next_match.get(
            "homeTeam", {}
        ).get("name", "TBD")
        away = next_match.get("awayTeam", {}).get("shortName") or next_match.get(
            "awayTeam", {}
        ).get("name", "TBD")
        team_name = settings.FOOTBALL_TEAM_NAME
        home_id = next_match.get("homeTeam", {}).get("id")
        opponent = away if home_id == settings.FOOTBALL_TEAM_ID else home

        return {
            "team": team_name,
            "updated_at": datetime.now(UTC).isoformat(),
            "timezone": settings.FOOTBALL_TIMEZONE,
            "stale": False,
            "next_match": {
                "id": next_match.get("id"),
                "competition": next_match.get("competition", {}).get("name", "Football"),
                "utc_date": kickoff_utc.isoformat(),
                "local_date": kickoff_local.strftime("%d/%m/%Y"),
                "local_time": kickoff_local.strftime("%H:%M"),
                "home_team": home,
                "away_team": away,
                "opponent": opponent,
                "venue": next_match.get("venue") or "Venue TBC",
                "status": next_match.get("status", "SCHEDULED"),
            },
            "background_url": "/api/football/manchester-united/background.jpg",
        }

    def _load_disk_cache(self) -> dict[str, Any] | None:
        try:
            cached = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return cached if isinstance(cached, dict) and cached.get("next_match") else None

    @staticmethod
    def _render_background(source: bytes) -> bytes:
        badge = Image.open(BytesIO(source)).convert("RGBA")
        # Keep the crest clear of the translucent header and fixture panel.
        # 134 px is approximately 80% of the original 168 px rendering.
        badge.thumbnail((134, 134), Image.Resampling.LANCZOS)

        canvas = Image.new("RGB", (320, 240), "#39000d")
        pixels = canvas.load()
        for y in range(240):
            shade = int(42 * (1 - y / 239))
            color = (91 + shade, 0, 21 + shade // 3)
            for x in range(320):
                pixels[x, y] = color

        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        x = (320 - badge.width) // 2
        y = 16
        overlay.alpha_composite(badge, (x, y))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

        output = BytesIO()
        canvas.save(output, format="JPEG", quality=86, optimize=True)
        return output.getvalue()


football_service = FootballService()
