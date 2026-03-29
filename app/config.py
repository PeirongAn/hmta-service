"""Service-wide configuration via pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM — OpenAI or any OpenAI-compatible endpoint
    openai_api_key: str = ""
    openai_base_url: str = ""          # leave empty to use official api.openai.com
    anthropic_api_key: str = ""
    coze_api_key: str = ""

    # Default models per role (can be overridden per-request)
    planner_model: str = "gpt-4o"
    builder_model: str = "gpt-4o"
    fixer_model: str = "gpt-4o-mini"

    # Zenoh
    zenoh_router: str = "tcp/localhost:7447"

    # FastAPI
    service_host: str = "0.0.0.0"
    service_port: int = 8000
    log_level: str = "INFO"

    # Generation limits
    default_max_iterations: int = 3

    # LangSmith tracing
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "hmta-service"


settings = Settings()
