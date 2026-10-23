import os
from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

class Settings(BaseModel):
    # Upstream Remote Target URLs
    target_mcp_url: str = os.getenv("TARGET_MCP_URL", "")
    target_auth_url: str = os.getenv("TARGET_AUTH_URL", "")
    target_token_url: str = os.getenv("TARGET_TOKEN_URL", "")

    # Server Configuration
    port: int = int(os.getenv("PORT", "8080"))
    host: str = os.getenv("HOST", "0.0.0.0")

    # Logging & Persistence Configuration
    gcs_log_bucket: str = os.getenv("GCS_LOG_BUCKET", "")
    local_log_dir: str = os.getenv("LOCAL_LOG_DIR", "./traffic_logs")
    log_payloads: bool = os.getenv("LOG_PAYLOADS", "true").lower() in ("true", "1", "yes")
    log_payload_max_bytes: int = int(os.getenv("LOG_PAYLOAD_MAX_BYTES", str(5 * 1024 * 1024))) # 5MB limit
    redact_secrets: bool = os.getenv("REDACT_SECRETS", "false").lower() in ("true", "1", "yes")

    # Upstream HTTP client timeout (MCP SSE sessions may remain open for an extended duration)
    client_timeout_seconds: float = float(os.getenv("CLIENT_TIMEOUT_SECONDS", "3600.0"))

settings = Settings()
