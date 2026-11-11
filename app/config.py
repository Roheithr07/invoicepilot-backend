"""
Application Configuration for InvoicePilot AI.
Loads settings from environment variables and .env file.
"""

from typing import List
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DB_PATH = (_BASE_DIR / "invoicepilot.db").as_posix()


class Settings(BaseSettings):
    # App Settings
    APP_NAME: str = "InvoicePilot AI"
    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Database — Canonical absolute path
    DATABASE_URL: str = f"sqlite:///{_DEFAULT_DB_PATH}"

    # PayPal Developer Sandbox
    PAYPAL_MODE: str = "sandbox"
    PAYPAL_CLIENT_ID: str = ""
    PAYPAL_CLIENT_SECRET: str = ""
    PAYPAL_BASE_URL: str = "https://api-m.sandbox.paypal.com"
    PAYPAL_WEBHOOK_ID: str = ""

    # AI Configuration (Free Local Ollama / Deterministic Fallback)
    AI_PROVIDER: str = "ollama"
    AI_API_KEY: str = ""
    AI_MODEL: str = "phi3:mini"
    OLLAMA_BASE_URL: str = "http://localhost:11434"

    # Security
    SECRET_KEY: str = "invoicepilot-dev-secret-key-change-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    @property
    def cors_origin_list(self) -> List[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    model_config = SettingsConfigDict(
        env_file=[(_BASE_DIR / ".env").as_posix(), ".env"],
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()

