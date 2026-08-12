"""プレイヤーの入退室と滞在時間（Issue #19）。

Palworld の REST API に入退室イベントは無い。取れるのは一覧の
スナップショットだけなので、差分から JOIN / LEFT を組み立てている。
「取りこぼしがありうる」ことも含めて仕様なので、そこもテストする。
"""

from __future__ import annotations

import json
import time

from app.presence import PresenceTracker


def player(uid: str, name: str) -> dict:
    return {"userId": uid, "name": name}


# ---- 差分の組み立て --------------------------------------------------------


def test_first_observation_is_not_a_join(tmp_path):
    """起動前から繋がっていた人を「いま入った」ことにしない。"""
    tracker = PresenceTracker(tmp_path / "p.json")
    events = tracker.observe([player("a", "Sora"), player("b", "Kenta")])
    assert events == []
    # 見えていることは取り込む
    assert tracker.since("a") is not None


def test_join_is_recorded_after_the_first_observation(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([player("a", "Sora")])

    events = tracker.observe([player("a", "Sora"), player("b", "Kenta")])
    assert len(events) == 1
    assert events[0].kind == "join"
    assert events[0].userid == "b"
    assert events[0].name == "Kenta"


def test_leave_is_recorded_with_the_stay_time(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([player("a", "Sora")])
    tracker.observe([player("a", "Sora"), player("b", "Kenta")])

    events = tracker.observe([player("a", "Sora")])
    assert len(events) == 1
    assert events[0].kind == "leave"
    assert events[0].userid == "b"
    assert events[0].stay_seconds is not None
    assert events[0].stay_seconds >= 0


def test_leaving_someone_seen_from_the_start_has_no_stay_time_of_its_own(tmp_path):
    """初回観測の人も since は入るので滞在時間は出る（起動時点からの値）。"""
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([player("a", "Sora")])
    events = tracker.observe([])
    assert events[0].kind == "leave"
    assert events[0].stay_seconds is not None


def test_no_events_when_nothing_changes(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([player("a", "Sora")])
    assert tracker.observe([player("a", "Sora")]) == []


def test_players_without_an_id_are_ignored(tmp_path):
    """userId が取れない相手を数えると、毎回 JOIN/LEFT が出続ける。"""
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([player("a", "Sora")])
    assert tracker.observe([player("a", "Sora"), {"name": "?"}]) == []


def test_playerid_is_used_when_userid_is_missing(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([])
    events = tracker.observe([{"playerId": "P1", "name": "Yuki"}])
    assert events[0].userid == "P1"


def test_a_renamed_player_is_not_treated_as_a_new_one(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([player("a", "Sora")])
    assert tracker.observe([player("a", "Sora2")]) == []


# ---- 滞在時間 --------------------------------------------------------------


def test_stay_seconds_counts_from_the_join(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([])
    tracker.observe([player("a", "Sora")])
    time.sleep(0.05)
    assert tracker.stay_seconds("a") >= 0.05


def test_stay_seconds_is_none_for_someone_not_connected(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json")
    assert tracker.stay_seconds("nobody") is None


def test_annotate_adds_the_stay_time(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([player("a", "Sora")])
    out = tracker.annotate([player("a", "Sora")])
    assert out[0]["name"] == "Sora"
    assert out[0]["stay_seconds"] is not None
    assert out[0]["joined_at"] is not None


# ---- キック/BAN ------------------------------------------------------------


def test_forget_closes_the_session_immediately(tmp_path):
    """次の観測を待たずに退出として確定させる。"""
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([player("a", "Sora")])

    tracker.forget("a")
    assert tracker.stay_seconds("a") is None
    assert tracker.list()[0]["kind"] == "leave"

    # もう居ないので、次の観測で二重に LEFT を出さない
    assert tracker.observe([]) == []


def test_forget_is_a_noop_for_someone_unknown(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.forget("nobody")
    assert len(tracker) == 0


# ---- 履歴 ------------------------------------------------------------------


def test_events_are_newest_first(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([])
    tracker.observe([player("a", "Sora")])
    tracker.observe([player("a", "Sora"), player("b", "Kenta")])

    events = tracker.list()
    assert [e["userid"] for e in events] == ["b", "a"]


def test_events_can_be_filtered_by_kind(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([])
    tracker.observe([player("a", "Sora")])
    tracker.observe([])

    assert [e["kind"] for e in tracker.list(kind="join")] == ["join"]
    assert [e["kind"] for e in tracker.list(kind="leave")] == ["leave"]


def test_history_is_capped(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json", limit=3)
    tracker.observe([])
    for i in range(5):
        tracker.observe([player(f"u{i}", f"P{i}")])
    assert len(tracker) == 3


def test_simultaneous_join_and_leave_are_both_recorded(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([player("a", "Sora")])
    events = tracker.observe([player("b", "Kenta")])
    kinds = sorted(e.kind for e in events)
    assert kinds == ["join", "leave"]


# ---- 永続化 ----------------------------------------------------------------


def test_history_survives_a_restart(tmp_path):
    path = tmp_path / "p.json"
    tracker = PresenceTracker(path)
    tracker.observe([])
    tracker.observe([player("a", "Sora")])

    revived = PresenceTracker(path)
    assert [e["userid"] for e in revived.list()] == ["a"]


def test_stay_time_keeps_counting_across_a_restart(tmp_path):
    """管理ツールを再起動しただけで滞在時間が 0 に戻らないこと。"""
    path = tmp_path / "p.json"
    tracker = PresenceTracker(path)
    tracker.observe([player("a", "Sora")])
    since = tracker.since("a")

    revived = PresenceTracker(path)
    assert revived.since("a") == since


def test_a_restart_does_not_replay_joins(tmp_path):
    """復元直後の観測で、居続けている人に JOIN を出さない。"""
    path = tmp_path / "p.json"
    tracker = PresenceTracker(path)
    tracker.observe([player("a", "Sora")])

    revived = PresenceTracker(path)
    assert revived.observe([player("a", "Sora")]) == []


def test_broken_store_is_ignored(tmp_path):
    path = tmp_path / "p.json"
    path.write_text("{壊れている", encoding="utf-8")
    tracker = PresenceTracker(path)
    assert len(tracker) == 0


def test_unwritable_store_does_not_raise(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("x")
    tracker = PresenceTracker(blocked / "p.json")
    tracker.observe([player("a", "Sora")])   # 例外を投げない
    assert tracker.since("a") is not None


def test_clear_empties_the_history_but_keeps_who_is_online(tmp_path):
    tracker = PresenceTracker(tmp_path / "p.json")
    tracker.observe([])
    tracker.observe([player("a", "Sora")])

    assert tracker.clear() == 1
    assert len(tracker) == 0
    assert tracker.since("a") is not None


def test_store_is_valid_json(tmp_path):
    path = tmp_path / "p.json"
    tracker = PresenceTracker(path)
    tracker.observe([player("a", "Sora")])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "online" in data and "events" in data


# ---- API -------------------------------------------------------------------


async def test_players_endpoint_returns_the_stay_time(client):
    body = (await client.get("/api/players")).json()
    assert body["count"] == 3
    for p in body["players"]:
        assert "stay_seconds" in p
        assert "joined_at" in p


async def test_events_endpoint_starts_empty(client):
    body = (await client.get("/api/players/events")).json()
    assert body["events"] == []
    assert body["total"] == 0


async def test_events_endpoint_records_a_leave_after_a_kick(client, mock_state):
    await client.get("/api/players")                    # 顔ぶれを取り込む
    userid = mock_state.players[0]["userId"]

    resp = await client.post("/api/players/kick", json={"userid": userid, "message": "x"})
    assert resp.status_code == 200

    body = (await client.get("/api/players/events")).json()
    assert body["events"][0]["kind"] == "leave"
    assert body["events"][0]["userid"] == userid
    assert body["events"][0]["kind_label"] == "退出"


async def test_events_endpoint_filters_by_kind(client, mock_state):
    await client.get("/api/players")
    await client.post("/api/players/kick", json={"userid": mock_state.players[0]["userId"]})

    joins = (await client.get("/api/players/events?kind=join")).json()
    leaves = (await client.get("/api/players/events?kind=leave")).json()
    assert joins["events"] == []
    assert len(leaves["events"]) == 1


async def test_events_endpoint_rejects_an_unknown_kind(client):
    assert (await client.get("/api/players/events?kind=nope")).status_code == 422
