import secrets
from typing import List

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from cryptography.fernet import Fernet

from app.core.google_scopes import DEFAULT_SERVICES, SERVICES


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Runtime ───────────────────────────────────────────────────────────────
    ENVIRONMENT: str = "development"  # development | staging | production
    LOG_LEVEL: str = "INFO"
    # Set when the app is served under a path prefix by a reverse proxy
    # (e.g. https://mcp.sabahhub.com/mcp) so generated URLs stay correct.
    ROOT_PATH: str = ""
    # Public origin of this service — used to build the OAuth redirect URI.
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://user:password@localhost:5432/mcpdb"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Security ──────────────────────────────────────────────────────────────
    SECRET_KEY: str = "changeme"
    FERNET_KEY: str = ""
    # Shared secret for server-to-server calls from SABAH.OS (Django).
    INTERNAL_API_KEY: str = ""

    # ── Google OAuth ──────────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/google/callback"
    # Google Workspace domain(s) allowed to sign in. Corporate deployment:
    # only company accounts may connect. Empty list = allow any (dev only).
    ALLOWED_EMAIL_DOMAINS: List[str] = []
    # Passed to Google as `hd` so the account chooser is pre-filtered.
    GOOGLE_HOSTED_DOMAIN: str = ""
    # Services requested on the consent screen.
    GOOGLE_SERVICES: List[str] = DEFAULT_SERVICES

    # ── SABAH.OS integration ──────────────────────────────────────────────────
    # Where to send the browser after the OAuth dance completes.
    FRONTEND_REDIRECT_URL: str = "http://localhost:3000/auth/google/callback"

    # ── LLM (optional) ────────────────────────────────────────────────────────
    # Leave OPENAI_API_KEY empty to run without an LLM: /automation/run then
    # returns 503 and everything else — Google OAuth, the direct /google
    # endpoints, the MCP server this app exposes — is unaffected.
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4.1"
    LLM_MAX_OUTPUT_TOKENS: int = 4096

    # ── CORS ──────────────────────────────────────────────────────────────────
    ALLOWED_ORIGINS: List[str] = ["http://localhost:3000"]

    # ── JWT ───────────────────────────────────────────────────────────────────
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60
    # Lifetime of the signed OAuth `state` value.
    OAUTH_STATE_TTL_SECONDS: int = 600

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in ("production", "prod", "staging")

    @field_validator("FERNET_KEY")
    @classmethod
    def validate_fernet_key(cls, v: str) -> str:
        if not v:
            return v
        try:
            Fernet(v.encode())
        except Exception as exc:
            raise ValueError(f"Invalid FERNET_KEY: {exc}") from exc
        return v

    @field_validator("ALLOWED_EMAIL_DOMAINS", mode="after")
    @classmethod
    def normalise_domains(cls, v: List[str]) -> List[str]:
        return [d.strip().lower().lstrip("@") for d in v if d and d.strip()]

    @field_validator("GOOGLE_SERVICES", mode="after")
    @classmethod
    def validate_services(cls, v: List[str]) -> List[str]:
        unknown = [s for s in v if s not in SERVICES]
        if unknown:
            raise ValueError(
                f"Unknown GOOGLE_SERVICES entries: {unknown}. "
                f"Valid keys: {sorted(SERVICES)}"
            )
        return v

    @model_validator(mode="after")
    def enforce_production_secrets(self) -> "Settings":
        """
        Refuse to boot a production deployment with development placeholders.
        A weak SECRET_KEY forges JWTs; a missing FERNET_KEY means Google refresh
        tokens cannot be encrypted at all.
        """
        if not self.is_production:
            return self

        problems = []
        if not self.FERNET_KEY:
            problems.append("FERNET_KEY is required in production")
        if self.SECRET_KEY in ("", "changeme") or len(self.SECRET_KEY) < 32:
            problems.append("SECRET_KEY must be a random value of at least 32 chars")
        if not self.INTERNAL_API_KEY or len(self.INTERNAL_API_KEY) < 32:
            problems.append("INTERNAL_API_KEY must be set (>= 32 chars) in production")
        if not self.GOOGLE_CLIENT_ID or not self.GOOGLE_CLIENT_SECRET:
            problems.append("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are required")
        if not self.ALLOWED_EMAIL_DOMAINS:
            problems.append(
                "ALLOWED_EMAIL_DOMAINS must list the corporate domain(s); "
                "an empty list would let any Google account connect"
            )
        if self.PUBLIC_BASE_URL.startswith("http://") and "localhost" not in self.PUBLIC_BASE_URL:
            problems.append("PUBLIC_BASE_URL must use https in production")
        if "*" in self.ALLOWED_ORIGINS:
            problems.append("ALLOWED_ORIGINS may not be '*' with credentialed CORS")

        if problems:
            raise ValueError(
                "Invalid production configuration:\n  - " + "\n  - ".join(problems)
            )
        return self

    def email_domain_allowed(self, email: str) -> bool:
        """Corporate gate: only company Google accounts may connect."""
        if not self.ALLOWED_EMAIL_DOMAINS:
            return True  # dev convenience; production config forbids the empty list
        domain = email.rsplit("@", 1)[-1].lower()
        return domain in self.ALLOWED_EMAIL_DOMAINS


settings = Settings()


def generate_internal_key() -> str:
    """`python -c "from app.core.config import generate_internal_key as g; print(g())"`"""
    return secrets.token_urlsafe(48)
