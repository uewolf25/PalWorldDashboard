"""サーバログのリアルタイム配信と、管理ツール自身のログの出力先設定。

journalctl / ファイル tail / なし の3ソースに対応。
どのソースでも、この管理ツール自身のログ（再起動シーケンス等）は必ず流す。
接続が遅い購読者のせいで全体が詰まらないよう、キューが溢れたら古い行を捨てる。

画面に流すだけだと `BACKLOG` 行のメモリ上リングバッファに載るだけで、
プロセスが再起動した時点で消える。障害の原因を後から追えるよう、
`configure_logging()` で stderr にも出して journald に残す。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from collections import deque
from typing import IO, Any, AsyncIterator

logger = logging.getLogger(__name__)

MAX_QUEUE = 500
BACKLOG = 200

# 画面で絞り込むための区分。重い順
LEVELS = ("error", "warn", "info")

# 出力先を設定済みかの目印。create_app が複数回呼ばれても二重に付けない
_HANDLER_MARK = "_dashboard_pal_stderr"


def configure_logging(level: str = "INFO", *, stream: IO[str] | None = None) -> logging.Handler | None:
    """管理ツール自身のログを stderr に出す。systemd 経由で journald に入る。

    何もしないとログはどこにも残らない。uvicorn が設定するのは `uvicorn*` の
    ロガーだけで root には何も付かず、そのうえ `app` ロガーには WebSocket 配信用の
    ハンドラが付くため、Python の lastResort（stderr / WARNING 以上）も発動しない。
    つまり ERROR すら stderr に出ない状態になる。

    root は WARNING のままにして、`app` 配下だけ指定のレベルまで下げる。
    root ごと INFO にすると httpx や apscheduler の内部ログで埋まる。

    設定済みなら何もせず None を返す。
    """
    root = logging.getLogger()
    if any(getattr(h, _HANDLER_MARK, False) for h in root.handlers):
        return None

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    # journald 側でも時刻は付くが、ログ画面や journalctl --output=cat で
    # 見たときに時系列を追えないと困る
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
    root.setLevel(logging.WARNING)

    resolved = getattr(logging, level.upper(), logging.INFO)
    if not isinstance(resolved, int):  # 不正な値で全部黙らせない
        resolved = logging.INFO
    logging.getLogger("app").setLevel(resolved)
    return handler

_FROM_LOGGING = {
    logging.CRITICAL: "error",
    logging.ERROR: "error",
    logging.WARNING: "warn",
    logging.INFO: "info",
    logging.DEBUG: "info",
}

# ゲームサーバ側の行には決まった書式が無いので、単語で当たりを付ける。
# 「0 errors」のような行まで拾ってしまうが、取りこぼすより出しすぎる方がよい
_ERROR_RE = re.compile(
    r"\b(error|errors|fatal|critical|exception|traceback|failed|failure)\b", re.I
)
_WARN_RE = re.compile(r"\b(warn|warning|deprecated)\b", re.I)


def detect_level(line: str) -> str:
    """行の見た目から区分を推測する。

    管理ツール自身のログは logging の levelno を渡すので、ここは通らない。
    使うのはゲームサーバ側の行だけ。
    """
    if _ERROR_RE.search(line):
        return "error"
    if _WARN_RE.search(line):
        return "warn"
    return "info"


class LogBroker:
    """行を購読者に配る。購読者ごとに独立したキューを持つ。"""

    def __init__(self, backlog: int = BACKLOG) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._backlog: deque[dict[str, Any]] = deque(maxlen=backlog)
        self._producer: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- 配信 ----------------------------------------------------------

    def publish(self, line: str, source: str = "server", level: str | None = None) -> None:
        text = line.rstrip("\n")
        record = {
            "ts": time.time(),
            "source": source,
            "line": text,
            # 呼び出し側が分かっているならそれを使う。管理ツール自身のログは
            # logging の levelno から確実に決められるので推測しない
            "level": level if level in LEVELS else detect_level(text),
        }
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

    def publish_threadsafe(
        self, line: str, source: str = "app", level: str | None = None
    ) -> None:
        """別スレッド（logging ハンドラ）から呼ぶ用。"""
        if self._loop is None or self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self.publish, line, source, level)
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

    def backlog(self, level: str | None = None) -> list[dict[str, Any]]:
        records = list(self._backlog)
        if level in LEVELS:
            records = [r for r in records if r["level"] == level]
        return records

    def level_counts(self) -> dict[str, int]:
        """区分ごとの件数。画面のフィルタに添えるバッジに使う。"""
        counts = {name: 0 for name in LEVELS}
        for record in self._backlog:
            counts[record["level"]] = counts.get(record["level"], 0) + 1
        return counts

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
            # 自分のログは levelno が分かっているので推測しない
            level = _FROM_LOGGING.get(record.levelno, "info")
            self._broker.publish_threadsafe(self.format(record), source="app", level=level)
        except Exception:  # pragma: no cover - ログ配信で例外を出さない
            pass
