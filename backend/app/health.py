"""ゲームサーバが生きているかの判定。

同じことを知りたい場所が3つある（設定ファイルを書いてよいか、落ちきったか、
起動しきったか）。判定の材料と、その材料をどう解釈するかは同じなので、
ばらばらに書かずここへ寄せる。

材料は2つ。

- **Palworld REST API に届くか。** 届けば確実に動いている。落ちる過程では
  プロセスが残っていても先に応答が止まるので、「プレイヤーが遊べるか」に
  一番近いのはこちら
- **プロセス制御バックエンドの is_active。** REST API を無効にした構成では
  こちらしか手がかりが無い。ただし LinuxGSM のように答えられない
  バックエンドもある（None が返る）

用途で使い分けること。

- `api_reachable()` … 応答しているか。停止/起動を待つ判定に使う
- `running()`       … 動いていないと**言い切ってよいか**。ini の書き込み可否など、
                      間違えると壊れる判断に使う（判定できなければ動いている扱い）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from .palapi import PalApiError, PalworldClient

logger = logging.getLogger(__name__)


class _Service(Protocol):
    async def is_active(self) -> bool | None: ...


class ServerHealth:
    def __init__(
        self,
        pal: PalworldClient,
        service: _Service,
        *,
        poll_interval: float = 1.0,
    ) -> None:
        self._pal = pal
        self._service = service
        self.poll_interval = poll_interval

    async def api_reachable(self) -> bool:
        """REST API が応答するか。"""
        try:
            await self._pal.info()
        except PalApiError:
            return False
        return True

    async def running(self) -> bool:
        """ゲームサーバが動いているか。

        REST API に届けば確実に動いている。届かない場合でもプロセス側が
        active と言うなら動いているとみなす。どちらでも判定できないときは
        「止まっている」として返す（保存を過剰に止めないため）。
        """
        if await self.api_reachable():
            return True
        return bool(await self._service.is_active())

    async def wait_until_down(self, timeout: float) -> float | None:
        """応答が止まるまで待つ。止まったらそこまでの秒数、駄目なら None。"""
        return await self._poll(timeout, target=False)

    async def wait_until_up(self, timeout: float) -> float | None:
        """応答が返るようになるまで待つ。返ったらそこまでの秒数、駄目なら None。

        起動コマンドはプロセスを起こした時点で返るが、実機の Palworld が
        接続を受け付けるまでは数十秒かかる。「コマンドが通った」と
        「遊べるようになった」は別物なので、後者はここで確かめる。
        """
        return await self._poll(timeout, target=True)

    async def _poll(self, timeout: float, *, target: bool) -> float | None:
        if timeout <= 0:
            return None
        loop = asyncio.get_running_loop()
        began = loop.time()
        end = began + timeout
        while True:
            if await self.api_reachable() is target:
                return loop.time() - began
            remaining = end - loop.time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(self.poll_interval, remaining))
