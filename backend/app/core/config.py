from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Agentic HR & Employee Assistant"
    environment: str = "development"
    app_timezone: str = "Asia/Kolkata"
    database_url: str = "postgresql+psycopg://hr_app:hr_app@postgres:5432/hr_assistant"
    jwt_secret: str = Field(default="local-development-secret-change-me-32", min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 480
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "hr_policies"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    policy_docs_dir: str = "policy_docs"
    rag_collection_vector_size: int = 384
    rag_chunk_size: int = 1600
    rag_chunk_overlap: int = 250
    rag_top_k: int = 4
    rag_score_threshold: float = 0.45
    llm_provider: str = "bedrock-mantle"
    bedrock_region: str = "us-east-1"
    bedrock_api_key: str = ""
    bedrock_openai_base_url: str = "https://bedrock-mantle.us-east-1.api.aws/v1"
    router_model_id: str = "openai.gpt-oss-20b"
    standard_model_id: str = "openai.gpt-oss-120b"
    complex_model_id: str = "openai.gpt-oss-120b"
    router_reasoning_effort: str = "low"
    complex_reasoning_effort: str = "medium"
    router_max_output_tokens: int = 250
    standard_max_output_tokens: int = 700
    complex_max_output_tokens: int = 1200
    complex_escalation_threshold: float = 0.70
    llm_max_retries: int = 1
    llm_max_calls_per_request: int = Field(default=8, ge=1, le=20)
    leave_agent_max_iterations: int = Field(default=6, ge=1, le=12)
    leave_agent_max_tool_calls: int = Field(default=8, ge=1, le=20)
    # Use the provider's native tool calling instead of the JSON decision protocol.
    # Keep False until `python -m app.llm.probe` confirms the model supports it.
    leave_agent_native_tools: bool = False
    # Extra model turns allowed to repair invalid output or an ungrounded answer.
    leave_agent_max_repairs: int = Field(default=1, ge=0, le=3)
    parking_booking_horizon_days: int = Field(default=30, ge=1, le=365)
    parking_cancellation_cutoff_hour: int = Field(default=20, ge=0, le=23)
    parking_check_in_open_hour: int = Field(default=7, ge=0, le=23)
    parking_arrival_cutoff_hour: int = Field(default=11, ge=0, le=23)
    parking_no_show_grace_minutes: int = Field(default=15, ge=0, le=180)
    parking_no_show_lookback_days: int = Field(default=30, ge=1, le=365)
    parking_no_show_strike_limit: int = Field(default=3, ge=1, le=20)
    parking_suspension_days: int = Field(default=14, ge=1, le=365)
    # Department notification recipients.
    # These can be overridden through environment variables.
    hr_notification_email: str = "hr@example.com"
    it_notification_email: str = "it@example.com"
    finance_notification_email: str = "finance@example.com"
    facilities_notification_email: str = "facilities@example.com"
    # Shared secret the email provider sends as X-Inbound-Token on /api/v1/inbound/email.
    # Empty disables the endpoint.
    inbound_email_token: str = ""
    cors_origins: str = "http://localhost:5173"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
