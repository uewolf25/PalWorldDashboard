"""ゲームサーバのプロセス制御と、稼働状態の判定のテスト。

開発機には systemctl が無いため、モックサーバ自体を起動/停止する
バックエンドを用意している。これが無いと「停止」しても停止扱いにならず、
設定ファイルの編集（停止中のみ許可）が一度も試せない。
"""

from __future__ import annotations

import httpx
import pytest

from app.services import MockGameService, SimulatedService, SystemdService, build_service
from mock import mock_palworld


@pytest.fixture
async def mock_service(mock_state):
    """モックの制御エンドポイントに ASGI 直結するサービス。"""
    transport = httpx.ASGITransport(app=mock_palworld.app)
    http = httpx.AsyncClient(transport=transport, base_url="http://mock-palworld")
    service = MockGameService("http://mock-palworld", client=http)
    yield service
    await http.aclose()


# ---- モックの稼働状態 ------------------------------------------------------


async def test_mock_starts_running(mock_state, pal_client):
    assert mock_state.running is True
    assert (await pal_client.info())["servername"]


async def test_stopped_mock_refuses_api_calls(mock_state, pal_client):
    from app.palapi import PalApiError

    mock_state.running = False
    with pytest.raises(PalApiError):
        await pal_client.info()
    with pytest.raises(PalApiError):
        await pal_client.metrics()


async def test_shutdown_api_stops_the_server(mock_state, pal_client):
    """実機と同じく、shutdown API はプロセスを落とす。"""
    await pal_client.shutdown(waittime=0, message="test")
    assert mock_state.running is False


async def test_stop_api_stops_the_server(mock_state, pal_client):
    await pal_client.stop()
    assert mock_state.running is False


# ---- MockGameService -------------------------------------------------------


async def test_service_stops_and_starts_the_mock(mock_service, mock_state):
    assert await mock_service.is_active() is True

    result = await mock_service.stop()
    assert result.ok is True
    assert mock_state.running is False
    assert await mock_service.is_active() is False

    result = await mock_service.start()
    assert result.ok is True
    assert mock_state.running is True
    assert await mock_service.is_active() is True


async def test_service_restart_brings_a_stopped_server_back(mock_service, mock_state):
    mock_state.running = False
    assert (await mock_service.restart()).ok is True
    assert mock_state.running is True
    assert mock_state.players  # 起動時にプレイヤーが戻る


async def test_control_endpoints_work_while_stopped(mock_service, mock_state):
    """停止中でも制御はできること（systemd がゲームサーバと独立なのと同じ）。"""
    mock_state.running = False
    assert await mock_service.is_active() is False
    assert (await mock_service.start()).ok is True


async def test_service_reports_unknown_when_unreachable():
    """モックに繋がらないときは「分からない」を返す（稼働中と誤判定しない）。"""
    service = MockGameService("http://127.0.0.1:9", timeout=0.2)
    try:
        assert await service.is_active() is None
        assert (await service.stop()).ok is False
    finally:
        await service.aclose()


# ---- systemd バックエンド ---------------------------------------------------


async def test_systemd_is_simulated_without_systemctl():
    service = SystemdService("palworld.service", dry_run=True)
    result = await service.restart()
    assert result.ok is True and result.simulated is True
    # 実行できていないので稼働状態は「分からない」
    assert await service.is_active() is None


@pytest.mark.parametrize("backend,expected", [
    ("mock", MockGameService),
    ("simulated", SimulatedService),
    ("systemd", SystemdService),
    ("", SystemdService),          # 未指定は systemd
])
def test_build_service_selects_backend(backend, expected):
    service = build_service(backend, unit="x.service", dry_run=False, mock_control_url="http://h")
    assert isinstance(service, expected)


async def test_simulated_service_never_touches_the_host():
    """ホストに systemctl があってもなくても同じ結果になること。

    これが無いと、systemctl のある Linux（CI）と無い macOS で
    再起動シーケンスのテスト結果が変わる。
    """
    service = SimulatedService("palworld.service")
    for action in (service.start, service.stop, service.restart):
        result = await action()
        assert result.ok is True
        assert result.simulated is True
    assert await service.is_active() is None


# ---- 停止シーケンス → 設定編集 という一連の流れ -----------------------------


@pytest.fixture
async def dev_client(settings, pal_client, notifier, mock_state):
    """開発と同じ構成（モックバックエンド）のアプリ。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    settings.pal_service_backend = "mock"
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    # 制御エンドポイントも ASGI 直結にして、ポートを使わない
    transport = httpx.ASGITransport(app=mock_palworld.app)
    http = AsyncClient(transport=transport, base_url="http://mock-palworld")
    app.state.service._client = http
    app.state.service._owns_client = False
    app.state.service.control_url = "http://mock-palworld"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        yield c
    await http.aclose()


async def test_stop_sequence_actually_stops_the_server(dev_client, mock_state, ini_path):
    """停止シーケンスの完了後、設定を保存できる状態になること。

    これができないと、開発環境で設定編集を一度も試せない。
    """
    # 稼働中は保存できない
    assert (await dev_client.put(
        "/api/settings-ini/fields", json={"values": {"ExpRate": 2.0}}
    )).status_code == 409

    resp = await dev_client.post(
        "/api/shutdown",
        json={"announce_message": "{time}後に停止します", "notice_offsets": [0.01]},
    )
    assert resp.status_code == 200
    await dev_client._transport.app.state.restart.wait()

    assert mock_state.running is False
    body = (await dev_client.get("/api/settings-ini/fields")).json()
    assert body["server_running"] is False

    # 停止したので保存できる
    resp = await dev_client.put("/api/settings-ini/fields", json={"values": {"ExpRate": 2.0}})
    assert resp.status_code == 200
    assert "ExpRate=2.000000" in ini_path.read_text()


async def test_start_brings_the_server_back(dev_client, mock_state):
    mock_state.running = False
    resp = await dev_client.post("/api/service/start", json={"reason": "テスト"})
    assert resp.status_code == 200
    assert mock_state.running is True
    assert (await dev_client.get("/api/settings-ini/fields")).json()["server_running"] is True
