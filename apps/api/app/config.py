from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://guitar:guitar@postgres:5432/guitar"

    llm_provider: str = "qwen"
    llm_api_key: str = "none"
    llm_base_url: str = "http://qwen-vllm:6888/v1"   # QWEN ONLY — see below
    llm_model: str = "/models/Qwen3.5-9B"

    # Claude's base URL is a SEPARATE setting from `llm_base_url`, and the
    # distinction is load-bearing: `llm_base_url` is the Qwen vLLM box and is
    # ALWAYS truthy, so a ClaudeProvider that read it would quietly send every
    # request to the local 9B model. (It did, briefly. The symptom was the
    # Settings screen telling the tutor "this model is not available on your
    # account" for a perfectly good key — because vLLM 404s on
    # /v1/models/claude-sonnet-5.)
    #
    # Empty by default -> the SDK talks to api.anthropic.com. Set it only to
    # point at a gateway that speaks the Anthropic Messages API.
    anthropic_base_url: str = ""

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

    # ---- Auth (Plan 13 Task 3.1) ------------------------------------------
    #
    # OFF BY DEFAULT, and that is not laziness. `tests/conftest.py` imports
    # `app.main` at collection time and ~40 test modules drive the API through
    # `TestClient` with no cookie jar; a default of True would 401 all of them
    # at import. Production turns it on explicitly (docker-compose / .env).
    auth_enabled: bool = False
    # The single shared password. Empty => every login attempt fails, even with
    # `auth_enabled=True` (fail closed: a blank password must never be a valid
    # one). See `app.auth.session.check_password`.
    app_password: str = ""
    # Signs the `gt_session` cookie. NOT the same secret as `encryption_secret`
    # below: rotating this one logs the tutor out; if it also derived the Fernet
    # key it would additionally destroy his stored Anthropic key.
    app_secret: str = "dev-insecure-session-secret-change-me"
    session_max_age: int = 60 * 60 * 24 * 14   # 14 days
    # Empty => a HOST-ONLY cookie, which is what localhost needs: cookies ignore
    # the port, so a host-only cookie set by the API on `localhost:8791` is sent
    # to the web app on `localhost:8790`. In production this is
    # `.cgrigoriadis.online` so ONE cookie covers `guitar.` and `guitar-api.`.
    cookie_domain: str = ""
    cookie_secure: bool = False   # True in prod (HTTPS); False for plain-http dev

    # ---- Secret at rest (Plan 13 Task 3.4) --------------------------------
    #
    # Fernet key material for `app_setting.anthropic_key_ct`. Deliberately
    # distinct from `app_secret` — see above.
    encryption_secret: str = "dev-insecure-encryption-secret-change-me"

settings = Settings()
