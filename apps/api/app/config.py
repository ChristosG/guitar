from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://guitar:guitar@postgres:5432/guitar"

    llm_provider: str = "qwen"
    llm_api_key: str = "none"
    llm_base_url: str = "http://qwen-vllm:6888/v1"
    llm_model: str = "/models/Qwen3.5-9B"
    embed_base_url: str = "http://qwen-emb-vllm:8090/v1"
    embed_model: str = "qwen3-emb-4b"
    embed_dim: int = 2560

    # Comma-separated allowlist of browser origins the API accepts (CORS).
    cors_origins: str = "http://localhost:3000,http://localhost:8790,https://guitar.cgrigoriadis.online"

settings = Settings()
