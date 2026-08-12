"""ワールドセーブの情報とバックアップ。

ini のバックアップ（settings_ini.py）とは別物。あちらは数KBのテキスト1枚だが、
こちらは数百MBのディレクトリを丸ごと固める。性質が違うので分けている。

**Issue #5 の R-27 では「ワールドセーブのバックアップは守備範囲外」としていた。**
Issue #19 の画面設計に入ったため実装したが、注意点は変わっていない。

- **稼働中のセーブは一貫していない。** ゲームが書いている最中のファイルを
  固めると壊れたバックアップができる。取る前にワールド保存を挟み、
  それでも「停止中に取るのが確実」であることは画面に出す
- **復元は停止中しかできない。** 稼働中に差し替えても、ゲームが持っている
  メモリ上の状態で上書きされて消える。ini と同じ理屈
- **tar 化は重い。** 数百MBを固める間イベントループを止めないよう、
  呼び出し側は必ずスレッドに逃がすこと（run_in_executor / to_thread）
"""

from __future__ import annotations

import logging
import shutil
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PREFIX = "world-"
SUFFIX = ".tar.gz"


class WorldBackupError(Exception):
    """バックアップ/復元に失敗した。画面にそのまま出す文言を持たせる。"""


@dataclass
class WorldBackup:
    name: str
    path: Path
    size: int
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size": self.size,
            "created_at": self.created_at,
        }


def _dir_stats(path: Path) -> tuple[int, int, float | None]:
    """ディレクトリ配下の合計サイズ・ファイル数・最終更新時刻。"""
    total = 0
    count = 0
    newest: float | None = None
    for item in path.rglob("*"):
        try:
            st = item.stat()
        except OSError:
            continue
        if not item.is_file():
            continue
        total += st.st_size
        count += 1
        if newest is None or st.st_mtime > newest:
            newest = st.st_mtime
    return total, count, newest


class WorldStore:
    """セーブディレクトリの参照とバックアップ。"""

    def __init__(self, save_dir: Path, backup_dir: Path, keep: int = 10) -> None:
        self.save_dir = Path(save_dir)
        self.backup_dir = Path(backup_dir)
        self.keep = keep

    # ---- 参照 ---------------------------------------------------------

    def exists(self) -> bool:
        return self.save_dir.is_dir()

    def stats(self) -> dict[str, Any]:
        """画面のヘッダに出す情報。

        「最終保存」はセーブディレクトリの最終更新時刻で代用している。
        Palworld は保存した時刻を教えてくれないので、これが一番近い。
        """
        if not self.exists():
            return {
                "exists": False,
                "path": str(self.save_dir),
                "size": 0,
                "files": 0,
                "last_saved_at": None,
            }
        size, files, newest = _dir_stats(self.save_dir)
        return {
            "exists": True,
            "path": str(self.save_dir),
            "size": size,
            "files": files,
            "last_saved_at": (
                datetime.fromtimestamp(newest).isoformat(timespec="seconds") if newest else None
            ),
        }

    def list_backups(self) -> list[WorldBackup]:
        if not self.backup_dir.is_dir():
            return []
        out: list[WorldBackup] = []
        for path in sorted(self.backup_dir.glob(f"{PREFIX}*{SUFFIX}"), reverse=True):
            try:
                st = path.stat()
            except OSError:
                continue
            out.append(
                WorldBackup(
                    name=path.name,
                    path=path,
                    size=st.st_size,
                    created_at=datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                )
            )
        return out

    def resolve(self, name: str) -> Path:
        """バックアップ名から実ファイルを引く。

        名前は画面から来る。ディレクトリ外に出られないよう、
        バックアップ置き場の直下にあることを必ず確かめる。
        """
        candidate = self.backup_dir / name
        if (
            candidate.parent.resolve() != self.backup_dir.resolve()
            or not candidate.is_file()
            or not name.startswith(PREFIX)
            or not name.endswith(SUFFIX)
        ):
            raise WorldBackupError(f"バックアップが見つかりません: {name}")
        return candidate

    # ---- 作成 ---------------------------------------------------------

    def create(self) -> WorldBackup:
        """セーブディレクトリを固める。**重いのでスレッドから呼ぶこと。**"""
        if not self.exists():
            raise WorldBackupError(f"セーブディレクトリが見つかりません: {self.save_dir}")

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.backup_dir / f"{PREFIX}{stamp}{SUFFIX}"
        n = 1
        while dest.exists():
            dest = self.backup_dir / f"{PREFIX}{stamp}-{n}{SUFFIX}"
            n += 1

        # 途中で失敗したものを「使えるバックアップ」として残さない。
        # 一時ファイルに書き切ってから名前を付ける
        tmp = dest.with_suffix(".part")
        try:
            with tarfile.open(tmp, "w:gz") as tar:
                tar.add(self.save_dir, arcname=self.save_dir.name)
            tmp.replace(dest)
        except (OSError, tarfile.TarError) as exc:
            tmp.unlink(missing_ok=True)
            raise WorldBackupError(f"バックアップを作成できませんでした: {exc}") from exc

        self._prune(keep_always=dest)
        st = dest.stat()
        return WorldBackup(
            name=dest.name,
            path=dest,
            size=st.st_size,
            created_at=datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        )

    def _prune(self, keep_always: Path | None = None) -> None:
        """古い方から間引く。

        名前順ではなく更新時刻順で並べる。同じ秒に作ったものは
        `world-....tar.gz` と `world-...-1.tar.gz` が混ざり、名前順だと
        後から作った方が先頭に来てしまう（`-` < `.`）。それだと
        いま作ったばかりのバックアップを自分で消すことになる。
        """
        def sort_key(p: Path) -> tuple[float, str]:
            try:
                return (p.stat().st_mtime, p.name)
            except OSError:
                return (0.0, p.name)

        backups = sorted(self.backup_dir.glob(f"{PREFIX}*{SUFFIX}"), key=sort_key)
        for path in backups[: max(0, len(backups) - self.keep)]:
            if keep_always is not None and path == keep_always:
                continue
            try:
                path.unlink()
            except OSError as exc:  # pragma: no cover - 権限まわりの保険
                logger.warning("ワールドバックアップの削除に失敗: %s (%s)", path, exc)

    def delete(self, name: str) -> None:
        self.resolve(name).unlink()

    # ---- 復元 ---------------------------------------------------------

    def restore(self, name: str) -> None:
        """バックアップでセーブディレクトリを置き換える。

        **サーバ停止中にのみ呼ぶこと**（判定は呼び出し側）。

        いきなり消して展開すると、展開に失敗したときに何も残らない。
        一時ディレクトリへ展開して中身を確かめてから入れ替える。
        """
        archive = self.resolve(name)
        parent = self.save_dir.parent
        parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(dir=parent, prefix=".restore-") as staging:
            stage = Path(staging)
            try:
                with tarfile.open(archive, "r:gz") as tar:
                    _safe_extract(tar, stage)
            except (OSError, tarfile.TarError) as exc:
                raise WorldBackupError(f"バックアップを展開できませんでした: {exc}") from exc

            extracted = stage / self.save_dir.name
            if not extracted.is_dir():
                raise WorldBackupError(
                    f"バックアップの中身が想定と違います（{self.save_dir.name} が入っていません）"
                )

            # 復元前の状態も退避しておく。復元先を間違えたときに戻せる
            rescued = None
            if self.save_dir.exists():
                rescued = parent / f".before-restore-{int(time.time())}"
                self.save_dir.rename(rescued)
            try:
                extracted.rename(self.save_dir)
            except OSError as exc:
                if rescued is not None:
                    rescued.rename(self.save_dir)   # 元に戻す
                raise WorldBackupError(f"セーブを差し替えられませんでした: {exc}") from exc

            if rescued is not None:
                shutil.rmtree(rescued, ignore_errors=True)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """展開先の外に書き出させない。

    tar の中身は自分で作ったものだが、置き場を書き換えられる可能性はある。
    `../` や絶対パス、シンボリックリンクでの脱出を弾く。
    """
    root = dest.resolve()
    for member in tar.getmembers():
        target = (root / member.name).resolve()
        if not target.is_relative_to(root):
            raise WorldBackupError(f"バックアップに不正なパスが含まれています: {member.name}")
        if member.issym() or member.islnk():
            link = (target.parent / member.linkname).resolve()
            if not link.is_relative_to(root):
                raise WorldBackupError(
                    f"バックアップに不正なリンクが含まれています: {member.name}"
                )
    tar.extractall(dest)
