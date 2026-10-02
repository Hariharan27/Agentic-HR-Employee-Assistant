from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Agentic HR & Employee Assistant"
    environment: str = "development"
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
    bedrock_anthropic_base_url: str = "https://bedrock-mantle.us-east-1.api.aws/anthropic"
    router_model_id: str = "openai.gpt-oss-20b"
    standard_model_id: str = "openai.gpt-oss-120b"
    complex_model_id: str = "anthropic.claude-haiku-4-5"
    router_max_output_tokens: int = 250
    standard_max_output_tokens: int = 700
    complex_max_output_tokens: int = 900
    complex_escalation_threshold: float = 0.70
    llm_max_retries: int = 1
    llm_max_calls_per_request: int = 3
    cors_origins: str = "http://localhost:5173"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
