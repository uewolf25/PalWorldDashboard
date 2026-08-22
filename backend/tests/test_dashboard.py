"""ダッシュボード・プレイヤー管理まわりの API テスト。"""

from __future__ import annotations

import pytest


async def test_status_returns_metrics_when_online(client, mock_state):
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["online"] is True
    assert body["error"] is None
    assert body["metrics"]["currentplayernum"] == 3
    assert body["metrics"]["serverfps"] > 0
    assert body["info"]["servername"] == "Mock Palworld Server"
    # ホスト側のリソースも一緒に返す
    assert 0 <= body["host"]["mem_percent"] <= 100


async def test_status_stays_200_when_server_is_down(client, mock_state):
    """ゲームサーバが落ちてもダッシュボードは描画できること。"""
    mock_state.fail_all = True
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["online"] is False
    assert body["error"]


async def test_players_list(client, mock_state):
    resp = await client.get("/api/players")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    first = body["players"][0]
    for key in ("name", "userId", "ping", "level", "location_x", "building_count"):
        assert key in first


async def test_kick_removes_player_from_server(client, mock_state):
    target = mock_state.players[0]["userId"]
    resp = await client.post(
        "/api/players/kick", json={"userid": target, "message": "AFKのため"}
    )
    assert resp.status_code == 200
    assert mock_state.kicked == [{"userid": target, "message": "AFKのため"}]
    assert all(p["userId"] != target for p in mock_state.players)


async def test_ban_then_unban(client, mock_state):
    target = mock_state.players[1]["userId"]
    assert (await client.post("/api/players/ban", json={"userid": target, "message": "規約違反"})).status_code == 200
    assert target in mock_state.banned

    assert (await client.post("/api/players/unban", json={"userid": target})).status_code == 200
    assert target not in mock_state.banned
    assert mock_state.unbans == [target]


async def test_kick_unknown_player_returns_502(client, mock_state):
    """ゲームサーバ側のエラーは 502 に変換して UI に見せる。"""
    resp = await client.post("/api/players/kick", json={"userid": "steam_does_not_exist"})
    assert resp.status_code == 502
    assert "detail" in resp.json()


async def test_kick_is_skipped_in_dry_run(settings, pal_client, notifier, mock_state):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    settings.dry_run = True
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        target = mock_state.players[0]["userId"]
        resp = await c.post("/api/players/kick", json={"userid": target})
    assert resp.json()["result"] == "skipped"
    assert mock_state.kicked == []
    assert len(mock_state.players) == 3


async def test_announce_reaches_game_server(client, mock_state):
    resp = await client.post("/api/announce", json={"message": "メンテナンスのお知らせ"})
    assert resp.status_code == 200
    assert mock_state.announcements == ["メンテナンスのお知らせ"]


async def test_announce_rejects_empty_message(client):
    resp = await client.post("/api/announce", json={"message": ""})
    assert resp.status_code == 422


async def test_save_world(client, mock_state):
    assert (await client.post("/api/save")).status_code == 200
    assert mock_state.saves == 1


async def test_world_map_and_anomalies(client, mock_state):
    mock_state.players[0]["ping"] = 400.0
    mock_state.players[1]["building_count"] = 900

    resp = await client.get("/api/world")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    assert all(p["x"] is not None and p["y"] is not None for p in body["points"])

    kinds = {a["type"] for a in body["anomalies"]}
    assert kinds == {"high_ping", "many_buildings"}


async def test_history_records_samples(client, mock_state):
    await client.post("/api/sample")
    await client.post("/api/sample")
    resp = await client.get("/api/history?minutes=60")
    assert resp.status_code == 200
    records = resp.json()["records"]
    assert len(records) == 2
    assert records[0]["online"] is True
    assert records[0]["players"] == 3


async def test_config_masks_secrets(settings, pal_client, notifier):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    settings.discord_webhook_url = "https://discord.com/api/webhooks/1234567890/abcdefghijklmnop"
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        body = (await c.get("/api/config")).json()

    assert "abcdefghijklmnop" not in body["discord_webhook_url"]
    assert "*" in body["discord_webhook_url"]
    assert "mockpass" not in body["pal_admin_password"]


async def test_config_exposes_version(settings, pal_client, notifier):
    """実機に入っている版を画面から見えるようにする。

    出どころは app/__init__.py の1か所だけ。ここが OpenAPI と /api/config の
    両方に出るので、片方だけ古い番号が残ることが無いようにしておく。
    """
    from httpx import ASGITransport, AsyncClient

    from app import __version__
    from app.main import create_app

    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        body = (await c.get("/api/config")).json()

    assert body["version"] == __version__
    assert app.version == __version__


@pytest.mark.parametrize("path", ["/api/status", "/api/players", "/api/config"])
async def test_viewing_stays_open_when_password_set(settings, pal_client, notifier, path):
    """パスワードを設定しても閲覧は誰でもできる（Issue #15 の追加実装）。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    settings.app_password = "s3cret"
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        assert (await c.get(path)).status_code == 200


async def test_basic_auth_is_enforced_for_operations(settings, pal_client, notifier):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    settings.app_password = "s3cret"
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    transport = ASGITransport(app=app)
    body = {"message": "操作にはログインが要る"}

    async with AsyncClient(transport=transport, base_url="http://manager") as c:
        assert (await c.post("/api/announce", json=body)).status_code == 401

    async with AsyncClient(
        transport=transport, base_url="http://manager", auth=("admin", "s3cret")
    ) as c:
        assert (await c.post("/api/announce", json=body)).status_code == 200
