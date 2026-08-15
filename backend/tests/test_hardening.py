"""実機投入前に潰したリスク（Issue #5 セクション1）の検証。

いずれもモック相手では踏めなかった、実機で初めて壊れる類のもの。
コードレビューで見つけた分を、ここで固定しておく。
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest

from app.cache import TTLCache
from app.monitor import Monitor
from app.palapi import PalApiError, PalworldClient
from app.services import SystemdService


# ---- R-04 タイムアウトの分離 ----------------------------------------------


def test_slow_operations_use_the_longer_timeout():
    """保存や停止は数十秒かかることがあるので、参照系と分ける。"""
    client = PalworldClient("http://x", "admin", "p", timeout=5.0, slow_timeout=120.0)

    assert client._timeout_for("save") == 120.0
    assert client._timeout_for("shutdown") == 120.0
    assert client._timeout_for("stop") == 120.0
    # ダッシュボードが 1 秒ごとに叩く側は短いまま
    assert client._timeout_for("info") == 5.0
    assert client._timeout_for("metrics") == 5.0
    assert client._timeout_for("players") == 5.0


def test_slow_timeout_never_shorter_than_the_normal_one():
    client = PalworldClient("http://x", "admin", "p", timeout=30.0, slow_timeout=5.0)
    assert client._timeout_for("save") == 30.0


async def test_timeout_is_reported_as_such_not_as_a_failure():
    """タイムアウトは「保存が失敗した」ことの証明ではない。"""

    async def always_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(always_timeout), base_url="http://x")
    client = PalworldClient("http://x", "admin", "p", slow_timeout=42.0, client=http)
    try:
        with pytest.raises(PalApiError) as exc:
            await client.save()
    finally:
        await http.aclose()

    assert exc.value.timed_out is True
    assert "42 秒以内に応答しませんでした" in str(exc.value)
    assert "続いている可能性" in str(exc.value)


async def test_connection_error_is_not_marked_as_timeout():
    async def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(refused), base_url="http://x")
    client = PalworldClient("http://x", "admin", "p", client=http)
    try:
        with pytest.raises(PalApiError) as exc:
            await client.info()
    finally:
        await http.aclose()
    assert exc.value.timed_out is False


async def test_save_timeout_aborts_with_an_honest_message(settings, pal_client, notifier, mock_state):
    """保存が確認できない以上は中止するが、文面は断定しない。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    async def timing_out_save():
        raise PalApiError("応答なし", timed_out=True)

    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    app.state.pal.save = timing_out_save

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        await c.post("/api/restart", json={
            "announce_message": "{time}後に再起動します", "notice_offsets": [0.01],
        })
        await app.state.restart.wait()

    status = app.state.restart.status
    assert status.phase == "failed"
    assert "確認できなかった" in status.message      # 「失敗した」とは言わない
    assert mock_state.shutdowns == []                # 停止処理には入らない


# ---- R-05 停止待ち ---------------------------------------------------------


async def test_wait_until_down_returns_as_soon_as_the_server_goes_quiet(
    settings, pal_client, notifier, mock_state
):
    """固定 sleep をやめ、落ちた時点で先へ進む。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    settings.restart_shutdown_wait = 0
    settings.restart_shutdown_grace = 5.0
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)

    started = time.monotonic()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        await c.post("/api/shutdown", json={
            "announce_message": "{time}後に停止します", "notice_offsets": [0.01],
        })
        await app.state.restart.wait()
    elapsed = time.monotonic() - started

    # shutdown API がモックを落とすので、猶予 5 秒を待たずに抜けるはず
    assert elapsed < 3.0
    steps = {s["name"]: s for s in app.state.restart.status.steps}
    assert steps["wait_until_down"]["ok"] is True


async def test_wait_until_down_gives_up_after_the_grace(settings, pal_client, notifier, mock_state):
    """落ちたと確認できなくても、猶予を過ぎたら停止処理へ進む。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    settings.restart_shutdown_wait = 0
    settings.restart_shutdown_grace = 0.15
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)

    # shutdown API を無効化して、サーバが落ちない状況を作る
    async def noop_shutdown(waittime=0, message=""):
        return {"result": "ok"}

    app.state.pal.shutdown = noop_shutdown
    app.state.restart.poll_interval = 0.02

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        await c.post("/api/shutdown", json={
            "announce_message": "{time}後に停止します", "notice_offsets": [0.01],
        })
        await app.state.restart.wait()

    steps = {s["name"]: s for s in app.state.restart.status.steps}
    assert steps["wait_until_down"]["ok"] is False
    assert "停止処理に進みます" in steps["wait_until_down"]["detail"]
    # 諦めたあとも停止自体は完了させる
    assert app.state.restart.status.phase == "done"


# ---- R-02 ini の所有者・権限 -----------------------------------------------


def test_write_preserves_the_inode(ini_path, settings):
    """置き換えではなく上書きすること。

    一時ファイル + rename だと別 inode になり、所有者とパーミッションが
    書き込んだプロセスのものになる。Palworld が steam、管理ツールが
    mntuser という構成だと、ゲーム側が停止時に ini を書き戻せなくなる。
    """
    from app.settings_ini import SettingsIniStore

    store = SettingsIniStore(ini_path, settings.backup_dir)
    before = ini_path.stat().st_ino

    store.update_options({"ExpRate": "2.000000"})

    assert ini_path.stat().st_ino == before
    assert "ExpRate=2.000000" in ini_path.read_text()


def test_write_preserves_the_permission_bits(ini_path, settings):
    """グループ書き込みを付けた状態が保たれること。"""
    from app.settings_ini import SettingsIniStore

    os.chmod(ini_path, 0o664)
    store = SettingsIniStore(ini_path, settings.backup_dir)

    store.update_options({"ExpRate": "3.000000"})

    assert oct(ini_path.stat().st_mode & 0o777) == oct(0o664)


def test_full_text_write_also_preserves_the_inode(ini_path, settings):
    from app.settings_ini import SettingsIniStore

    store = SettingsIniStore(ini_path, settings.backup_dir)
    before = ini_path.stat().st_ino
    store.write_text(ini_path.read_text().replace("None", "Hard"))
    assert ini_path.stat().st_ino == before


def test_new_file_is_created_when_missing(tmp_path, settings):
    from app.settings_ini import SettingsIniStore

    target = tmp_path / "new" / "PalWorldSettings.ini"
    store = SettingsIniStore(target, settings.backup_dir)
    store._write("[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(ExpRate=1.000000)\n")
    assert target.is_file()


# ---- R-03 sudo systemctl ---------------------------------------------------


def test_sudo_is_prefixed_with_non_interactive_flag():
    """-n が無いとパスワード待ちで固まり、シーケンス全体が止まる。"""
    service = SystemdService("palworld.service", use_sudo=True)
    assert service._command(("restart", "palworld.service")) == [
        "sudo", "-n", "systemctl", "restart", "palworld.service",
    ]


def test_without_sudo_the_command_is_unchanged():
    service = SystemdService("palworld.service", use_sudo=False)
    assert service._command(("restart", "palworld.service")) == [
        "systemctl", "restart", "palworld.service",
    ]


async def test_sudo_shows_up_in_the_simulated_output():
    service = SystemdService("palworld.service", dry_run=True, use_sudo=True)
    result = await service.restart()
    assert "sudo -n systemctl restart" in result.stdout


def _unit_directives(name: str) -> list[str]:
    from pathlib import Path

    unit = Path(__file__).resolve().parents[2] / name
    return [
        line.strip() for line in unit.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_unit_can_drop_privilege_escalation():
    """LinuxGSM 構成では特権昇格を使わないので、これを付けられる。

    issue #28 は sudo と NoNewPrivileges の同居で全操作が失敗した件。
    ゲームサーバの systemd 運用をやめて sudo ごと無くしたので、
    今度は**付いていること**が正しい状態になる。
    """
    directives = _unit_directives("dashboard-Pal.service")
    assert "NoNewPrivileges=true" in directives


def test_the_unit_keeps_tmp_shared_for_linuxgsm():
    """PrivateTmp=true にすると tmux のソケットが見えなくなる。

    LinuxGSM は /tmp/tmux-<uid>/ にソケットを作る。名前空間が分かれると、
    SSH から起動したセッションを管理ツールが掴めず、停止も状態確認もすれ違う。
    """
    directives = _unit_directives("dashboard-Pal.service")
    assert "PrivateTmp=false" in directives
    assert "PrivateTmp=true" not in directives


def test_no_game_server_unit_template_is_shipped():
    """ゲームサーバの systemd 運用は本番から廃止した。

    雛形を残しておくと、次に構築する人がそちらへ戻してしまう。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert not (root / "palworld.service.example").exists()


# ---- R-08 メンテ中の誤警報 -------------------------------------------------


@pytest.fixture
def quiet_monitor(pal_client, notifier):
    return Monitor(pal_client, notifier, interval=3600.0)


async def test_downtime_alert_is_suppressed_during_maintenance(quiet_monitor, notifier, mock_state):
    """意図的に落としている間は「応答なし」を通知しない。"""
    await quiet_monitor.sample()                      # online

    quiet_monitor.suppress_downtime_alerts(60)
    mock_state.running = False
    await quiet_monitor.sample()                      # 落ちたが通知しない

    assert not any(n["title"] == "サーバ応答なし" for n in notifier.sent)


async def test_recovery_during_suppression_is_silent(quiet_monitor, notifier, mock_state):
    """抑止中に復帰したら、下がった通知も戻った通知も出さない。"""
    await quiet_monitor.sample()

    quiet_monitor.suppress_downtime_alerts(60)
    mock_state.running = False
    await quiet_monitor.sample()
    mock_state.running = True
    await quiet_monitor.sample()

    titles = [n["title"] for n in notifier.sent]
    assert "サーバ応答なし" not in titles
    assert "サーバ復帰" not in titles


async def test_still_down_after_the_grace_does_alert(quiet_monitor, notifier, mock_state):
    """猶予が明けてもまだ落ちていれば、そこで初めて通知する。"""
    await quiet_monitor.sample()

    quiet_monitor.suppress_downtime_alerts(0.05)
    mock_state.running = False
    await quiet_monitor.sample()
    assert not any(n["title"] == "サーバ応答なし" for n in notifier.sent)

    await asyncio.sleep(0.08)
    await quiet_monitor.sample()
    assert any(n["title"] == "サーバ応答なし" for n in notifier.sent)


async def test_maintenance_probe_also_suppresses(quiet_monitor, notifier, mock_state):
    """猶予の計算に取りこぼしがあっても、進行中フラグで止める。"""
    in_progress = True
    quiet_monitor.set_maintenance_probe(lambda: in_progress)

    await quiet_monitor.sample()
    mock_state.running = False
    await quiet_monitor.sample()
    assert not any(n["title"] == "サーバ応答なし" for n in notifier.sent)


async def test_memory_alerts_still_fire_during_maintenance(quiet_monitor, notifier, monkeypatch):
    """抑止するのは「応答なし」だけ。リソース監視は止めない。"""
    quiet_monitor.suppress_downtime_alerts(60)
    monkeypatch.setattr(quiet_monitor, "_host_stats", lambda: {
        "cpu_percent": 10.0, "mem_percent": 95.0, "mem_used_mb": 9500, "mem_total_mb": 10000,
    })
    await quiet_monitor.sample()
    assert any(n["level"] == "crit" for n in notifier.sent)


async def test_restart_sequence_suppresses_downtime_alerts(settings, pal_client, notifier):
    """再起動シーケンスが実際に抑止を掛けること。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    settings.restart_alert_grace = 90.0
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        await c.post("/api/restart", json={
            "announce_message": "{time}後に再起動します", "notice_offsets": [0.01],
        })
        await app.state.restart.wait()

    assert app.state.monitor._suppress_until > time.time() + 60


# ---- R-10 問い合わせのキャッシュ -------------------------------------------


async def test_cache_serves_within_ttl():
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return calls

    cache: TTLCache[int] = TTLCache(60.0)
    assert await cache.get(factory) == 1
    assert await cache.get(factory) == 1
    assert calls == 1


async def test_cache_coalesces_concurrent_requests():
    """タブを何枚開いても、ゲームサーバへの問い合わせは 1 本にまとまること。"""
    calls = 0

    async def slow_factory():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return calls

    cache: TTLCache[int] = TTLCache(60.0)
    results = await asyncio.gather(*(cache.get(slow_factory) for _ in range(10)))

    assert calls == 1
    assert results == [1] * 10


async def test_cache_refetches_after_ttl():
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return calls

    cache: TTLCache[int] = TTLCache(0.03)
    await cache.get(factory)
    await asyncio.sleep(0.05)
    await cache.get(factory)
    assert calls == 2


async def test_zero_ttl_disables_caching():
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return calls

    cache: TTLCache[int] = TTLCache(0)
    await cache.get(factory)
    await cache.get(factory)
    assert calls == 2


async def test_status_endpoint_does_not_multiply_upstream_calls(client, mock_state):
    """1 秒ごとのポーリングがタブ数ぶん増えないこと。"""
    before = len(mock_state.players)
    responses = await asyncio.gather(*(client.get("/api/status") for _ in range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["online"] is True for r in responses)
    assert len(mock_state.players) == before


async def test_players_cache_is_invalidated_after_a_kick(client, mock_state):
    """キック直後に古い一覧を見せないこと。"""
    target = mock_state.players[0]["userId"]
    assert (await client.get("/api/players")).json()["count"] == 3

    await client.post("/api/players/kick", json={"userid": target})

    body = (await client.get("/api/players")).json()
    assert body["count"] == 2
    assert all(p["userId"] != target for p in body["players"])
