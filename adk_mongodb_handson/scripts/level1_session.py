"""Level 1: Session Persistence の検証スクリプト。

使い方 (adk_mongodb_handson/ で実行):
  python -m scripts.level1_session demo                      # 作成・取得・競合制御・InMemory 比較
  python -m scripts.level1_session write --db DB --session-id S1   # プロセス A: 書き込み
  python -m scripts.level1_session read  --db DB --session-id S1   # プロセス B: 別プロセスで復元
"""

from __future__ import annotations

import argparse
import asyncio
import warnings

warnings.filterwarnings("ignore", message=r"\[EXPERIMENTAL\]")

from google.adk.integrations.mongodb import MongoDbSessionService  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402

from handson import config  # noqa: E402
from handson.common import make_event, race_append, texts, unique_name  # noqa: E402

APP, USER = "handson_app", "alice"


async def write(uri: str, db: str, session_id: str) -> None:
    service = MongoDbSessionService(connection_string=uri, database_name=db)
    session = await service.create_session(
        app_name=APP,
        user_id=USER,
        session_id=session_id,
        state={"topic": "hokkaido", "user:language": "ja", "app:version": 1},
    )
    await service.append_event(session, make_event("user", "北海道旅行の計画を立てたい"))
    await service.append_event(session, make_event("agent", "いつ頃の予定ですか?"))
    print(f"[write] session={session_id} events=2 revision={session._storage_update_marker}")
    await service.close()


async def read(uri: str, db: str, session_id: str) -> None:
    service = MongoDbSessionService(connection_string=uri, database_name=db)
    session = await service.get_session(app_name=APP, user_id=USER, session_id=session_id)
    if session is None:
        print("[read] session not found")
    else:
        print(f"[read] events={texts(session.events)}")
        print(f"[read] state={session.state}")
    await service.close()


async def demo(uri: str) -> None:
    db = unique_name("adk_l1")
    a = MongoDbSessionService(connection_string=uri, database_name=db)
    b = MongoDbSessionService(connection_string=uri, database_name=db)  # 別インスタンス = 別プロセス相当

    session = await a.create_session(app_name=APP, user_id=USER, session_id="s1", state={"k": 1})
    await a.append_event(session, make_event("user", "hello"))
    restored = await b.get_session(app_name=APP, user_id=USER, session_id="s1")
    print("1) 別インスタンスから復元:", texts(restored.events), restored.state)

    print("2) 競合制御 (8 writer が同じリビジョンから同時追記)")
    print("   リトライなし:", await race_append([a, b], app_name=APP, user_id=USER, session_id="s1", writers=8, retry=False))
    print("   再取得リトライ:", await race_append([a, b], app_name=APP, user_id=USER, session_id="s1", writers=8, retry=True))

    mem = InMemorySessionService()
    ms = await mem.create_session(app_name=APP, user_id=USER, session_id="s1")
    await mem.append_event(ms, make_event("user", "hello"))
    print("3) InMemory は新インスタンスで消える:", await InMemorySessionService().get_session(app_name=APP, user_id=USER, session_id="s1"))

    a._client.drop_database(db)
    await a.close()
    await b.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["demo", "write", "read"])
    parser.add_argument("--uri", default=None, help="未指定なら MONGODB_URI_REPLICASET / 既定値")
    parser.add_argument("--db", default="adk_l1_restart")
    parser.add_argument("--session-id", default="s-restart")
    args = parser.parse_args()
    uri = args.uri or config.get_uri("replicaset")
    if args.command == "demo":
        asyncio.run(demo(uri))
    elif args.command == "write":
        asyncio.run(write(uri, args.db, args.session_id))
    else:
        asyncio.run(read(uri, args.db, args.session_id))


if __name__ == "__main__":
    main()
