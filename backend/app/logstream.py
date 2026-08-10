"""サーバログのリアルタイム配信。

journalctl / ファイル tail / なし の3ソースに対応。
どのソースでも、この管理ツール自身のログ（再起動シーケンス等）は必ず流す。
接続が遅い購読者のせいで全体が詰まらないよう、キューが溢れたら古い行を捨てる。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

MAX_QUEUE = 500
BACKLOG = 200


class LogBroker:
    """行を購読者に配る。購読者ごとに独立したキューを持つ。"""

    def __init__(self, backlog: int = BACKLOG) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._backlog: deque[dict[str, Any]] = deque(maxlen=backlog)
        self._producer: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- 配信 ----------------------------------------------------------

    def publish(self, line: str, source: str = "server") -> None:
        record = {"ts": time.time(), "source": source, "line": line.rstrip("\n")}
        self._backlog.append(record)
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()  # 古い行を捨てて最新を優先する
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:  # pragma: no cover
                pass

    def bind_loop(self) -> None:
        """publish_threadsafe 用に、現在のイベントループを覚えておく。"""
        self._loop = asyncio.get_running_loop()

    def publish_threadsafe(self, line: str, source: str = "app") -> None:
        """別スレッド（logging ハンドラ）から呼ぶ用。"""
        if self._loop is None or self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self.publish, line, source)
        except RuntimeError:  # pragma: no cover - ループ停止中
            pass

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        # 接続直後に直近ログを流し込む
        for record in list(self._backlog):
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:
                break
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def backlog(self) -> list[dict[str, Any]]:
        return list(self._backlog)

    # ---- 取り込み元 ----------------------------------------------------

    async def start(self, source: str, *, unit: str = "", path: str = "") -> None:
        self._loop = asyncio.get_running_loop()
        if self._producer is not None and not self._producer.done():
            return
        if source == "journald":
            self._producer = asyncio.create_task(self._run_journald(unit), name="log-journald")
        elif source == "file":
            self._producer = asyncio.create_task(self._tail_file(path), name="log-file")
        else:
            self.publish("ログ取り込みは無効です（LOG_SOURCE=none）", source="app")

    async def stop(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
        if self._producer is not None:
            self._producer.cancel()
            try:
                await self._producer
            except asyncio.CancelledError:
                pass
            self._producer = None

    async def _run_journald(self, unit: str) -> None:
        cmd = ["journalctl", "-u", unit, "-n", "100", "-f", "--output", "cat"]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except (FileNotFoundError, NotImplementedError) as exc:
            self.publish(f"journalctl を起動できません: {exc}", source="app")
            return
        assert self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                self.publish(raw.decode(errors="replace"), source="server")
        except asyncio.CancelledError:
            raise
        finally:
            if self._proc.returncode is None:
                self._proc.terminate()

    async def _tail_file(self, path: str) -> None:
        from pathlib import Path

        target = Path(path)
        self.publish(f"{target} を追跡します", source="app")
        pos = 0
        inode = None
        while True:
            try:
                if not target.is_file():
                    await asyncio.sleep(1.0)
                    continue
                st = target.stat()
                if inode is None:
                    inode = st.st_ino
                    pos = st.st_size  # 初回は末尾から
                elif st.st_ino != inode or st.st_size < pos:
                    # ローテートされた
                    inode = st.st_ino
                    pos = 0
                with target.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    for line in fh:
                        self.publish(line, source="server")
                    pos = fh.tell()
            except OSError as exc:  # pragma: no cover
                self.publish(f"ログ読み取りエラー: {exc}", source="app")
            await asyncio.sleep(0.5)


class BrokerLogHandler(logging.Handler):
    """管理ツール自身のログを WebSocket に流すハンドラ。"""

    def __init__(self, broker: LogBroker) -> None:
        super().__init__()
        self._broker = broker

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._broker.publish_threadsafe(self.format(record), source="app")
        except Exception:  # pragma: no cover - ログ配信で例外を出さない
            pass
