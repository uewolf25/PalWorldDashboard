"""予約の「今回だけ見送る」（Issue #19）。

無効化との違いが肝心。無効化は次から一切動かないが、見送りは1回きりで
その次からはまた動く。「今日はメンテを飛ばしたいが明日からは通常どおり」用。
"""

from __future__ import annotations

import pytest

from app.scheduler import ServerScheduler


@pytest.fixture
def scheduler(settings, tmp_path) -> ServerScheduler:
    """発火だけ確かめたいので、実行系はダミーに差し替える。"""

    class FakeRestart:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def request(self, **kwargs):
            self.calls.append(kwargs)

    class FakeService:
        def __init__(self) -> None:
            self.started = 0

        async def start(self):
            self.started += 1
            return type("R", (), {"ok": True, "stderr": ""})()

    class FakeAnnouncer:
        def __init__(self) -> None:
            self.sent: list[tuple] = []

        async def discord_only(self, title, detail, **kwargs):
            self.sent.append((title, detail))

    sched = ServerScheduler(
        FakeRestart(),
        FakeService(),
        FakeAnnouncer(),
        timezone="Asia/Tokyo",
        store_path=tmp_path / "schedules.json",
    )
    return sched


def make(scheduler, **over):
    payload = dict(
        kind="daily",
        spec="04:00",
        label="毎朝の再起動",
        enabled=True,
        announce_message="サーバーは{time}後に再起動します。",
        notice_offsets=[300.0],
        action="restart",
    )
    payload.update(over)
    return scheduler.add(
        payload["kind"],
        payload["spec"],
        payload["label"],
        payload["enabled"],
        announce_message=payload["announce_message"],
        notice_offsets=payload["notice_offsets"],
        action=payload["action"],
    )


# ---- 既定値 ----------------------------------------------------------------


def test_new_schedules_are_not_skipped(scheduler):
    assert make(scheduler)["skip_next"] is False


# ---- 見送り ----------------------------------------------------------------


async def test_a_skipped_schedule_does_not_run(scheduler):
    sid = make(scheduler)["id"]
    scheduler.update(sid, skip_next=True)

    await scheduler._fire(sid)
    assert scheduler._restart.calls == []


async def test_skipping_only_applies_once(scheduler):
    """見送りは1回きり。その次はふつうに動く。"""
    sid = make(scheduler)["id"]
    scheduler.update(sid, skip_next=True)

    await scheduler._fire(sid)     # 見送られる
    await scheduler._fire(sid)     # こちらは動く

    assert len(scheduler._restart.calls) == 1


async def test_the_flag_is_cleared_after_firing(scheduler):
    sid = make(scheduler)["id"]
    scheduler.update(sid, skip_next=True)
    await scheduler._fire(sid)

    item = next(s for s in scheduler.list() if s["id"] == sid)
    assert item["skip_next"] is False


async def test_skipping_leaves_the_schedule_enabled(scheduler):
    """無効化とは違うことの確認。"""
    sid = make(scheduler)["id"]
    scheduler.update(sid, skip_next=True)
    await scheduler._fire(sid)

    item = next(s for s in scheduler.list() if s["id"] == sid)
    assert item["enabled"] is True


async def test_skipping_is_announced_to_discord(scheduler):
    sid = make(scheduler)["id"]
    scheduler.update(sid, skip_next=True)
    await scheduler._fire(sid)

    assert scheduler._announcer.sent
    assert "見送り" in scheduler._announcer.sent[0][0]


async def test_a_skipped_start_schedule_does_not_start_the_server(scheduler):
    sid = make(scheduler, action="start", announce_message="", notice_offsets=[])["id"]
    scheduler.update(sid, skip_next=True)

    await scheduler._fire(sid)
    assert scheduler._service.started == 0


async def test_skipping_a_once_schedule_ends_it(scheduler):
    """1回しか無い予約を見送ったなら、その予約はもう用済み。"""
    sid = make(scheduler, kind="once", spec="2099-01-01T04:00:00")["id"]
    scheduler.update(sid, skip_next=True)
    await scheduler._fire(sid)

    item = next(s for s in scheduler.list() if s["id"] == sid)
    assert item["enabled"] is False


# ---- 永続化 ----------------------------------------------------------------


def _revive(scheduler, path) -> ServerScheduler:
    """保存済みの定義を読み直した状態のスケジューラを作る。

    読み込みは start() の中で行われるので、そこまで通す。
    """
    revived = ServerScheduler(
        scheduler._restart,
        scheduler._service,
        scheduler._announcer,
        timezone="Asia/Tokyo",
        store_path=path,
    )
    revived.start()
    return revived


async def test_skip_survives_a_restart(tmp_path, scheduler):
    sid = make(scheduler)["id"]
    scheduler.update(sid, skip_next=True)

    revived = _revive(scheduler, tmp_path / "schedules.json")
    try:
        item = next(s for s in revived.list() if s["id"] == sid)
        assert item["skip_next"] is True
    finally:
        revived.shutdown()


async def test_old_definitions_without_the_field_still_load(tmp_path, scheduler):
    """skip_next を持たない頃の定義を読み捨てない。"""
    import json

    make(scheduler)
    path = tmp_path / "schedules.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    for item in raw:
        item.pop("skip_next", None)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    revived = _revive(scheduler, path)
    try:
        assert len(revived.list()) == 1
        assert revived.list()[0]["skip_next"] is False
    finally:
        revived.shutdown()


# ---- API -------------------------------------------------------------------


async def test_patch_endpoint_sets_skip(client):
    created = (await client.post("/api/schedules", json={
        "kind": "daily", "spec": "04:00", "action": "restart",
        "announce_message": "まもなく再起動します。", "notice_offsets": [300.0],
    })).json()

    resp = await client.patch(f"/api/schedules/{created['id']}", json={"skip_next": True})
    assert resp.status_code == 200
    assert resp.json()["skip_next"] is True


async def test_skip_can_be_cancelled(client):
    created = (await client.post("/api/schedules", json={
        "kind": "daily", "spec": "04:00", "action": "restart",
        "announce_message": "まもなく再起動します。", "notice_offsets": [300.0],
    })).json()

    await client.patch(f"/api/schedules/{created['id']}", json={"skip_next": True})
    resp = await client.patch(f"/api/schedules/{created['id']}", json={"skip_next": False})
    assert resp.json()["skip_next"] is False


async def test_schedules_list_exposes_skip(client):
    await client.post("/api/schedules", json={
        "kind": "daily", "spec": "05:00", "action": "start",
    })
    body = (await client.get("/api/schedules")).json()
    assert body["schedules"][0]["skip_next"] is False
