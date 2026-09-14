from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

# Point the app at the test database before anything imports settings.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/codescout_test",
)
os.environ["LLM_PROVIDER"] = "fake"
os.environ["EMBEDDING_PROVIDER"] = "fake"

from app.config import get_settings  # noqa: E402
from app.db import dispose_engine, get_engine, get_sessionmaker  # noqa: E402
from app.models import Base, IndexRun, Repository, RunStatus, User  # noqa: E402

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema(settings):
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def session(_schema):
    async with get_sessionmaker()() as s:
        yield s
        await s.rollback()


@pytest_asyncio.fixture
async def user(session):
    u = User(
        id=uuid.uuid4(),
        github_id=int(uuid.uuid4().int % 10_000_000),
        login="tester",
        access_token=b"encrypted",
    )
    session.add(u)
    await session.commit()
    return u


@pytest_asyncio.fixture
async def repository(session):
    repo = Repository(
        id=uuid.uuid4(),
        github_id=int(uuid.uuid4().int % 10_000_000),
        owner="acme",
        name=f"sample-{uuid.uuid4().hex[:8]}",
        default_branch="main",
    )
    session.add(repo)
    await session.commit()
    return repo


@pytest_asyncio.fixture
async def index_run(session, repository):
    run = IndexRun(
        id=uuid.uuid4(),
        repository_id=repository.id,
        commit_sha="a" * 40,
        status=RunStatus.QUEUED.value,
    )
    session.add(run)
    await session.commit()
    return run


@pytest_asyncio.fixture
async def indexed_run(session, index_run, settings):
    """A fully ingested fixture repository. The workhorse fixture."""
    from app.ingest.pipeline import run_ingest
    from app.providers.fake import FakeEmbeddings

    await run_ingest(
        session,
        run_id=index_run.id,
        settings=settings,
        embedder=FakeEmbeddings(),
        llm=None,
        local_path=FIXTURE_REPO,
    )
    await session.commit()
    return index_run
