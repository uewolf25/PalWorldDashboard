"""ゲームサーバへの問い合わせを間引くための小さなキャッシュ。

ダッシュボードは 1 秒ごとに /api/status を叩き、その中で
info と metrics の 2 本を呼ぶ。タブを 3 枚開けば毎秒 6 回、
ゲームサーバに問い合わせが飛び続けることになる。

TTL だけでは同時に来たリクエストが揃って素通りするので、
先頭の 1 本だけを実行して残りはその結果を待つ（合流させる）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """TTL 付きの単一値キャッシュ。同時アクセスは 1 本に合流する。"""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._value: T | None = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    def _fresh(self, now: float) -> bool:
        return self._value is not None and (now - self._fetched_at) < self.ttl

    async def get(self, factory: Callable[[], Awaitable[T]]) -> T:
        if self.ttl <= 0:
            return await factory()

        now = time.monotonic()
        if self._fresh(now):
            return self._value  # type: ignore[return-value]

        async with self._lock:
            # ロック待ちの間に別のリクエストが取ってきているかもしれない
            now = time.monotonic()
            if self._fresh(now):
                return self._value  # type: ignore[return-value]
            value = await factory()
            self._value = value
            self._fetched_at = time.monotonic()
            return value

    def invalidate(self) -> None:
        """次回の取得で必ず取り直す。

        キックのように、こちらの操作で内容が変わったことが分かっている
        場合に呼ぶ。古い一覧を最大 TTL 秒見せ続けないため。
        """
        self._value = None
        self._fetched_at = 0.0

    def stats(self) -> dict[str, Any]:
        return {
            "ttl": self.ttl,
            "cached": self._value is not None,
            "age": round(time.monotonic() - self._fetched_at, 3) if self._value is not None else None,
        }
