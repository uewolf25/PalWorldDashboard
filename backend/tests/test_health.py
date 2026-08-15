"""サーバの生死判定（app/health.py）。

判定を1か所にまとめた狙いは、停止待ち・起動待ち・ini を書いてよいかの
3つが同じ材料と同じ解釈を使うようにすること。ここが崩れると、
「止まったと思って ini を書く」「起動したと思って完了を名乗る」が起きる。
"""

from __future__ import annotations

import pytest

from app.health import ServerHealth


class _Service:
    """is_active だけ答えるサービス。"""

    def __init__(self, active: bool | None) -> None:
        self.active = active

    async def is_active(self) -> bool | None:
        return self.active


async def test_api_reachable_follows_the_server(pal_client, mock_state):
    health = ServerHealth(pal_client, _Service(None))
    assert await health.api_reachable() is True

    mock_state.running = False
    assert await health.api_reachable() is False


async def test_running_trusts_the_api_first(pal_client, mock_state):
    """REST API に届けば、プロセス側が何と言おうと動いている。"""
    health = ServerHealth(pal_client, _Service(False))
    assert await health.running() is True


async def test_running_falls_back_to_the_process(pal_client, mock_state):
    """REST API を無効にした構成では、プロセス側しか手がかりが無い。"""
    mock_state.running = False
    assert await ServerHealth(pal_client, _Service(True)).running() is True


async def test_running_treats_unknown_as_stopped(pal_client, mock_state):
    """判定できない（LinuxGSM は None を返す）なら停止扱い。

    ここを「動いている」に倒すと、停止中にしか許していない ini の書き込みが
    永久にできなくなる。
    """
    mock_state.running = False
    assert await ServerHealth(pal_client, _Service(None)).running() is False


async def test_wait_until_down_reports_how_long_it_took(pal_client, mock_state):
    health = ServerHealth(pal_client, _Service(None), poll_interval=0.01)
    mock_state.running = False
    elapsed = await health.wait_until_down(1.0)
    assert elapsed is not None and elapsed < 1.0


async def test_wait_until_down_gives_up(pal_client, mock_state):
    """落ちないまま制限時間を過ぎたら None。呼び出し側が先へ進めるように。"""
    health = ServerHealth(pal_client, _Service(None), poll_interval=0.01)
    assert await health.wait_until_down(0.05) is None


async def test_wait_until_up_waits_for_the_api(pal_client, mock_state):
    """起動コマンドが通っただけでは「上がった」と言えない。"""
    import asyncio

    mock_state.running = False
    health = ServerHealth(pal_client, _Service(None), poll_interval=0.01)

    async def boot_later() -> None:
        await asyncio.sleep(0.05)
        mock_state.running = True

    asyncio.get_running_loop().create_task(boot_later())
    elapsed = await health.wait_until_up(2.0)
    assert elapsed is not None and elapsed >= 0.05


async def test_wait_until_up_gives_up(pal_client, mock_state):
    mock_state.running = False
    health = ServerHealth(pal_client, _Service(None), poll_interval=0.01)
    assert await health.wait_until_up(0.05) is None


@pytest.mark.parametrize("timeout", [0.0, -1.0])
async def test_zero_timeout_does_not_wait(pal_client, mock_state, timeout):
    health = ServerHealth(pal_client, _Service(None))
    assert await health.wait_until_up(timeout) is None
    assert await health.wait_until_down(timeout) is None
