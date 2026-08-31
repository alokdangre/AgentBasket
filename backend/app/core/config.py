from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AgentBasket Commerce Core"
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://agentbasket:agentbasket@localhost:5432/agentbasket"
    sql_echo: bool = False
    checkout_ttl_minutes: int = Field(default=10, ge=1, le=60)
    auth_session_ttl_days: int = Field(default=30, ge=1, le=90)
    merchant_admin_email: str | None = None
    merchant_admin_password: str | None = Field(default=None, min_length=12)
    merchant_admin_name: str = "Merchant Owner"
    demo_customer_email: str | None = None
    demo_customer_password: SecretStr | None = Field(default=None, min_length=12)
    demo_customer_name: str = "Aarav Mehta"
    demo_customer_phone: str = "+919876543210"
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None
    razorpay_api_url: str = "https://api.razorpay.com/v1"
    razorpay_timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)
    google_api_key: SecretStr | None = None
    agent_model: str = "gemini-3.5-flash-lite"
    agent_timeout_seconds: float = Field(default=30.0, ge=5.0, le=60.0)
    agent_max_history_messages: int = Field(default=20, ge=4, le=40)
    agent_max_output_characters: int = Field(default=6000, ge=1000, le=12000)
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "agentbasket-local"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_workspace_id: str | None = None
    langsmith_hide_inputs: bool = True
    langsmith_hide_outputs: bool = True
    ap2_audience: str = "agentbasket-commerce"
    ap2_challenge_ttl_seconds: int = Field(default=300, ge=60, le=900)
    ap2_merchant_issuer: str = "urn:agentbasket:merchant:ember-and-leaf"
    ap2_merchant_key_id: str = "ember-merchant-test-1"
    ap2_merchant_private_key_pem: SecretStr | None = None
    ap2_trusted_surface_issuer: str = "urn:agentbasket:trusted-surface:test"
    ap2_trusted_surface_key_id: str = "agentbasket-surface-test-1"
    ap2_trusted_surface_private_key_pem: SecretStr | None = None
    ap2_payment_processor_issuer: str = "urn:agentbasket:payment-processor:razorpay-test"
    ap2_payment_processor_key_id: str = "agentbasket-processor-test-1"
    ap2_payment_processor_private_key_pem: SecretStr | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
