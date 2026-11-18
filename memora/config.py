"""Typed runtime configuration loaded from environment / .env."""
from __future__ import annotations

from datetime import time
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SHEET = "https://docs.google.com/spreadsheets/d/15WaaKu0J5bfssa3Z5whWSCl1tJ7HeQlz3xa6AgyM0ic/export?format=csv&gid="


def _parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram -----------------------------------------------------------
    telegram_bot_token: str = ""
    owner_chat_id: int = 812304675
    owner_name: str = "Maxmanazar"

    # --- Storage ------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://memora:memora@db:5432/memora"

    # --- Locale -------------------------------------------------------------
    timezone: str = "Asia/Tashkent"

    # --- Timetable ----------------------------------------------------------
    timetable_sheet_urls: str = f"{_SHEET}1454027058,{_SHEET}1920812576"
    timetable_groups: str = "25-408,Intermediate 13"
    timetable_sync_minutes: int = 30

    # --- Google -------------------------------------------------------------
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"
    google_tasklist_id: str = "@default"
    google_sync_minutes: int = 3

    # --- LLM routing --------------------------------------------------------
    gemini_api_key: str = ""
    openai_api_key: str = ""
    groq_api_key: str = ""
    openrouter_api_key: str = ""
    llm_custom_base_url: str = ""
    llm_custom_api_key: str = ""
    llm_smart_chain: str = "gemini/gemini-3.8-flash,gemini/gemini-3.6-flash"
    llm_fast_chain: str = "gemini/gemini-3.5-flash-lite,gemini/gemini-3.8-flash"
    llm_reasoning_effort: str = ""
    llm_timeout_seconds: float = 45.0
    llm_max_retries: int = 3

    # --- Speech-to-text -----------------------------------------------------
    stt_chain: str = "gemini,groq,openai"
    stt_gemini_model: str = "gemini-3.8-flash"
    stt_groq_model: str = "whisper-large-v3"
    stt_openai_model: str = "gpt-4o-transcribe"

    # --- Proactive engine ---------------------------------------------------
    briefing_time: str = "07:30"
    class_alert_minutes: int = 15
    weekly_digest_time: str = "20:00"
    review_nudge_time: str = "19:30"
    date_only_reminder_time: str = "09:00"
    homework_auto_create: bool = True
    homework_reminder_time: str = "19:00"
    lesson_log_timeout_minutes: int = 180
    action_ttl_minutes: int = 30

    # --- Observability ------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("briefing_time", "weekly_digest_time", "date_only_reminder_time", "homework_reminder_time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        _parse_hhmm(v)
        return v

    @field_validator("review_nudge_time")
    @classmethod
    def _check_optional_time(cls, v: str) -> str:
        if v.strip():
            _parse_hhmm(v)
        return v.strip()

    # Derived helpers --------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def sheet_urls(self) -> list[str]:
        return [u.strip() for u in self.timetable_sheet_urls.split(",") if u.strip()]

    @property
    def groups(self) -> list[str]:
        return [g.strip() for g in self.timetable_groups.split(",") if g.strip()]

    @property
    def briefing_at(self) -> time:
        return _parse_hhmm(self.briefing_time)

    @property
    def weekly_digest_at(self) -> time:
        return _parse_hhmm(self.weekly_digest_time)

    @property
    def review_nudge_at(self) -> time | None:
        return _parse_hhmm(self.review_nudge_time) if self.review_nudge_time else None

    @property
    def date_only_reminder_at(self) -> time:
        return _parse_hhmm(self.date_only_reminder_time)

    @property
    def homework_reminder_at(self) -> time:
        return _parse_hhmm(self.homework_reminder_time)

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_refresh_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
