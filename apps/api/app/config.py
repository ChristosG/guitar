from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://guitar:guitar@postgres:5432/guitar"

    llm_provider: str = "qwen"
    llm_api_key: str = "none"
    llm_base_url: str = "http://qwen-vllm:6888/v1"
    llm_model: str = "/models/Qwen3.5-9B"

    # ---- Embeddings (a SEPARATE seam from the chat provider — Claude has no
    # embeddings endpoint; see app/llm/embedder.py's docstring) -------------
    #
    # `qwen` is still the default because the live `chunk.embedding` column is
    # `vector(2560)` and holds 408 Qwen vectors. Flipping to `local-e5` is a
    # DATA migration (ALTER TYPE vector(384) + a full re-embed), not a config
    # change — doing it out of order hands a 384-dim query to a 2560-dim column
    # and fails every search at the DB. Plan 13 Stage 4 flips this default in
    # the same commit as the migration.
    embed_backend: str = "qwen"                  # "qwen" | "local-e5"
    embed_base_url: str = "http://qwen-emb-vllm:8090/v1"   # qwen backend only
    embed_model: str = "qwen3-emb-4b"                      # qwen backend only
    embed_dim: int = 2560                                  # qwen backend only
    # local-e5 backend: weights are baked into the image at build time.
    embed_model_dir: str = "/opt/models/e5-small"
    embed_threads: int = 4
    embed_batch_size: int = 16

    # Comma-separated allowlist of browser origins the API accepts (CORS).
    cors_origins: str = "http://localhost:3000,http://localhost:8790,https://guitar.cgrigoriadis.online"

    media_dir: str = "/media"     # page scans live here; mounted volume

settings = Settings()
