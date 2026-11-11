"""Level 2: Long-term Memory (keyword モード)。`--profile replicaset` が必要。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from google.adk.integrations.mongodb import MongoDbMemoryService, MongoDbSessionService

from handson.common import make_event

APP = "app"
pytestmark = pytest.mark.replicaset


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def clients(replicaset_uri, db_name, mongo_factory):
    return mongo_factory(replicaset_uri, db_name)


@pytest.fixture
def memory(clients, db_name):
    return MongoDbMemoryService(mongo_client=clients, database_name=db_name)


EVENTS = [
    make_event("user", "I want to go hiking in Hokkaido next summer"),
    make_event("agent", "Hokkaido has great trails in Daisetsuzan"),
    make_event("user", "私はMongoDBが好きです"),
    make_event("user", "今日は 天気 が いい"),
]


async def recall(memory, user, query):
    r = await memory.search_memory(app_name=APP, user_id=user, query=query)
    return [m.content.parts[0].text for m in r.memories]


def test_memory_from_session_is_searchable_from_a_different_session(clients, db_name, memory):
    async def go():
        sessions = MongoDbSessionService(mongo_client=clients, database_name=db_name)
        s = await sessions.create_session(app_name=APP, user_id="alice", session_id="trip-1")
        for e in EVENTS[:2]:
            await sessions.append_event(s, e)
        await memory.add_session_to_memory(await sessions.get_session(app_name=APP, user_id="alice", session_id="trip-1"))
        # 新しいセッション (trip-2) でも user 単位で検索できる。検索 API は session_id を取らない
        await sessions.create_session(app_name=APP, user_id="alice", session_id="trip-2")
        return await recall(memory, "alice", "hokkaido")

    found = run(go())
    assert set(found) == {EVENTS[0].content.parts[0].text, EVENTS[1].content.parts[0].text}


def test_reingesting_same_session_is_idempotent(memory):
    async def go():
        for _ in range(3):
            await memory.add_events_to_memory(app_name=APP, user_id="alice", events=EVENTS, session_id="s1")

    run(go())
    assert memory._memories().count_documents({}) == len(EVENTS)


def test_same_event_ids_in_different_sessions_are_separate_memories(memory):
    async def go():
        await memory.add_events_to_memory(app_name=APP, user_id="alice", events=EVENTS[:1], session_id="s1")
        await memory.add_events_to_memory(app_name=APP, user_id="alice", events=EVENTS[:1], session_id="s2")

    run(go())
    assert memory._memories().count_documents({}) == 2  # _id = (app, user, session, event)


def test_memories_are_isolated_per_user_and_app(memory):
    async def go():
        await memory.add_events_to_memory(app_name=APP, user_id="alice", events=EVENTS, session_id="s1")
        return (
            await recall(memory, "bob", "hokkaido"),
            await recall(memory, "alice", "hokkaido"),
            (await memory.search_memory(app_name="other_app", user_id="alice", query="hokkaido")).memories,
        )

    bob, alice, other_app = run(go())
    assert bob == [] and len(alice) == 2 and other_app == []


def test_keyword_search_semantics_and_japanese_limits(memory):
    async def go():
        await memory.add_events_to_memory(app_name=APP, user_id="alice", events=EVENTS, session_id="s1")
        return {
            "case": await recall(memory, "alice", "HOKKAIDO"),
            "mixed": await recall(memory, "alice", "MongoDB"),
            "spaced_ja": await recall(memory, "alice", "天気"),
            "unspaced_ja": await recall(memory, "alice", "好き"),
            "synonym": await recall(memory, "alice", "trekking"),
            "stopwords_only": await recall(memory, "alice", "the of"),
            "empty": await recall(memory, "alice", "  "),
        }

    r = run(go())
    assert len(r["case"]) == 2
    assert r["mixed"] == ["私はMongoDBが好きです"]
    assert r["spaced_ja"] == ["今日は 天気 が いい"]
    assert r["unspaced_ja"] == []  # 形態素解析なし
    assert r["synonym"] == []  # 同義語は拾えない = 意味検索ではない
    assert r["stopwords_only"] == [] and r["empty"] == []


def test_results_are_ranked_by_keyword_overlap_then_recency(memory):
    async def go():
        evs = [make_event("user", "hokkaido"), make_event("user", "hokkaido hiking trails")]
        await memory.add_events_to_memory(app_name=APP, user_id="alice", events=evs, session_id="s1")
        return await recall(memory, "alice", "hiking hokkaido")

    assert run(go()) == ["hokkaido hiking trails", "hokkaido"]


def test_max_results_caps_recall(clients, db_name):
    small = MongoDbMemoryService(mongo_client=clients, database_name=db_name, max_results=2)

    async def go():
        evs = [make_event("user", f"hokkaido note {i}") for i in range(5)]
        await small.add_events_to_memory(app_name=APP, user_id="alice", events=evs, session_id="s1")
        return await recall(small, "alice", "hokkaido")

    assert len(run(go())) == 2


def test_non_text_events_are_skipped(memory):
    from google.adk.events.event import Event

    async def go():
        await memory.add_events_to_memory(app_name=APP, user_id="alice", events=[Event(author="agent")], session_id="s1")

    run(go())
    assert memory._memories().count_documents({}) == 0


def test_keyword_index_and_ttl_index_and_expire_at(clients, db_name):
    ttl = MongoDbMemoryService(mongo_client=clients, database_name=db_name, memory_ttl_days=7)
    run(ttl.add_events_to_memory(app_name=APP, user_id="alice", events=EVENTS[:1], session_id="s1"))
    info = ttl._memories().index_information()
    assert "app_user_keywords" in info and info["memory_ttl"]["expireAfterSeconds"] == 0
    doc = ttl._memories().find_one()
    remaining = doc["expire_at"].replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) < remaining <= timedelta(days=7)


@pytest.mark.slow
def test_ttl_monitor_deletes_expired_memories(clients, db_name):
    """TTL モニタは約 60 秒周期で動くため最大 ~100 秒かかる。"""
    ttl = MongoDbMemoryService(mongo_client=clients, database_name=db_name, memory_ttl_days=1)
    run(ttl.add_events_to_memory(app_name=APP, user_id="alice", events=EVENTS[:1], session_id="s1"))
    ttl._memories().update_many({}, {"$set": {"expire_at": datetime(2000, 1, 1)}})
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline and ttl._memories().count_documents({}) > 0:
        time.sleep(2)
    assert ttl._memories().count_documents({}) == 0
