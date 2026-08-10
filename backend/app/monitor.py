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
from typing import Any

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

    # ---- サンプリング --------------------------------------------------

    def _host_stats(self) -> dict[str, Any]:
        vm = psutil.virtual_memory()
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
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
        return record

    # ---- アラート ------------------------------------------------------

    def _cooldown_passed(self, key: str, now: float) -> bool:
        last = self._last_alert_at.get(key)
        return last is None or (now - last) >= self.alert_cooldown_sec

    async def _check_alerts(self, record: dict[str, Any]) -> None:
        now = record["ts"]

        # サーバの上下動（状態が変わった瞬間だけ通知する）
        online = bool(record["online"])
        if self._last_online is None:
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
