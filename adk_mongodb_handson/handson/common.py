"""セッション / 記憶まわりの共通ヘルパー。"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from google.adk.errors._stale_session_error import StaleSessionError
from google.adk.events.event import Event
from google.genai import types


def unique_name(prefix: str) -> str:
    """検証ごとに独立した DB 名を作る (前回データや検索インデックスの残骸を避ける)。"""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def make_event(author: str, text: str, **kwargs: Any) -> Event:
    role = "user" if author == "user" else "model"
    return Event(
        author=author,
        content=types.Content(role=role, parts=[types.Part(text=text)]),
        **kwargs,
    )


def texts(session_events: Sequence[Event]) -> list[str]:
    return [
        p.text
        for e in session_events
        if e.content and e.content.parts
        for p in e.content.parts
        if p.text
    ]


async def append_with_retry(
    service: Any,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    build_event: Callable[[], Event],
    max_retries: int = 20,
) -> int:
    """StaleSessionError 時にセッションを再取得して再試行する。

    ADK は競合を自動リトライしない (楽観的ロックの検出まで)。再試行はアプリ側の責務。
    戻り値は再試行回数。
    """
    for attempt in range(max_retries + 1):
        session = await service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        try:
            await service.append_event(session, build_event())
            return attempt
        except StaleSessionError:
            await asyncio.sleep(0)
    raise RuntimeError("retry limit exceeded")


async def race_append(
    services: Sequence[Any],
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    writers: int,
    retry: bool,
) -> dict[str, int]:
    """複数 writer が同一セッションへ同時に 1 イベントずつ追記する。

    retry=False: 競合は StaleSessionError として顕在化 (勝者のみ成功)。
    retry=True : 再取得リトライにより全 writer が最終的に成功する。
    """
    ok = stale = 0

    async def writer(i: int) -> None:
        nonlocal ok, stale
        service = services[i % len(services)]
        if retry:
            await append_with_retry(
                service,
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
                build_event=lambda: make_event("agent", f"writer-{i}"),
            )
            ok += 1
            return
        session = await service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        # 全 writer が同じリビジョンを読んでから書き込む (競合を確実に発生させる)
        await barrier.wait()
        try:
            await service.append_event(session, make_event("agent", f"writer-{i}"))
            ok += 1
        except StaleSessionError:
            stale += 1

    barrier = asyncio.Barrier(writers)
    await asyncio.gather(*(writer(i) for i in range(writers)))
    return {"succeeded": ok, "stale": stale}


class Stopwatch:
    def __enter__(self) -> "Stopwatch":
        self._t = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = (time.perf_counter() - self._t) * 1000
