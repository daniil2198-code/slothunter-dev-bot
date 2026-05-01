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

    # ─────── Daily digest (M1) ───────
    # ``HH:MM`` in Europe/Minsk; empty disables the cron job entirely.
    digest_time: str = ""
    digest_repo: Path = Path("/opt/slot-hunter")
    digest_healthz: str = ""
    digest_log_units: str = ""

    # ─────── Voice transcription via Groq Whisper (M1.5) ───────
    # Empty key disables voice handling — bot replies with the old
    # "не поддерживается" hint instead of crashing.
    groq_api_key: str = ""
    groq_whisper_model: str = "whisper-large-v3-turbo"
    # Hard cap on voice duration we'll transcribe. Telegram voice notes
    # rarely exceed a minute in practice; longer = likely mis-tap.
    voice_max_duration_sec: int = 600

    # ─────── Playwright MCP (M3.1) ───────
    # When true, Claude in this bot gets ``mcp__playwright__*`` tools
    # via the Playwright MCP server (``npx @playwright/mcp``). Browser
    # ops (click, type, screenshot) auto-approve; ``evaluate`` /
    # ``run_code_unsafe`` always ask. Requires ``npx`` + a working
    # Chromium install on the host (see scripts/install_playwright.sh).
    playwright_mcp_enabled: bool = False

    # ─────── Dev-mode auth bypass for Mini App (M3.2) ───────
    # Same shared secret that's set in slot-hunter's .env. Lets Claude
    # open https://slothunter.space/?dev_token=<this> and reach the API
    # without forging Telegram WebApp HMAC. The bot reads it from env
    # so the system prompt can give Claude the actual URL to navigate
    # to. Empty / unset = M3.2 effectively disabled (Claude only sees
    # the splash screen).
    dev_auth_token: str = ""
    # Used to assemble the dev-mode URL. Defaults to the production
    # Mini App; can override for staging.
    miniapp_url: str = "https://slothunter.space"

    @property
    def betas(self) -> list[str]:
        return [b.strip() for b in self.claude_betas.split(",") if b.strip()]

    @property
    def digest_log_units_list(self) -> list[str]:
        return [u.strip() for u in self.digest_log_units.split(",") if u.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton. Tests can override by clearing the cache."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
