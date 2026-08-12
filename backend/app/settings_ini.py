"""PalWorldSettings.ini の読み書き。

ファイル形式:

    [/Script/Pal.PalGameWorldSettings]
    OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,ServerName="My, Server")

OptionSettings は1行に全部入った独自形式なので、素の configparser では扱えない。
括弧の中をカンマ分割する専用パーサを持つ（引用符内のカンマは無視する）。

保存前には必ずタイムスタンプ付きバックアップを取る。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SECTION = "[/Script/Pal.PalGameWorldSettings]"
_OPTION_RE = re.compile(r"^\s*OptionSettings\s*=\s*\((?P<body>.*)\)\s*$")


class SettingsIniError(RuntimeError):
    pass


def split_options(body: str) -> list[str]:
    """OptionSettings の中身を、引用符を尊重しつつカンマで分割する。"""
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in body:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "," and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def parse_options(text: str) -> dict[str, str]:
    """ini 全文から OptionSettings のキー/値を取り出す。"""
    for line in text.splitlines():
        m = _OPTION_RE.match(line)
        if not m:
            continue
        options: dict[str, str] = {}
        for part in split_options(m.group("body")):
            key, sep, value = part.partition("=")
            if not sep:
                continue
            options[key.strip()] = value.strip()
        return options
    return {}


def render_options(options: dict[str, str]) -> str:
    body = ",".join(f"{k}={v}" for k, v in options.items())
    return f"OptionSettings=({body})"


def apply_options(text: str, updates: dict[str, str]) -> str:
    """ini 全文の OptionSettings 行だけを差し替えた全文を返す。

    未知のキーは追加する。行の順序とその他の行はそのまま保つ。
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _OPTION_RE.match(line):
            options = parse_options(line)
            options.update(updates)
            lines[i] = render_options(options)
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    raise SettingsIniError("OptionSettings 行が見つかりません")


@dataclass
class BackupInfo:
    name: str
    path: Path
    size: int
    created_at: str


class SettingsIniStore:
    def __init__(self, ini_path: Path, backup_dir: Path, keep: int = 30) -> None:
        self.ini_path = Path(ini_path)
        self.backup_dir = Path(backup_dir)
        self.keep = keep

    # ---- 参照 ---------------------------------------------------------

    def exists(self) -> bool:
        return self.ini_path.is_file()

    def read_text(self) -> str:
        if not self.exists():
            raise SettingsIniError(f"設定ファイルが見つかりません: {self.ini_path}")
        return self.ini_path.read_text(encoding="utf-8")

    def read_options(self) -> dict[str, str]:
        return parse_options(self.read_text())

    # ---- 更新 ---------------------------------------------------------

    def backup(self) -> BackupInfo:
        text = self.read_text()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.backup_dir / f"PalWorldSettings.{stamp}.ini"
        # 同一秒に2回保存された場合に上書きしない
        n = 1
        while dest.exists():
            dest = self.backup_dir / f"PalWorldSettings.{stamp}-{n}.ini"
            n += 1
        dest.write_text(text, encoding="utf-8")
        self._prune(keep_always=dest)
        st = dest.stat()
        return BackupInfo(
            name=dest.name,
            path=dest,
            size=st.st_size,
            created_at=datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        )

    def _prune(self, keep_always: Path | None = None) -> None:
        """古い方から間引く。

        名前順ではなく更新時刻順で並べる。同じ秒に2回保存すると
        `PalWorldSettings.<stamp>.ini` と `...<stamp>-1.ini` が混ざり、
        名前順だと後から作った方が先頭（＝古い扱い）に来てしまう（`-` < `.`）。
        """
        def sort_key(p: Path) -> tuple[float, str]:
            try:
                return (p.stat().st_mtime, p.name)
            except OSError:
                return (0.0, p.name)

        backups = sorted(self.backup_dir.glob("PalWorldSettings.*.ini"), key=sort_key)
        excess = len(backups) - self.keep
        for path in backups[:excess]:
            if keep_always is not None and path == keep_always:
                continue
            try:
                path.unlink()
            except OSError as exc:  # pragma: no cover - 権限まわりの保険
                logger.warning("バックアップ削除に失敗: %s (%s)", path, exc)

    def list_backups(self) -> list[BackupInfo]:
        if not self.backup_dir.is_dir():
            return []
        out: list[BackupInfo] = []
        for path in sorted(self.backup_dir.glob("PalWorldSettings.*.ini"), reverse=True):
            st = path.stat()
            out.append(
                BackupInfo(
                    name=path.name,
                    path=path,
                    size=st.st_size,
                    created_at=datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                )
            )
        return out

    def write_text(self, text: str) -> BackupInfo:
        """全文を差し替える。書き込み前に必ずバックアップを取る。"""
        if SECTION not in text:
            raise SettingsIniError(f"{SECTION} セクションがありません")
        if not _has_option_line(text):
            raise SettingsIniError("OptionSettings 行がありません")
        info = self.backup()
        self._write(text)
        return info

    def update_options(self, updates: dict[str, str]) -> BackupInfo:
        text = self.read_text()
        new_text = apply_options(text, updates)
        info = self.backup()
        self._write(new_text)
        return info

    def restore(self, name: str) -> None:
        src = self.backup_dir / name
        # ディレクトリ外への脱出を防ぐ
        if src.parent.resolve() != self.backup_dir.resolve() or not src.is_file():
            raise SettingsIniError(f"バックアップが見つかりません: {name}")
        self.backup()  # 復元前の状態も残す
        self._write(src.read_text(encoding="utf-8"))

    def _write(self, text: str) -> None:
        """設定ファイルを書き換える。

        既存ファイルは **inode を保ったまま上書きする**（open("w") で truncate）。
        一時ファイルを作って rename すると別 inode に置き換わり、
        所有者・グループ・パーミッションが書き込んだプロセスのものになる。
        Palworld が steam ユーザ、管理ツールが palmanager ユーザという構成だと、
        一度書いた時点でゲーム側が停止時に ini を書き戻せなくなる。
        chmod / chown での復元はファイルの所有者でないと効かないので、
        そもそも置き換えない方が確実。

        rename を使わないぶん原子性は落ちるが、直前に必ずバックアップを取っており、
        書き込むのはサーバ停止中の小さなファイル1つなので、この取り引きの方がよい。

        新規作成のときだけは既存の属性が無いので、そのまま作る。
        """
        if self.ini_path.exists():
            with self.ini_path.open("w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            return

        self.ini_path.parent.mkdir(parents=True, exist_ok=True)
        self.ini_path.write_text(text, encoding="utf-8")


def _has_option_line(text: str) -> bool:
    return any(_OPTION_RE.match(line) for line in text.splitlines())
