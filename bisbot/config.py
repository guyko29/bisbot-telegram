"""Runtime configuration, read from environment variables (and a local .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_TZ = "Asia/Jerusalem"

# python-telegram-bot's JobQueue counts weekdays 0-6 as Sunday-Saturday.
FRIDAY = 5


class ConfigError(RuntimeError):
    """Raised when the environment is missing or has invalid settings."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    anthropic_api_key: str
    allowed_chat_id: int
    claude_model: str = DEFAULT_MODEL
    claude_effort: str | None = "low"
    claude_refusal_fallback: bool = True
    tz: ZoneInfo = ZoneInfo(DEFAULT_TZ)
    db_path: Path = Path("data/bisbot.db")
    menu_path: Path = Path("menu.md")
    targets_path: Path = Path("menu_targets.json")
    log_level: str = "INFO"
    daily_summary_time: time = time(21, 0)
    weekly_summary_time: time = time(12, 0)
    weekly_summary_weekday: int = FRIDAY


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    """Build Settings from the environment. A .env file is loaded if present."""
    load_dotenv()

    chat_id_raw = _required("ALLOWED_CHAT_ID")
    try:
        allowed_chat_id = int(chat_id_raw)
    except ValueError as exc:
        raise ConfigError("ALLOWED_CHAT_ID must be an integer chat id") from exc

    tz_name = os.environ.get("TZ", "").strip() or DEFAULT_TZ
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Unknown timezone in TZ: {tz_name}") from exc

    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        anthropic_api_key=_required("ANTHROPIC_API_KEY"),
        allowed_chat_id=allowed_chat_id,
        claude_model=os.environ.get("CLAUDE_MODEL", "").strip() or DEFAULT_MODEL,
        claude_effort=os.environ.get("CLAUDE_EFFORT", "low").strip().lower() or None,
        claude_refusal_fallback=_flag("CLAUDE_REFUSAL_FALLBACK", True),
        tz=tz,
        db_path=Path(os.environ.get("DB_PATH", "").strip() or "data/bisbot.db"),
        menu_path=Path(os.environ.get("MENU_PATH", "").strip() or "menu.md"),
        targets_path=Path(
            os.environ.get("MENU_TARGETS_PATH", "").strip() or "menu_targets.json"
        ),
        log_level=os.environ.get("LOG_LEVEL", "").strip().upper() or "INFO",
    )
