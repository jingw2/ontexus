from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    environment: str = "development"  # development | production
    database_url: str = "sqlite:///./ontexus.db"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "dev-secret-key"
    encryption_key: str = ""
    first_admin_user: str = "admin"
    first_admin_password: str = "admin123"
    uploads_dir: str = "./uploads"
    access_token_expire_minutes: int = 1440  # 24h
    oauth_frontend_base_url: str = "http://localhost:5173"
    # Sign in with ChatGPT. Empty client id disables the flow.
    # Authorization https://auth.openai.com/api/accounts/authorize
    # Token https://auth.openai.com/api/accounts/oauth/token
    chatgpt_oauth_client_id: str = ""
    chatgpt_oauth_client_secret: str = ""
    chatgpt_oauth_redirect_uri: str = ""
    oauth_authorization_code_expire_seconds: int = 300  # 5 minutes
    oauth_access_token_expire_minutes: int = 60  # 1 hour
    oauth_refresh_token_expire_days: int = 30
    mcp_write_request_expire_hours: int = 72

    # CORS - 逗号分隔的来源列表，可通过环境变量配置
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # 上传限制
    max_upload_mb: int = 200
    allowed_upload_extensions: str = "csv,xlsx,xls,json,xml,pdf,docx,doc,pptx,ppt,md,txt"

    # v2 — Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "ontexus123"

    # v2 — MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_use_ssl: bool = False

    # v2 — ChromaDB
    chroma_host: str = "localhost"
    chroma_port: int = 8001

    # Business-journey acceptance (plan 2026-08-27): gates
    # `app.tasks.agent_turn._resolve_business_journey` — the ONLY switch
    # that can route a real Agent turn through the privileged, no-approval-
    # gate `LangGraphRuntime._run_business_journey_turn` protocol (server
    # `DEEPSEEK_API_KEY` credential, automatic `entity_instances` mutation).
    # `model_config_versions.options` is an editor-writable, unvalidated
    # JSON blob — without this flag, ANY editor could tag their own model
    # config with a `business_journey` block (using only public constants)
    # and opt their own Agent into that privileged mode. Must be true ONLY
    # in the acceptance-testing/CI environment this plan controls; false
    # (the default) in every real deployment.
    business_journey_acceptance_enabled: bool = False
    # The preparation-evidence writer is a CI/gate identity, not a general
    # editor API. Empty is deliberate: writes fail closed until a dedicated
    # gate user is explicitly configured.
    business_journey_gate_username: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}

settings = Settings()

# .env.example ships this fixed value so every docker-compose service reads
# the SAME encryption key by default (an empty value made each container
# generate its own random per-process key, so a value encrypted by the API
# server could never be decrypted by a worker container).
_DEV_ENCRYPTION_KEY = "R6uyMI-_OtgEug0gGXuDYVvNzFmASSe9ltykKeIkpwQ="

# 生产环境禁止使用默认凭据 — 启动即失败, 避免带默认密钥上线
if settings.environment == "production":
    _insecure = []
    if settings.secret_key == "dev-secret-key":
        _insecure.append("SECRET_KEY")
    if settings.first_admin_password == "admin123":
        _insecure.append("FIRST_ADMIN_PASSWORD")
    if settings.minio_access_key == "minioadmin" or settings.minio_secret_key == "minioadmin":
        _insecure.append("MINIO_ACCESS_KEY/MINIO_SECRET_KEY")
    if not settings.encryption_key or settings.encryption_key == _DEV_ENCRYPTION_KEY:
        _insecure.append("ENCRYPTION_KEY")
    if _insecure:
        raise RuntimeError(
            f"ENVIRONMENT=production 但以下配置仍为默认值, 必须通过环境变量注入: {', '.join(_insecure)}"
        )
    if settings.business_journey_acceptance_enabled:
        raise RuntimeError(
            "ENVIRONMENT=production 禁止启用 BUSINESS_JOURNEY_ACCEPTANCE_ENABLED "
            "(该开关仅供本计划受控的验收/CI 环境使用)"
        )
