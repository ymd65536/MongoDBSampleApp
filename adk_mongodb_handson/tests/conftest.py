"""pytest 共通設定。

MongoDB が必要なテストはマーカーで分離され、接続できなければ skip される
(REQUIRE_MONGO=1 の場合は skip ではなく fail。CI で使用)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handson import config  # noqa: E402
from handson.common import unique_name  # noqa: E402

MARKER_TO_KIND = {"replicaset": "replicaset", "standalone": "standalone", "atlas_local": "atlas_local"}


def _reachable(uri: str) -> bool:
    client = MongoClient(uri, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
        return True
    except Exception:
        return False
    finally:
        client.close()


def _require(uri: str, label: str) -> None:
    if _reachable(uri):
        return
    msg = f"{label} に接続できません ({uri.split('@')[-1]}). docker compose の該当 profile を起動してください"
    if os.environ.get("REQUIRE_MONGO") == "1":
        pytest.fail(msg)
    pytest.skip(msg)


@pytest.fixture(scope="session")
def replicaset_uri() -> str:
    uri = config.get_uri("replicaset")
    _require(uri, "replicaset")
    return uri


@pytest.fixture(scope="session")
def standalone_uri() -> str:
    uri = config.get_uri("standalone")
    _require(uri, "standalone")
    return uri


@pytest.fixture(scope="session")
def atlas_local_uri() -> str:
    uri = config.get_uri("atlas_local")
    _require(uri, "atlas-local")
    return uri


@pytest.fixture
def db_name() -> str:
    return unique_name("adk_test")


@pytest.fixture
def mongo_factory():
    """URI からクライアントを作り、テスト後にクローズ + DB を削除する。"""
    clients: list[tuple[MongoClient, str]] = []

    def make(uri: str, database: str, **kwargs) -> MongoClient:
        client = MongoClient(uri, **kwargs)
        clients.append((client, database))
        return client

    yield make
    for client, database in clients:
        try:
            client.drop_database(database)
        finally:
            client.close()


@pytest.fixture(scope="session")
def span_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter
