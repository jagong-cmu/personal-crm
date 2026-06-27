"""Application settings (pydantic-settings, .env-driven).

T12 (key management): the Fernet key is loaded from TOKEN_ENCRYPTION_KEY_FILE when
set, keeping it separate from DATABASE_URL. Falls back to the inline env var only
if no file path is given.
"""
from __future__ import annotations

import uuid
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    # Migrations need DDL + role privileges, so they run as the superuser/owner while the
    # app runs as the non-superuser 'crm_app' role (RLS enforcement). Falls back to
    # database_url when unset (e.g. CI, where one role both migrates and queries).
    admin_database_url: str = ""

    anthropic_api_key: str
    voyage_api_key: str

    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    token_encryption_key: str = ""
    token_encryption_key_file: str = ""

    default_tenant_id: str = "00000000-0000-0000-0000-000000000001"

    voyage_model: str = "voyage-3.5"
    voyage_dim: int = 1024
    anthropic_model: str = "claude-opus-4-8"
    rag_top_k: int = Field(default=8, ge=1, le=50)

    @property
    def migration_url(self) -> str:
        """DB URL for migrations/DDL — the admin role when set, else the runtime URL."""
        return self.admin_database_url or self.database_url

    @property
    def tenant_uuid(self) -> uuid.UUID:
        return uuid.UUID(self.default_tenant_id)

    @property
    def fernet_key(self) -> str:
        """Resolve the encryption key: file path wins over inline env var."""
        if self.token_encryption_key_file:
            path = Path(self.token_encryption_key_file)
            if not path.exists():
                raise RuntimeError(
                    f"TOKEN_ENCRYPTION_KEY_FILE={path} not found. Generate one:\n"
                    "  python -c \"from cryptography.fernet import Fernet; "
                    "print(Fernet.generate_key().decode())\" > secrets/token.key"
                )
            return path.read_text().strip()
        if self.token_encryption_key:
            return self.token_encryption_key
        raise RuntimeError(
            "No encryption key configured. Set TOKEN_ENCRYPTION_KEY_FILE or "
            "TOKEN_ENCRYPTION_KEY in .env."
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
