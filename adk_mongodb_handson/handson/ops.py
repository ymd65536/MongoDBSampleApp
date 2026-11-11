"""Level 4: 運用ヘルパー (OpenTelemetry コマンドトレース、データ削除、統計)。"""

from __future__ import annotations

import statistics
import time
from typing import Any

from opentelemetry import trace
from pymongo import monitoring

SERVICE_TRACER = "adk-mongodb-handson"


class MongoCommandTracer(monitoring.CommandListener):
    """pymongo のコマンドを OpenTelemetry の span にする。

    ADK 自体は `invoke_agent` / `generate_content` / `execute_tool` の span を出すが、
    MongoDbSessionService 内部の DB 呼び出しは span にならない (実測)。
    pymongo の CommandListener で補い、ADK の span の子として DB コマンドを可視化する。
    asyncio.to_thread は contextvars を引き継ぐため親子関係が保たれる。
    コマンド本文 (会話内容を含みうる) は記録せず、コマンド名・DB・所要時間のみ記録する。
    """

    def __init__(self, ignore: frozenset[str] = frozenset({"hello", "isMaster", "ismaster", "ping", "endSessions"})):
        self._tracer = trace.get_tracer(SERVICE_TRACER)
        self._spans: dict[int, Any] = {}
        self._ignore = ignore

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        if event.command_name in self._ignore:
            return
        span = self._tracer.start_span(f"mongodb.{event.command_name}")
        span.set_attribute("db.system", "mongodb")
        span.set_attribute("db.name", event.database_name)
        span.set_attribute("db.operation", event.command_name)
        self._spans[event.request_id] = span

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        span = self._spans.pop(event.request_id, None)
        if span:
            span.set_attribute("db.duration_micros", event.duration_micros)
            span.end()

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        span = self._spans.pop(event.request_id, None)
        if span:
            span.set_attribute("error", True)
            span.set_attribute("db.failure", str(event.failure)[:200])
            span.end()


def purge_sessions(db: Any, *, older_than_seconds: float, app_name: str | None = None) -> dict[str, int]:
    """`update_time` が基準より古いセッションとイベントを削除するバッチ。

    MongoDbSessionService は update_time を epoch 秒 (float) で保存するため、
    TTL インデックス (Date 型のみ対象) は使えない。定期バッチで消す必要がある。
    app_state / user_state は削除しない (セッション横断の状態のため)。
    """
    cutoff = time.time() - older_than_seconds
    query: dict[str, Any] = {"update_time": {"$lt": cutoff}}
    if app_name:
        query["app_name"] = app_name
    victims = list(db.sessions.find(query, {"app_name": 1, "user_id": 1, "id": 1}))
    events_deleted = 0
    for v in victims:
        events_deleted += db.events.delete_many(
            {"app_name": v["app_name"], "user_id": v["user_id"], "session_id": v["id"]}
        ).deleted_count
    sessions_deleted = db.sessions.delete_many({"_id": {"$in": [v["_id"] for v in victims]}}).deleted_count
    return {"sessions": sessions_deleted, "events": events_deleted}


def latency_summary(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return {
        "n": len(ordered),
        "p50_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(p95, 2),
        "mean_ms": round(statistics.fmean(ordered), 2),
    }
