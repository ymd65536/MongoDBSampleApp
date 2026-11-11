"""Level 1: Session Persistence。`docker compose --profile replicaset up -d --wait` が必要。"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from google.adk.errors._stale_session_error import StaleSessionError
from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.integrations.mongodb import MongoDbSessionService
from google.adk.sessions import InMemorySessionService
from google.adk.sessions.base_session_service import GetSessionConfig

from handson.common import append_with_retry, make_event, race_append, texts

APP, USER = "app", "alice"
ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.replicaset


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def service(replicaset_uri, db_name, mongo_factory):
    client = mongo_factory(replicaset_uri, db_name)
    return MongoDbSessionService(mongo_client=client, database_name=db_name)


def test_create_and_get_session_with_events_and_state(service):
    async def go():
        s = await service.create_session(
            app_name=APP, user_id=USER, session_id="s1", state={"k": 1, "user:lang": "ja", "app:ver": 2}
        )
        await service.append_event(s, make_event("user", "hello"))
        await service.append_event(s, make_event("agent", "hi"))
        return await service.get_session(app_name=APP, user_id=USER, session_id="s1")

    got = run(go())
    assert texts(got.events) == ["hello", "hi"]
    assert got.state == {"k": 1, "user:lang": "ja", "app:ver": 2}


def test_duplicate_session_id_and_missing_session(service):
    async def go():
        await service.create_session(app_name=APP, user_id=USER, session_id="s1")
        with pytest.raises(AlreadyExistsError):
            await service.create_session(app_name=APP, user_id=USER, session_id="s1")
        return await service.get_session(app_name=APP, user_id=USER, session_id="nope")

    assert run(go()) is None


def test_state_scopes_are_stored_separately_and_shared(service):
    """session 状態は session 文書、user:/app: は別コレクションに保存され他セッションへ共有される。"""

    async def go():
        await service.create_session(app_name=APP, user_id=USER, session_id="a", state={"k": 1, "user:lang": "ja", "app:ver": 2})
        other = await service.create_session(app_name=APP, user_id=USER, session_id="b")
        stranger = await service.create_session(app_name=APP, user_id="bob", session_id="c")
        return other.state, stranger.state

    other, stranger = run(go())
    assert other == {"user:lang": "ja", "app:ver": 2}  # 同一ユーザーの別セッションに user:/app: が見える
    assert stranger == {"app:ver": 2}  # 別ユーザーには app: のみ


def test_restore_after_service_restart_same_process(replicaset_uri, db_name, mongo_factory):
    mongo_factory(replicaset_uri, db_name)

    async def go():
        first = MongoDbSessionService(connection_string=replicaset_uri, database_name=db_name)
        s = await first.create_session(app_name=APP, user_id=USER, session_id="s1")
        await first.append_event(s, make_event("user", "persist me"))
        await first.close()
        second = MongoDbSessionService(connection_string=replicaset_uri, database_name=db_name)
        got = await second.get_session(app_name=APP, user_id=USER, session_id="s1")
        await second.close()
        return got

    assert texts(run(go()).events) == ["persist me"]


def test_restore_across_real_process_restart(replicaset_uri, db_name, mongo_factory):
    mongo_factory(replicaset_uri, db_name)
    env = {**os.environ, "MONGODB_URI_REPLICASET": replicaset_uri}
    base = [sys.executable, "-W", "ignore", "-m", "scripts.level1_session"]
    args = ["--db", db_name, "--session-id", "proc"]
    subprocess.run([*base, "write", *args], cwd=ROOT, env=env, check=True, capture_output=True)
    out = subprocess.run([*base, "read", *args], cwd=ROOT, env=env, check=True, capture_output=True, text=True).stdout
    assert "北海道旅行の計画を立てたい" in out and "'user:language': 'ja'" in out


def test_in_memory_service_is_not_durable_but_has_same_interface(service):
    async def scenario(svc):
        s = await svc.create_session(app_name=APP, user_id=USER, session_id="s1", state={"k": 1})
        await svc.append_event(s, make_event("user", "x"))
        return await svc.get_session(app_name=APP, user_id=USER, session_id="s1")

    mongo_result, mem_result = run(scenario(service)), run(scenario(InMemorySessionService()))
    assert texts(mongo_result.events) == texts(mem_result.events)  # 振る舞いは同等
    assert run(InMemorySessionService().get_session(app_name=APP, user_id=USER, session_id="s1")) is None


def test_stale_session_is_rejected_by_optimistic_concurrency(service):
    async def go():
        await service.create_session(app_name=APP, user_id=USER, session_id="s1")
        c1 = await service.get_session(app_name=APP, user_id=USER, session_id="s1")
        c2 = await service.get_session(app_name=APP, user_id=USER, session_id="s1")
        await service.append_event(c1, make_event("agent", "first"))
        with pytest.raises(StaleSessionError):
            await service.append_event(c2, make_event("agent", "second"))
        return await service.get_session(app_name=APP, user_id=USER, session_id="s1")

    assert texts(run(go()).events) == ["first"]  # 後勝ちで上書きされない


def test_concurrent_writers_without_retry_only_one_wins_and_nothing_is_lost(replicaset_uri, db_name, mongo_factory):
    mongo_factory(replicaset_uri, db_name)

    async def go():
        a = MongoDbSessionService(connection_string=replicaset_uri, database_name=db_name)
        b = MongoDbSessionService(connection_string=replicaset_uri, database_name=db_name)  # 別インスタンス = 別プロセス相当
        await a.create_session(app_name=APP, user_id=USER, session_id="s1")
        result = await race_append([a, b], app_name=APP, user_id=USER, session_id="s1", writers=8, retry=False)
        got = await a.get_session(app_name=APP, user_id=USER, session_id="s1")
        return result, got

    result, got = run(go())
    assert result == {"succeeded": 1, "stale": 7}
    assert len(got.events) == 1


def test_concurrent_writers_with_reload_and_retry_all_succeed(replicaset_uri, db_name, mongo_factory):
    mongo_factory(replicaset_uri, db_name)

    async def go():
        a = MongoDbSessionService(connection_string=replicaset_uri, database_name=db_name)
        b = MongoDbSessionService(connection_string=replicaset_uri, database_name=db_name)
        await a.create_session(app_name=APP, user_id=USER, session_id="s1")
        result = await race_append([a, b], app_name=APP, user_id=USER, session_id="s1", writers=8, retry=True)
        return result, await b.get_session(app_name=APP, user_id=USER, session_id="s1")

    result, got = run(go())
    assert result["succeeded"] == 8 and len(got.events) == 8
    assert sorted(texts(got.events)) == sorted(f"writer-{i}" for i in range(8))


def test_append_with_retry_returns_retry_count(service):
    async def go():
        await service.create_session(app_name=APP, user_id=USER, session_id="s1")
        return await append_with_retry(
            service, app_name=APP, user_id=USER, session_id="s1", build_event=lambda: make_event("user", "x")
        )

    assert run(go()) == 0


def test_get_session_config_limits_loaded_events(service):
    async def go():
        s = await service.create_session(app_name=APP, user_id=USER, session_id="s1")
        for i in range(20):
            await service.append_event(s, make_event("user", f"m{i}"))
        recent = await service.get_session(app_name=APP, user_id=USER, session_id="s1", config=GetSessionConfig(num_recent_events=3))
        none = await service.get_session(app_name=APP, user_id=USER, session_id="s1", config=GetSessionConfig(num_recent_events=0))
        return recent, none

    recent, none = run(go())
    assert texts(recent.events) == ["m17", "m18", "m19"]  # 時系列順を維持
    assert none.events == []


def test_list_and_delete_session_removes_events(service, mongo_factory):
    async def go():
        s = await service.create_session(app_name=APP, user_id=USER, session_id="s1")
        await service.append_event(s, make_event("user", "x"))
        await service.create_session(app_name=APP, user_id=USER, session_id="s2")
        listed = await service.list_sessions(app_name=APP, user_id=USER)
        await service.delete_session(app_name=APP, user_id=USER, session_id="s1")
        return listed, await service.get_session(app_name=APP, user_id=USER, session_id="s1")

    listed, deleted = run(go())
    assert {s.id for s in listed.sessions} == {"s1", "s2"}
    assert deleted is None
    assert service._events().count_documents({"session_id": "s1"}) == 0


def test_recommended_indexes_are_created(service):
    run(service.create_session(app_name=APP, user_id=USER, session_id="s1"))
    assert "app_user_session_ts" in service._events().index_information()
    assert "app_user" in service._sessions().index_information()


@pytest.mark.standalone
def test_standalone_falls_back_without_transactions_but_still_detects_conflicts(standalone_uri, db_name, mongo_factory, caplog):
    client = mongo_factory(standalone_uri, db_name)
    svc = MongoDbSessionService(mongo_client=client, database_name=db_name)

    async def go():
        await svc.create_session(app_name=APP, user_id=USER, session_id="s1")
        c1 = await svc.get_session(app_name=APP, user_id=USER, session_id="s1")
        c2 = await svc.get_session(app_name=APP, user_id=USER, session_id="s1")
        await svc.append_event(c1, make_event("agent", "first"))
        with pytest.raises(StaleSessionError):
            await svc.append_event(c2, make_event("agent", "second"))

    with caplog.at_level(logging.WARNING):
        run(go())
    assert svc._transactions_supported is False
    assert "does not support" in caplog.text
