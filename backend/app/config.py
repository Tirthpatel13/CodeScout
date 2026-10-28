"""Application settings, loaded from environment."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# voyage-code-3 native dimension. Changing this requires a migration, because the
# pgvector column dimension is fixed at DDL time.
EMBEDDING_DIM = 1024


def _rewrite_query(
    url: str, *, rename: dict[str, str] | None = None, drop: set[str] | None = None
) -> str:
    """Rename or drop query parameters, leaving the rest of the URL byte-for-byte alone."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    rename, drop = rename or {}, drop or set()
    pairs = [
        (rename.get(key, key), value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in drop
    ]
    return urlunsplit(parts._replace(query=urlencode(pairs)))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    debug: bool = False

    # --- storage -------------------------------------------------------
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/codescout"
    redis_url: str = "redis://localhost:6379/0"

    # --- auth ----------------------------------------------------------
    session_secret: str = "dev-only-insecure-session-secret-change-me"
    token_encryption_key: str = ""  # urlsafe base64 32-byte Fernet key
    github_client_id: str = ""
    github_client_secret: str = ""
    frontend_origin: str = "http://localhost:3000"

    # --- providers -----------------------------------------------------
    llm_provider: Literal["anthropic", "fake"] = "anthropic"
    embedding_provider: Literal["voyage", "fake"] = "voyage"
    anthropic_api_key: str = ""
    voyage_api_key: str = ""
    answer_model: str = "claude-sonnet-4-5"
    overview_model: str = "claude-haiku-4-5"
    embedding_model: str = "voyage-code-3"

    # --- agent ---------------------------------------------------------
    tool_budget: int = 12
    max_answer_tokens: int = 2048
    prompt_version: str = "answer_v1"

    # --- ingestion limits ----------------------------------------------
    max_repo_files: int = 50_000
    max_file_bytes: int = 1_000_000
    max_chunk_tokens: int = 700
    chunk_overlap_lines: int = 2
    embed_batch_size: int = 96
    clone_depth: int = 1

    # --- rate limits ---------------------------------------------------
    questions_per_minute: int = 10
    index_jobs_per_hour: int = 3

    workspace_dir: str = Field(default="/tmp/codescout-workspaces")

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        # Managed Postgres providers (Render, Neon, Heroku, ...) hand back a
        # plain postgres:// or postgresql:// URL, often with libpq-only query
        # params. Normalize to the asyncpg driver and its spelling of them
        # rather than rejecting the URL.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                v = "postgresql+asyncpg://" + v[len(prefix) :]
                break
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must be a postgres connection string")
        return _rewrite_query(v, rename={"sslmode": "ssl"}, drop={"channel_binding"})

    @property
    def sync_database_url(self) -> str:
        """Alembic runs synchronously, over psycopg3 (which speaks libpq's sslmode)."""
        url = self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
        return _rewrite_query(url, rename={"ssl": "sslmode"})


@lru_cache
def get_settings() -> Settings:
    return Settings()
