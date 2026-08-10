"""Palworld 専用サーバ REST API (http://<host>:8212/v1/api) のクライアント。

認証は Basic 認証（ユーザ名 admin / PalWorldSettings.ini の AdminPassword）。
レスポンスのキー名はサーバのバージョンで揺れることがあるため、
呼び出し側では必ず .get() 経由で読むこと（KeyError で画面が落ちないように）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class PalApiError(RuntimeError):
    """Palworld API への到達失敗・エラー応答。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PalworldClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = (username, password)
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                auth=self._auth,
                timeout=self._timeout,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, json: dict | None = None) -> Any:
        client = await self._get_client()
        url = f"/v1/api/{path.lstrip('/')}"
        try:
            resp = await client.request(method, url, json=json, auth=self._auth)
        except httpx.HTTPError as exc:
            raise PalApiError(f"Palworld API に接続できません: {exc}") from exc

        if resp.status_code == 401:
            raise PalApiError("Palworld API の認証に失敗しました（AdminPassword を確認）", status_code=401)
        if resp.status_code >= 400:
            raise PalApiError(
                f"Palworld API がエラーを返しました: HTTP {resp.status_code} {resp.text[:200]}",
                status_code=resp.status_code,
            )

        if not resp.content:
            return {}
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype:
            # /announce などは "OK" のようなプレーンテキストを返すことがある
            return {"raw": resp.text.strip()}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text.strip()}

    # ---- 参照系 -------------------------------------------------------

    async def info(self) -> dict:
        return await self._request("GET", "info")

    async def metrics(self) -> dict:
        return await self._request("GET", "metrics")

    async def players(self) -> list[dict]:
        data = await self._request("GET", "players")
        if isinstance(data, dict):
            players = data.get("players", [])
            return players if isinstance(players, list) else []
        return []

    async def settings(self) -> dict:
        data = await self._request("GET", "settings")
        return data if isinstance(data, dict) else {}

    # ---- 操作系 -------------------------------------------------------

    async def announce(self, message: str) -> Any:
        return await self._request("POST", "announce", {"message": message})

    async def kick(self, userid: str, message: str = "") -> Any:
        return await self._request("POST", "kick", {"userid": userid, "message": message})

    async def ban(self, userid: str, message: str = "") -> Any:
        return await self._request("POST", "ban", {"userid": userid, "message": message})

    async def unban(self, userid: str) -> Any:
        return await self._request("POST", "unban", {"userid": userid})

    async def save(self) -> Any:
        return await self._request("POST", "save")

    async def shutdown(self, waittime: int = 30, message: str = "") -> Any:
        return await self._request(
            "POST", "shutdown", {"waittime": waittime, "message": message}
        )

    async def stop(self) -> Any:
        return await self._request("POST", "stop")
