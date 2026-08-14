"""ゲームサーバのプロセス制御と、稼働状態の判定のテスト。

開発機には systemctl が無いため、モックサーバ自体を起動/停止する
バックエンドを用意している。これが無いと「停止」しても停止扱いにならず、
設定ファイルの編集（停止中のみ許可）が一度も試せない。
"""

from __future__ import annotations

import httpx
import pytest

from app.services import (
    LgsmService,
    MockGameService,
    SimulatedService,
    SystemdService,
    build_service,
)
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
    ("lgsm", LgsmService),
    ("", SystemdService),          # 未指定は systemd
])
def test_build_service_selects_backend(backend, expected):
    service = build_service(
        backend, unit="x.service", dry_run=False, mock_control_url="http://h",
        command="/home/mntuser/pwserver",
    )
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


# ---- 事前チェック（issue #28） ---------------------------------------------


class _FakeProc:
    """`create_subprocess_exec` の代役。

    実装は標準出力を一時ファイルに受けてプロセスの終了だけを待つ
    （パイプの EOF を待つと常駐プロセスに握られて戻らない: issue #34）。
    代役もその形に合わせ、渡されたファイルに書いてから wait() で返す。
    """

    def __init__(self, returncode: int, out_text: str, err_text: str, **kwargs) -> None:
        self.returncode = returncode
        for handle, text in ((kwargs.get("stdout"), out_text), (kwargs.get("stderr"), err_text)):
            if hasattr(handle, "write"):
                handle.write(text.encode())

    async def wait(self):
        return self.returncode

    def kill(self):  # pragma: no cover - タイムアウト経路でしか呼ばれない
        pass


@pytest.fixture
def fake_systemctl(monkeypatch):
    """systemctl の実行を差し替える。ホストに systemctl が無くても通す。"""
    import asyncio

    from app import services

    monkeypatch.setattr(services.shutil, "which", lambda name: f"/usr/bin/{name}")

    def install(
        returncode: int, stdout: str = "", stderr: str = "", *, load_state: str = "loaded"
    ):
        async def fake_exec(*args, **kwargs):
            # ユニットの存在確認は別問い合わせなので、別の応答を返す
            if "show" in args:
                return _FakeProc(0, f"LoadState={load_state}", "", **kwargs)
            return _FakeProc(returncode, stdout, stderr, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    return install


async def test_preflight_passes_when_the_unit_is_merely_stopped(fake_systemctl):
    """`is-active` は停止中に非ゼロを返す。それは操作できない理由ではない。"""
    fake_systemctl(3, stdout="inactive")
    service = SystemdService("palworld.service", use_sudo=True)

    result = await service.preflight()
    assert result.ok is True


async def test_preflight_fails_when_sudo_cannot_escalate(fake_systemctl):
    """issue #28 の本体: NoNewPrivileges 下では sudo が昇格できない。

    ここで弾けないと、ゲームサーバを落としてから気づくことになる。
    """
    fake_systemctl(
        1,
        stderr='sudo: The "no new privileges" flag is set, '
               "which prevents sudo from running as root.",
    )
    service = SystemdService("palworld.service", use_sudo=True)

    result = await service.preflight()
    assert result.ok is False
    assert "no new privileges" in result.stderr


async def test_preflight_passes_while_the_unit_is_running(fake_systemctl):
    fake_systemctl(0, stdout="active")
    service = SystemdService("palworld.service", use_sudo=True)

    assert (await service.preflight()).ok is True


async def test_preflight_fails_when_the_unit_does_not_exist(fake_systemctl):
    """`is-active` は存在しないユニットにも inactive を返す。それだけでは見抜けない。

    実機では PAL_SERVICE_NAME=palworld.service なのにユニットが無く、
    ここを通してしまうとサーバを落としてから起動できないことに気づく。
    """
    fake_systemctl(3, stdout="inactive", load_state="not-found")
    service = SystemdService("palworld.service", use_sudo=True)

    result = await service.preflight()
    assert result.ok is False
    assert "not-found" in result.stderr
    assert "PAL_SERVICE_NAME" in result.stderr


async def test_the_existence_check_does_not_need_sudo(fake_systemctl):
    """sudoers に show を足さずに済ませる（デプロイ手順を増やさない）。"""
    service = SystemdService("palworld.service", use_sudo=True)

    assert service._command(("show", "x", "--property=LoadState"), privileged=False) == [
        "systemctl", "show", "x", "--property=LoadState",
    ]
    # 操作系は今までどおり sudo を通す
    assert service._command(("stop", "x"))[:2] == ["sudo", "-n"]


async def test_preflight_is_simulated_without_systemctl():
    """systemctl が無い環境では、事前チェックがシーケンスを止めないこと。"""
    service = SystemdService("palworld.service", dry_run=True, use_sudo=True)
    result = await service.preflight()
    assert result.ok is True and result.simulated is True


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


async def test_a_failed_systemctl_is_logged(fake_systemctl, caplog):
    """失敗した systemctl が唯一残る場所。画面の 500 だけでは後から追えない。"""
    import logging

    fake_systemctl(1, stderr='sudo: The "no new privileges" flag is set, ...')
    service = SystemdService("palworld.service", use_sudo=True)

    with caplog.at_level(logging.WARNING, logger="app.services"):
        result = await service.stop()

    assert result.ok is False
    assert "sudo -n systemctl stop palworld.service" in caplog.text
    assert "no new privileges" in caplog.text


async def test_polling_is_active_does_not_flood_the_log(fake_systemctl, caplog):
    """is-active は画面から繰り返し呼ばれる。成功時に INFO を出すと journald が埋まる。"""
    import logging

    fake_systemctl(0, stdout="active")
    service = SystemdService("palworld.service", use_sudo=True)

    with caplog.at_level(logging.INFO, logger="app.services"):
        await service.is_active()

    assert caplog.text == ""


# ---- LinuxGSM バックエンド --------------------------------------------------


@pytest.fixture
def lgsm_script(tmp_path):
    """実行可能な LinuxGSM 管理スクリプトの代役。"""
    path = tmp_path / "pwserver"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


async def test_lgsm_runs_the_management_script(lgsm_script):
    from app.services import LgsmService

    service = LgsmService(str(lgsm_script))
    for action in (service.start, service.stop, service.restart):
        assert (await action()).ok is True


async def test_lgsm_needs_no_privilege_escalation(lgsm_script, monkeypatch):
    """issue #28 の原因クラスを消すのが狙い。sudo を挟まないこと。"""
    import asyncio

    seen: list[tuple] = []

    async def spy(*args, **kwargs):
        seen.append(args)
        return _FakeProc(0, "", "", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    from app.services import LgsmService

    await LgsmService(str(lgsm_script)).stop()
    assert seen == [(str(lgsm_script), "stop")]
    assert "sudo" not in " ".join(seen[0])


async def test_lgsm_preflight_fails_when_the_script_is_missing(tmp_path):
    from app.services import LgsmService

    service = LgsmService(str(tmp_path / "nope"))
    result = await service.preflight()
    assert result.ok is False
    assert "PAL_SERVICE_COMMAND" in result.stderr


async def test_lgsm_preflight_fails_without_execute_permission(lgsm_script):
    """管理ツールと別ユーザで置かれていると、落としてから起動できなくなる。"""
    from app.services import LgsmService

    lgsm_script.chmod(0o644)
    result = await LgsmService(str(lgsm_script)).preflight()
    assert result.ok is False
    assert "実行権限" in result.stderr


async def test_lgsm_preflight_does_not_touch_the_server(lgsm_script, monkeypatch):
    """LinuxGSM の monitor は副作用がある。事前チェックで叩いてはいけない。"""
    import asyncio

    async def boom(*args, **kwargs):  # pragma: no cover - 呼ばれたら失敗
        raise AssertionError("preflight でスクリプトを実行してはいけない")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

    from app.services import LgsmService

    assert (await LgsmService(str(lgsm_script)).preflight()).ok is True


async def test_lgsm_strips_terminal_colours(lgsm_script, monkeypatch):
    """LinuxGSM は端末向けに色を付ける。そのままログに残すと読めない。"""
    import asyncio

    async def coloured(*args, **kwargs):
        return _FakeProc(1, "\x1b[0;31mFAIL\x1b[0m Starting pwserver", "", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", coloured)

    from app.services import LgsmService

    result = await LgsmService(str(lgsm_script)).start()
    assert result.ok is False
    assert "\x1b" not in result.stdout
    # 失敗の理由が stdout にしか無くても拾う
    assert "FAIL Starting pwserver" in result.stderr


async def test_lgsm_state_is_left_to_the_rest_api(lgsm_script):
    """details の書式は版で変わる。稼働判定は REST API に委ねる。"""
    from app.services import LgsmService

    assert await LgsmService(str(lgsm_script)).is_active() is None


async def test_an_unknown_backend_is_reported(caplog):
    """切り戻しで踏みやすい。古い版に lgsm を渡すと黙って systemd に落ちる。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="app.services"):
        service = build_service(
            "systemdd", unit="x.service", dry_run=False, mock_control_url="http://h"
        )

    assert isinstance(service, SystemdService)
    assert "知らない値" in caplog.text


async def test_the_default_backend_is_not_reported(caplog):
    """未指定は systemd が正しい既定なので、警告を出さない。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="app.services"):
        build_service("", unit="x.service", dry_run=False, mock_control_url="http://h")

    assert caplog.text == ""


# ---- 常駐プロセスに握られても戻ること（issue #34） -------------------------


@pytest.fixture
def daemonising_script(tmp_path):
    """自分は終わるが、子を常駐させて標準出力を握らせるスクリプト。

    LinuxGSM の `start` そのもの。ゲームは tmux の中に残り、管理スクリプトは
    先に終了する。本番ではこれで `pwserver start` が 300 秒返らなくなった。
    """
    path = tmp_path / "pwserver"
    path.write_text(
        "#!/bin/sh\n"
        "echo \"[ OK ] Starting pwserver\"\n"
        # 標準出力を継いだまま残る子。パイプで受けていると EOF が来ない
        "sleep 30 &\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


async def test_lgsm_start_returns_when_the_script_exits(daemonising_script):
    """常駐した子が標準出力を握っていても、スクリプトの終了で先へ進むこと。"""
    import time

    from app.services import LgsmService

    service = LgsmService(str(daemonising_script), timeout=10.0)
    began = time.monotonic()
    result = await service.start()
    elapsed = time.monotonic() - began

    assert result.ok is True, result.stderr
    assert "Starting pwserver" in result.stdout
    # 待つのはプロセスの終了だけ。常駐した子（30秒）には付き合わない
    assert elapsed < 5.0, f"{elapsed:.1f}秒かかった（常駐プロセス待ちに戻っている）"


async def test_systemctl_returns_when_the_command_exits(tmp_path, monkeypatch):
    """systemd 経路も同じ。ユニット起動が常駐しても待たされないこと。"""
    import time

    from app import services

    script = tmp_path / "systemctl"
    script.write_text("#!/bin/sh\necho done\nsleep 30 &\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(services.shutil, "which", lambda name: str(script))
    monkeypatch.setattr(
        services.SystemdService, "_command", lambda self, args, privileged=True: [str(script), *args]
    )

    service = services.SystemdService("palworld.service", timeout=10.0)
    began = time.monotonic()
    result = await service.start()

    assert result.ok is True
    assert time.monotonic() - began < 5.0


async def test_command_that_never_exits_still_times_out(tmp_path):
    """本当に返らないコマンドは、これまでどおりタイムアウトで打ち切ること。"""
    from app.services import LgsmService

    script = tmp_path / "pwserver"
    script.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    script.chmod(0o755)

    result = await LgsmService(str(script), timeout=0.5).start()
    assert result.ok is False
    assert "タイムアウト" in result.stderr


async def test_backends_report_what_they_actually_run():
    """通知やログに出す名前は実体に合わせる（issue #34 の切り分けで混乱した）。"""
    from app.services import LgsmService, SimulatedService, SystemdService

    assert LgsmService("/home/mntuser/pwserver").label == "pwserver"
    assert SystemdService("palworld.service").label == "systemctl"
    assert "simulated" in SimulatedService().label
