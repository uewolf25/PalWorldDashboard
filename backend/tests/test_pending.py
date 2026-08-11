"""設定変更の予約（保留中の変更）のテスト。

狙いは「管理者がいつ設定を入力しても、メンテナンス枠で自動的に反映される」こと。
サーバは停止時に ini を上書きするため、稼働中は ini に書かずに退避しておき、
停止シーケンスがサーバを止めた直後に書き込む。
"""

from __future__ import annotations

import json

import httpx
import pytest

from mock import mock_palworld

FULL_INI = """[/Script/Pal.PalGameWorldSettings]
OptionSettings=(Difficulty=None,ExpRate=1.000000,PalCaptureRate=1.000000,bIsPvP=False,ServerName="Test Server",RESTAPIEnabled=True,RESTAPIPort=8212)
"""

ANNOUNCE = {
    "announce_message": "サーバーは{time}後に再起動します。",
    "notice_offsets": [0.03, 0.01],
}


@pytest.fixture
def full_ini(tmp_path):
    path = tmp_path / "PalWorldSettings.ini"
    path.write_text(FULL_INI, encoding="utf-8")
    return path


@pytest.fixture
def dev_settings(settings, full_ini):
    """開発と同じくモックバックエンドを使う設定。"""
    settings.pal_settings_ini = full_ini
    settings.pal_service_backend = "mock"
    return settings


@pytest.fixture
async def client2(dev_settings, pal_client, notifier, mock_state):
    """モックの制御まで ASGI 直結したアプリ。ポートを使わない。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app(dev_settings, pal_client=pal_client, notifier=notifier, start_background=False)
    control = AsyncClient(
        transport=httpx.ASGITransport(app=mock_palworld.app), base_url="http://mock-palworld"
    )
    app.state.service._client = control
    app.state.service._owns_client = False
    app.state.service.control_url = "http://mock-palworld"
    app.state.scheduler.start()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        c.app = app
        yield c
    app.state.scheduler.shutdown()
    await control.aclose()


def stage(**values):
    return {"values": values, "when": "next_stop"}


# ---- 稼働中でも保存できる --------------------------------------------------


async def test_can_save_while_running_by_scheduling(client2, full_ini, mock_state):
    """稼働中でも「次の停止時に反映」なら保存できること。

    これができないと、管理者はサーバを止められる時間帯にしか設定を入力できない。
    """
    assert mock_state.running is True

    resp = await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.5))
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "scheduled"
    assert body["pending_id"]

    # まだ ini には書かれていない（書いても停止時に上書きされるため）
    assert "ExpRate=1.000000" in full_ini.read_text()


async def test_immediate_save_is_still_blocked_while_running(client2, full_ini):
    resp = await client2.put(
        "/api/settings-ini/fields", json={"values": {"ExpRate": 2.5}, "when": "now"}
    )
    assert resp.status_code == 409
    assert "ExpRate=1.000000" in full_ini.read_text()


async def test_pending_is_listed_with_its_target(client2):
    await client2.put("/api/settings-ini/fields", json={
        "values": {"ExpRate": 2.5}, "when": "next_stop", "note": "経験値2.5倍",
    })

    body = (await client2.get("/api/settings-ini/pending")).json()
    assert body["total"] == 1
    item = body["pending"][0]
    assert item["updates"] == {"ExpRate": "2.500000"}
    assert item["note"] == "経験値2.5倍"
    assert item["target_label"] == "次にサーバが停止するとき"


# ---- 停止シーケンスで自動反映される ----------------------------------------


async def test_pending_is_applied_when_the_server_stops(client2, full_ini, mock_state):
    """本命: 稼働中に入力した変更が、停止シーケンスで自動的に ini へ入ること。"""
    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.5, bIsPvP=True))

    resp = await client2.post("/api/shutdown", json={
        "reason": "メンテ", "announce_message": "{time}後に停止します", "notice_offsets": [0.01],
    })
    assert resp.status_code == 200
    await client2.app.state.restart.wait()

    text = full_ini.read_text()
    assert "ExpRate=2.500000" in text
    assert "bIsPvP=True" in text
    # 反映済みの保留は消える
    assert (await client2.get("/api/settings-ini/pending")).json()["total"] == 0


async def test_restart_applies_pending_between_stop_and_start(client2, full_ini, mock_state):
    """再起動でも、停止と起動の間に書き込まれること。

    systemctl restart のままだと ini を書き換える隙間が作れないので、
    保留があるときだけ stop → 書き込み → start に分ける。
    """
    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=3.0))

    await client2.post("/api/restart", json={
        "reason": "設定反映", "announce_message": "{time}後に再起動します", "notice_offsets": [0.01],
    })
    await client2.app.state.restart.wait()

    status = client2.app.state.restart.status
    steps = [s["name"] for s in status.steps]
    assert status.phase == "done"
    assert "systemctl_stop" in steps
    assert "apply_settings" in steps
    assert "systemctl_start" in steps
    assert steps.index("systemctl_stop") < steps.index("apply_settings") < steps.index("systemctl_start")

    assert "ExpRate=3.000000" in full_ini.read_text()
    assert mock_state.running is True   # 起動し直されている


async def test_restart_without_pending_uses_plain_restart(client2, mock_state):
    """保留が無ければ従来どおり restart 一発（余計な停止時間を作らない）。"""
    await client2.post("/api/restart", json={
        "reason": "通常", "announce_message": "{time}後に再起動します", "notice_offsets": [0.01],
    })
    await client2.app.state.restart.wait()

    steps = [s["name"] for s in client2.app.state.restart.status.steps]
    assert "systemctl_restart" in steps
    assert "apply_settings" not in steps


async def test_applied_result_is_reported_in_status(client2):
    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.0))
    await client2.post("/api/shutdown", json={
        "announce_message": "{time}後に停止します", "notice_offsets": [0.01],
    })
    await client2.app.state.restart.wait()

    applied = (await client2.get("/api/restart")).json()["applied"]
    assert applied["ok"] is True
    assert applied["keys"] == ["ExpRate"]
    assert applied["backup"]


async def test_multiple_saves_accumulate_and_last_one_wins(client2, full_ini):
    """複数回に分けて入力した変更が、まとめて反映されること。"""
    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.0))
    await client2.put("/api/settings-ini/fields", json=stage(bIsPvP=True))
    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=5.0))  # 上書き

    assert (await client2.get("/api/settings-ini/pending")).json()["total"] == 3

    await client2.post("/api/shutdown", json={
        "announce_message": "{time}後に停止します", "notice_offsets": [0.01],
    })
    await client2.app.state.restart.wait()

    text = full_ini.read_text()
    assert "ExpRate=5.000000" in text     # 後から保存したものが勝つ
    assert "bIsPvP=True" in text


# ---- 特定の予約に紐づける ---------------------------------------------------


async def test_pending_bound_to_a_schedule_waits_for_it(client2, full_ini):
    created = (await client2.post("/api/schedules", json={
        "kind": "daily", "spec": "04:00", "action": "stop", "label": "週次メンテ", **ANNOUNCE,
    })).json()

    resp = await client2.put("/api/settings-ini/fields", json={
        "values": {"ExpRate": 2.5}, "when": "schedule", "schedule_id": created["id"],
    })
    assert resp.status_code == 200

    item = (await client2.get("/api/settings-ini/pending")).json()["pending"][0]
    assert "週次メンテ" in item["target_label"]

    # 別の（手動の）停止では反映されない
    await client2.post("/api/shutdown", json={
        "announce_message": "{time}後に停止します", "notice_offsets": [0.01],
    })
    await client2.app.state.restart.wait()
    assert "ExpRate=1.000000" in full_ini.read_text()
    assert (await client2.get("/api/settings-ini/pending")).json()["total"] == 1


async def test_pending_is_applied_by_its_own_schedule(client2, full_ini, mock_state):
    created = (await client2.post("/api/schedules", json={
        "kind": "daily", "spec": "04:00", "action": "stop", "label": "週次メンテ", **ANNOUNCE,
    })).json()
    await client2.put("/api/settings-ini/fields", json={
        "values": {"ExpRate": 2.5}, "when": "schedule", "schedule_id": created["id"],
    })

    await client2.app.state.scheduler._fire(created["id"])
    await client2.app.state.restart.wait()

    assert "ExpRate=2.500000" in full_ini.read_text()
    assert (await client2.get("/api/settings-ini/pending")).json()["total"] == 0


async def test_unbound_pending_is_applied_by_any_schedule(client2, full_ini):
    """予約を指定していない変更は、どの停止機会でも反映される。"""
    created = (await client2.post("/api/schedules", json={
        "kind": "daily", "spec": "04:00", "action": "restart", "label": "定期", **ANNOUNCE,
    })).json()
    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.5))

    await client2.app.state.scheduler._fire(created["id"])
    await client2.app.state.restart.wait()

    assert "ExpRate=2.500000" in full_ini.read_text()


async def test_schedule_list_shows_pending_count(client2):
    created = (await client2.post("/api/schedules", json={
        "kind": "daily", "spec": "04:00", "action": "stop", "label": "メンテ", **ANNOUNCE,
    })).json()
    await client2.put("/api/settings-ini/fields", json={
        "values": {"ExpRate": 2.5}, "when": "schedule", "schedule_id": created["id"],
    })

    entry = next(s for s in (await client2.get("/api/schedules")).json()["schedules"]
                 if s["id"] == created["id"])
    assert entry["pending_changes"] == 1


async def test_start_schedule_never_applies_settings(client2):
    """起動の予約は ini を書き換えない（サーバが上がってしまうため）。"""
    await client2.post("/api/schedules", json={
        "kind": "daily", "spec": "05:00", "action": "start", "label": "朝の起動",
    })
    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.5))

    entry = next(s for s in (await client2.get("/api/schedules")).json()["schedules"]
                 if s["action"] == "start")
    assert entry["pending_changes"] == 0


async def test_unknown_schedule_is_rejected(client2):
    resp = await client2.put("/api/settings-ini/fields", json={
        "values": {"ExpRate": 2.5}, "when": "schedule", "schedule_id": "nope",
    })
    assert resp.status_code == 404


async def test_schedule_target_requires_an_id(client2):
    resp = await client2.put("/api/settings-ini/fields", json={
        "values": {"ExpRate": 2.5}, "when": "schedule",
    })
    assert resp.status_code == 400


# ---- 失敗時の扱い ----------------------------------------------------------


async def test_server_is_restarted_even_if_applying_fails(client2, full_ini, mock_state):
    """反映に失敗しても、サーバは必ず起動し直すこと。

    設定が変わらないより、サーバが落ちたままの方が困る。
    """
    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.5))
    full_ini.write_text("壊れた内容", encoding="utf-8")   # 書き込めない状態にする

    await client2.post("/api/restart", json={
        "reason": "テスト", "announce_message": "{time}後に再起動します", "notice_offsets": [0.01],
    })
    await client2.app.state.restart.wait()

    status = client2.app.state.restart.status
    assert status.phase == "done"
    assert mock_state.running is True            # 起動している
    assert status.applied["ok"] is False
    # 保留は消さない。次の停止機会に再試行できる
    assert (await client2.get("/api/settings-ini/pending")).json()["total"] == 1


async def test_failed_apply_is_notified(client2, full_ini, notifier):
    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.5))
    full_ini.write_text("壊れた内容", encoding="utf-8")

    await client2.post("/api/shutdown", json={
        "announce_message": "{time}後に停止します", "notice_offsets": [0.01],
    })
    await client2.app.state.restart.wait()

    assert any(n["title"] == "設定変更の反映に失敗しました" and n["level"] == "crit"
               for n in notifier.sent)


async def test_pending_is_validated_at_save_time(client2):
    """保存の時点で弾く（反映時まで気づかないと困る）。"""
    resp = await client2.put("/api/settings-ini/fields", json=stage(Difficulty="Nope"))
    assert resp.status_code == 400
    assert (await client2.get("/api/settings-ini/pending")).json()["total"] == 0


async def test_pending_warns_on_range_but_is_accepted(client2):
    """範囲は目安なので、警告しつつ予約は受け付ける。"""
    resp = await client2.put("/api/settings-ini/fields", json=stage(ExpRate=999))
    assert resp.status_code == 200
    assert any("推奨範囲" in w for w in resp.json()["warnings"])
    assert (await client2.get("/api/settings-ini/pending")).json()["total"] == 1


# ---- 取り消しと即時反映 ----------------------------------------------------


async def test_pending_can_be_cancelled(client2, full_ini):
    created = (await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.5))).json()

    assert (await client2.delete("/api/settings-ini/pending/" + created["pending_id"])).status_code == 200
    assert (await client2.get("/api/settings-ini/pending")).json()["total"] == 0

    await client2.post("/api/shutdown", json={
        "announce_message": "{time}後に停止します", "notice_offsets": [0.01],
    })
    await client2.app.state.restart.wait()
    assert "ExpRate=1.000000" in full_ini.read_text()


async def test_cancelling_unknown_pending_is_404(client2):
    assert (await client2.delete("/api/settings-ini/pending/nope")).status_code == 404


async def test_pending_can_be_applied_immediately_when_stopped(client2, full_ini, mock_state):
    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.5))
    mock_state.running = False

    resp = await client2.post("/api/settings-ini/pending/apply")
    assert resp.status_code == 200
    assert resp.json()["applied"] == 1
    assert "ExpRate=2.500000" in full_ini.read_text()


async def test_immediate_apply_is_blocked_while_running(client2, full_ini):
    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.5))
    resp = await client2.post("/api/settings-ini/pending/apply")
    assert resp.status_code == 409
    assert "ExpRate=1.000000" in full_ini.read_text()


# ---- 永続化 ----------------------------------------------------------------


async def test_pending_survives_a_process_restart(client2, dev_settings, pal_client, notifier):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.5))

    stored = json.loads(dev_settings.pending_store.read_text())
    assert stored[0]["updates"] == {"ExpRate": "2.500000"}

    fresh = create_app(dev_settings, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=fresh), base_url="http://manager") as c:
        assert (await c.get("/api/settings-ini/pending")).json()["total"] == 1


async def test_pending_limit_is_enforced(dev_settings, pal_client, notifier, mock_state):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    dev_settings.pending_limit = 2
    app = create_app(dev_settings, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        assert (await c.put("/api/settings-ini/fields", json=stage(ExpRate=2.0))).status_code == 200
        assert (await c.put("/api/settings-ini/fields", json=stage(ExpRate=3.0))).status_code == 200
        resp = await c.put("/api/settings-ini/fields", json=stage(ExpRate=4.0))
    assert resp.status_code == 409
    assert "上限" in resp.json()["detail"]


async def test_pending_can_be_cleared(client2):
    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.0))
    await client2.put("/api/settings-ini/fields", json=stage(bIsPvP=True))

    assert (await client2.delete("/api/settings-ini/pending")).json()["cleared"] == 2
    assert (await client2.get("/api/settings-ini/pending")).json()["total"] == 0


async def test_fields_endpoint_reports_pending_total(client2):
    await client2.put("/api/settings-ini/fields", json=stage(ExpRate=2.0))
    body = (await client2.get("/api/settings-ini/fields")).json()
    assert body["pending_total"] == 1
