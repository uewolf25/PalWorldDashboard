"""再起動スケジューラの CRUD とバリデーションのテスト。"""

from __future__ import annotations

import json

import pytest

# 予約にもアナウンス文と予告タイミングが必須
ANNOUNCE = {
    "announce_message": "サーバーは{time}後に再起動します。",
    "notice_offsets": [0.06, 0.03, 0.01],
}


# scheduler.add() を直接呼ぶとき用（API 経由ではない）
ANNOUNCE_KW = {
    "announce_message": ANNOUNCE["announce_message"],
    "notice_offsets": ANNOUNCE["notice_offsets"],
}


def schedule_body(**overrides):
    body = dict(ANNOUNCE)
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
async def running_scheduler(app):
    """start_background=False なのでテスト側でスケジューラを立ち上げる。

    AsyncIOScheduler は起動時に走行中のイベントループを掴むため、
    非同期フィクスチャにしておく必要がある。
    """
    app.state.scheduler.start()
    yield app.state.scheduler
    app.state.scheduler.shutdown()


async def test_add_daily_schedule(client, settings):
    resp = await client.post(
        "/api/schedules", json=schedule_body(kind="daily", spec="04:00", label="毎朝の定期再起動")
    )
    assert resp.status_code == 200
    created = resp.json()
    assert created["kind"] == "daily"
    assert created["enabled"] is True

    listed = (await client.get("/api/schedules")).json()
    assert listed["timezone"] == "Asia/Tokyo"
    assert len(listed["schedules"]) == 1
    assert listed["schedules"][0]["next_fire_at"] is not None


async def test_add_cron_schedule(client):
    resp = await client.post("/api/schedules", json=schedule_body(kind="cron", spec="0 5 * * 1"))
    assert resp.status_code == 200
    assert (await client.get("/api/schedules")).json()["schedules"][0]["spec"] == "0 5 * * 1"


async def test_add_once_schedule(client):
    from datetime import datetime, timedelta

    when = (datetime.now() + timedelta(days=1)).replace(microsecond=0).isoformat()
    resp = await client.post("/api/schedules", json=schedule_body(kind="once", spec=when))
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "daily", "spec": "25:00"},
        {"kind": "daily", "spec": "あさ4時"},
        {"kind": "cron", "spec": "* * *"},
        {"kind": "once", "spec": "2020-01-01T00:00:00"},  # 過去
        {"kind": "weekly", "spec": "mon"},  # 未対応の種別
    ],
)
async def test_invalid_schedule_is_rejected(client, payload):
    resp = await client.post("/api/schedules", json=schedule_body(**payload))
    assert resp.status_code == 400
    assert (await client.get("/api/schedules")).json()["schedules"] == []


async def test_update_and_disable_schedule(client):
    created = (await client.post("/api/schedules", json=schedule_body(kind="daily", spec="04:00"))).json()

    resp = await client.patch(
        f"/api/schedules/{created['id']}", json={"spec": "05:30", "enabled": False}
    )
    assert resp.status_code == 200
    assert resp.json()["spec"] == "05:30"

    entry = (await client.get("/api/schedules")).json()["schedules"][0]
    assert entry["enabled"] is False
    assert entry["next_fire_at"] is None  # 無効化したら発火しない


async def test_delete_schedule(client):
    created = (await client.post("/api/schedules", json=schedule_body(kind="daily", spec="04:00"))).json()
    assert (await client.delete(f"/api/schedules/{created['id']}")).status_code == 200
    assert (await client.get("/api/schedules")).json()["schedules"] == []


async def test_unknown_schedule_returns_404(client):
    assert (await client.delete("/api/schedules/deadbeef")).status_code == 404
    assert (await client.patch("/api/schedules/deadbeef", json={"spec": "04:00"})).status_code == 404


async def test_schedules_are_persisted_to_disk(client, settings):
    await client.post("/api/schedules", json=schedule_body(kind="daily", spec="04:00", label="朝"))

    stored = json.loads(settings.schedule_store.read_text())
    assert len(stored) == 1
    assert stored[0]["label"] == "朝"


async def test_schedules_reload_on_restart(client, settings, pal_client, notifier):
    """プロセスを再起動してもスケジュールが残ること。"""
    from app.main import create_app

    await client.post("/api/schedules", json=schedule_body(kind="daily", spec="04:00", label="朝"))

    fresh = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    fresh.state.scheduler.start()
    try:
        entries = fresh.state.scheduler.list()
        assert len(entries) == 1
        assert entries[0]["label"] == "朝"
        assert entries[0]["next_fire_at"] is not None
    finally:
        fresh.state.scheduler.shutdown()


async def test_daily_job_fires_before_target_time(settings, pal_client, notifier):
    """予告リードぶん手前で発火すること（04:00 指定・5分前予告 → 03:55 に発火）。

    他のテストは予告間隔を 0.06 秒まで潰しているので、
    ここだけ実運用と同じ 300/60/30 秒で確認する。
    """
    from datetime import datetime

    from app.main import create_app

    settings.restart_notice_offsets = "300,60,30"
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    app.state.scheduler.start()
    try:
        created = app.state.scheduler.add(
            "daily", "04:00",
            announce_message="{time}後に再起動します",
            notice_offsets=[300, 60, 30],
        )
        entry = next(e for e in app.state.scheduler.list() if e["id"] == created["id"])

        fire = datetime.fromisoformat(entry["next_fire_at"])
        action_at = datetime.fromisoformat(entry["next_action_at"])

        assert (fire.hour, fire.minute) == (3, 55)
        assert (action_at.hour, action_at.minute) == (4, 0)
    finally:
        app.state.scheduler.shutdown()


async def test_fire_time_follows_the_schedules_own_offsets(settings, pal_client, notifier):
    """予約ごとの予告時間が発火時刻に反映されること。

    グローバル既定（30秒前）ではなく予約の 300秒前を使うので、
    04:00 指定なら 03:55 に発火し、再起動は 04:00 ちょうどになる。
    """
    from datetime import datetime

    from app.main import create_app

    settings.restart_notice_offsets = "30"  # グローバル既定はあえて別の値にする
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    app.state.scheduler.start()
    try:
        created = app.state.scheduler.add(
            "daily", "04:00", "朝",
            announce_message="{time}後に再起動します",
            notice_offsets=[300, 60, 30],
        )
        entry = next(e for e in app.state.scheduler.list() if e["id"] == created["id"])

        fire = datetime.fromisoformat(entry["next_fire_at"])
        action_at = datetime.fromisoformat(entry["next_action_at"])

        assert (fire.hour, fire.minute) == (3, 55)
        assert (action_at.hour, action_at.minute) == (4, 0)
    finally:
        app.state.scheduler.shutdown()


async def test_scheduled_fire_triggers_restart(app, mock_state):
    """スケジュール発火が実際に再起動シーケンスを起動すること。"""
    scheduler = app.state.scheduler
    created = scheduler.add("daily", "04:00", "テスト", **ANNOUNCE_KW)

    await scheduler._fire(created["id"])
    await app.state.restart.wait()

    assert app.state.restart.status.phase == "done"
    assert "スケジュール(テスト)" in app.state.restart.status.reason
    assert mock_state.saves == 1


async def test_schedule_uses_its_own_announcement(client, app, mock_state):
    """予約ごとに設定したアナウンス文が使われること。"""
    created = (
        await client.post(
            "/api/schedules",
            json=schedule_body(
                kind="daily", spec="04:00", label="夜間",
                announce_message="深夜メンテのため{time}後に再起動します。",
            ),
        )
    ).json()

    await app.state.scheduler._fire(created["id"])
    await app.state.restart.wait()

    assert all("深夜メンテのため" in a for a in mock_state.announcements[:3])


async def test_legacy_definition_without_announcement_still_runs(settings, pal_client, notifier, mock_state):
    """動作種別もアナウンス欄も持たない古い JSON を読み込んでも動くこと。"""
    import json

    from app.main import create_app

    settings.schedule_store.parent.mkdir(parents=True, exist_ok=True)
    settings.schedule_store.write_text(json.dumps([{
        "id": "legacy01", "kind": "daily", "spec": "04:00", "label": "旧定義",
        "enabled": True, "created_at": "2026-01-01T00:00:00",
    }]), encoding="utf-8")

    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    app.state.scheduler.start()
    try:
        entry = next(e for e in app.state.scheduler.list() if e["id"] == "legacy01")
        assert entry["action"] == "restart"          # 既定は再起動
        assert entry["notice_offsets"] is None       # 既定値に任せる

        await app.state.scheduler._fire("legacy01")
        await app.state.restart.wait()
        assert app.state.restart.status.phase == "done"
        assert mock_state.announcements              # 既定文面で予告が出る
    finally:
        app.state.scheduler.shutdown()


@pytest.mark.parametrize(
    "bad",
    [
        {"announce_message": "", "notice_offsets": [30]},
        {"announce_message": "文面あり", "notice_offsets": []},
    ],
)
async def test_schedule_requires_announcement(client, bad):
    """再起動/停止の予約は、無告知では作れない。"""
    resp = await client.post(
        "/api/schedules", json={"kind": "daily", "spec": "04:00", **bad}
    )
    assert resp.status_code == 400
    assert "アナウンス" in resp.json()["detail"]
    assert (await client.get("/api/schedules")).json()["schedules"] == []


async def test_patch_can_change_announcement(client):
    created = (await client.post("/api/schedules", json=schedule_body(kind="daily", spec="04:00"))).json()

    resp = await client.patch(
        f"/api/schedules/{created['id']}",
        json={"announce_message": "変更後の文面 {time}", "notice_offsets": [120, 30]},
    )
    assert resp.status_code == 200
    assert resp.json()["announce_message"] == "変更後の文面 {time}"
    assert resp.json()["notice_offsets"] == [120.0, 30.0]


async def test_once_schedule_disables_itself_after_firing(app):
    from datetime import datetime, timedelta

    scheduler = app.state.scheduler
    when = (datetime.now() + timedelta(days=1)).replace(microsecond=0).isoformat()
    created = scheduler.add("once", when, "単発", **ANNOUNCE_KW)

    await scheduler._fire(created["id"])
    await app.state.restart.wait()

    entry = next(e for e in scheduler.list() if e["id"] == created["id"])
    assert entry["enabled"] is False


# ---- 動作種別（起動 / 停止 / 再起動） --------------------------------------


async def test_default_action_is_restart(client):
    created = (await client.post("/api/schedules", json=schedule_body(kind="daily", spec="04:00"))).json()
    assert created["action"] == "restart"
    assert created["action_label"] == "再起動"
    assert created["announced"] is True


async def test_stop_schedule_runs_the_stop_sequence(client, app, mock_state):
    created = (await client.post("/api/schedules", json=schedule_body(
        kind="daily", spec="04:00", action="stop", label="メンテ枠",
        announce_message="メンテのため{time}後に停止します",
    ))).json()
    assert created["action_label"] == "停止"

    await app.state.scheduler._fire(created["id"])
    await app.state.restart.wait()

    status = app.state.restart.status
    assert status.mode == "stop"
    assert status.phase == "done"
    assert "systemctl_stop" in [s["name"] for s in status.steps]
    assert all("停止します" in a for a in mock_state.announcements[:3])


async def test_start_schedule_starts_the_server_without_announcing(client, app, mock_state, notifier):
    """起動の予約は予告を伴わない（停止中のサーバには送れない）。"""
    mock_state.running = False
    created = (await client.post("/api/schedules", json={
        "kind": "daily", "spec": "05:00", "action": "start", "label": "朝の起動",
    })).json()
    assert created["announced"] is False
    assert created["notice_offsets"] is None

    await app.state.scheduler._fire(created["id"])

    # 開発以外のバックエンドでは systemctl が simulated になるだけで、呼び出しは走る
    assert mock_state.announcements == []
    assert any(n["title"] == "サーバーを起動しました" for n in notifier.sent)


async def test_start_schedule_needs_no_announcement(client):
    resp = await client.post("/api/schedules", json={
        "kind": "daily", "spec": "05:00", "action": "start",
    })
    assert resp.status_code == 200


async def test_start_schedule_ignores_supplied_announcement(client):
    """起動に予告を渡しても保持しない（送れないため）。"""
    created = (await client.post("/api/schedules", json=schedule_body(
        kind="daily", spec="05:00", action="start",
    ))).json()
    assert created["announce_message"] == ""
    assert created["notice_offsets"] is None


async def test_start_schedule_fires_exactly_on_time(client, app):
    """起動は予告リードが無いので、指定時刻ちょうどに発火する。"""
    from datetime import datetime

    created = (await client.post("/api/schedules", json={
        "kind": "daily", "spec": "05:00", "action": "start",
    })).json()
    entry = next(e for e in app.state.scheduler.list() if e["id"] == created["id"])

    fire = datetime.fromisoformat(entry["next_fire_at"])
    action_at = datetime.fromisoformat(entry["next_action_at"])
    assert (fire.hour, fire.minute) == (5, 0)
    assert fire == action_at


async def test_unknown_action_is_rejected(client):
    resp = await client.post("/api/schedules", json=schedule_body(
        kind="daily", spec="04:00", action="explode",
    ))
    assert resp.status_code == 400
    assert "動作" in resp.json()["detail"]


async def test_changing_action_to_start_drops_the_announcement(client):
    created = (await client.post("/api/schedules", json=schedule_body(kind="daily", spec="04:00"))).json()
    assert created["announce_message"]

    resp = await client.patch(f"/api/schedules/{created['id']}", json={"action": "start"})
    assert resp.status_code == 200
    assert resp.json()["action"] == "start"
    assert resp.json()["announce_message"] == ""
    assert resp.json()["notice_offsets"] is None


async def test_changing_action_to_stop_requires_an_announcement(client):
    created = (await client.post("/api/schedules", json={
        "kind": "daily", "spec": "05:00", "action": "start",
    })).json()

    resp = await client.patch(f"/api/schedules/{created['id']}", json={"action": "stop"})
    assert resp.status_code == 400


async def test_actions_are_listed_for_the_ui(client):
    body = (await client.get("/api/schedules")).json()
    assert [a["value"] for a in body["actions"]] == ["restart", "stop", "start"]


async def test_maintenance_window_can_be_scheduled(client, app):
    """停止と起動を別々に予約して、メンテ枠を作れること。"""
    await client.post("/api/schedules", json=schedule_body(
        kind="daily", spec="04:00", action="stop", label="メンテ開始",
        announce_message="メンテのため{time}後に停止します",
    ))
    await client.post("/api/schedules", json={
        "kind": "daily", "spec": "04:30", "action": "start", "label": "メンテ終了",
    })

    entries = (await client.get("/api/schedules")).json()["schedules"]
    by_label = {e["label"]: e for e in entries}
    assert by_label["メンテ開始"]["action"] == "stop"
    assert by_label["メンテ終了"]["action"] == "start"
    assert by_label["メンテ開始"]["next_action_at"] < by_label["メンテ終了"]["next_action_at"]


async def test_schedules_with_actions_survive_a_reload(client, settings, pal_client, notifier):
    from app.main import create_app

    await client.post("/api/schedules", json={
        "kind": "daily", "spec": "05:00", "action": "start", "label": "朝",
    })

    fresh = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    fresh.state.scheduler.start()
    try:
        entries = fresh.state.scheduler.list()
        assert len(entries) == 1
        assert entries[0]["action"] == "start"
    finally:
        fresh.state.scheduler.shutdown()
