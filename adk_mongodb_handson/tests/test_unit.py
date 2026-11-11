"""MongoDB 不要のユニットテスト (CI の最小ジョブで実行)。"""

from __future__ import annotations

import asyncio
import pickle

import pytest
from google.adk.agents import Agent
from google.adk.integrations.mongodb import (
    MongoDbMemoryService,
    MongoDbSessionService,
    MongoDbToolset,
    MongoDbToolSettings,
)
from google.adk.integrations.mongodb import _search_tool
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from opentelemetry import trace

from handson.common import make_event, texts
from handson.fakes import EMBEDDING_DIM, FakeGenaiClient, ScriptedLlm, fake_embed
from handson.ops import MongoCommandTracer, latency_summary

URI = "mongodb://localhost:1/?serverSelectionTimeoutMS=100"  # 接続はしない (遅延接続)


def test_public_api_is_exported():
    import google.adk.integrations.mongodb as m

    assert {"MongoDbSessionService", "MongoDbMemoryService", "MongoDbToolset", "MongoDbToolSettings"} <= set(dir(m))


def test_session_service_requires_exactly_one_connection_source():
    with pytest.raises(ValueError):
        MongoDbSessionService(database_name="d")
    with pytest.raises(ValueError):
        MongoDbSessionService(database_name="d", connection_string=URI, mongo_client=object())


@pytest.mark.parametrize(
    "kwargs",
    [{"max_results": 0}, {"memory_ttl_days": 0}, {"retrieval_mode": "bm25"}],
)
def test_memory_service_validates_arguments(kwargs):
    with pytest.raises(ValueError):
        MongoDbMemoryService(database_name="d", connection_string=URI, **kwargs)


def test_toolset_requires_exactly_one_connection_source():
    with pytest.raises(ValueError):
        MongoDbToolset(database_name="d")


def test_toolset_exposes_two_tools_and_hides_infrastructure_params():
    toolset = MongoDbToolset(database_name="d", connection_string=URI)
    tools = asyncio.run(toolset.get_tools())
    assert sorted(t.name for t in tools) == ["hybrid_search", "vector_search"]
    for tool in tools:
        declaration = tool._get_declaration()
        props = set(declaration.parameters_json_schema["properties"])
        assert {"client", "database_name", "settings", "genai_client"}.isdisjoint(props)
        assert {"collection_name", "query"} <= props


def test_toolset_with_env_var_is_picklable_without_credentials(monkeypatch):
    monkeypatch.setenv("HANDSON_TEST_URI", "mongodb://user:secret@localhost:1/")
    toolset = MongoDbToolset(database_name="d", connection_string_env_var="HANDSON_TEST_URI")
    blob = pickle.dumps(toolset)
    assert b"secret" not in blob  # 資格情報は pickle (Agent Engine パッケージ) に入らない
    monkeypatch.delenv("HANDSON_TEST_URI")
    with pytest.raises(ValueError):  # 復元先で環境変数が無ければ明示的に失敗する
        pickle.loads(blob)


def test_toolset_with_caller_owned_client_is_not_picklable():
    from pymongo import MongoClient

    toolset = MongoDbToolset(database_name="d", mongo_client=MongoClient(URI))
    with pytest.raises(TypeError):
        pickle.dumps(toolset)


@pytest.mark.parametrize(
    "bad_filter",
    [{"$where": "sleep(1000)"}, {"x": {"$function": {}}}, {"$expr": {"$eq": [1, 1]}}],
)
def test_search_tool_rejects_unsafe_filter_operators_before_touching_db(bad_filter):
    result = asyncio.run(
        _search_tool.vector_search(
            collection_name="c",
            query="q",
            client=None,
            database_name="d",
            settings=MongoDbToolSettings(),
            filter=bad_filter,
        )
    )
    assert result["status"] == "ERROR"


def test_keyword_extraction_behaviour_for_english_and_japanese():
    service = MongoDbMemoryService(database_name="d", connection_string=URI)
    extract = service._extract_keywords
    assert extract("I love hiking in Hokkaido") == {"love", "hiking", "hokkaido"}  # ストップワード除去
    assert "mongodb" in extract("私はMongoDBが好きです")  # ラテン文字の連続は切り出される
    assert "好き" not in extract("私はMongoDBが好きです")  # 形態素解析はない: 空白区切りでない日本語は部分一致しない
    assert "天気" in extract("今日は 天気 が いい")  # 空白区切りなら単語として扱われる


def test_fake_embedding_is_deterministic_and_normalized():
    a, b = fake_embed("pet hair vacuum"), fake_embed("pet hair vacuum")
    assert a == b and len(a) == EMBEDDING_DIM
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9
    cos = lambda u, v: sum(x * y for x, y in zip(u, v))
    assert cos(a, fake_embed("vacuum for pet hair")) > cos(a, fake_embed("espresso machine"))


def test_fake_genai_client_matches_adk_embed_call_shape():
    client = FakeGenaiClient()
    response = asyncio.run(client.aio.models.embed_content(model="m", contents=["a", "b"], config=None))
    assert len(response.embeddings) == 2 and len(response.embeddings[0].values) == EMBEDDING_DIM


def test_in_memory_session_service_loses_state_across_instances():
    async def run():
        first = InMemorySessionService()
        s = await first.create_session(app_name="a", user_id="u", session_id="s1")
        await first.append_event(s, make_event("user", "hello"))
        second = InMemorySessionService()  # プロセス再起動に相当
        return await second.get_session(app_name="a", user_id="u", session_id="s1")

    assert asyncio.run(run()) is None


def test_agent_loop_runs_offline_with_scripted_llm():
    async def run():
        service = InMemorySessionService()
        runner = Runner(app_name="app", agent=Agent(name="a", model=ScriptedLlm()), session_service=service)
        s = await service.create_session(app_name="app", user_id="u")
        async for _ in runner.run_async(
            user_id="u", session_id=s.id, new_message=types.Content(role="user", parts=[types.Part(text="hi")])
        ):
            pass
        return texts((await service.get_session(app_name="app", user_id="u", session_id=s.id)).events)

    assert asyncio.run(run()) == ["hi", "echo: hi"]


def test_adk_emits_agent_spans_and_mongo_listener_is_inert_without_db(span_exporter):
    span_exporter.clear()

    async def run():
        service = InMemorySessionService()
        runner = Runner(app_name="app", agent=Agent(name="a", model=ScriptedLlm()), session_service=service)
        s = await service.create_session(app_name="app", user_id="u")
        async for _ in runner.run_async(
            user_id="u", session_id=s.id, new_message=types.Content(role="user", parts=[types.Part(text="hi")])
        ):
            pass

    asyncio.run(run())
    names = {s.name for s in span_exporter.get_finished_spans()}
    assert {"invoke_agent a", "generate_content scripted"} <= names
    assert trace.get_tracer_provider() is not None
    assert MongoCommandTracer()  # 生成のみ


def test_latency_summary():
    s = latency_summary([1, 2, 3, 4, 100])
    assert s["n"] == 5 and s["p50_ms"] == 3
