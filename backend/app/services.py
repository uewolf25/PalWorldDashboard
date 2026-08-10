"""ゲームサーバの systemd ユニット操作。

Linux 以外（開発用の macOS など）や dry_run 時は実行せず、
「実行したことにして」結果を返す。実機との差分はここに閉じ込める。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass

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


class SystemdService:
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

    async def is_active(self) -> bool:
        result = await self._run("is-active", self.unit)
        if result.simulated:
            return True
        return result.stdout.strip() == "active"
