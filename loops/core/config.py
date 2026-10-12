from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Nebius Token Factory (OpenAI-compatible)
    nebius_api_key: str = ""
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    chat_model: str = "nvidia/Llama-3_3-Nemotron-Super-49B-v1_5"
    embed_model: str = "Qwen/Qwen3-Embedding-8B"
    embed_dim: int = 4096
    embed_batch_size: int = 32
    llm_timeout_s: float = 120.0
    # Nemotron reasoning models read a "/think" or "/no_think" system-prompt toggle.
    chat_system_prefix: str = "/no_think"

    database_url: str = "postgresql+asyncpg://loops:loops@localhost:5432/loops"

    # Gmail
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_redirect_uri: str = "http://localhost:8000/sources/gmail/callback"

    # Telegram user account (Telethon)
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session_path: str = "secrets/telegram.session"
    tg_chat_allowlist: list[int] = []
    tg_chat_denylist: list[int] = []
    tg_max_chats: int = 200

    # Telegram bot
    bot_token: str = ""
    owner_telegram_id: int = 0

    encryption_key: str = ""
    web_password: str = ""
    public_base_url: str = "http://localhost:8000"

    backfill_days: int = 90
    sync_interval_min: int = 5
    timezone: str = "Africa/Addis_Ababa"
    digest_hour: int = 8

    # "tools": free tool calling; "router": fixed classify -> tool -> answer pipeline.
    agent_mode: str = "tools"

    extract_min_confidence: float = 0.6
    waiting_on_days: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
