"""稼働状況の定期サンプリングと閾値アラート。

- monitor_interval 秒ごとに Palworld のメトリクスとホストのリソースを記録
- メモリ使用率が警告/危険閾値を超えたら Discord に通知（同じレベルは cooldown 中は再送しない）
- サーバの up/down が切り替わったら通知
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

import psutil

from .notify import DiscordNotifier
from .palapi import PalApiError, PalworldClient

logger = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self,
        pal: PalworldClient,
        notifier: DiscordNotifier,
        *,
        interval: float = 30.0,
        history_size: int = 2880,
        mem_warn_percent: float = 80.0,
        mem_crit_percent: float = 90.0,
        alert_cooldown_sec: float = 1800.0,
    ) -> None:
        self._pal = pal
        self._notifier = notifier
        self.interval = interval
        self.mem_warn_percent = mem_warn_percent
        self.mem_crit_percent = mem_crit_percent
        self.alert_cooldown_sec = alert_cooldown_sec

        self.history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self.last_sample: dict[str, Any] | None = None
        self.last_error: str | None = None

        self._task: asyncio.Task | None = None
        self._last_alert_at: dict[str, float] = {}
        self._last_online: bool | None = None
        # この時刻までは「応答なし」を通知しない（意図的に落としている間）
        self._suppress_until: float = 0.0
        # 再起動シーケンスなどが進行中かを尋ねるフック
        self._maintenance: Callable[[], bool] | None = None
        # サンプルのたびに呼ぶ追加処理（入退室の観測など）
        self._after_sample: Callable[[], Awaitable[None]] | None = None

    # ---- サンプリング --------------------------------------------------

    def _host_stats(self) -> dict[str, Any]:
        vm = psutil.virtual_memory()
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            # 使用率だけだと「何コアの何%か」が分からない。画面に添えるため持たせる
            "cpu_count": psutil.cpu_count() or 0,
            "mem_percent": vm.percent,
            "mem_used_mb": round(vm.used / 1024 / 1024),
            "mem_total_mb": round(vm.total / 1024 / 1024),
        }

    async def sample(self) -> dict[str, Any]:
        """1回分のサンプルを取り、履歴に積む。例外は投げない。"""
        now = time.time()
        host = self._host_stats()
        record: dict[str, Any] = {
            "ts": now,
            "online": False,
            "fps": None,
            "players": None,
            "frametime": None,
            "uptime": None,
            **host,
        }
        try:
            metrics = await self._pal.metrics()
            record.update(
                {
                    "online": True,
                    "fps": metrics.get("serverfps"),
                    "players": metrics.get("currentplayernum"),
                    "frametime": metrics.get("serverframetime"),
                    "uptime": metrics.get("uptime"),
                    "max_players": metrics.get("maxplayernum"),
                }
            )
            self.last_error = None
        except PalApiError as exc:
            self.last_error = str(exc)

        self.history.append(record)
        self.last_sample = record
        await self._check_alerts(record)

        if self._after_sample is not None:
            # 入退室の観測など。ここで失敗しても監視は続ける
            try:
                await self._after_sample()
            except Exception:
                logger.exception("サンプル後の処理で想定外のエラー")
        return record

    # ---- メンテナンス中の抑止 ------------------------------------------

    def set_maintenance_probe(self, probe: Callable[[], bool]) -> None:
        """再起動シーケンスなどが進行中かを尋ねるフックを登録する。"""
        self._maintenance = probe

    def set_after_sample(self, hook: Callable[[], Awaitable[None]]) -> None:
        """サンプルのたびに呼ぶ処理を登録する。

        入退室の観測に使う。画面を開いている人がいなくても記録が続くよう、
        UI のポーリングではなくこのループから叩く。
        """
        self._after_sample = hook

    def suppress_downtime_alerts(self, seconds: float) -> None:
        """この先 seconds 秒は「応答なし」を通知しない。

        起動コマンドはプロセスを起こした時点で返るが、実機の Palworld が
        接続を受け付けるまでは数十秒かかる。その間の「応答なし」は誤報で、
        毎回の再起動で飛ぶと本物の障害通知が埋もれる。
        """
        self._suppress_until = max(self._suppress_until, time.time() + max(seconds, 0.0))

    def _downtime_suppressed(self, now: float) -> bool:
        if now < self._suppress_until:
            return True
        return bool(self._maintenance and self._maintenance())

    # ---- アラート ------------------------------------------------------

    def _cooldown_passed(self, key: str, now: float) -> bool:
        last = self._last_alert_at.get(key)
        return last is None or (now - last) >= self.alert_cooldown_sec

    async def _check_alerts(self, record: dict[str, Any]) -> None:
        now = record["ts"]

        # サーバの上下動（状態が変わった瞬間だけ通知する）
        online = bool(record["online"])
        if self._downtime_suppressed(now):
            # 意図的に落としている最中。_last_online をあえて更新しないので、
            # 抑止が明けてもまだ落ちていれば、そこで初めて通知が飛ぶ。
            # 抑止中に復帰していれば状態が変わらないまま終わり、何も飛ばない
            pass
        elif self._last_online is None:
            self._last_online = online
        elif online != self._last_online:
            self._last_online = online
            if online:
                await self._notifier.send("サーバ復帰", "Palworld API に再び到達できました。", "info")
            else:
                await self._notifier.send(
                    "サーバ応答なし",
                    f"Palworld API に到達できません。\n{self.last_error or ''}",
                    "crit",
                )

        # メモリ使用率
        mem = float(record["mem_percent"])
        if mem >= self.mem_crit_percent:
            key = "mem_crit"
        elif mem >= self.mem_warn_percent:
            key = "mem_warn"
        else:
            # 正常域に戻ったら次に超えたとき即通知できるようリセット
            self._last_alert_at.pop("mem_warn", None)
            self._last_alert_at.pop("mem_crit", None)
            return

        if self._cooldown_passed(key, now):
            self._last_alert_at[key] = now
            level = "crit" if key == "mem_crit" else "warn"
            await self._notifier.send(
                f"メモリ使用率 {mem:.1f}%",
                f"{record['mem_used_mb']}MB / {record['mem_total_mb']}MB を使用中です。",
                level,
            )

    # ---- ループ制御 ----------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - 監視ループは絶対に止めない
                logger.exception("監視サンプリングで想定外のエラー")
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="monitor-loop")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def history_since(self, seconds: float) -> list[dict[str, Any]]:
        cutoff = time.time() - seconds
        return [r for r in self.history if r["ts"] >= cutoff]
