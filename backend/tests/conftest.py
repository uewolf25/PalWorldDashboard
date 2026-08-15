"""テスト共通のフィクスチャ。

ゲームサーバは無いので、モック Palworld API を ASGI 経由で直接叩く。
ネットワークもポートも使わないので CI でもそのまま動く。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.notify import DiscordNotifier
from app.palapi import PalworldClient
from mock import mock_palworld

SAMPLE_INI = """[/Script/Pal.PalGameWorldSettings]
OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,ExpRate=1.000000,ServerName="Test, Server",ServerPlayerMaxNum=32,bIsPvP=False)
"""


@pytest.fixture
def mock_state():
    """モックサーバの状態をテストごとに初期化する。"""
    mock_palworld.STATE.reset(player_count=3)
    return mock_palworld.STATE


@pytest.fixture
async def pal_client(mock_state):
    """モック API に ASGI 直結する Palworld クライアント。"""
    transport = httpx.ASGITransport(app=mock_palworld.app)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="http://mock-palworld",
        auth=("admin", "mockpass"),
    )
    client = PalworldClient("http://mock-palworld", "admin", "mockpass", client=http)
    yield client
    await http.aclose()


@pytest.fixture
def server_stopped(mock_state):
    """ゲームサーバが停止している状態。

    Palworld は停止時にメモリ上の設定で ini を上書きするため、
    設定ファイルの書き換えは停止中しか許可されない。
    """
    mock_state.running = False
    return mock_state


@pytest.fixture
def ini_path(tmp_path: Path) -> Path:
    path = tmp_path / "PalWorldSettings.ini"
    path.write_text(SAMPLE_INI, encoding="utf-8")
    return path


@pytest.fixture
def settings(tmp_path: Path, ini_path: Path) -> Settings:
    return Settings(
        env="test",
        pal_admin_password="mockpass",
        app_password="",
        pal_settings_ini=ini_path,
        backup_dir=tmp_path / "backups",
        schedule_store=tmp_path / "schedules.json",
        announce_store=tmp_path / "announcements.json",
        pending_store=tmp_path / "pending.json",
        presence_store=tmp_path / "presence.json",
        session_secret_file=tmp_path / "session-secret",
        pal_save_dir_raw=str(tmp_path / "SaveGames"),
        world_backup_dir=tmp_path / "world-backups",
        schedule_timezone="Asia/Tokyo",
        # ホストに systemctl があるかどうかでテスト結果が変わらないようにする。
        # 既定の systemd バックエンドのままだと、systemctl のある Linux（CI など）で
        # 実際に `systemctl restart palworld.service` が走って失敗し、
        # 再起動シーケンスが phase=failed になる。macOS では systemctl が無く
        # 自動的に simulated へ落ちるので、この差が手元では出ない。
        pal_service_backend="simulated",
        # テストでは待ち時間を潰す
        restart_notice_offsets="0.06,0.03,0.01",
        restart_shutdown_wait=0,
        restart_debounce_sec=0.0,
        # simulated バックエンドはモックサーバを起こさないので、
        # 既定の180秒だと起動待ちで毎回待たされる
        restart_startup_timeout=0.05,
        monitor_interval=3600.0,
        log_source="none",
    )


@pytest.fixture
def notifier() -> DiscordNotifier:
    # webhook URL 未設定なので送信はされず、.sent にだけ積まれる
    return DiscordNotifier()


@pytest.fixture
def app(settings, pal_client, notifier):
    return create_app(
        settings,
        pal_client=pal_client,
        notifier=notifier,
        start_background=False,
    )


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as c:
        yield c
