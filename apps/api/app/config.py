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

    # ---- claude_cli provider: Claude on the SUBSCRIPTION, via `claude -p` ----
    #
    # A THIRD wallet, and the distinction is the whole point. `llm_api_key` buys
    # Anthropic API tokens. A Claude Max subscription buys ZERO of those — it
    # authenticates the Claude Code CLI, and nothing else. `LLM_PROVIDER=claude_cli`
    # spends the subscription instead of the card (see app/llm/claude_cli.py).
    #
    # The CLI does not run in THIS container and its credentials are not mounted
    # here. It runs in the sibling `claude-bridge` service (docker-compose.yml),
    # which is the only container that ever sees ~/.claude — this one is the
    # web-facing process and deliberately gets nothing. Plain compose DNS: the
    # service name resolves on `appnet`.
    claude_bridge_url: str = "http://claude-bridge:8799"
    # Shared secret. The bridge binds a TCP port that SPENDS MONEY, so it refuses
    # to start without one, and refuses any request that does not carry it. Must be
    # byte-identical to the value the bridge was started with.
    claude_bridge_token: str = ""

    # ---- Embeddings (a SEPARATE seam from the chat provider — Claude has no
    # embeddings endpoint; see app/llm/embedder.py's docstring) -------------
    #
    # `local-e5` since Plan 13 Stage 4, flipped in the SAME commit as the
    # migration that made it true (`a3c7e1b90d42`: chunk.embedding is now
    # `vector(384)`). The order was never optional: flipping this before the
    # column changed would have handed a 384-dim query vector to a 2560-dim
    # column and failed every search at the DB.
    #
    # `qwen` remains selectable, and it is now a FOOT-GUN, not a fallback: the
    # column is 384 wide, so a Qwen backend fails at the first insert. It stays
    # only so a future embedding swap has a second implementation to look at.
    embed_backend: str = "local-e5"              # "local-e5" | "qwen"
    embed_base_url: str = "http://qwen-emb-vllm:8090/v1"   # qwen backend only
    embed_model: str = "qwen3-emb-4b"                      # qwen backend only
    # qwen backend ONLY. The live column width is `app.models.knowledge.EMBED_DIM`,
    # which is hardcoded — a column width is a fact about the bytes on disk, not a
    # setting (see that constant's comment).
    embed_dim: int = 2560
    # local-e5 backend: weights are baked into the image at build time.
    embed_model_dir: str = "/opt/models/e5-small"
    embed_threads: int = 4
    embed_batch_size: int = 16

    # Comma-separated allowlist of browser origins the API accepts (CORS).
    # `127.0.0.1` variants are NOT redundant with `localhost`: the browser
    # compares CORS origins as strings, and a tutor who types 127.0.0.1:8790
    # into the address bar gets a login page whose every API call fails
    # preflight — same app, same machine, "Couldn't reach the server."
    cors_origins: str = (
        "http://localhost:3000,http://localhost:8790,"
        "http://127.0.0.1:3000,http://127.0.0.1:8790,"
        "https://guitar.cgrigoriadis.online"
    )

    media_dir: str = "/media"     # page scans live here; mounted volume

    # Pages ROUTED TO VISION are re-rendered from the source PDF at this DPI.
    # `paginate.RENDER_DPI = 110` is a QWEN CEILING, not a quality choice: at
    # 150dpi the local vLLM rejects the image outright ("image item with length
    # 2080 exceeds pre-allocated encoder cache size 2048") and every page fails.
    # Claude is high-resolution tier — 110dpi is 1,496 visual tokens, 150dpi is
    # 2,714 — so pointing it at the stored 110dpi JPEG would silently cap its
    # fidelity on exactly the glyphs this plan exists for: `¼` and `⅛` differ by
    # a few pixels at page scale, and "¼-inch" read as "4-inch" is the wrong
    # FACT that started all of this.
    #
    # Only vision pages pay it (on Powers, ~40 of 888). The stored scan stays at
    # 110 — it is the Reader's thumbnail and does not need more.
    ocr_render_dpi: int = 150

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

    # ---- Curriculum authoring (Plan 13 Stage 6) ---------------------------
    #
    # Above this, the tutor's library does NOT go into the prompt whole and
    # authoring degrades to per-module retrieval — with a banner, never silently
    # (see app/curriculum/corpus.py). 600K leaves ~400K of Sonnet 5's 1M window
    # for a 32K lesson output plus the volatile tail. His real library measures
    # ~90K, so this is headroom for a library 6x the one he has.
    full_context_budget: int = 600_000
    # Lesson drafts that run at once. TWO, deliberately: each is a 32K-output
    # call, and a fresh Anthropic account's per-minute OUTPUT token limit is the
    # binding constraint long before wall-clock is. A 429 puts a lesson back to
    # `queued`, not `failed` — but the cheapest 429 is the one we never provoke.
    draft_concurrency: int = 2

    # ---- Connection pool --------------------------------------------------
    #
    # RAISED from SQLAlchemy's default 5/10 for Stage 6, and this is not tuning.
    # The draft fan-out runs `draft_concurrency` worker threads, each opening its
    # OWN SessionLocal, while the board polls `GET /curricula/{root}/progress`
    # every 2 seconds from the request path. At 5/10 those compete, and the one
    # that loses is the poll — which times out, and a timed-out progress poll
    # looks EXACTLY like the flagship feature being broken. It isn't; it just
    # cannot get a connection to say so.
    db_pool_size: int = 15
    db_max_overflow: int = 10

    # ---- Secret at rest (Plan 13 Task 3.4) --------------------------------
    #
    # Fernet key material for `app_setting.anthropic_key_ct`. Deliberately
    # distinct from `app_secret` — see above.
    encryption_secret: str = "dev-insecure-encryption-secret-change-me"

settings = Settings()
