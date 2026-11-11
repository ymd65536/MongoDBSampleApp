"""Level 3: 検索用コレクション・インデックスの構築ヘルパー。

ベクトル検索インデックスと全文検索インデックスは非同期に構築される。
`wait_until_queryable` で queryable になるまで待たないと、検索が空配列になる (実測済み)。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from pymongo.operations import SearchIndexModel

from .fakes import EMBEDDING_DIM, fake_embed

PRODUCTS = [
    ("Robot Vacuum", "cordless robot vacuum for pet hair on carpet and floors", "home"),
    ("Steam Mop", "steam mop that sanitizes hard floors without chemicals", "home"),
    ("Air Purifier", "quiet air purifier with HEPA filter for allergies", "home"),
    ("Hiking Boots", "waterproof hiking boots for mountain trails", "outdoor"),
    ("Camping Stove", "lightweight camping stove for backpacking trips", "outdoor"),
    ("Trail Backpack", "durable backpack for hiking and camping", "outdoor"),
    ("Espresso Machine", "compact espresso machine with milk frother", "kitchen"),
    ("Rice Cooker", "smart rice cooker with multiple cooking modes", "kitchen"),
]


def seed_products(
    db: Any, embed: Callable[[str], list[float]] = fake_embed, collection: str = "products"
) -> None:
    db[collection].insert_many(
        [
            {"name": n, "description": d, "category": c, "embedding": embed(d)}
            for n, d, c in PRODUCTS
        ]
    )


def create_product_indexes(
    db: Any,
    collection: str = "products",
    dim: int = EMBEDDING_DIM,
    vector_index: str = "vector_index",
    text_index: str = "default",
) -> None:
    coll = db[collection]
    coll.create_search_index(
        SearchIndexModel(
            name=vector_index,
            type="vectorSearch",
            definition={
                "fields": [
                    {"type": "vector", "path": "embedding", "numDimensions": dim, "similarity": "cosine"},
                    {"type": "filter", "path": "category"},
                ]
            },
        )
    )
    coll.create_search_index(
        SearchIndexModel(
            name=text_index,
            definition={"mappings": {"dynamic": False, "fields": {"description": {"type": "string"}}}},
        )
    )


def wait_until_queryable(coll: Any, timeout: float = 120.0, expected: int | None = None) -> float:
    """全検索インデックスが queryable になるまでの秒数を返す。タイムアウトで例外。"""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        indexes = list(coll.list_search_indexes())
        if indexes and all(i.get("queryable") for i in indexes):
            if expected is None or len(indexes) >= expected:
                return time.monotonic() - start
        time.sleep(0.5)
    raise TimeoutError(f"search index が {timeout}s 以内に queryable になりませんでした")
