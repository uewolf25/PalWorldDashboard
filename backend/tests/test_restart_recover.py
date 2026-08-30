"""管理ツールが不意に停止させられたときの守り (issue #41)。

OS のパッケージ更新のあと needrestart が `systemctl restart` を打つため、
管理ツールは自分の意思と無関係に、数秒のうちに2〜3回落とされることがある。

重点:
  1. サーバを止めた直後に消えていたら、次の起動で起こし直す
  2. **サーバに触っていない段階での中断では、勝手に起こさない**
     （落ちているのは別の理由なので、こちらの判断で上書きしない）
  3. 同じ中断を二度拾わない
  4. 停止時は、危険な段階のシーケンスが終わるまで待つ
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.restart import RestartManager
from app.services import CommandResult

from test_restart import restart_body


class FakeAnnouncer:
    def __init__(self) -> None:
        self.discord: list[tuple[str, str, str]] = []
        self.ingame: list[str] = []

    async def discord_only(
        self, title: str, detail: str = "", *, source: str = "system",
        level: str = "info", reason: str = "",
    ) -> None:
        self.discord.append((title, detail, level))

    async def send(self, text: str, *, source: str = "system", reason: str = ""):
        self.ingame.append(text)
        return SimpleNamespace(ok=True)


class FakeService:
    label = "テスト用サービス"

    def __init__(
        self, *, start_ok: bool = True, active: bool = False, start_delay: float = 0.0
    ) -> None:
        self.calls: list[str] = []
        self.start_ok = start_ok
        self.active = active
        self.start_delay = start_delay

    async def preflight(self) -> CommandResult:
        self.calls.append("preflight")
        return CommandResult(True, 0, "active", "")

    async def start(self) -> CommandResult:
        self.calls.append("start")
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        if not self.start_ok:
            return CommandResult(False, 1, "", "起動コマンドが通りません")
        self.active = True
        return CommandResult(True, 0, "started", "")

    async def stop(self) -> CommandResult:
        self.calls.append("stop")
        self.active = False
        return CommandResult(True, 0, "stopped", "")

    async def restart(self) -> CommandResult:
        self.calls.append("restart")
        self.active = True
        return CommandResult(True, 0, "restarted", "")

    async def is_active(self) -> bool | None:
        return self.active

    async def aclose(self) -> None:
        return None


class FakeHealth:
    """REST API の無い構成として、プロセスの生死だけで判定する。"""

    def __init__(self, service: FakeService) -> None:
        self._service = service

    async def running(self) -> bool:
        return bool(await self._service.is_active())

    async def api_reachable(self) -> bool:
        return bool(await self._service.is_active())

    async def wait_until_up(self, timeout: float) -> float | None:
        return 0.5 if await self.running() else None

    async def wait_until_down(self, timeout: float) -> float | None:
        return None if await self.running() else 0.5


def make_manager(tmp_path: Path, service: FakeService, **overrides) -> RestartManager:
    announcer = FakeAnnouncer()
    manager = RestartManager(
        SimpleNamespace(),  # recover() は Palworld API を触らない
        announcer,
        service,
        health=FakeHealth(service),
        store_path=tmp_path / "restart-state.json",
        startup_timeout=0.05,
        **overrides,
    )
    manager.test_announcer = announcer  # type: ignore[attr-defined]
    return manager


def write_record(path: Path, **overrides) -> Path:
    """中断された（＝終端していない）進行状態を作る。"""
    now = time.time()
    record = {
        "phase": "restarting",
        "mode": "restart",
        "reason": "定期メンテ",
        "schedule_id": None,
        "started_at": now - 300,
        "finished_at": None,
        "message": "サーバを再起動しています",
        "steps": [
            {"name": "world_save", "ok": True, "detail": "", "ts": now - 20},
            {"name": "shutdown_api", "ok": True, "detail": "", "ts": now - 15},
            {"name": "service_stop", "ok": True, "detail": "", "ts": now - 10},
        ],
        "saved_at": now - 10,
    }
    record.update(overrides)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return path


# ---- 復旧 ---------------------------------------------------------------


async def test_recover_starts_the_server_cut_off_between_stop_and_start(tmp_path):
    """本丸。停止まで進んだところで消されたなら、起こし直すこと。

    ここを拾えないと、ゲームサーバは誰にも気づかれないまま落ちたままになる。
    """
    service = FakeService(active=False)
    manager = make_manager(tmp_path, service)
    write_record(manager.store_path)

    result = await manager.recover()

    assert result is not None
    assert result["outcome"] == "rescued"
    assert "start" in service.calls
    assert service.active is True
    assert manager.status.phase == "done"
    assert manager.in_progress is False


async def test_recover_does_not_start_the_server_while_still_announcing(tmp_path):
    """予告中の中断ではサーバに触っていない。落ちているのは別の理由。

    ここで起こすと、操作者が意図して止めたサーバを管理ツールの都合で
    起こしてしまう。報せるだけにする。
    """
    service = FakeService(active=False)
    manager = make_manager(tmp_path, service)
    write_record(manager.store_path, phase="announcing", steps=[])

    result = await manager.recover()

    assert result["outcome"] == "not_touched"
    assert service.calls == []
    title, detail, level = manager.test_announcer.discord[-1]
    assert level == "crit"
    assert "停止操作まで進んでいません" in detail


async def test_recover_does_not_start_the_server_while_saving(tmp_path):
    """保存中の中断も同じ。shutdown API はまだ投げていない。"""
    service = FakeService(active=False)
    manager = make_manager(tmp_path, service)
    write_record(manager.store_path, phase="saving", steps=[])

    result = await manager.recover()

    assert result["outcome"] == "not_touched"
    assert service.calls == []


async def test_recover_leaves_an_intended_stop_stopped(tmp_path):
    """停止シーケンスの中断では、落ちているのが正しい姿。"""
    service = FakeService(active=False)
    manager = make_manager(tmp_path, service)
    write_record(manager.store_path, mode="stop")

    result = await manager.recover()

    assert result["outcome"] == "stopped_as_intended"
    assert service.calls == []


async def test_recover_does_nothing_when_the_server_is_already_running(tmp_path):
    """再起動が実際には完了していた場合に、二重に起こしに行かないこと。"""
    service = FakeService(active=True)
    manager = make_manager(tmp_path, service)
    write_record(manager.store_path)

    result = await manager.recover()

    assert result["outcome"] == "running"
    assert service.calls == []


async def test_recover_ignores_a_sequence_that_finished(tmp_path):
    """終端状態で終わっている記録は、そもそも中断ではない。"""
    service = FakeService(active=True)
    manager = make_manager(tmp_path, service)
    write_record(manager.store_path, phase="done", finished_at=time.time())

    assert await manager.recover() is None
    assert manager.test_announcer.discord == []


async def test_recover_ignores_a_missing_or_broken_record(tmp_path):
    service = FakeService(active=False)
    manager = make_manager(tmp_path, service)

    assert await manager.recover() is None

    manager.store_path.write_text("{壊れた", encoding="utf-8")
    assert await manager.recover() is None
    assert service.calls == []


async def test_recover_only_reports_an_old_interruption(tmp_path):
    """何時間も前の中断を今さら起こさないこと。

    その間に人が意図して止めた可能性があり、踏み潰すと事故になる。
    """
    service = FakeService(active=False)
    manager = make_manager(tmp_path, service, recover_max_age=3600.0)
    write_record(manager.store_path, saved_at=time.time() - 7200)

    result = await manager.recover()

    assert result["outcome"] == "too_old"
    assert service.calls == []
    assert manager.test_announcer.discord[-1][2] == "crit"


async def test_recover_reports_a_failed_rescue(tmp_path):
    service = FakeService(start_ok=False, active=False)
    manager = make_manager(tmp_path, service)
    write_record(manager.store_path)

    result = await manager.recover()

    assert result["outcome"] == "rescue_failed"
    assert manager.status.phase == "failed"
    assert manager.test_announcer.discord[-1][2] == "crit"


async def test_recover_runs_once_per_interruption(tmp_path):
    """復旧したら記録を終端させ、次の起動で同じ中断を拾わないこと。"""
    service = FakeService(active=False)
    manager = make_manager(tmp_path, service)
    write_record(manager.store_path)

    assert (await manager.recover())["outcome"] == "rescued"
    service.calls.clear()

    assert await manager.recover() is None
    assert service.calls == []


async def test_recover_does_not_start_when_process_control_is_off(tmp_path):
    """プロセス制御をしない構成では、そもそも起こす手段が無い。"""
    service = FakeService(active=False)
    manager = make_manager(tmp_path, service, control_process=False)
    write_record(manager.store_path)

    result = await manager.recover()

    assert result["outcome"] == "no_control"
    assert service.calls == []


async def test_the_record_survives_until_the_server_is_actually_started(tmp_path):
    """起動コマンドが通るまでは、ディスク上の中断記録を消さないこと。

    needrestart は数秒のうちに2〜3回落としてくる。先に「処理済み」と
    書いてしまうと、復旧の最中に殺された回で記録だけ消えて、
    ゲームサーバが落ちたまま次の起動が素通りする。
    """
    service = FakeService(active=False)
    manager = make_manager(tmp_path, service)
    write_record(manager.store_path)

    seen: dict[str, object] = {}
    original_start = service.start

    async def watched_start():
        # 起動コマンドを打っている最中＝まだやり直しが要る時点
        seen["record"] = manager._load_record()
        return await original_start()

    service.start = watched_start  # type: ignore[method-assign]

    await manager.recover()

    assert seen["record"] is not None
    assert seen["record"]["phase"] == "restarting"
    # 起動が通ったので、ここで初めて終端する
    assert manager._load_record() is None


async def test_a_recovery_cut_short_leaves_the_work_for_the_next_boot(tmp_path):
    """復旧の途中でもう一度落とされたら、次の起動がやり直せること。"""
    service = FakeService(active=False, start_delay=5.0)
    manager = make_manager(tmp_path, service)
    write_record(manager.store_path)

    task = asyncio.create_task(manager.recover())
    for _ in range(200):
        if "start" in service.calls:
            break
        await asyncio.sleep(0.005)
    else:  # pragma: no cover - 起動コマンドまで進まなかった
        raise AssertionError("起動コマンドまで進みませんでした")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = manager._load_record()
    assert record is not None and record["phase"] == "restarting"


# ---- 永続化 -------------------------------------------------------------


async def test_sequence_state_reaches_disk_while_running(client, app):
    """進行中の状態がディスクにも出ていること。

    メモリ上にしか無いと、プロセスごと消えた瞬間に手がかりが無くなる。
    """
    store = app.state.restart.store_path
    await client.post("/api/restart", json=restart_body(notice_offsets=[30]))

    record = json.loads(store.read_text(encoding="utf-8"))
    assert record["phase"] == "announcing"
    assert record["mode"] == "restart"

    app.state.restart.cancel()
    await app.state.restart.wait()

    record = json.loads(store.read_text(encoding="utf-8"))
    assert record["phase"] == "cancelled"
    # 終端しているので、次の起動では拾われない
    assert app.state.restart._load_record() is None


async def test_completed_sequence_leaves_nothing_to_recover(client, app):
    await client.post("/api/restart", json=restart_body())
    await app.state.restart.wait()

    assert app.state.restart.status.phase == "done"
    assert app.state.restart._load_record() is None


# ---- 停止時の drain -----------------------------------------------------


async def test_drain_cancels_a_pending_announcement(client, app):
    """予告中に管理ツールが止まるなら、予告は取り消してから落ちる。

    残したまま消えると、ゲーム内には予告だけ流れて何も起きない。
    """
    await client.post("/api/restart", json=restart_body(notice_offsets=[30]))

    assert await app.state.restart.drain(timeout=2.0) == "cancelled"
    assert app.state.restart.status.phase == "cancelled"
    assert app.state.restart.in_progress is False


async def test_drain_is_a_no_op_when_nothing_is_running(app):
    assert await app.state.restart.drain(timeout=0.1) == "idle"


class SlowService:
    """再起動コマンドが返るまで時間のかかるサービス。"""

    label = "のろいサービス"

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls: list[str] = []

    async def preflight(self) -> CommandResult:
        return CommandResult(True, 0, "active", "")

    async def restart(self) -> CommandResult:
        self.calls.append("restart")
        await asyncio.sleep(self.delay)
        return CommandResult(True, 0, "restarted", "")

    async def start(self) -> CommandResult:
        self.calls.append("start")
        return CommandResult(True, 0, "started", "")

    async def stop(self) -> CommandResult:
        self.calls.append("stop")
        return CommandResult(True, 0, "stopped", "")

    async def is_active(self) -> bool | None:
        return True

    async def aclose(self) -> None:
        return None


async def _wait_until_touching_the_server(manager, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if manager.status.phase == "restarting":
            return
        await asyncio.sleep(0.005)
    raise AssertionError("サーバ操作の段階まで進みませんでした")


async def test_drain_waits_for_a_sequence_that_is_touching_the_server(client, app):
    """サーバを操作している最中なら、終わるまで待ってから落ちること。

    ここを待たずに消えると、停止と起動の隙間で殺されたときに
    ゲームサーバが落ちたまま残る。
    """
    app.state.restart._service = SlowService(0.3)
    await client.post("/api/restart", json=restart_body())
    await _wait_until_touching_the_server(app.state.restart)

    assert await app.state.restart.drain(timeout=5.0) == "finished"
    assert app.state.restart.status.phase == "done"


async def test_drain_gives_up_but_leaves_the_record_for_the_next_boot(client, app):
    """待ちきれなくても、進行状態はディスクに残して次の起動に渡すこと。"""
    app.state.restart._service = SlowService(3.0)
    await client.post("/api/restart", json=restart_body())
    await _wait_until_touching_the_server(app.state.restart)

    assert await app.state.restart.drain(timeout=0.1) == "timeout"
    # 中断された記録として読めること（＝次の起動で recover が拾える）
    assert app.state.restart._load_record()["phase"] == "restarting"

    # 後始末: 走らせたままにしない
    app.state.restart.release("テストの後始末")
