"""起動/停止の配線が、不意の再起動をそうと分かる形で扱えているか (issue #41)。

`start_background=True` のときにしか通らない経路なので、他のテストが使う
app フィクスチャでは触れられない。ここだけ lifespan を直接回す。
"""

from __future__ import annotations

import json
import time

import pytest

from app.main import create_app


@pytest.fixture
def live_app_factory(settings, pal_client, notifier):
    """背景タスク込みで起動する app を、同じ設定で何度でも作れるようにする。"""

    def build():
        return create_app(
            settings, pal_client=pal_client, notifier=notifier, start_background=True
        )

    return build


async def _run_once(build) -> None:
    app = build()
    async with app.router.lifespan_context(app):
        pass


def _messages(notifier, keyword: str) -> list[dict]:
    return [m for m in notifier.sent if keyword in m["title"]]


async def test_a_quick_restart_is_reported_as_such(live_app_factory, notifier):
    """数秒で戻ってきたら、外部要因の可能性があると通知に書くこと。

    書いていないと、受け取った側からは「誰かが管理ツールを触った」
    ようにしか見えない。
    """
    await _run_once(live_app_factory)
    await _run_once(live_app_factory)

    started = _messages(notifier, "管理ツールを起動しました")
    assert len(started) == 2
    # 初回は前回の記録が無いので、余計なことを言わない
    assert "自動再起動の可能性" not in started[0]["description"]
    assert "自動再起動の可能性" in started[1]["description"]
    assert started[1]["level"] == "warn"


async def test_a_normal_first_start_stays_quiet(live_app_factory, notifier):
    await _run_once(live_app_factory)

    started = _messages(notifier, "管理ツールを起動しました")[0]
    assert started["level"] == "info"
    assert started["description"].strip() == "環境: test"


async def test_the_runtime_record_survives_the_process(live_app_factory, settings):
    await _run_once(live_app_factory)

    record = json.loads(settings.runtime_state_store.read_text(encoding="utf-8"))
    assert record["stopped_at"] is not None
    assert record["stopped_at"] <= time.time()


async def test_startup_recovers_an_interrupted_sequence(live_app_factory, settings, notifier):
    """前回の中断が残っていたら、起動時に拾って処理すること。"""
    now = time.time()
    settings.restart_state_store.parent.mkdir(parents=True, exist_ok=True)
    settings.restart_state_store.write_text(
        json.dumps(
            {
                "phase": "restarting",
                "mode": "restart",
                "reason": "定期メンテ",
                "schedule_id": None,
                "started_at": now - 300,
                "finished_at": None,
                "message": "サーバを再起動しています",
                "steps": [{"name": "service_stop", "ok": True, "detail": "", "ts": now - 10}],
                "saved_at": now - 10,
            }
        ),
        encoding="utf-8",
    )

    app = live_app_factory()
    async with app.router.lifespan_context(app):
        # 復旧は背景タスクなので、終わるまで待つ
        for _ in range(400):
            if app.state.restart._load_record() is None:
                break
            await _tick()
        else:  # pragma: no cover - 復旧が走らなかった
            pytest.fail("中断された記録が処理されませんでした")

    assert _messages(notifier, "中断された再起動シーケンスを検知しました")
    # 一度処理したら終端しているので、次の起動では拾われない
    assert app.state.restart._load_record() is None


async def _tick() -> None:
    import asyncio

    await asyncio.sleep(0.01)
