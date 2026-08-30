"""Steam アップデートの検知（issue #30 / Phase 1）。

ここで守りたいのは2つ。

1. **サーバに触らないこと。** Phase 1 は検知と表示だけで、適用は当面まだ
   cron の update-watch.sh が持つ
2. **黙って止まらないこと。** 現行 cron の壊れ方（ロックが残って検知が
   素通りするのに何も通知されない）を、実装ごと持ち込まない
"""

from __future__ import annotations

import dataclasses

import pytest

from app.main import create_app
from app.notify import DiscordNotifier
from app.services import (
    LgsmService,
    SimulatedService,
    SystemdService,
    parse_check_update,
    supports_update,
)
from app.updates import UpdateWatcher

# LinuxGSM の実際の出力（色は落としたあとの形）
LGSM_AVAILABLE = """[ INFO ] Check Update pwserver: Update available:
Local build: 12345678
Remote build: 12349999"""
LGSM_UP_TO_DATE = "[  OK  ] Check Update pwserver: No update available"


# ---- 出力の判定 ------------------------------------------------------------


@pytest.mark.parametrize(
    "output, expected",
    [
        (LGSM_AVAILABLE, True),
        (LGSM_UP_TO_DATE, False),
        # 「更新なし」の行にも "update available" が含まれる。
        # 打ち消しを先に見ていないと、毎回「更新あり」になる
        ("no update available", False),
        ("UPDATE AVAILABLE", True),
    ],
)
def test_parse_check_update_reads_the_verdict(output, expected):
    assert parse_check_update(output) is expected


def test_unknown_output_is_not_rounded_down_to_no_update():
    """書式が変わったら「判定できない」を返すこと。

    「更新なし」に丸めると、LinuxGSM の出力が変わった日から永久に更新を
    見落とし、しかも誰も気づけない。
    """
    assert parse_check_update("Steam Servers are busy") is None


# ---- 能力の切り分け --------------------------------------------------------


def test_only_lgsm_can_update():
    """権限昇格が要る構成には、更新の口をそもそも作らない（issue #30）。"""
    assert supports_update(LgsmService("/tmp/pwserver")) is True
    assert supports_update(SimulatedService()) is True
    assert supports_update(SystemdService("palworld.service")) is False


# ---- LinuxGSM の check-update ---------------------------------------------


@pytest.fixture
def fake_lgsm(tmp_path):
    """出力と終了コードを差し替えられる pwserver の代役。"""

    def make(stdout: str, rc: int = 0) -> LgsmService:
        path = tmp_path / "pwserver"
        path.write_text(
            "#!/bin/sh\ncat <<'EOF'\n" + stdout + "\nEOF\nexit " + str(rc) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return LgsmService(str(path))

    return make


async def test_lgsm_detects_an_available_update(fake_lgsm):
    result = await fake_lgsm(LGSM_AVAILABLE).check_update()
    assert result.ok is True
    assert result.available is True
    assert "Remote build" in result.detail


async def test_lgsm_reports_up_to_date(fake_lgsm):
    result = await fake_lgsm(LGSM_UP_TO_DATE).check_update()
    assert result.ok is True
    assert result.available is False


async def test_lgsm_strips_the_terminal_colouring(fake_lgsm):
    """LinuxGSM は端末向けに色を付ける。素で grep すると判定を外す。"""
    coloured = "\x1b[0;32m[  OK  ]\x1b[0m Check Update pwserver: No update available"
    result = await fake_lgsm(coloured).check_update()
    assert result.ok is True
    assert result.available is False
    assert "\x1b[" not in result.detail


async def test_lgsm_failure_is_not_silence(fake_lgsm):
    result = await fake_lgsm("Steam servers are down", rc=1).check_update()
    assert result.ok is False
    assert result.error


async def test_lgsm_unparseable_output_is_a_failure(fake_lgsm):
    result = await fake_lgsm("something else entirely").check_update()
    assert result.ok is False
    assert result.available is False
    assert "判定できません" in result.error


async def test_lgsm_dry_run_does_not_claim_up_to_date(tmp_path):
    """dry_run では Steam に聞いていない。「更新なし」と断定させない。"""
    service = LgsmService(str(tmp_path / "pwserver"), dry_run=True)
    result = await service.check_update()
    assert result.ok is True
    assert result.available is False


# ---- 検知の状態と通知 ------------------------------------------------------


@pytest.fixture
def watcher(tmp_path):
    service = SimulatedService()
    notifier = DiscordNotifier()
    w = UpdateWatcher(service, notifier, store_path=tmp_path / "update-state.json")
    return w, service, notifier


async def test_detection_notifies_once(watcher):
    w, service, notifier = watcher

    await w.check()
    assert w.state.available is False
    assert notifier.sent == []

    service.update_available = True
    await w.check()
    assert w.state.available is True
    assert len(notifier.sent) == 1
    assert "アップデートを検知" in notifier.sent[0]["title"]

    # 更新が出ている間、10分おきに同じ通知を流さない
    await w.check()
    await w.check()
    assert len(notifier.sent) == 1


async def test_a_later_update_notifies_again(watcher):
    """適用されたら、次の更新はまた通知できる状態に戻すこと。"""
    w, service, notifier = watcher

    service.update_available = True
    await w.check()
    service.update_available = False
    await w.check()
    assert w.state.available is False
    assert w.state.notified_at is None

    service.update_available = True
    await w.check()
    assert len(notifier.sent) == 2


async def test_failure_keeps_the_previous_verdict(watcher):
    """確かめられなかっただけで、出ている更新が消えたわけではない。"""
    w, service, _ = watcher

    service.update_available = True
    await w.check()
    service.fail_check_update = True
    await w.check()

    assert w.state.available is True
    assert w.state.last_error


async def test_repeated_failures_are_reported(tmp_path):
    """黙って検知が止まるのを作らない（現行 cron のロック残留と同じ壊れ方）。"""
    service = SimulatedService()
    notifier = DiscordNotifier()
    w = UpdateWatcher(
        service, notifier, store_path=tmp_path / "u.json", fail_alert_threshold=3
    )
    service.fail_check_update = True

    await w.check()
    await w.check()
    assert notifier.sent == []       # 1〜2回は Steam 側の一時不調でも起きる

    await w.check()
    assert len(notifier.sent) == 1
    assert notifier.sent[0]["level"] == "warn"

    # 失敗が続いても通知は積み上げない
    await w.check()
    assert len(notifier.sent) == 1

    # 復帰したら分かるようにする
    service.fail_check_update = False
    await w.check()
    assert len(notifier.sent) == 2
    assert "復帰" in notifier.sent[1]["title"]
    assert w.state.fail_streak == 0


async def test_state_survives_a_restart(tmp_path):
    """管理ツールを再起動しても、同じ更新の通知を繰り返さない。"""
    store = tmp_path / "update-state.json"
    service = SimulatedService()
    service.update_available = True
    first = UpdateWatcher(service, DiscordNotifier(), store_path=store)
    await first.check()
    assert store.is_file()

    notifier = DiscordNotifier()
    second = UpdateWatcher(service, notifier, store_path=store)
    assert second.state.available is True
    await second.check()
    assert notifier.sent == []


async def test_unsupported_backend_stays_quiet(tmp_path):
    w = UpdateWatcher(
        SystemdService("palworld.service"),
        DiscordNotifier(),
        store_path=tmp_path / "u.json",
    )
    assert w.supported is False
    await w.check()
    assert w.state.checked_at is None
    w.start()
    assert w._task is None


async def test_the_loop_skips_while_a_sequence_is_running(tmp_path):
    """停止/起動の最中に pwserver をもう1つ走らせない。"""
    import asyncio

    service = SimulatedService()
    w = UpdateWatcher(
        service, DiscordNotifier(), interval=0.01, store_path=tmp_path / "u.json"
    )
    busy = True
    w.set_busy_probe(lambda: busy)

    w.start()
    await asyncio.sleep(0.05)
    assert w.state.checked_at is None

    busy = False
    await asyncio.sleep(0.05)
    assert w.state.checked_at is not None
    await w.stop()


# ---- API -------------------------------------------------------------------


async def test_api_reports_the_update_state(client, app):
    app.state.service.update_available = True
    res = await client.post("/api/update/check")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert body["supports_update"] is True

    res = await client.get("/api/update")
    assert res.json()["available"] is True


async def test_api_update_is_readable_while_the_server_is_down(client, server_stopped):
    """ゲームサーバに問い合わせないので、落ちていても答えられること。"""
    res = await client.get("/api/update")
    assert res.status_code == 200
    assert res.json()["supports_update"] is True


async def test_config_exposes_the_capability(client):
    assert (await client.get("/api/config")).json()["supports_update"] is True


async def test_unsupported_backend_hides_the_feature(settings, pal_client, notifier):
    """更新を扱えない構成では、画面ごと隠せるように false を返す。"""
    import httpx

    cfg = dataclasses.replace(settings, pal_service_backend="systemd")
    app = create_app(cfg, pal_client=pal_client, notifier=notifier, start_background=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as c:
        assert (await c.get("/api/config")).json()["supports_update"] is False
        assert (await c.get("/api/update")).json()["supports_update"] is False
        # 能力の無い構成に操作の口を残さない
        assert (await c.post("/api/update/check")).status_code == 501


async def test_check_updates_nothing_on_the_server(client, app, mock_state):
    """Phase 1 は読むだけ。サーバのプロセスにも設定にも触らない。"""
    before = (mock_state.running, mock_state.saves, len(mock_state.announcements))
    await client.post("/api/update/check")
    assert (mock_state.running, mock_state.saves, len(mock_state.announcements)) == before


async def test_check_result_shape(client):
    body = (await client.get("/api/update")).json()
    for key in ("supports_update", "available", "checked_at", "detail",
                "last_error", "scheduled", "interval", "checking"):
        assert key in body
    # 予約の自動生成は Phase 2。いまは常に空
    assert body["scheduled"] is None
