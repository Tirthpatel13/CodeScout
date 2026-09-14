"""API tests, including the access-control test that must never regress."""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from itsdangerous import URLSafeSerializer

from app.config import get_settings
from app.main import create_app
from app.models import Conversation, RepoAccess, User


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def cookie_for(user: User) -> dict[str, str]:
    settings = get_settings()
    token = URLSafeSerializer(settings.session_secret, salt="codescout-session").dumps(
        {"user_id": str(user.id)}
    )
    return {"codescout_session": token}


@pytest.fixture
async def granted_user(session, user, repository):
    session.add(RepoAccess(user_id=user.id, repository_id=repository.id))
    await session.commit()
    return user


@pytest.fixture
async def other_user(session):
    u = User(
        id=uuid.uuid4(),
        github_id=int(uuid.uuid4().int % 10_000_000),
        login="intruder",
        access_token=b"encrypted",
    )
    session.add(u)
    await session.commit()
    return u


# --------------------------------------------------------------------------
# basics
# --------------------------------------------------------------------------
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_request_id_header_is_echoed(client):
    resp = await client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert resp.headers["X-Request-ID"] == "abc-123"


async def test_unauthenticated_requests_are_rejected(client, repository):
    resp = await client.get(f"/api/repos/{repository.id}")
    assert resp.status_code == 401


async def test_garbage_session_cookie_is_rejected(client, repository):
    resp = await client.get(
        f"/api/repos/{repository.id}", cookies={"codescout_session": "not-a-real-token"}
    )
    assert resp.status_code == 401


async def test_me_returns_the_session_user(client, user):
    resp = await client.get("/api/auth/me", cookies=cookie_for(user))
    assert resp.status_code == 200
    assert resp.json()["login"] == "tester"


# --------------------------------------------------------------------------
# access control — the test that must never regress
# --------------------------------------------------------------------------
async def test_user_cannot_read_a_repository_they_were_not_granted(
    client, repository, granted_user, other_user
):
    ok = await client.get(f"/api/repos/{repository.id}", cookies=cookie_for(granted_user))
    assert ok.status_code == 200

    denied = await client.get(f"/api/repos/{repository.id}", cookies=cookie_for(other_user))
    # 404, not 403: the response must not confirm the repository exists.
    assert denied.status_code == 404


async def test_user_cannot_read_another_users_index_run(
    client, indexed_run, granted_user, other_user
):
    ok = await client.get(f"/api/index-runs/{indexed_run.id}", cookies=cookie_for(granted_user))
    assert ok.status_code == 200

    denied = await client.get(f"/api/index-runs/{indexed_run.id}", cookies=cookie_for(other_user))
    assert denied.status_code == 404


async def test_user_cannot_read_another_users_conversation(
    client, session, repository, granted_user, other_user
):
    conversation = Conversation(
        id=uuid.uuid4(), repository_id=repository.id, user_id=granted_user.id, title="mine"
    )
    session.add(conversation)
    await session.commit()

    denied = await client.get(
        f"/api/conversations/{conversation.id}", cookies=cookie_for(other_user)
    )
    assert denied.status_code == 404


# --------------------------------------------------------------------------
# repo + run reads
# --------------------------------------------------------------------------
async def test_get_repo_includes_latest_ready_run(client, indexed_run, granted_user):
    resp = await client.get(
        f"/api/repos/{indexed_run.repository_id}", cookies=cookie_for(granted_user)
    )
    body = resp.json()
    assert body["latest_run"]["id"] == str(indexed_run.id)
    assert body["latest_run"]["stats"]["files"] > 0


async def test_run_status_exposes_progress(client, indexed_run, granted_user):
    resp = await client.get(f"/api/index-runs/{indexed_run.id}", cookies=cookie_for(granted_user))
    body = resp.json()
    assert body["status"] == "ready"
    assert body["phase_pct"] == 100


async def test_read_file_endpoint_returns_a_line_window(client, indexed_run, granted_user):
    resp = await client.get(
        f"/api/index-runs/{indexed_run.id}/files/app/auth/tokens.py",
        params={"start": 1, "end": 3},
        cookies=cookie_for(granted_user),
    )
    body = resp.json()
    assert body["path"] == "app/auth/tokens.py"
    assert body["end"] == 3
    assert body["content"].count("\n") == 2


async def test_read_file_unknown_path_is_404(client, indexed_run, granted_user):
    resp = await client.get(
        f"/api/index-runs/{indexed_run.id}/files/nope.py", cookies=cookie_for(granted_user)
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# chat + SSE
# --------------------------------------------------------------------------
async def test_create_conversation_requires_repo_access(client, repository, other_user):
    resp = await client.post(
        "/api/conversations",
        json={"repository_id": str(repository.id)},
        cookies=cookie_for(other_user),
    )
    assert resp.status_code == 404


async def test_ask_streams_the_investigation(
    client, session, indexed_run, granted_user, monkeypatch
):
    from app.providers.fake import ScriptedLLM

    monkeypatch.setattr(
        "app.api.conversations.get_llm",
        lambda settings=None, model=None: ScriptedLLM(
            [
                [("find_symbol", {"name": "verify_token"})],
                "Verified in [app/auth/tokens.py:31-48].\n\nCONFIDENCE: high",
            ]
        ),
    )

    created = await client.post(
        "/api/conversations",
        json={"repository_id": str(indexed_run.repository_id)},
        cookies=cookie_for(granted_user),
    )
    assert created.status_code == 201
    conversation_id = created.json()["id"]

    async with client.stream(
        "POST",
        f"/api/conversations/{conversation_id}/messages",
        json={"question": "How are tokens verified?"},
        cookies=cookie_for(granted_user),
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in resp.aiter_text()])

    events = [ln[len("event: ") :] for ln in body.split("\n") if ln.startswith("event: ")]
    assert events[0] == "step"
    assert "citation" in events
    assert events[-1] == "done"

    done_payload = json.loads(
        [ln for ln in body.split("\n") if ln.startswith("data: ")][-1][len("data: ") :]
    )
    assert done_payload["confidence"] == "high"
    assert done_payload["citations"][0]["path"] == "app/auth/tokens.py"
    assert done_payload["tool_calls"] == 1


async def test_asked_question_and_answer_are_persisted(
    client, session, indexed_run, granted_user, monkeypatch
):
    from app.providers.fake import ScriptedLLM

    monkeypatch.setattr(
        "app.api.conversations.get_llm",
        lambda settings=None, model=None: ScriptedLLM(
            ["Answer with [app/auth/tokens.py:1-5].\n\nCONFIDENCE: medium"]
        ),
    )

    created = await client.post(
        "/api/conversations",
        json={"repository_id": str(indexed_run.repository_id)},
        cookies=cookie_for(granted_user),
    )
    conversation_id = created.json()["id"]

    async with client.stream(
        "POST",
        f"/api/conversations/{conversation_id}/messages",
        json={"question": "anything?"},
        cookies=cookie_for(granted_user),
    ) as resp:
        async for _ in resp.aiter_text():
            pass

    fetched = await client.get(
        f"/api/conversations/{conversation_id}", cookies=cookie_for(granted_user)
    )
    messages = fetched.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["citations"][0]["path"] == "app/auth/tokens.py"
    assert messages[1]["confidence"] == "medium"


async def test_ask_without_a_ready_index_is_409(client, session, repository, granted_user):
    created = await client.post(
        "/api/conversations",
        json={"repository_id": str(repository.id)},
        cookies=cookie_for(granted_user),
    )
    conversation_id = created.json()["id"]

    resp = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"question": "why not?"},
        cookies=cookie_for(granted_user),
    )
    assert resp.status_code == 409


async def test_question_validation_rejects_empty_input(client, indexed_run, granted_user):
    created = await client.post(
        "/api/conversations",
        json={"repository_id": str(indexed_run.repository_id)},
        cookies=cookie_for(granted_user),
    )
    resp = await client.post(
        f"/api/conversations/{created.json()['id']}/messages",
        json={"question": "x"},
        cookies=cookie_for(granted_user),
    )
    assert resp.status_code == 422
