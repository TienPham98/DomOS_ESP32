from pathlib import Path
from typing import Literal

from pydantic import Field

from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    # The shared root .env contains deployment secrets. backend-python/.env may
    # override non-secret developer settings when the service is run directly.
    model_config = SettingsConfigDict(
        env_file=(BACKEND_DIR.parent / ".env", BACKEND_DIR / ".env"),
        extra="ignore",
    )

    APP_NAME: str = "DomOS OpenRouter Voice Gateway"
    HOST: str
    PORT: int
    # Namespaced to avoid collisions with generic DEBUG variables injected by
    # shells, IDEs and package managers.
    DOMOS_DEBUG: bool = False

    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str
    OPENROUTER_MODEL: str
    OPENROUTER_AUDIO_MODEL: str
    OPENROUTER_TIMEOUT_SEC: float
    OPENROUTER_HTTP_REFERER: str
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str
    OPENAI_MODEL: str
    OPENAI_STT_MODEL: str
    OPENAI_TIMEOUT_SEC: float
    LLM_PROVIDER_ORDER: str
    STT_PROVIDER: str
    STT_LANGUAGE: str
    # Wake checks need not wait for the command STT provider's quota/fallbacks.
    WAKE_STT_PROVIDER: Literal["google-web", "configured"] = "google-web"
    WAKE_STT_TIMEOUT_SEC: float = Field(default=3.0, gt=0, le=15)
    # OpenRouter currently requires a minimum account balance for audio input,
    # including some :free models. Keep this opt-in so free voice recognition
    # continues through Google Web STT instead of failing with HTTP 402.
    STT_OPENROUTER_FALLBACK: bool
    TTS_PROVIDER: str
    TTS_VOICE: str
    TTS_TIMEOUT_SEC: float
    CONVERSATION_DB_PATH: str = "data/conversations.db"

    # Codex plan usage. A local Codex CLI can refresh this cache directly;
    # production gateways receive normalized snapshots from the sync script.
    CODEX_USAGE_LOCAL_ENABLED: bool = True
    CODEX_CLI_PATH: str = "codex"
    CODEX_USAGE_CACHE_PATH: str = "data/codex_usage.json"
    CODEX_USAGE_TIMEZONE: str = "Asia/Bangkok"
    CODEX_USAGE_REFRESH_SECONDS: int = 60
    CODEX_USAGE_STALE_SECONDS: int = 900
    CODEX_USAGE_COLLECT_TIMEOUT_SECONDS: float = 10.0
    CODEX_USAGE_SYNC_TOKEN: str = ""

    # Manchester United schedule. Provider credentials and URLs stay in .env.
    FOOTBALL_DATA_API_KEY: str = ""
    FOOTBALL_DATA_BASE_URL: str
    FOOTBALL_TEAM_ID: int = 66
    FOOTBALL_TEAM_NAME: str = "Manchester United"
    FOOTBALL_TIMEZONE: str = "Asia/Bangkok"
    FOOTBALL_CACHE_TTL_SECONDS: int = 86400
    FOOTBALL_CACHE_PATH: str = "data/manchester_united_schedule.json"
    MANCHESTER_UNITED_BADGE_URL: str

    # Deployment addresses come from the shared root .env.
    MQTT_BROKER_HOST: str
    MQTT_BROKER_PORT: int
    MQTT_CLIENT_ID: str
    MQTT_USERNAME: str = ""
    MQTT_PASSWORD: str = ""

    # Voice Protocol v3: PCM, 16kHz, mono, 60ms frames
    VOICE_SESSION_TIMEOUT_SEC: int = 30
    VOICE_AUTH_TOKEN: str = ""


settings = Settings()
