"""Level 3: Vector Search / Hybrid Search (MongoDbToolset) の検証スクリプト。

Atlas Local (docker compose --profile atlas-local) が必要。
既定は API キー不要の FakeGenaiClient。`--live` で実 Embedding (GOOGLE_API_KEY) を使う。

  python -m scripts.level3_search
  python -m scripts.level3_search --live    # 外部 API 利用 (任意)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import warnings

warnings.filterwarnings("ignore", message=r"\[EXPERIMENTAL\]")

from google.adk.agents import Agent  # noqa: E402
from google.adk.integrations.mongodb import MongoDbSessionService, MongoDbToolset, MongoDbToolSettings  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.genai import types  # noqa: E402
from pymongo import MongoClient  # noqa: E402

from handson import config  # noqa: E402
from handson.common import texts, unique_name  # noqa: E402
from handson.fakes import EMBEDDING_DIM, FakeGenaiClient, ScriptedLlm  # noqa: E402
from handson.search_setup import create_product_indexes, seed_products, wait_until_queryable  # noqa: E402


async def main(live: bool) -> None:
    client = MongoClient(config.get_uri("atlas_local"))
    db_name = unique_name("adk_l3")
    db = client[db_name]

    if live:  # 未検証: 外部 API (GOOGLE_API_KEY) が必要。README「未検証事項」参照
        from google import genai

        real = genai.Client()
        dim, genai_client = 768, None  # None: ツールセットが環境変数から Client を構築する

        def embed(text: str) -> list[float]:
            res = real.models.embed_content(model="text-embedding-005", contents=[text])
            return list(res.embeddings[0].values)

        seed_products(db, embed)
    else:
        genai_client, dim = FakeGenaiClient(), EMBEDDING_DIM
        seed_products(db)
    create_product_indexes(db, dim=dim)
    print(f"index queryable まで {wait_until_queryable(db.products, expected=2):.1f}s")

    settings = MongoDbToolSettings(vertex_ai_embedding_output_dimensionality=None if live else dim)
    toolset = MongoDbToolset(mongo_client=client, database_name=db_name, genai_client=genai_client, settings=settings)
    tools = {t.name: t for t in await toolset.get_tools()}

    common = dict(client=client, database_name=db_name, settings=settings, genai_client=genai_client)
    query = "pet hair vacuum"
    for label, tool, args in [
        ("vector", "vector_search", dict(collection_name="products", query=query, limit=3, output_fields=["name"])),
        ("vector+filter(outdoor)", "vector_search", dict(collection_name="products", query=query, filter={"category": "outdoor"}, limit=3, output_fields=["name"])),
        ("hybrid", "hybrid_search", dict(collection_name="products", query=query, text_search_field="description", limit=3, output_fields=["name"])),
    ]:
        result = await tools[tool].func(**args, **common)
        print(label, json.dumps(result, ensure_ascii=False, default=str))

    # エージェント経由: ツール結果が最終回答に使われ、会話は MongoDB に永続化される
    llm = ScriptedLlm(
        tool_name="mongodb_vector_search",
        tool_args={"collection_name": "products", "query": "{query}", "limit": 2, "output_fields": ["name"]},
    )
    sessions = MongoDbSessionService(mongo_client=client, database_name=db_name)
    runner = Runner(app_name="shop", agent=Agent(name="shop", model=llm, tools=[toolset]), session_service=sessions)
    s = await sessions.create_session(app_name="shop", user_id="u1")
    async for _ in runner.run_async(user_id="u1", session_id=s.id, new_message=types.Content(role="user", parts=[types.Part(text=query)])):
        pass
    saved = await sessions.get_session(app_name="shop", user_id="u1", session_id=s.id)
    print("agent answer:", texts(saved.events)[-1])

    client.drop_database(db_name)
    client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    asyncio.run(main(p.parse_args().live))
