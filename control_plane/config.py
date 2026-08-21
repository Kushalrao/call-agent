from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from the environment. Nothing is hardcoded, and
    nothing secret ever reaches the iOS bundle except LIVEKIT_URL (spec §9)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- auth -------------------------------------------------------------
    # dev: name + seeded code. apple: Sign in with Apple (deferred, spec §1.1).
    auth_mode: Literal["dev", "apple"] = "dev"
    jwt_signing_key: str = "dev-insecure-change-me"
    jwt_ttl_hours: int = 24 * 30

    # --- storage ----------------------------------------------------------
    # SQLite for local dev; swap to postgresql+asyncpg://... for prod. The
    # SQLAlchemy models are Postgres-compatible, so this is the only change.
    database_url: str = "sqlite+aiosqlite:///./hands_free.db"

    # --- livekit ----------------------------------------------------------
    livekit_url: str = ""  # wss://<project>.livekit.cloud
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    # Join window only; the session persists after join (spec §1.4).
    livekit_token_ttl_seconds: int = 120

    # --- agent ------------------------------------------------------------
    # Explicit dispatch, fired when the call reaches ACTIVE (spec §3 as amended
    # 2026-08-20 - there is no consent gate).
    dispatch_agent: bool = False  # flip on once the Phase 2 worker exists
    agent_name: str = "trip-copilot"

    # --- model providers --------------------------------------------------
    # Kept in Settings rather than read from os.environ by each plugin: .env is
    # loaded here and here only, so an unset key fails loudly at startup
    # instead of at the first utterance of a live call.
    deepgram_api_key: str = ""
    anthropic_api_key: str = ""
    elevenlabs_api_key: str = ""
    # Free-tier accounts are refused *library* voices via the API (402
    # paid_plan_required), so the voice is pinned to one that works rather than
    # left to the plugin's default.
    elevenlabs_voice_id: str = "EXAVITQu4vr4xnSDxMaL"
    elevenlabs_model: str = "eleven_flash_v2_5"
    # The conversational agent the app talks to. Configured by
    # scripts/provision_agent.py; the app never sees this or the key.
    elevenlabs_agent_id: str = ""

    # --- flight search ----------------------------------------------------
    # The Watermelon extension's checkout. flight_scout defaults to ~/Desktop,
    # which was wrong on this machine and produced a search that "found no
    # flights" instead of reporting a missing extension.
    watermelon_extension_dir: str = ""

    # --- behaviour --------------------------------------------------------
    ring_timeout_seconds: int = 45

    # --- observability ----------------------------------------------------
    log_dir: str = "logs"
    # level=info logs metadata only. Text is gated behind this flag and must
    # stay off in prod (spec §11.1 privacy split).
    log_transcripts: bool = False
    log_pretty: bool = True

    # --- rate limits ------------------------------------------------------
    dev_login_per_min_per_ip: int = 5
    calls_per_min_per_caller: int = 10
    # The callee-side limit is what stops one user being used as a ring-spam
    # target. Do not remove it (spec §1.2).
    calls_per_min_per_callee: int = 5

    @property
    def stt_configured(self) -> bool:
        return bool(self.deepgram_api_key)

    @property
    def livekit_api_url(self) -> str:
        """The server SDK wants http(s); clients want ws(s)."""
        u = self.livekit_url
        if u.startswith("wss://"):
            return "https://" + u[len("wss://") :]
        if u.startswith("ws://"):
            return "http://" + u[len("ws://") :]
        return u

    @property
    def livekit_configured(self) -> bool:
        return bool(self.livekit_url and self.livekit_api_key and self.livekit_api_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
