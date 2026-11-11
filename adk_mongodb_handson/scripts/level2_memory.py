"""Level 2: Long-term Memory (MongoDbMemoryService, keyword モード) の検証スクリプト。

  python -m scripts.level2_memory
"""

from __future__ import annotations

import asyncio
import warnings

warnings.filterwarnings("ignore", message=r"\[EXPERIMENTAL\]")

from google.adk.integrations.mongodb import MongoDbMemoryService, MongoDbSessionService  # noqa: E402

from handson import config  # noqa: E402
from handson.common import make_event, unique_name  # noqa: E402

APP = "handson_app"


async def recall(ms: MongoDbMemoryService, user: str, query: str) -> list[str]:
    r = await ms.search_memory(app_name=APP, user_id=user, query=query)
    return [m.content.parts[0].text for m in r.memories]


async def main() -> None:
    uri = config.get_uri("replicaset")
    db = unique_name("adk_l2")
    sessions = MongoDbSessionService(connection_string=uri, database_name=db)
    memory = MongoDbMemoryService(connection_string=uri, database_name=db)

    s1 = await sessions.create_session(app_name=APP, user_id="alice", session_id="trip-1")
    for author, text in [
        ("user", "I want to go hiking in Hokkaido next summer"),
        ("agent", "Hokkaido has great trails in Daisetsuzan"),
        ("user", "私はMongoDBが好きです"),
    ]:
        await sessions.append_event(s1, make_event(author, text))
    s1 = await sessions.get_session(app_name=APP, user_id="alice", session_id="trip-1")

    await memory.add_session_to_memory(s1)
    await memory.add_session_to_memory(s1)  # 再登録しても重複しない
    print("1) 登録件数 (2 回登録):", memory._memories().count_documents({}))

    # 別セッション (trip-2) の文脈からでも、同一ユーザーの記憶として検索できる
    print("2) 'hokkaido':", await recall(memory, "alice", "hokkaido"))
    print("3) 'MongoDB' (日本語混在):", await recall(memory, "alice", "MongoDB"))
    print("4) '好き' (部分一致なし):", await recall(memory, "alice", "好き"))
    print("5) 別ユーザー bob:", await recall(memory, "bob", "hokkaido"))

    memory._client.drop_database(db)
    await memory.close()
    await sessions.close()


if __name__ == "__main__":
    asyncio.run(main())
