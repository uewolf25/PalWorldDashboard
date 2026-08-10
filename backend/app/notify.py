"""Discord Webhook 通知。

通知の失敗で本処理を止めないこと。必ず握りつぶしてログに残す。
"""

from __future__ import annotations

import logging
import time
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

Level = Literal["info", "warn", "crit"]

_COLORS: dict[str, int] = {
    "info": 0x3BA55D,   # green
    "warn": 0xE8A33D,   # amber
    "crit": 0xED4245,   # red
}


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: str = "",
        alert_webhook_url: str = "",
        *,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.alert_webhook_url = alert_webhook_url or webhook_url
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        # テスト・UI 確認用に送信内容を保持する
        self.sent: list[dict] = []

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def send(self, title: str, description: str = "", level: Level = "info") -> bool:
        url = self.alert_webhook_url if level in ("warn", "crit") else self.webhook_url
        payload = {
            "embeds": [
                {
                    "title": title[:256],
                    "description": description[:4000],
                    "color": _COLORS.get(level, _COLORS["info"]),
                    "footer": {"text": "Palworld Server Manager"},
                }
            ]
        }
        self.sent.append({"title": title, "description": description, "level": level, "ts": time.time()})
        if not url:
            logger.debug("Discord webhook 未設定のため送信をスキップ: %s", title)
            return False
        try:
            client = await self._get_client()
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning("Discord 通知に失敗: HTTP %s %s", resp.status_code, resp.text[:200])
                return False
            return True
        except httpx.HTTPError as exc:
            logger.warning("Discord 通知に失敗: %s", exc)
            return False
