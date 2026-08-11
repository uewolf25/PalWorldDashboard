"""設定変更の予約（保留中の変更）。

Palworld は停止時にメモリ上の設定で PalWorldSettings.ini を上書きするため、
稼働中に ini を書き換えても次の停止で消える。かといって毎回
「サーバを止めて、編集して、起動する」を人が張り付いてやるのは現実的でない。

そこで変更内容を ini には書かずにここへ退避しておき、
停止シーケンスがサーバを止めた直後に読み出して ini へ書き込む。
これで管理者はいつでも変更を入力でき、反映はメンテナンス枠で自動的に起きる。

保留中の変更は JSON に永続化する（プロセスが落ちても消えないように）。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PendingChange:
    """1回の保存操作ぶんの変更。

    updates は ini にそのまま書ける形（キー → 書式化済みの値）で持つ。
    保存した時点で検証と書式化を済ませておき、反映時は書き込むだけにする。
    """

    id: str
    created_at: str
    # 反映先の予約。None なら「次にサーバが停止するとき」
    schedule_id: str | None = None
    note: str = ""
    updates: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplyResult:
    ok: bool
    applied_ids: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    backup: str | None = None
    error: str = ""

    @property
    def count(self) -> int:
        return len(self.keys)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "applied": len(self.applied_ids),
            "keys": self.keys,
            "backup": self.backup,
            "error": self.error,
        }


class PendingChangeStore:
    """保留中の変更を溜めておく。新しいものが後ろ。"""

    def __init__(self, store_path: Path | None = None, limit: int = 50) -> None:
        self.store_path = Path(store_path) if store_path else None
        self.limit = limit
        self._items: list[PendingChange] = []
        self._load()

    # ---- 永続化 --------------------------------------------------------

    def _load(self) -> None:
        if not self.store_path or not self.store_path.is_file():
            return
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("保留中の変更の読み込みに失敗: %s", exc)
            return
        for item in raw if isinstance(raw, list) else []:
            try:
                self._items.append(PendingChange(**item))
            except TypeError:
                logger.warning("壊れた保留変更を無視しました")

    def _save(self) -> None:
        if not self.store_path:
            return
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps([i.as_dict() for i in self._items], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.store_path)
        except OSError as exc:
            logger.warning("保留中の変更の保存に失敗: %s", exc)

    # ---- CRUD ----------------------------------------------------------

    def add(
        self,
        updates: dict[str, str],
        *,
        schedule_id: str | None = None,
        note: str = "",
    ) -> PendingChange:
        if len(self._items) >= self.limit:
            raise ValueError(
                f"保留中の変更が上限（{self.limit}件）に達しています。"
                "反映するか、不要なものを取り消してください。"
            )
        item = PendingChange(
            id=uuid.uuid4().hex[:12],
            created_at=datetime.now().isoformat(timespec="seconds"),
            schedule_id=schedule_id,
            note=note,
            updates=dict(updates),
        )
        self._items.append(item)
        self._save()
        return item

    def list(self) -> list[dict[str, Any]]:
        return [i.as_dict() for i in self._items]

    def get(self, change_id: str) -> PendingChange | None:
        return next((i for i in self._items if i.id == change_id), None)

    def remove(self, change_id: str) -> bool:
        before = len(self._items)
        self._items = [i for i in self._items if i.id != change_id]
        if len(self._items) != before:
            self._save()
            return True
        return False

    def remove_many(self, ids: list[str]) -> None:
        wanted = set(ids)
        self._items = [i for i in self._items if i.id not in wanted]
        self._save()

    def clear(self) -> int:
        count = len(self._items)
        self._items = []
        self._save()
        return count

    def __len__(self) -> int:
        return len(self._items)

    # ---- 反映対象の取り出し ---------------------------------------------

    def due_for(self, schedule_id: str | None) -> list[PendingChange]:
        """この停止機会で反映すべき変更。

        予約を指定していない変更（schedule_id=None）は、
        予約・手動を問わず次にサーバが停止したときに反映する。
        特定の予約に紐づけた変更は、その予約のときだけ反映する。
        """
        return [
            i for i in self._items
            if i.schedule_id is None or (schedule_id is not None and i.schedule_id == schedule_id)
        ]

    def merged(self, items: list[PendingChange]) -> dict[str, str]:
        """複数の保留変更を1つのキー→値に畳む。後から保存したものが勝つ。"""
        merged: dict[str, str] = {}
        for item in items:
            merged.update(item.updates)
        return merged

    def summary_for(self, schedule_id: str | None) -> dict[str, Any]:
        items = self.due_for(schedule_id)
        return {"changes": len(items), "keys": sorted(self.merged(items))}
