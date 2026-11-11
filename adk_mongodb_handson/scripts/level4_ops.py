"""Level 4: 運用観点の実測スクリプト。

  python -m scripts.level4_ops bench     # レイテンシ / イベント数に対する get_session の増加 / 保存サイズ
  python -m scripts.level4_ops failure [timeoutMS]  # MongoDB 停止→復旧時の挙動 (docker compose を操作)
  python -m scripts.level4_ops auth      # 認証・最小権限 (auth profile)
  python -m scripts.level4_ops otel      # OpenTelemetry のトレース構造
  python -m scripts.level4_ops ttl       # 記憶の TTL 削除と、セッションに TTL が効かない確認 (約 70〜130 秒)
  python -m scripts.level4_ops purge     # セッションの定期削除バッチ
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message=r"\[EXPERIMENTAL\]")

from google.adk.agents import Agent  # noqa: E402
from google.adk.integrations.mongodb import MongoDbMemoryService, MongoDbSessionService  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.adk.sessions.base_session_service import GetSessionConfig  # noqa: E402
from google.genai import types  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from pymongo import MongoClient  # noqa: E402

from handson import config  # noqa: E402
from handson.common import Stopwatch, make_event, unique_name  # noqa: E402
from handson.fakes import ScriptedLlm  # noqa: E402
from handson.ops import MongoCommandTracer, latency_summary, purge_sessions  # noqa: E402

APP, USER = "ops_app", "alice"
COMPOSE_DIR = Path(__file__).resolve().parent.parent


async def _bench_one(label: str, service, n_events: int = 200) -> None:
    s = await service.create_session(app_name=APP, user_id=USER, session_id="bench")
    append_ms, get_ms = [], []
    for i in range(n_events):
        with Stopwatch() as w:
            await service.append_event(s, make_event("user" if i % 2 == 0 else "agent", f"message {i} " + "x" * 200))
        append_ms.append(w.ms)
    for _ in range(30):
        with Stopwatch() as w:
            await service.get_session(app_name=APP, user_id=USER, session_id="bench")
        get_ms.append(w.ms)
    with Stopwatch() as w_recent:
        for _ in range(30):
            await service.get_session(
                app_name=APP, user_id=USER, session_id="bench", config=GetSessionConfig(num_recent_events=10)
            )
    print(f"[{label}] append_event({n_events}): {latency_summary(append_ms)}")
    print(f"[{label}] get_session(全{n_events}イベント): {latency_summary(get_ms)}")
    print(f"[{label}] get_session(直近10イベント) mean: {w_recent.ms / 30:.2f}ms")


async def bench() -> None:
    await _bench_one("InMemory", InMemorySessionService())
    for kind in ("standalone", "replicaset"):
        db = unique_name("adk_bench")
        service = MongoDbSessionService(connection_string=config.get_uri(kind), database_name=db)
        await _bench_one(f"MongoDB/{kind}", service)
        stats = service._client[db].command("collstats", "events")
        print(f"[MongoDB/{kind}] events: count={stats['count']} avgObjSize={stats['avgObjSize']}B storageSize={stats['storageSize']}B")
        service._client.drop_database(db)
        await service.close()


def _compose(*args: str) -> None:
    subprocess.run(["docker", "compose", "--profile", "replicaset", *args], cwd=COMPOSE_DIR, check=True, capture_output=True)


async def failure() -> None:
    logging.getLogger("pymongo").setLevel(logging.ERROR)
    uri, db = config.get_uri("replicaset"), unique_name("adk_fail")
    # 第 2 引数 (ms) で timeoutMS を指定すると、停止中の書き込みが待たされる時間を制限できる
    timeout_ms = int(sys.argv[2]) if len(sys.argv) > 2 else None
    options = {"timeoutMS": timeout_ms} if timeout_ms else {}
    print(f"timeoutMS={timeout_ms}")
    client = MongoClient(uri, serverSelectionTimeoutMS=3000, **options)
    service = MongoDbSessionService(mongo_client=client, database_name=db)
    s = await service.create_session(app_name=APP, user_id=USER, session_id="f1")
    await service.append_event(s, make_event("user", "before-outage"))
    acked = 1

    print("MongoDB を停止します ...")
    _compose("stop", "mongo-rs")
    t0 = time.perf_counter()
    try:
        # timeoutMS を指定しても with_transaction の再試行 (約 120 秒) は短縮されなかったため、
        # アプリ側でも asyncio.wait_for で待ち時間を制限する (実行中のスレッドは止まらない点に注意)
        await asyncio.wait_for(
            service.append_event(s, make_event("user", "during-outage")),
            timeout=(timeout_ms / 1000) if timeout_ms else None,
        )
    except Exception as exc:
        print(f"停止中の書き込み: {type(exc).__name__} ({(time.perf_counter() - t0):.1f}s 後に失敗)")

    print("MongoDB を再起動します ...")
    _compose("start", "mongo-rs")
    t1 = time.perf_counter()
    while True:
        try:
            fresh = await service.get_session(app_name=APP, user_id=USER, session_id="f1")
            await service.append_event(fresh, make_event("user", "after-recovery"))
            acked += 1
            break
        except Exception:
            await asyncio.sleep(0.5)
    print(f"再起動から書き込み再開まで: {time.perf_counter() - t1:.1f}s (同一 MongoClient / プロセス再起動なし)")
    final = await service.get_session(app_name=APP, user_id=USER, session_id="f1")
    print(f"ack 済み書き込み={acked} / 保存イベント={len(final.events)} (ack 済みの欠損なし={len(final.events) >= acked} / タイムアウトしたが実際には保存された書き込み={len(final.events) - acked})")
    client.drop_database(db)
    client.close()


async def auth() -> None:
    from pymongo.errors import OperationFailure

    db = config.auth_db_name()
    logging.basicConfig(level=logging.WARNING, format="  [log] %(message).160s")
    cases = [("adk_app", "ADK_APP_PASSWORD"), ("adk_reader", "ADK_READER_PASSWORD"), ("adk_noindex", "ADK_NOINDEX_PASSWORD")]
    for user, env in cases:
        client = MongoClient(config.get_auth_uri(user, env), serverSelectionTimeoutMS=5000)
        service = MongoDbSessionService(mongo_client=client, database_name=db)
        sid = unique_name(user)
        try:
            s = await service.create_session(app_name=APP, user_id=USER, session_id=sid)
            await service.append_event(s, make_event("user", "hi"))
            print(f"{user}: 書き込み OK")
        except OperationFailure as exc:
            print(f"{user}: 書き込み拒否 code={exc.code} ({(exc.details or {}).get("codeName")})")
        client.close()


async def otel() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    uri, db = config.get_uri("replicaset"), unique_name("adk_otel")
    client = MongoClient(uri, event_listeners=[MongoCommandTracer()])
    service = MongoDbSessionService(mongo_client=client, database_name=db)
    runner = Runner(app_name=APP, agent=Agent(name="agent", model=ScriptedLlm()), session_service=service)
    s = await service.create_session(app_name=APP, user_id=USER)
    exporter.clear()
    # アプリ側で親 span を作ると、ADK の span と MongoDB コマンドが 1 つのトレースにまとまる
    with trace.get_tracer("handson").start_as_current_span("handle_request"):
        async for _ in runner.run_async(user_id=USER, session_id=s.id, new_message=types.Content(role="user", parts=[types.Part(text="hi")])):
            pass
    spans = exporter.get_finished_spans()
    by_id = {sp.context.span_id: sp for sp in spans}

    def depth(sp) -> int:
        d = 0
        while sp.parent is not None and sp.parent.span_id in by_id:
            sp, d = by_id[sp.parent.span_id], d + 1
        return d

    for sp in sorted(spans, key=lambda x: x.start_time):
        print("  " * depth(sp) + sp.name)
    print(f"trace_id の種類数 = {len({sp.context.trace_id for sp in spans})}")
    client.drop_database(db)
    client.close()


async def ttl() -> None:
    uri, db_name = config.get_uri("replicaset"), unique_name("adk_ttl")
    client = MongoClient(uri)
    db = client[db_name]
    memory = MongoDbMemoryService(mongo_client=client, database_name=db_name, memory_ttl_days=1)
    await memory.add_events_to_memory(app_name=APP, user_id=USER, events=[make_event("user", "expire me")], session_id="s")
    doc = db.memories.find_one()
    print("expire_at (ingest + 1 day):", doc["expire_at"])

    # セッション: update_time は float(epoch 秒)。TTL インデックスは Date 型にしか効かない
    sessions = MongoDbSessionService(mongo_client=client, database_name=db_name)
    await sessions.create_session(app_name=APP, user_id=USER, session_id="old")
    db.sessions.create_index("update_time", expireAfterSeconds=1, name="try_ttl")

    # 記憶側は expire_at を過去に書き換えて TTL モニタ (約 60 秒周期) の削除を待つ
    db.memories.update_one({"_id": doc["_id"]}, {"$set": {"expire_at": doc["expire_at"].replace(year=2000)}})
    start = time.monotonic()
    while time.monotonic() - start < 150 and db.memories.count_documents({}) > 0:
        await asyncio.sleep(2)
    print(f"memories 削除まで: {time.monotonic() - start:.0f}s (残件={db.memories.count_documents({})})")
    print(f"sessions (TTL index を張っても) 残件={db.sessions.count_documents({})}  ← Date 型でないため削除されない")
    client.drop_database(db_name)
    client.close()


async def purge() -> None:
    uri, db_name = config.get_uri("replicaset"), unique_name("adk_purge")
    service = MongoDbSessionService(connection_string=uri, database_name=db_name)
    for sid in ("s1", "s2"):
        s = await service.create_session(app_name=APP, user_id=USER, session_id=sid)
        await service.append_event(s, make_event("user", sid))
    db = service._client[db_name]
    db.sessions.update_one({"id": "s1"}, {"$set": {"update_time": time.time() - 40 * 86400}})
    print("削除前:", db.sessions.count_documents({}), "sessions /", db.events.count_documents({}), "events")
    print("purge (30 日超):", purge_sessions(db, older_than_seconds=30 * 86400))
    print("削除後:", db.sessions.count_documents({}), "sessions /", db.events.count_documents({}), "events")
    service._client.drop_database(db_name)
    await service.close()


COMMANDS = {"bench": bench, "failure": failure, "auth": auth, "otel": otel, "ttl": ttl, "purge": purge}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        raise SystemExit(__doc__)
    asyncio.run(COMMANDS[sys.argv[1]]())
