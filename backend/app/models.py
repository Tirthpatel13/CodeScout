"""SQLAlchemy 2.0 models.

Design note: an IndexRun is an immutable snapshot of a repository pinned to a
commit SHA. Files, symbols, edges and chunks all hang off a run, never off the
repository directly. Answers record which run produced them, so a citation can
never drift onto a line that has since moved.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import EMBEDDING_DIM


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    login: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    # Fernet-encrypted GitHub OAuth token. Never logged, never serialised to the client.
    access_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = _ts()


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("owner", "name", name="uq_repositories_owner_name"),)

    id: Mapped[uuid.UUID] = _pk()
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _ts()

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


class RepoAccess(Base):
    __tablename__ = "repo_access"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = _ts()


# --------------------------------------------------------------------------
# ingestion
# --------------------------------------------------------------------------
class RunStatus(str, enum.Enum):
    QUEUED = "queued"
    CLONING = "cloning"
    PARSING = "parsing"
    RESOLVING = "resolving"
    EMBEDDING = "embedding"
    SUMMARIZING = "summarizing"
    READY = "ready"
    FAILED = "failed"


TERMINAL_STATUSES = {RunStatus.READY, RunStatus.FAILED}


class IndexRun(Base):
    __tablename__ = "index_runs"
    __table_args__ = (
        UniqueConstraint("repository_id", "commit_sha", name="uq_index_runs_repo_commit"),
        Index("ix_index_runs_repo_status", "repository_id", "status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=RunStatus.QUEUED.value)
    phase_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    overview: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()

    repository: Mapped[Repository] = relationship(lazy="joined")


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("index_run_id", "path", name="uq_files_run_path"),
        Index("ix_files_run_lang", "index_run_id", "language"),
        Index("ix_files_content_hash", "content_hash"),
    )

    id: Mapped[uuid.UUID] = _pk()
    index_run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("index_runs.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(32))
    loc: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Full text, stored with the run. read_file and grep must see exactly the
    # snapshot that produced an answer, and reassembling it from overlapping
    # chunks is both lossy and slower than keeping the source of truth here.
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")


class SymbolKind(str, enum.Enum):
    FUNCTION = "function"
    CLASS = "class"
    METHOD = "method"
    INTERFACE = "interface"
    TYPE = "type"
    CONST = "const"


class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        Index("ix_symbols_file", "file_id"),
        Index("ix_symbols_run_qname", "index_run_id", "qualified_name"),
        Index(
            "ix_symbols_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        CheckConstraint("end_line >= start_line", name="ck_symbols_line_order"),
    )

    id: Mapped[uuid.UUID] = _pk()
    index_run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("index_runs.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("symbols.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str | None] = mapped_column(Text)
    docstring: Mapped[str | None] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)

    file: Mapped[File] = relationship(lazy="joined")


class EdgeKind(str, enum.Enum):
    CALLS = "calls"
    IMPORTS = "imports"
    INHERITS = "inherits"
    REFERENCES = "references"


class SymbolEdge(Base):
    """A directed edge in the code graph.

    dst_symbol_id is nullable on purpose: an unresolved target is almost always a
    third-party dependency, and knowing a symbol calls `requests.get` is useful
    even though `requests` is not in this repo.
    """

    __tablename__ = "symbol_edges"
    __table_args__ = (
        Index("ix_edges_src", "src_symbol_id", "kind"),
        Index("ix_edges_dst", "dst_symbol_id", "kind"),
        Index("ix_edges_run_dstname", "index_run_id", "dst_name"),
    )

    id: Mapped[uuid.UUID] = _pk()
    index_run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("index_runs.id", ondelete="CASCADE"), nullable=False
    )
    src_symbol_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False
    )
    dst_symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("symbols.id", ondelete="CASCADE")
    )
    dst_name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    line: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ChunkKind(str, enum.Enum):
    CODE = "code"
    DOC = "doc"
    CONFIG = "config"


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_file", "file_id"),
        Index("ix_chunks_run_kind", "index_run_id", "kind"),
        Index("ix_chunks_content_hash", "content_hash"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        CheckConstraint("end_line >= start_line", name="ck_chunks_line_order"),
    )

    id: Mapped[uuid.UUID] = _pk()
    index_run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("index_runs.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("symbols.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))

    file: Mapped[File] = relationship(lazy="joined")


class EmbeddingCache(Base):
    """content_hash -> vector, shared across every run and repository.

    This is what makes re-indexing a new commit cheap: unchanged code is never
    re-embedded. Report the hit rate; it is the best number in the project.
    """

    __tablename__ = "embedding_cache"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = _ts()


# --------------------------------------------------------------------------
# conversations
# --------------------------------------------------------------------------
class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user_repo", "user_id", "repository_id"),)

    id: Mapped[uuid.UUID] = _pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation", "conversation_id", "created_at"),)

    id: Mapped[uuid.UUID] = _pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    index_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("index_runs.id", ondelete="SET NULL")
    )
    confidence: Mapped[str | None] = mapped_column(String(16))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    total_cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _ts()


class AgentStep(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (Index("ix_agent_steps_message", "message_id", "idx"),)

    id: Mapped[uuid.UUID] = _pk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)


class Citation(Base):
    __tablename__ = "citations"
    __table_args__ = (Index("ix_citations_message", "message_id"),)

    id: Mapped[uuid.UUID] = _pk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)


# --------------------------------------------------------------------------
# evals
# --------------------------------------------------------------------------
class EvalCase(Base):
    __tablename__ = "eval_cases"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    repo_slug: Mapped[str] = mapped_column(String(255), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    expected_paths: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    expected_symbols: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    rubric: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = _pk()
    git_sha: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvalResult(Base):
    __tablename__ = "eval_results"
    __table_args__ = (Index("ix_eval_results_run", "eval_run_id"),)

    id: Mapped[uuid.UUID] = _pk()
    eval_run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    eval_case_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("eval_cases.id", ondelete="CASCADE"), nullable=False
    )
    answer: Mapped[str | None] = mapped_column(Text)
    score: Mapped[int | None] = mapped_column(Integer)
    judge_reasoning: Mapped[str | None] = mapped_column(Text)
    file_recall: Mapped[float | None] = mapped_column(Numeric(5, 4))
    citation_validity: Mapped[float | None] = mapped_column(Numeric(5, 4))
    abstained: Mapped[bool | None] = mapped_column(Boolean)
    tool_calls: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
