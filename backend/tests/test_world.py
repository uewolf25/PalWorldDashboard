"""ワールドセーブの情報とバックアップ（Issue #19）。

Issue #5 の R-27 では「守備範囲外」としていた領域。実装したので、
壊れたバックアップを作らないこと・復元で元を失わないことを重点的に見る。
"""

from __future__ import annotations

import tarfile

import pytest

from app.world import WorldBackupError, WorldStore


@pytest.fixture
def save_dir(tmp_path):
    """それらしいセーブディレクトリを作る。"""
    d = tmp_path / "SaveGames"
    (d / "0" / "ABCDEF").mkdir(parents=True)
    (d / "0" / "ABCDEF" / "Level.sav").write_bytes(b"level-data")
    (d / "0" / "ABCDEF" / "LocalData.sav").write_bytes(b"local")
    (d / "0" / "ABCDEF" / "Players").mkdir()
    (d / "0" / "ABCDEF" / "Players" / "p1.sav").write_bytes(b"player-one")
    return d


@pytest.fixture
def store(save_dir, tmp_path):
    return WorldStore(save_dir, tmp_path / "world-backups", keep=3)


# ---- 参照 ------------------------------------------------------------------


def test_stats_reports_size_and_file_count(store):
    stats = store.stats()
    assert stats["exists"] is True
    assert stats["files"] == 3
    assert stats["size"] == len(b"level-data") + len(b"local") + len(b"player-one")
    assert stats["last_saved_at"] is not None


def test_stats_when_the_save_dir_is_missing(tmp_path):
    store = WorldStore(tmp_path / "nope", tmp_path / "b")
    stats = store.stats()
    assert stats["exists"] is False
    assert stats["size"] == 0
    assert stats["last_saved_at"] is None


def test_no_backups_at_first(store):
    assert store.list_backups() == []


# ---- 作成 ------------------------------------------------------------------


def test_create_makes_a_readable_archive(store, save_dir):
    backup = store.create()
    assert backup.name.startswith("world-")
    assert backup.name.endswith(".tar.gz")
    assert backup.size > 0

    with tarfile.open(backup.path, "r:gz") as tar:
        names = tar.getnames()
    assert f"{save_dir.name}/0/ABCDEF/Level.sav" in names
    assert f"{save_dir.name}/0/ABCDEF/Players/p1.sav" in names


def test_create_fails_without_a_save_dir(tmp_path):
    store = WorldStore(tmp_path / "nope", tmp_path / "b")
    with pytest.raises(WorldBackupError, match="セーブディレクトリが見つかりません"):
        store.create()


def test_backups_are_listed_newest_first(store):
    first = store.create()
    second = store.create()
    names = [b.name for b in store.list_backups()]
    assert names == sorted([first.name, second.name], reverse=True)


def test_same_second_backups_do_not_overwrite(store):
    a = store.create()
    b = store.create()
    assert a.name != b.name
    assert len(store.list_backups()) == 2


def test_old_backups_are_pruned(store):
    for _ in range(5):
        store.create()
    assert len(store.list_backups()) == 3     # keep=3


def test_pruning_never_deletes_the_backup_just_created(store):
    """同一秒に作ると `world-....tar.gz` と `world-...-1.tar.gz` が混ざる。

    名前順で間引くと `-1` の方が先頭に来て、作った直後に自分で消してしまう。
    """
    made = [store.create() for _ in range(5)]
    assert made[-1].path.is_file()
    assert made[-1].name in [b.name for b in store.list_backups()]


def test_a_failed_backup_does_not_leave_a_usable_file(store, monkeypatch):
    """途中で落ちたものを「使えるバックアップ」として残さない。"""
    import tarfile as tf

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tf.TarFile, "add", boom)
    with pytest.raises(WorldBackupError):
        store.create()
    assert store.list_backups() == []
    assert list(store.backup_dir.glob("*.part")) == []


# ---- 復元 ------------------------------------------------------------------


def test_restore_puts_the_contents_back(store, save_dir):
    backup = store.create()
    (save_dir / "0" / "ABCDEF" / "Level.sav").write_bytes(b"broken")

    store.restore(backup.name)
    assert (save_dir / "0" / "ABCDEF" / "Level.sav").read_bytes() == b"level-data"


def test_restore_removes_files_added_after_the_backup(store, save_dir):
    backup = store.create()
    (save_dir / "0" / "ABCDEF" / "extra.sav").write_bytes(b"x")

    store.restore(backup.name)
    assert not (save_dir / "0" / "ABCDEF" / "extra.sav").exists()


def test_restore_works_even_if_the_save_dir_is_gone(store, save_dir):
    import shutil

    backup = store.create()
    shutil.rmtree(save_dir)

    store.restore(backup.name)
    assert (save_dir / "0" / "ABCDEF" / "Level.sav").read_bytes() == b"level-data"


def test_restore_rejects_an_unknown_name(store):
    with pytest.raises(WorldBackupError, match="見つかりません"):
        store.restore("world-nope.tar.gz")


@pytest.mark.parametrize(
    "name",
    ["../etc/passwd", "world-../../x.tar.gz", "notabackup.txt", "world-x.zip"],
)
def test_resolve_rejects_paths_outside_the_backup_dir(store, name):
    with pytest.raises(WorldBackupError):
        store.resolve(name)


def test_restore_rejects_an_archive_that_escapes(store, tmp_path):
    """自分で作った tar しか置かない前提だが、置き換えられる可能性は潰す。"""
    store.backup_dir.mkdir(parents=True, exist_ok=True)
    evil = store.backup_dir / "world-20990101-000000.tar.gz"
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(outside, arcname="../../escaped.txt")

    with pytest.raises(WorldBackupError, match="不正なパス"):
        store.restore(evil.name)


def test_restore_rejects_an_archive_without_the_expected_dir(store):
    store.backup_dir.mkdir(parents=True, exist_ok=True)
    odd = store.backup_dir / "world-20990101-000001.tar.gz"
    with tarfile.open(odd, "w:gz") as tar:
        info = tarfile.TarInfo("somethingelse/file.txt")
        info.size = 0
        tar.addfile(info, None)

    with pytest.raises(WorldBackupError, match="中身が想定と違います"):
        store.restore(odd.name)


def test_restore_keeps_the_old_save_if_it_cannot_swap(store, save_dir, monkeypatch):
    """差し替えに失敗しても、元のセーブを消したままにしない。"""
    backup = store.create()

    original = __import__("pathlib").Path.rename
    calls = {"n": 0}

    def flaky(self, target):
        calls["n"] += 1
        # 1回目は退避（成功させる）、2回目の入れ替えで失敗させる
        if calls["n"] == 2:
            raise OSError("cross-device link")
        return original(self, target)

    monkeypatch.setattr(__import__("pathlib").Path, "rename", flaky)
    with pytest.raises(WorldBackupError, match="差し替えられませんでした"):
        store.restore(backup.name)

    assert (save_dir / "0" / "ABCDEF" / "Level.sav").read_bytes() == b"level-data"


def test_delete_removes_a_backup(store):
    backup = store.create()
    store.delete(backup.name)
    assert store.list_backups() == []


# ---- API -------------------------------------------------------------------


async def test_backups_endpoint_reports_the_save_state(client, settings):
    (settings.pal_save_dir / "0").mkdir(parents=True)
    (settings.pal_save_dir / "0" / "Level.sav").write_bytes(b"x" * 10)

    body = (await client.get("/api/world/backups")).json()
    assert body["exists"] is True
    assert body["size"] == 10
    assert body["backups"] == []


async def test_create_backup_endpoint(client, settings, mock_state):
    (settings.pal_save_dir / "0").mkdir(parents=True)
    (settings.pal_save_dir / "0" / "Level.sav").write_bytes(b"x" * 10)

    resp = await client.post("/api/world/backups")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backup"]["name"].startswith("world-")
    # 稼働中なので、固める前にワールド保存を挟んでいる
    assert body["world_saved_first"] is True
    assert mock_state.saves == 1


async def test_create_backup_without_a_save_dir_is_a_400(client):
    assert (await client.post("/api/world/backups")).status_code == 400


async def test_restore_is_refused_while_the_server_runs(client, settings):
    (settings.pal_save_dir / "0").mkdir(parents=True)
    (settings.pal_save_dir / "0" / "Level.sav").write_bytes(b"x")
    name = (await client.post("/api/world/backups")).json()["backup"]["name"]

    resp = await client.post(f"/api/world/backups/{name}/restore")
    assert resp.status_code == 409
    assert "停止" in resp.json()["detail"]


async def test_restore_works_when_the_server_is_stopped(client, settings, server_stopped):
    (settings.pal_save_dir / "0").mkdir(parents=True)
    (settings.pal_save_dir / "0" / "Level.sav").write_bytes(b"original")
    name = (await client.post("/api/world/backups")).json()["backup"]["name"]

    (settings.pal_save_dir / "0" / "Level.sav").write_bytes(b"broken")
    resp = await client.post(f"/api/world/backups/{name}/restore")
    assert resp.status_code == 200
    assert resp.json()["start_required"] is True
    assert (settings.pal_save_dir / "0" / "Level.sav").read_bytes() == b"original"


async def test_download_returns_the_archive(client, settings):
    (settings.pal_save_dir / "0").mkdir(parents=True)
    (settings.pal_save_dir / "0" / "Level.sav").write_bytes(b"x")
    name = (await client.post("/api/world/backups")).json()["backup"]["name"]

    resp = await client.get(f"/api/world/backups/{name}/download")
    assert resp.status_code == 200
    assert resp.content[:2] == b"\x1f\x8b"       # gzip のシグネチャ


async def test_download_rejects_an_unknown_name(client):
    assert (await client.get("/api/world/backups/world-nope.tar.gz/download")).status_code == 404


async def test_delete_endpoint(client, settings):
    (settings.pal_save_dir / "0").mkdir(parents=True)
    (settings.pal_save_dir / "0" / "Level.sav").write_bytes(b"x")
    name = (await client.post("/api/world/backups")).json()["backup"]["name"]

    assert (await client.delete(f"/api/world/backups/{name}")).status_code == 200
    assert (await client.get("/api/world/backups")).json()["backups"] == []
