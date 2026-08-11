"""再起動/停止シーケンスのテスト。

重点:
  1. ワールド保存に失敗したら再起動しない
  2. 二重に走らない
  3. 予告中にキャンセルできる
  4. アナウンス文と予告タイミングは必須（無告知で落とさない）
  5. Discord は開始/完了/中止のみ、予告の途中経過は流さない
"""

from __future__ import annotations

import asyncio

import pytest

# テストでは予告間隔を潰す
FAST = [0.06, 0.03, 0.01]
MSG = "サーバーは{time}後に再起動します。"


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    """背景タスクの副作用が現れるまで待つ。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("条件が満たされませんでした")


def restart_body(**overrides):
    body = {"reason": "テスト", "announce_message": MSG, "notice_offsets": FAST}
    body.update(overrides)
    return body


async def test_restart_announces_saves_then_shuts_down(client, app, mock_state):
    resp = await client.post("/api/restart", json=restart_body(reason="定期メンテ"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["phase"] == "announcing"
    assert body["mode"] == "restart"

    await app.state.restart.wait()

    status = app.state.restart.status
    assert status.phase == "done", status.message

    assert len(mock_state.announcements) == 3
    assert all("再起動します" in a for a in mock_state.announcements)
    assert mock_state.saves == 1
    assert len(mock_state.shutdowns) == 1

    step_names = [s["name"] for s in status.steps]
    assert step_names.count("announce") == 3
    assert step_names.index("world_save") < step_names.index("shutdown_api")
    assert "systemctl_restart" in step_names


async def test_announce_template_renders_remaining_time(client, app, mock_state):
    await client.post("/api/restart", json=restart_body(notice_offsets=[120, 60]))
    # シーケンスは背景タスクなので、最初の予告が出るまで待つ
    await _wait_for(lambda: mock_state.announcements)
    assert mock_state.announcements[0] == "サーバーは2分後に再起動します。"

    app.state.restart.cancel()
    await app.state.restart.wait()


async def test_template_without_placeholder_is_sent_as_is(client, app, mock_state):
    await client.post("/api/restart", json=restart_body(announce_message="まもなくメンテです"))
    await app.state.restart.wait()
    assert mock_state.announcements[:3] == ["まもなくメンテです"] * 3


@pytest.mark.parametrize(
    "payload,field",
    [
        ({"announce_message": "", "notice_offsets": FAST}, "announce_message"),
        ({"notice_offsets": FAST}, "announce_message"),
        ({"announce_message": MSG, "notice_offsets": []}, "notice_offsets"),
        ({"announce_message": MSG}, "notice_offsets"),
    ],
)
async def test_restart_requires_message_and_offsets(client, mock_state, payload, field):
    """無告知でサーバを落とせないこと。"""
    resp = await client.post("/api/restart", json=payload)
    assert resp.status_code == 422
    assert mock_state.saves == 0
    assert mock_state.shutdowns == []


async def test_restart_rejects_absurd_offsets(client, mock_state):
    resp = await client.post("/api/restart", json=restart_body(notice_offsets=[-5]))
    assert resp.status_code == 400
    resp = await client.post("/api/restart", json=restart_body(notice_offsets=[100000]))
    assert resp.status_code == 400
    assert mock_state.shutdowns == []


async def test_restart_aborts_when_world_save_fails(client, app, mock_state, notifier):
    """保存できないまま再起動するとセーブデータを失うので、必ず中止する。"""
    mock_state.fail_save = True

    await client.post("/api/restart", json=restart_body())
    await app.state.restart.wait()

    status = app.state.restart.status
    assert status.phase == "failed"
    assert "ワールド保存に失敗" in status.message
    assert mock_state.shutdowns == []
    assert mock_state.stops == 0
    assert any("中止" in a for a in mock_state.announcements)
    assert any(n["level"] == "crit" for n in notifier.sent)


async def test_restart_can_be_cancelled_during_countdown(client, app, mock_state):
    await client.post("/api/restart", json=restart_body(notice_offsets=[30, 10]))
    assert app.state.restart.status.phase == "announcing"

    resp = await client.post("/api/restart/cancel")
    assert resp.status_code == 200

    await app.state.restart.wait()
    assert app.state.restart.status.phase == "cancelled"
    assert mock_state.saves == 0
    assert mock_state.shutdowns == []
    assert any("キャンセル" in a for a in mock_state.announcements)


async def test_cancel_without_pending_restart_is_409(client):
    assert (await client.post("/api/restart/cancel")).status_code == 409


async def test_second_restart_is_rejected_while_running(client, app):
    await client.post("/api/restart", json=restart_body(notice_offsets=[30, 10]))
    resp = await client.post("/api/restart", json=restart_body())
    assert resp.status_code == 409
    assert "進行中" in resp.json()["detail"]

    app.state.restart.cancel()
    await app.state.restart.wait()


async def test_debounce_blocks_immediate_rerun(settings, pal_client, notifier, mock_state):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    settings.restart_debounce_sec = 300.0
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        await c.post("/api/restart", json=restart_body())
        await app.state.restart.wait()
        assert app.state.restart.status.phase == "done"

        assert (await c.post("/api/restart", json=restart_body())).status_code == 429

        resp = await c.post("/api/restart", json=restart_body(force=True))
        assert resp.status_code == 200
        await app.state.restart.wait()


async def test_restart_survives_announce_failure(client, app, mock_state, monkeypatch):
    """アナウンスが失敗しても再起動自体は続行する。"""
    from app.palapi import PalApiError

    async def failing_announce(message: str):
        raise PalApiError("announce unavailable")

    monkeypatch.setattr(app.state.pal, "announce", failing_announce)

    await client.post("/api/restart", json=restart_body())
    await app.state.restart.wait()

    assert app.state.restart.status.phase == "done"
    assert mock_state.saves == 1
    assert len(mock_state.shutdowns) == 1
    # 失敗した予告も履歴には ok=False で残る
    records = app.state.announce_log.list()
    assert any(r["ok"] is False for r in records)


async def test_restart_status_endpoint_reports_countdown(client, app):
    await client.post("/api/restart", json=restart_body(notice_offsets=[60, 30]))
    body = (await client.get("/api/restart")).json()
    assert body["in_progress"] is True
    assert body["phase"] == "announcing"
    assert body["cancellable"] is True
    assert 0 < body["seconds_remaining"] <= 60

    app.state.restart.cancel()
    await app.state.restart.wait()


# ---- 停止シーケンス --------------------------------------------------------


async def test_shutdown_stops_without_restarting(client, app, mock_state):
    resp = await client.post(
        "/api/shutdown",
        json={"reason": "メンテ", "announce_message": "サーバーは{time}後に停止します。",
              "notice_offsets": FAST},
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "stop"

    await app.state.restart.wait()

    status = app.state.restart.status
    assert status.phase == "done"
    assert "停止" in status.message
    step_names = [s["name"] for s in status.steps]
    assert "systemctl_stop" in step_names
    assert "systemctl_restart" not in step_names
    assert all("停止します" in a for a in mock_state.announcements[:3])


async def test_shutdown_also_aborts_when_save_fails(client, app, mock_state):
    mock_state.fail_save = True
    await client.post(
        "/api/shutdown",
        json={"announce_message": "停止します", "notice_offsets": FAST},
    )
    await app.state.restart.wait()

    assert app.state.restart.status.phase == "failed"
    assert mock_state.shutdowns == []


async def test_restart_and_shutdown_share_the_same_lock(client, app):
    await client.post("/api/restart", json=restart_body(notice_offsets=[30]))
    resp = await client.post(
        "/api/shutdown", json={"announce_message": "停止します", "notice_offsets": [30]}
    )
    assert resp.status_code == 409

    app.state.restart.cancel()
    await app.state.restart.wait()


# ---- Discord への流量 ------------------------------------------------------


async def test_discord_gets_start_and_finish_only(client, app, notifier, mock_state):
    """予告の途中経過は Discord に流さない（チャンネルが荒れるため）。"""
    await client.post("/api/restart", json=restart_body(notice_offsets=[0.06, 0.03, 0.01]))
    await app.state.restart.wait()

    titles = [n["title"] for n in notifier.sent]
    # 予告の時点では「これから実行する」と分かる文面にする（Issue #14）
    assert any(t.startswith("【予告】") and "再起動します" in t for t in titles)
    assert "サーバー再起動が完了しました" in titles
    # ゲーム内には3回予告したが、Discord は2通だけ
    assert len(mock_state.announcements) == 3
    assert len(titles) == 2


async def test_discord_notified_on_cancel(client, app, notifier):
    await client.post("/api/restart", json=restart_body(notice_offsets=[30]))
    await client.post("/api/restart/cancel")
    await app.state.restart.wait()

    assert any(n["title"] == "サーバー再起動をキャンセルしました" for n in notifier.sent)


@pytest.mark.parametrize(
    "seconds,expected",
    [(300, "5分"), (60, "1分"), (30, "30秒"), (90, "1分30秒"), (5, "5秒")],
)
def test_humanize(seconds, expected):
    from app.restart import humanize

    assert humanize(seconds) == expected


# ---- 通知の文面（Issue #14） -----------------------------------------------


async def _advance_notice(notifier):
    """予告の Discord 通知が出るまで待って、それを返す。

    シーケンスは背景タスクなので、送信前に読むと取りこぼす。
    保存の通知などが先に混ざることもあるので、索引ではなく内容で拾う。
    """
    await _wait_for(lambda: any(
        n["title"].startswith("【予告】") or n["title"].startswith("サーバーを")
        for n in notifier.sent
    ))
    return next(n for n in notifier.sent
                if n["title"].startswith("【予告】") or n["title"].startswith("サーバーを"))


async def test_advance_notice_says_when_not_now(client, app, notifier):
    """予告の時点で「開始します」と書くと、今落ちると受け取られる。

    実際に落ちるのは予告リードぶん後なので、いつ実行されるのかを主語にする。
    """
    await client.post("/api/restart", json=restart_body(notice_offsets=[300, 60]))
    title = (await _advance_notice(notifier))["title"]

    assert title == "【予告】5分後にサーバーを再起動します"
    assert "開始します" not in title

    app.state.restart.cancel()
    await app.state.restart.wait()


async def test_advance_notice_for_stop_uses_the_right_verb(client, app, notifier):
    await client.post("/api/shutdown", json={
        "announce_message": "{time}後に停止します", "notice_offsets": [60],
    })
    assert (await _advance_notice(notifier))["title"] == "【予告】1分後にサーバーを停止します"

    app.state.restart.cancel()
    await app.state.restart.wait()


async def test_advance_notice_mentions_cancellability(client, app, notifier):
    await client.post("/api/restart", json=restart_body(notice_offsets=[60]))
    assert "キャンセルできます" in (await _advance_notice(notifier))["description"]

    app.state.restart.cancel()
    await app.state.restart.wait()


async def test_immediate_run_does_not_claim_a_lead_time(client, app, notifier):
    """予告ゼロ秒のときに「0秒後に」と書かない。"""
    await client.post("/api/restart", json=restart_body(notice_offsets=[0]))
    await app.state.restart.wait()

    assert (await _advance_notice(notifier))["title"] == "サーバーを再起動します"


async def test_advance_notice_mentions_pending_settings(client, app, notifier):
    """このタイミングで設定が反映されることを予告に含める。"""
    await client.put("/api/settings-ini/fields", json={
        "values": {"ExpRate": 2.0}, "when": "next_stop",
    })
    await client.post("/api/restart", json=restart_body(notice_offsets=[60]))

    assert "設定変更 1 件を反映します" in (await _advance_notice(notifier))["description"]

    app.state.restart.cancel()
    await app.state.restart.wait()
