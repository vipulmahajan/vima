"""Centralized configuration loaded from environment variables.

Keep all secrets / tunables here. Use `from config import settings`.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    app_env: str = Field(default="development")
    debug: bool = Field(default=True)
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    base_url: str = Field(default="http://localhost:8000")

    # When true, the webhook short-circuits to a simple echo reply with the
    # ViMa greeting — no DB, no Claude. Useful for first-time wiring tests.
    echo_mode: bool = Field(default=False)

    # --- Claude (Anthropic) ---
    anthropic_api_key: str = Field(default="")
    claude_model: str = Field(default="claude-sonnet-4-6")
    claude_max_tokens: int = Field(default=2048)

    # --- Gupshup (WhatsApp) ---
    gupshup_api_key: str = Field(default="")
    gupshup_app_name: str = Field(default="")
    gupshup_source_number: str = Field(default="")
    gupshup_webhook_secret: str = Field(default="")

    # --- Supabase ---
    supabase_url: str = Field(default="")
    supabase_service_key: str = Field(default="")
    supabase_anon_key: str = Field(default="")

    # --- Razorpay ---
    razorpay_key_id: str = Field(default="")
    razorpay_key_secret: str = Field(default="")
    razorpay_webhook_secret: str = Field(default="")

    # --- Pricing (INR paise) ---
    price_subscription_paise: int = Field(default=179900)  # Rs.1799/month

    # --- Storage ---
    supabase_storage_bucket: str = Field(default="vima-artifacts")

    # --- Voice: AWS Transcribe ---
    aws_access_key_id: str = Field(default="")
    aws_secret_access_key: str = Field(default="")
    aws_region: str = Field(default="ap-south-1")
    # Transcribe writes its JSON output to this S3 bucket (may reuse storage bucket).
    aws_transcribe_s3_bucket: str = Field(default="")

    # --- Public-facing site ---
    # E.164 without leading '+' (e.g. 919876543210). Used to build the
    # `wa.me/<number>` deeplink on the landing page.
    vima_whatsapp_number: str = Field(default="")
    # Email shown in the privacy policy for data-deletion requests.
    support_email: str = Field(default="hello@vima.coach")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
