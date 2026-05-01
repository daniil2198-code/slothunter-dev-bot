"""Runtime config — read from .env via pydantic-settings.

Three knobs the user actually changes:
  * ``TELEGRAM_BOT_TOKEN``  — from @BotFather
  * ``ALLOWED_USER_ID``     — the single Telegram user_id allowed to talk to us
  * ``DEFAULT_WORKDIR``     — where Claude operates by default

Everything else has sensible defaults. Anything missing fails fast at import.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─────── Required ───────
    telegram_bot_token: str = Field(min_length=10)
    allowed_user_id: int = Field(gt=0)

    # ─────── Optional ───────
    default_workdir: Path = Path("/opt/slot-hunter")
    log_level: str = "INFO"

    # Where we stash per-chat session metadata (last claude session_id, last
    # cwd, …) so the bot can resume across restarts. Lives outside the repo.
    state_dir: Path = Path("/var/lib/slothunter-dev-bot")

    # The model Claude Code runs as. Subscription plans use whatever the
    # user logged in to claude.ai gets — leaving this on default lets the
    # CLI pick it. Override only if you know what you're doing.
    model: str | None = None

    # Comma-separated beta flags forwarded to Claude Code. The 1M context
    # window for Opus is gated behind ``context-1m-2025-08-07``.
    claude_betas: str = ""

    @property
    def betas(self) -> list[str]:
        return [b.strip() for b in self.claude_betas.split(",") if b.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton. Tests can override by clearing the cache."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
