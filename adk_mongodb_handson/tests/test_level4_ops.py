"""Level 4: 運用観点 (トレース、削除、認証)。"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from google.adk.agents import Agent
from google.adk.integrations.mongodb import MongoDbSessionService
from google.adk.runners import Runner
from google.genai import types
from opentelemetry import trace
from pymongo import MongoClient
from pymongo.errors import OperationFailure

from handson import config
from handson.common import make_event, texts, unique_name
from handson.fakes import ScriptedLlm
from handson.ops import MongoCommandTracer, purge_sessions


def run(coro):
    return asyncio.run(coro)


@pytest.mark.replicaset
def test_adk_and_mongodb_spans_share_one_trace_under_app_root_span(replicaset_uri, db_name, mongo_factory, span_exporter):
    client = mongo_factory(replicaset_uri, db_name, event_listeners=[MongoCommandTracer()])
    service = MongoDbSessionService(mongo_client=client, database_name=db_name)
    runner = Runner(app_name="app", agent=Agent(name="agent", model=ScriptedLlm()), session_service=service)

    async def go():
        s = await service.create_session(app_name="app", user_id="u")
        span_exporter.clear()
        with trace.get_tracer("test").start_as_current_span("handle_request"):
            async for _ in runner.run_async(user_id="u", session_id=s.id, new_message=types.Content(role="user", parts=[types.Part(text="hi")])):
                pass

    run(go())
    spans = span_exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert {"invoke_agent agent", "generate_content scripted", "mongodb.insert", "mongodb.findAndModify"} <= set(names)
    assert len({s.context.trace_id for s in spans}) == 1  # to_thread が contextvars を引き継ぐ
    db_spans = [s for s in spans if s.name.startswith("mongodb.")]
    assert all(s.attributes["db.system"] == "mongodb" for s in db_spans)
    assert not any("hi" in str(dict(s.attributes).values()) for s in db_spans)  # 会話内容は属性に入れない


@pytest.mark.replicaset
def test_adk_does_not_trace_session_service_calls_by_itself(replicaset_uri, db_name, mongo_factory, span_exporter):
    """MongoDbSessionService 自体は span を出さない (listener を付けない限り DB 呼び出しは不可視)。"""
    client = mongo_factory(replicaset_uri, db_name)
    service = MongoDbSessionService(mongo_client=client, database_name=db_name)
    span_exporter.clear()
    run(service.create_session(app_name="app", user_id="u"))
    assert [s.name for s in span_exporter.get_finished_spans()] == []


@pytest.mark.replicaset
def test_purge_sessions_removes_old_sessions_and_events_only(replicaset_uri, db_name, mongo_factory):
    client = mongo_factory(replicaset_uri, db_name)
    service = MongoDbSessionService(mongo_client=client, database_name=db_name)

    async def go():
        for sid in ("old", "new"):
            s = await service.create_session(app_name="app", user_id="u", session_id=sid, state={"user:pref": "keep"})
            await service.append_event(s, make_event("user", sid))

    run(go())
    db = client[db_name]
    db.sessions.update_one({"id": "old"}, {"$set": {"update_time": time.time() - 40 * 86400}})
    assert purge_sessions(db, older_than_seconds=30 * 86400) == {"sessions": 1, "events": 1}
    assert run(service.get_session(app_name="app", user_id="u", session_id="old")) is None
    assert texts(run(service.get_session(app_name="app", user_id="u", session_id="new")).events) == ["new"]
    assert run(service.get_user_state(app_name="app", user_id="u")) == {"pref": "keep"}  # user 状態は残る


@pytest.mark.replicaset
def test_multi_instance_session_sharing_and_resume_after_instance_loss(replicaset_uri, db_name, mongo_factory):
    """インスタンス A が消えても、B が同じ MongoDB から会話を継続できる (ステートレス Runtime)。"""
    mongo_factory(replicaset_uri, db_name)

    async def turn(service, text):
        runner = Runner(app_name="app", agent=Agent(name="agent", model=ScriptedLlm()), session_service=service)
        async for _ in runner.run_async(user_id="u", session_id="shared", new_message=types.Content(role="user", parts=[types.Part(text=text)])):
            pass

    async def go():
        a = MongoDbSessionService(connection_string=replicaset_uri, database_name=db_name)
        await a.create_session(app_name="app", user_id="u", session_id="shared")
        await turn(a, "from A")
        await a.close()  # インスタンス A 消失
        b = MongoDbSessionService(connection_string=replicaset_uri, database_name=db_name)
        await turn(b, "from B")
        return await b.get_session(app_name="app", user_id="u", session_id="shared")

    assert texts(run(go()).events) == ["from A", "echo: from A", "from B", "echo: from B"]


@pytest.mark.auth
def test_least_privilege_users_and_index_privilege_fallback(caplog, monkeypatch):
    try:
        uris = {u: config.get_auth_uri(u, env) for u, env in
                [("adk_app", "ADK_APP_PASSWORD"), ("adk_reader", "ADK_READER_PASSWORD"), ("adk_noindex", "ADK_NOINDEX_PASSWORD")]}
    except RuntimeError as exc:
        pytest.skip(str(exc))
    probe = MongoClient(uris["adk_app"], serverSelectionTimeoutMS=2000)
    try:
        probe.admin.command("ping")
    except Exception:
        pytest.skip("auth profile の MongoDB に接続できません")

    db = config.auth_db_name()

    async def write(user: str) -> None:
        client = MongoClient(uris[user], serverSelectionTimeoutMS=5000)
        try:
            svc = MongoDbSessionService(mongo_client=client, database_name=db)
            s = await svc.create_session(app_name="app", user_id="u", session_id=unique_name(user))
            await svc.append_event(s, make_event("user", "x"))
        finally:
            client.close()

    run(write("adk_app"))  # readWrite: 成功 (インデックスも作成可能)
    with pytest.raises(OperationFailure) as denied:
        run(write("adk_reader"))  # read のみ: 書き込み拒否
    assert denied.value.code == 13
    with caplog.at_level(logging.WARNING):
        run(write("adk_noindex"))  # createIndex なし: 警告のみで動作継続
    assert "could not create the recommended index" in caplog.text

    monkeypatch.setenv("ADK_APP_PASSWORD", "wrong-password")
    bad = MongoClient(config.get_auth_uri("adk_app", "ADK_APP_PASSWORD"), serverSelectionTimeoutMS=3000)
    with pytest.raises(OperationFailure) as bad_auth:
        bad[db].sessions.find_one()
    assert bad_auth.value.code == 18  # AuthenticationFailed
