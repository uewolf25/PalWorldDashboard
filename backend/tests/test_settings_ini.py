"""PalWorldSettings.ini の編集・バックアップのテスト。"""

from __future__ import annotations

import pytest

from app.settings_ini import apply_options, parse_options, split_options


def test_split_options_respects_quoted_commas():
    body = 'Difficulty=None,ServerName="My, Server",ExpRate=1.0'
    assert split_options(body) == ['Difficulty=None', 'ServerName="My, Server"', 'ExpRate=1.0']


def test_parse_options_reads_all_keys(ini_path):
    options = parse_options(ini_path.read_text())
    assert options["Difficulty"] == "None"
    assert options["ServerName"] == '"Test, Server"'
    assert options["ServerPlayerMaxNum"] == "32"


def test_apply_options_preserves_other_keys_and_adds_new(ini_path):
    text = ini_path.read_text()
    updated = apply_options(text, {"ExpRate": "2.000000", "PalCaptureRate": "1.500000"})
    options = parse_options(updated)
    assert options["ExpRate"] == "2.000000"
    assert options["PalCaptureRate"] == "1.500000"  # 新規キーは追加される
    assert options["ServerName"] == '"Test, Server"'  # 既存はそのまま
    assert updated.startswith("[/Script/Pal.PalGameWorldSettings]")


async def test_get_ini_returns_text_and_options(client, ini_path):
    resp = await client.get("/api/settings-ini")
    assert resp.status_code == 200
    body = resp.json()
    assert body["exists"] is True
    assert body["options"]["Difficulty"] == "None"
    assert body["backups"] == []


async def test_update_options_creates_backup(client, ini_path, settings):
    resp = await client.put("/api/settings-ini", json={"options": {"ExpRate": "3.000000"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["restart_required"] is True
    assert body["backup"].startswith("PalWorldSettings.")

    # 実ファイルに反映されている
    assert "ExpRate=3.000000" in ini_path.read_text()
    # バックアップには変更前の値が残っている
    backup = settings.backup_dir / body["backup"]
    assert "ExpRate=1.000000" in backup.read_text()


async def test_update_full_text(client, ini_path):
    new_text = (
        "[/Script/Pal.PalGameWorldSettings]\n"
        'OptionSettings=(Difficulty=Hard,ServerName="Renamed")\n'
    )
    resp = await client.put("/api/settings-ini", json={"text": new_text})
    assert resp.status_code == 200
    assert "Difficulty=Hard" in ini_path.read_text()


@pytest.mark.parametrize(
    "bad_text",
    [
        "これはiniではありません",
        "[/Script/Pal.PalGameWorldSettings]\n# OptionSettings 行が無い\n",
    ],
)
async def test_update_rejects_malformed_ini(client, ini_path, bad_text):
    """壊れた内容で上書きしてサーバを起動不能にしない。"""
    original = ini_path.read_text()
    resp = await client.put("/api/settings-ini", json={"text": bad_text})
    assert resp.status_code == 400
    assert ini_path.read_text() == original  # ファイルは無傷


async def test_update_requires_a_payload(client):
    resp = await client.put("/api/settings-ini", json={})
    assert resp.status_code == 400


async def test_restore_from_backup(client, ini_path):
    first = await client.put("/api/settings-ini", json={"options": {"ExpRate": "5.000000"}})
    backup_name = first.json()["backup"]
    assert "ExpRate=5.000000" in ini_path.read_text()

    resp = await client.post("/api/settings-ini/restore", json={"name": backup_name})
    assert resp.status_code == 200
    assert "ExpRate=1.000000" in ini_path.read_text()


async def test_restore_rejects_path_traversal(client, ini_path):
    resp = await client.post("/api/settings-ini/restore", json={"name": "../../etc/passwd"})
    assert resp.status_code == 400


async def test_backups_are_listed_newest_first(client):
    await client.put("/api/settings-ini", json={"options": {"ExpRate": "2.000000"}})
    await client.put("/api/settings-ini", json={"options": {"ExpRate": "4.000000"}})

    backups = (await client.get("/api/settings-ini")).json()["backups"]
    assert len(backups) == 2
    assert backups[0]["created_at"] >= backups[1]["created_at"]


async def test_missing_ini_reports_gracefully(settings, pal_client, notifier, tmp_path):
    """ini が無くても 500 にせず UI に「未検出」を返す。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    settings.pal_settings_ini = tmp_path / "nope.ini"
    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        body = (await c.get("/api/settings-ini")).json()
    assert body["exists"] is False
    assert body["text"] == ""
