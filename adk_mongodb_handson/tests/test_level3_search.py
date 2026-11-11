"""Level 3: Vector / Hybrid Search。`--profile atlas-local` (Atlas Local 8.2+) が必要。

Embedding は決定的な FakeGenaiClient (字句ベース)。検索経路・インデックス・結果の受け渡しを検証するもので、
埋め込みモデルの意味的な品質は検証対象外 (README「未検証事項」)。
"""

from __future__ import annotations

import asyncio

import pytest
from google.adk.agents import Agent
from google.adk.integrations.mongodb import (
    MongoDbMemoryService,
    MongoDbSessionService,
    MongoDbToolset,
    MongoDbToolSettings,
)
from google.adk.runners import Runner
from google.genai import types
from pymongo import MongoClient

from handson.common import make_event, texts, unique_name
from handson.fakes import EMBEDDING_DIM, FakeGenaiClient, ScriptedLlm
from handson.search_setup import create_product_indexes, seed_products, wait_until_queryable

pytestmark = pytest.mark.atlas_local


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def products(atlas_local_uri):
    client = MongoClient(atlas_local_uri)
    name = unique_name("adk_l3")
    db = client[name]
    seed_products(db)
    create_product_indexes(db)
    wait_until_queryable(db.products, expected=2)
    yield client, name
    client.drop_database(name)
    client.close()


@pytest.fixture
def toolset_env(products):
    client, name = products
    settings = MongoDbToolSettings(vertex_ai_embedding_output_dimensionality=EMBEDDING_DIM, max_results=5)
    fake = FakeGenaiClient()
    toolset = MongoDbToolset(mongo_client=client, database_name=name, genai_client=fake, settings=settings)
    tools = {t.name: t for t in run(toolset.get_tools())}
    common = dict(client=client, database_name=name, settings=settings, genai_client=fake)
    return toolset, tools, common, client, name


def call(env, tool, **args):
    _, tools, common, _, _ = env
    return run(tools[tool].func(**args, **common))


def names(result):
    assert result["status"] == "SUCCESS", result
    return [r["name"] for r in result["rows"]]


def test_indexes_are_listed_as_queryable(products):
    client, name = products
    idx = {i["name"]: i for i in client[name].products.list_search_indexes()}
    assert idx["vector_index"]["type"] == "vectorSearch" and idx["vector_index"]["queryable"]
    assert idx["default"]["type"] == "search" and idx["default"]["queryable"]


def test_vector_search_ranks_relevant_product_first_with_score(toolset_env):
    result = call(toolset_env, "vector_search", collection_name="products", query="pet hair vacuum", limit=3, output_fields=["name"])
    assert names(result)[0] == "Robot Vacuum"
    scores = [r["search_score"] for r in result["rows"]]
    assert scores == sorted(scores, reverse=True) and scores[0] > scores[1]
    assert all("embedding" not in r for r in result["rows"])  # 埋め込みは既定で除外される


def test_vector_search_has_no_relevance_threshold(toolset_env):
    """無関係なクエリでも最近傍が limit 件返る (スコア 0.5 = 直交)。閾値処理はアプリ側の責務。"""
    result = call(toolset_env, "vector_search", collection_name="products", query="quantum chromodynamics", limit=3)
    assert len(result["rows"]) == 3


def test_vector_search_prefilter_restricts_candidates(toolset_env):
    result = call(toolset_env, "vector_search", collection_name="products", query="pet hair vacuum", filter={"category": "outdoor"}, limit=5)
    assert set(names(result)) == {"Hiking Boots", "Camping Stove", "Trail Backpack"}


def test_vector_search_filter_on_non_indexed_field_is_an_error_status(toolset_env):
    result = call(toolset_env, "vector_search", collection_name="products", query="x", filter={"name": "Robot Vacuum"})
    assert result["status"] == "ERROR"  # filter に使うフィールドは index 定義に type:filter が必要


def test_limit_is_capped_by_settings_max_results(toolset_env):
    result = call(toolset_env, "vector_search", collection_name="products", query="hiking", limit=100)
    assert len(result["rows"]) == 5  # max_results=5 (全 8 件)


def test_vector_search_missing_index_returns_error_not_exception(toolset_env):
    result = call(toolset_env, "vector_search", collection_name="products", query="x", index_name="nope")
    assert result["status"] == "ERROR" or result["rows"] == []


def test_hybrid_search_fuses_text_and_vector_rankings(toolset_env):
    result = call(toolset_env, "hybrid_search", collection_name="products", query="pet hair vacuum", text_search_field="description", limit=8, output_fields=["name"])
    got = names(result)
    assert got[0] == "Robot Vacuum"
    top, second = result["rows"][0]["search_score"], result["rows"][1]["search_score"]
    assert top > second > 0  # RRF スコア (cosine 類似度とはスケールが異なる)


def test_hybrid_search_weights_change_ranking_inputs(toolset_env):
    text_only = call(toolset_env, "hybrid_search", collection_name="products", query="hiking", text_search_field="description", vector_weight=0.0, limit=8, output_fields=["name"])
    # vector 側の重み 0: 全文一致が上位を占め、vector のみの候補は score 0 で末尾に残る
    assert set(names(text_only)[:2]) == {"Hiking Boots", "Trail Backpack"}
    assert [r["search_score"] for r in text_only["rows"][2:]] == [0.0] * (len(text_only["rows"]) - 2)


def test_hybrid_search_rejects_unsafe_filter(toolset_env):
    result = call(toolset_env, "hybrid_search", collection_name="products", query="x", text_search_field="description", filter={"$where": "1"})
    assert result["status"] == "ERROR"


def test_memory_vector_mode_roundtrip_and_user_isolation(atlas_local_uri):
    client = MongoClient(atlas_local_uri)
    name = unique_name("adk_l3m")
    try:
        ms = MongoDbMemoryService(
            mongo_client=client, database_name=name, retrieval_mode="vector",
            genai_client=FakeGenaiClient(), embedding_output_dimensionality=EMBEDDING_DIM,
        )
        evs = [make_event("user", "I love hiking in Hokkaido"), make_event("user", "I enjoy cooking ramen")]
        run(ms.add_events_to_memory(app_name="a", user_id="alice", events=evs, session_id="s1"))
        wait_until_queryable(client[name].memories, expected=1)
        alice = run(ms.search_memory(app_name="a", user_id="alice", query="hiking hokkaido"))
        bob = run(ms.search_memory(app_name="a", user_id="bob", query="hiking hokkaido"))
        assert alice.memories[0].content.parts[0].text == "I love hiking in Hokkaido"
        assert len(alice.memories) == 2  # 閾値なし: 無関係な記憶も最近傍として返る
        assert bob.memories == []  # app_name/user_id の事前フィルタでユーザー分離
        # keyword モードで投入した記憶は vector モードから見えない (モード不一致の罠)
        kw = MongoDbMemoryService(mongo_client=client, database_name=name)
        assert run(kw.search_memory(app_name="a", user_id="alice", query="hokkaido")).memories == []
    finally:
        client.drop_database(name)
        client.close()


def test_agent_uses_search_results_in_answer_and_persists_conversation(toolset_env):
    _, _, _, client, name = toolset_env
    llm = ScriptedLlm(
        tool_name="mongodb_vector_search",
        tool_args={"collection_name": "products", "query": "{query}", "limit": 2, "output_fields": ["name"]},
    )
    toolset = MongoDbToolset(
        mongo_client=client, database_name=name, genai_client=FakeGenaiClient(),
        settings=MongoDbToolSettings(vertex_ai_embedding_output_dimensionality=EMBEDDING_DIM),
    )

    async def go():
        sessions = MongoDbSessionService(mongo_client=client, database_name=name)
        runner = Runner(app_name="shop", agent=Agent(name="shop", model=llm, tools=[toolset]), session_service=sessions)
        s = await sessions.create_session(app_name="shop", user_id="u1")
        async for _ in runner.run_async(
            user_id="u1", session_id=s.id,
            new_message=types.Content(role="user", parts=[types.Part(text="pet hair vacuum")]),
        ):
            pass
        return await MongoDbSessionService(mongo_client=client, database_name=name).get_session(app_name="shop", user_id="u1", session_id=s.id)

    saved = run(go())
    assert texts(saved.events)[-1].startswith("search results: Robot Vacuum")
    calls = [p.function_call.name for e in saved.events if e.content for p in e.content.parts if p.function_call]
    assert calls == ["mongodb_vector_search"]  # ツール呼び出しも会話履歴として永続化される


@pytest.mark.replicaset
def test_vector_search_is_unavailable_on_plain_mongod(replicaset_uri, db_name, mongo_factory):
    """通常の mongod (8.0) では $vectorSearch が使えず、ツールは例外でなく ERROR status を返す。"""
    client = mongo_factory(replicaset_uri, db_name)
    client[db_name].products.insert_one({"name": "x", "embedding": [0.0] * EMBEDDING_DIM})
    settings = MongoDbToolSettings(vertex_ai_embedding_output_dimensionality=EMBEDDING_DIM)
    toolset = MongoDbToolset(mongo_client=client, database_name=db_name, genai_client=FakeGenaiClient(), settings=settings)
    tool = {t.name: t for t in run(toolset.get_tools())}["vector_search"]
    result = run(tool.func(collection_name="products", query="x", client=client, database_name=db_name, settings=settings, genai_client=FakeGenaiClient()))
    assert result["status"] == "ERROR" and "$vectorSearch" in result["error_details"]
