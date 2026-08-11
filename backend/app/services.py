"""ゲームサーバのプロセス制御。

実機は systemd ユニットを操作する。開発機には systemctl が無いので、
同じインタフェースでモックサーバを起動/停止するバックエンドを用意し、
どちらを使うかは設定（PAL_SERVICE_BACKEND）で切り替える。

実機との差分はこのモジュールに閉じ込める。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    simulated: bool = False

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "simulated": self.simulated,
        }


class GameService(Protocol):
    """ゲームサーバのプロセスを起動/停止する口。"""

    async def start(self) -> CommandResult: ...
    async def stop(self) -> CommandResult: ...
    async def restart(self) -> CommandResult: ...

    async def is_active(self) -> bool | None:
        """稼働しているか。判定できない場合は None を返す。"""
        ...

    async def aclose(self) -> None: ...


class SystemdService:
    """systemctl でゲームサーバのユニットを操作する（本番）。"""

    def __init__(self, unit: str, *, dry_run: bool = False, timeout: float = 60.0) -> None:
        self.unit = unit
        self.dry_run = dry_run
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return shutil.which("systemctl") is not None

    async def _run(self, *args: str) -> CommandResult:
        if self.dry_run or not self.available:
            reason = "dry_run" if self.dry_run else "systemctl が無い環境"
            logger.info("systemctl %s をスキップ (%s)", " ".join(args), reason)
            return CommandResult(
                ok=True,
                returncode=0,
                stdout=f"[simulated] systemctl {' '.join(args)}",
                stderr="",
                simulated=True,
            )
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return CommandResult(False, -1, "", f"systemctl {' '.join(args)} がタイムアウトしました")
        return CommandResult(
            ok=proc.returncode == 0,
            returncode=proc.returncode or 0,
            stdout=out.decode(errors="replace").strip(),
            stderr=err.decode(errors="replace").strip(),
        )

    async def restart(self) -> CommandResult:
        return await self._run("restart", self.unit)

    async def start(self) -> CommandResult:
        return await self._run("start", self.unit)

    async def stop(self) -> CommandResult:
        return await self._run("stop", self.unit)

    async def is_active(self) -> bool | None:
        result = await self._run("is-active", self.unit)
        if result.simulated:
            # 実行できていないので「分からない」。稼働中と誤判定させない
            return None
        return result.stdout.strip() == "active"

    async def aclose(self) -> None:  # pragma: no cover - 保持リソース無し
        return None


class MockGameService:
    """モック Palworld サーバを起動/停止する（開発用）。

    systemctl の代わりにモックの制御エンドポイントを叩く。
    これがないと開発機では「停止」しても停止扱いにならず、
    設定ファイルの編集（停止中のみ許可）が一度も試せない。
    """

    def __init__(
        self,
        control_url: str,
        *,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.control_url = control_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str) -> CommandResult:
        url = f"{self.control_url}/__mock__/{path}"
        try:
            client = await self._get_client()
            resp = await client.post(url)
        except httpx.HTTPError as exc:
            return CommandResult(False, -1, "", f"モックサーバに接続できません: {exc}")
        if resp.status_code >= 400:
            return CommandResult(False, resp.status_code, "", resp.text[:200])
        return CommandResult(True, 0, f"[mock] {path}", "")

    async def start(self) -> CommandResult:
        return await self._post("start")

    async def stop(self) -> CommandResult:
        return await self._post("stop")

    async def restart(self) -> CommandResult:
        return await self._post("restart")

    async def is_active(self) -> bool | None:
        try:
            client = await self._get_client()
            resp = await client.get(f"{self.control_url}/__mock__/status")
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        try:
            return bool(resp.json().get("running"))
        except ValueError:  # pragma: no cover
            return None


def build_service(
    backend: str,
    *,
    unit: str,
    dry_run: bool,
    mock_control_url: str,
) -> GameService:
    if backend == "mock":
        logger.info("ゲームサーバの制御にモックバックエンドを使います: %s", mock_control_url)
        return MockGameService(mock_control_url)
    return SystemdService(unit, dry_run=dry_run)
