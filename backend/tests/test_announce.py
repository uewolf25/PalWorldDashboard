"""アナウンス履歴とサービス操作のテスト。"""

from __future__ import annotations

import json

import pytest


async def test_manual_announce_is_recorded(client, mock_state):
    resp = await client.post("/api/announce", json={"message": "メンテナンスのお知らせ"})
    assert resp.status_code == 200
    assert mock_state.announcements == ["メンテナンスのお知らせ"]

    records = (await client.get("/api/announcements")).json()["records"]
    assert len(records) == 1
    assert records[0]["message"] == "メンテナンスのお知らせ"
    assert records[0]["source"] == "manual"
    assert records[0]["source_label"] == "手動"
    assert records[0]["to_game"] is True
    assert records[0]["ok"] is True


async def test_announce_can_also_go_to_discord(client, notifier, mock_state):
    await client.post("/api/announce", json={"message": "全体連絡", "to_discord": True})

    assert mock_state.announcements == ["全体連絡"]
    assert any(n["description"] == "全体連絡" for n in notifier.sent)


async def test_announce_defaults_to_game_only(client, notifier, mock_state):
    await client.post("/api/announce", json={"message": "ゲーム内だけ"})
    assert notifier.sent == []


async def test_history_is_newest_first(client):
    for i in range(3):
        await client.post("/api/announce", json={"message": f"{i}番目"})

    records = (await client.get("/api/announcements")).json()["records"]
    assert [r["message"] for r in records] == ["2番目", "1番目", "0番目"]


async def test_history_records_failed_sends(client, mock_state):
    """送信に失敗したアナウンスも履歴に残す。"""
    mock_state.fail_all = True
    resp = await client.post("/api/announce", json={"message": "届かない放送"})
    assert resp.status_code == 502

    records = (await client.get("/api/announcements")).json()["records"]
    assert len(records) == 1
    assert records[0]["ok"] is False
    assert records[0]["detail"]


async def test_restart_announcements_are_recorded_with_source(client, app, mock_state):
    await client.post(
        "/api/restart",
        json={"announce_message": "{time}後に再起動", "notice_offsets": [0.03, 0.01]},
    )
    await app.state.restart.wait()

    records = (await client.get("/api/announcements?source=restart")).json()["records"]
    messages = [r["message"] for r in records]
    # ゲーム内予告2件 + Discord への開始/完了2件
    assert any("後に再起動" in m for m in messages)
    assert any(r["to_game"] is False and r["to_discord"] is False for r in records)
    assert all(r["source"] == "restart" for r in records)


async def test_history_can_be_filtered_by_source(client, app):
    await client.post("/api/announce", json={"message": "手動のもの"})
    await client.post(
        "/api/restart",
        json={"announce_message": "再起動のもの", "notice_offsets": [0.01]},
    )
    await app.state.restart.wait()

    manual = (await client.get("/api/announcements?source=manual")).json()["records"]
    assert len(manual) == 1
    assert manual[0]["message"] == "手動のもの"


async def test_history_limit_is_respected(client):
    for i in range(5):
        await client.post("/api/announce", json={"message": f"m{i}"})

    records = (await client.get("/api/announcements?limit=2")).json()
    assert len(records["records"]) == 2
    assert records["total"] == 5


async def test_history_is_persisted(client, settings):
    await client.post("/api/announce", json={"message": "永続化テスト"})

    stored = json.loads(settings.announce_store.read_text())
    assert stored[0]["message"] == "永続化テスト"


async def test_history_survives_restart(client, settings, pal_client, notifier):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    await client.post("/api/announce", json={"message": "再起動をまたぐ"})

    fresh = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=fresh), base_url="http://manager") as c:
        records = (await c.get("/api/announcements")).json()["records"]
    assert records[0]["message"] == "再起動をまたぐ"


async def test_history_can_be_cleared(client):
    await client.post("/api/announce", json={"message": "消される"})
    resp = await client.delete("/api/announcements")
    assert resp.json()["cleared"] == 1
    assert (await client.get("/api/announcements")).json()["records"] == []


async def test_history_is_capped_at_limit(settings, pal_client, notifier):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    settings.announce_history_limit = 3
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        for i in range(6):
            await c.post("/api/announce", json={"message": f"m{i}"})
        records = (await c.get("/api/announcements")).json()
    assert len(records["records"]) == 3
    assert records["records"][0]["message"] == "m5"


# ---- サービス操作 ----------------------------------------------------------


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
async def test_service_actions(client, action):
    resp = await client.post(f"/api/service/{action}", json={"reason": "動作確認"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # macOS には systemctl が無いのでシミュレート実行になる
    assert body["simulated"] is True


async def test_service_action_is_recorded(client):
    await client.post("/api/service/start", json={"reason": "朝の起動"})
    records = (await client.get("/api/announcements?source=system")).json()["records"]
    assert any("起動しました" in r["message"] for r in records)
    assert all(r["to_game"] is False for r in records)


async def test_service_action_records_the_real_command(settings, pal_client, notifier, tmp_path):
    """実際に走ったコマンドを残す。systemd 前提の決め打ちだと構成次第で嘘になる。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    script = tmp_path / "pwserver"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    settings.pal_service_backend = "lgsm"
    settings.pal_service_command = str(script)

    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        assert (await c.post("/api/service/start", json={"reason": "朝の起動"})).status_code == 200
        records = (await c.get("/api/announcements?source=system")).json()["records"]

    assert any(f"{script} start" in r["message"] for r in records)
    assert not any("systemctl" in r["message"] for r in records)


async def test_unknown_service_action_is_rejected(client):
    assert (await client.post("/api/service/reload", json={})).status_code == 400
