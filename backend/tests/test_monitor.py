"""監視・アラート・ログ配信のテスト。"""

from __future__ import annotations

import asyncio

import pytest

from app.logstream import LogBroker
from app.monitor import Monitor
from app.notify import DiscordNotifier
from app.palapi import PalworldClient


@pytest.fixture
def monitor(pal_client: PalworldClient, notifier: DiscordNotifier) -> Monitor:
    return Monitor(
        pal_client,
        notifier,
        interval=3600.0,
        mem_warn_percent=80.0,
        mem_crit_percent=90.0,
        alert_cooldown_sec=1800.0,
    )


async def test_sample_records_metrics(monitor, mock_state):
    record = await monitor.sample()
    assert record["online"] is True
    assert record["players"] == 3
    assert record["fps"] > 0
    assert len(monitor.history) == 1


async def test_sample_records_offline_without_raising(monitor, mock_state):
    mock_state.fail_all = True
    record = await monitor.sample()
    assert record["online"] is False
    assert monitor.last_error


async def test_memory_warning_is_sent_once_within_cooldown(monitor, notifier, monkeypatch):
    monkeypatch.setattr(
        monitor, "_host_stats", lambda: {
            "cpu_percent": 10.0, "mem_percent": 85.0,
            "mem_used_mb": 8500, "mem_total_mb": 10000,
        }
    )
    await monitor.sample()
    await monitor.sample()

    warns = [n for n in notifier.sent if n["level"] == "warn"]
    assert len(warns) == 1  # cooldown 中は再送しない
    assert "85.0%" in warns[0]["title"]


async def test_memory_critical_uses_crit_level(monitor, notifier, monkeypatch):
    monkeypatch.setattr(
        monitor, "_host_stats", lambda: {
            "cpu_percent": 10.0, "mem_percent": 95.0,
            "mem_used_mb": 9500, "mem_total_mb": 10000,
        }
    )
    await monitor.sample()
    assert any(n["level"] == "crit" for n in notifier.sent)


async def test_recovery_resets_alert_so_next_spike_notifies(monitor, notifier, monkeypatch):
    levels = iter([85.0, 40.0, 85.0])
    monkeypatch.setattr(
        monitor, "_host_stats", lambda: {
            "cpu_percent": 10.0, "mem_percent": next(levels),
            "mem_used_mb": 8500, "mem_total_mb": 10000,
        }
    )
    for _ in range(3):
        await monitor.sample()

    assert len([n for n in notifier.sent if n["level"] == "warn"]) == 2


async def test_server_down_and_up_transitions_notify(monitor, notifier, mock_state):
    await monitor.sample()                      # online
    mock_state.fail_all = True
    await monitor.sample()                      # -> down
    mock_state.fail_all = False
    await monitor.sample()                      # -> up

    titles = [n["title"] for n in notifier.sent]
    assert "サーバ応答なし" in titles
    assert "サーバ復帰" in titles
    # 状態が変わった瞬間だけ通知する
    assert titles.count("サーバ応答なし") == 1


async def test_history_since_filters_old_records(monitor, mock_state):
    await monitor.sample()
    monitor.history[0]["ts"] -= 7200  # 2時間前に偽装
    await monitor.sample()

    assert len(monitor.history_since(3600)) == 1


async def test_notifier_records_without_webhook(notifier):
    ok = await notifier.send("タイトル", "本文", "info")
    assert ok is False          # URL 未設定なので送信しない
    assert notifier.sent[-1]["title"] == "タイトル"


# ---- ログ配信 ------------------------------------------------------------


async def test_broker_delivers_lines_to_subscriber():
    broker = LogBroker()
    received = []

    async def consume():
        async for record in broker.subscribe():
            received.append(record["line"])
            if len(received) == 2:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    broker.publish("line-1")
    broker.publish("line-2")
    await asyncio.wait_for(task, timeout=2.0)

    assert received == ["line-1", "line-2"]


async def test_broker_replays_backlog_to_new_subscriber():
    broker = LogBroker()
    broker.publish("過去ログ")

    agen = broker.subscribe()
    record = await asyncio.wait_for(agen.__anext__(), timeout=2.0)
    await agen.aclose()

    assert record["line"] == "過去ログ"


async def test_broker_drops_oldest_when_subscriber_is_slow():
    """遅い購読者がいても publish 側が詰まらないこと。"""
    broker = LogBroker()
    agen = broker.subscribe()

    # 1行受け取らせて購読キューを登録済みにし、以降は読ませない
    broker.publish("first")
    assert (await asyncio.wait_for(agen.__anext__(), timeout=2.0))["line"] == "first"
    assert broker.subscriber_count == 1

    # キュー上限(500)を大きく超えても publish はブロックも例外もしない
    for i in range(1500):
        broker.publish(f"line-{i}")

    assert len(broker.backlog()) == 200  # backlog の上限まで
    await agen.aclose()
    assert broker.subscriber_count == 0


async def test_logs_endpoint_returns_backlog(client, app):
    app.state.broker.publish("テスト行", source="app")
    body = (await client.get("/api/logs")).json()
    assert body["lines"][-1]["line"] == "テスト行"
