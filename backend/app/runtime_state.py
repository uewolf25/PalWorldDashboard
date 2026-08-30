"""管理ツール自身の稼働記録。

**何のためにあるか。** 管理ツールは自分の意思と無関係に停止させられる。
OS のパッケージ更新のあと needrestart が
`systemctl restart dashboard-Pal.service` を打つためで、1回の自動更新で
数秒のうちに2〜3回落とされることもある（issue #41）。

停止と起動はそれぞれ Discord に流れるので、受け取った側からは
「誰かが管理ツールを触った」ようにしか見えない。意図した再起動と
外部要因の再起動を見分けられるように、前回いつ止まったかをプロセスを
またいで残しておく。

判断材料は2つだけ。

- **停止からの経過秒数。** 数秒で戻っているなら人の操作ではまずない
- **前回の停止が記録されているか。** 記録が無い＝正常な停止処理を
  通らずに消えた（SIGKILL、OOM、電源断）ということなので、
  シーケンスの中断を疑う手がかりになる
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class StartupInfo:
    """起動時に分かる、前回の停止の様子。"""

    # 前回の停止からの経過秒数。記録が無ければ None
    gap: float | None = None
    # 短時間で戻ってきたか（外部要因による再起動の可能性）
    quick: bool = False
    # 前回、停止処理を通らずに消えたか（強制終了の可能性）
    unclean: bool = False
    # 前回の停止時に進行中シーケンスをどう扱ったか（drain の結果）
    drain: str = ""

    @property
    def suspect_external(self) -> bool:
        """外部要因による再起動を疑うべきか。"""
        return self.quick or self.unclean


class RuntimeState:
    """`{stopped_at, started_at, drain}` だけを持つ小さな状態ファイル。

    書けなくても管理ツールは動き続ける（通知の文面が一段そっけなくなるだけ）。
    ここで例外を投げて起動を止めるほどの情報ではない。
    """

    def __init__(self, store_path: Path | None, *, quick_restart_sec: float = 60.0) -> None:
        self.store_path = Path(store_path) if store_path else None
        self.quick_restart_sec = quick_restart_sec

    def _load(self) -> dict[str, Any]:
        if self.store_path is None or not self.store_path.is_file():
            return {}
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("稼働記録を読み込めません: %s", exc)
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        if self.store_path is None:
            return
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.store_path)
        except OSError as exc:
            logger.warning("稼働記録を保存できません: %s", exc)

    def mark_started(self) -> StartupInfo:
        """起動を記録し、前回の停止の様子を返す。"""
        previous = self._load()
        now = time.time()
        stopped_at = previous.get("stopped_at")
        info = StartupInfo(drain=str(previous.get("drain") or ""))

        if isinstance(stopped_at, (int, float)):
            # 時計が巻き戻った場合に負の経過時間を配らない
            info.gap = max(0.0, now - float(stopped_at))
            info.quick = info.gap <= self.quick_restart_sec
        elif previous.get("started_at") is not None:
            # 起動の記録はあるのに停止の記録が無い＝停止処理を通らずに消えた
            info.unclean = True

        self._save({"started_at": now, "stopped_at": None, "drain": ""})
        return info

    def mark_stopped(self, *, drain: str = "") -> None:
        """停止を記録する。

        `drain` には進行中シーケンスの扱い（`RestartManager.drain()` の戻り値）を
        入れておく。次の起動で「中断されたシーケンスがあるはずだ」と分かる。
        """
        data = self._load()
        data.update({"stopped_at": time.time(), "drain": drain})
        self._save(data)
