"""ゲーム内アナウンスの送信と履歴。

アナウンスの送信口をここに一本化する。理由は2つ:
  - どこから送っても履歴に必ず残るようにするため
  - ゲーム内 / Discord のどちらに流すかを1箇所で決めるため

履歴は JSON に永続化する（プロセス再起動後も残す）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .notify import DiscordNotifier
from .palapi import PalApiError, PalworldClient

logger = logging.getLogger(__name__)

Source = Literal["manual", "restart", "stop", "schedule", "system"]

SOURCE_LABELS: dict[str, str] = {
    "manual": "手動",
    "restart": "再起動",
    "stop": "停止",
    "schedule": "予約",
    "system": "システム",
}


@dataclass
class AnnouncementRecord:
    ts: float
    message: str
    source: str
    to_game: bool = True
    to_discord: bool = False
    ok: bool = True
    detail: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_label"] = SOURCE_LABELS.get(self.source, self.source)
        return data


class AnnouncementLog:
    """アナウンス履歴。新しいものが先頭。"""

    def __init__(self, store_path: Path | None = None, limit: int = 500) -> None:
        self.store_path = Path(store_path) if store_path else None
        self.limit = limit
        self._records: list[AnnouncementRecord] = []
        self._load()

    def _load(self) -> None:
        if not self.store_path or not self.store_path.is_file():
            return
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("アナウンス履歴の読み込みに失敗: %s", exc)
            return
        for item in raw if isinstance(raw, list) else []:
            try:
                self._records.append(AnnouncementRecord(**item))
            except TypeError:
                continue

    def _save(self) -> None:
        if not self.store_path:
            return
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps([asdict(r) for r in self._records], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.store_path)
        except OSError as exc:
            logger.warning("アナウンス履歴の保存に失敗: %s", exc)

    def add(self, record: AnnouncementRecord) -> AnnouncementRecord:
        self._records.insert(0, record)
        del self._records[self.limit :]
        self._save()
        return record

    def list(self, limit: int = 100, source: str | None = None) -> list[dict[str, Any]]:
        records = self._records
        if source:
            records = [r for r in records if r.source == source]
        return [r.as_dict() for r in records[:limit]]

    def clear(self) -> int:
        count = len(self._records)
        self._records = []
        self._save()
        return count

    def __len__(self) -> int:
        return len(self._records)


class Announcer:
    """ゲーム内アナウンスを送り、必要なら Discord にも流し、履歴に残す。"""

    def __init__(
        self,
        pal: PalworldClient,
        notifier: DiscordNotifier,
        log: AnnouncementLog,
    ) -> None:
        self._pal = pal
        self._notifier = notifier
        self.log = log

    async def send(
        self,
        message: str,
        *,
        source: Source = "manual",
        to_discord: bool = False,
        discord_title: str = "サーバーアナウンス",
        reason: str = "",
        raise_on_error: bool = False,
    ) -> AnnouncementRecord:
        """アナウンスを送信して履歴に記録する。

        raise_on_error=False（既定）ならゲーム側への送信失敗も履歴に残して返すだけ。
        再起動シーケンスの予告は、失敗しても再起動自体を止めたくないのでこちらを使う。
        """
        ok = True
        detail = ""
        try:
            await self._pal.announce(message)
        # RuntimeError はアプリ停止中に HTTP クライアントが閉じられた場合。
        # シャットダウン途中のキャンセル通知で落ちないよう、ここで受ける。
        except (PalApiError, RuntimeError) as exc:
            ok = False
            detail = str(exc)
            logger.warning("アナウンス送信に失敗: %s", exc)
            if raise_on_error:
                self.log.add(
                    AnnouncementRecord(
                        ts=time.time(), message=message, source=source,
                        to_game=True, to_discord=False, ok=False, detail=detail, reason=reason,
                    )
                )
                raise

        discord_sent = False
        if to_discord:
            discord_sent = await self._notifier.send(discord_title, message, "info")

        return self.log.add(
            AnnouncementRecord(
                ts=time.time(),
                message=message,
                source=source,
                to_game=True,
                to_discord=discord_sent,
                ok=ok,
                detail=detail,
                reason=reason,
            )
        )

    async def discord_only(
        self,
        title: str,
        message: str,
        *,
        source: Source = "system",
        level: str = "info",
        reason: str = "",
    ) -> AnnouncementRecord:
        """ゲーム内には出さず Discord にだけ流す（開始/完了通知など）。"""
        sent = await self._notifier.send(title, message, level)  # type: ignore[arg-type]
        return self.log.add(
            AnnouncementRecord(
                ts=time.time(),
                message=f"{title}: {message}" if message else title,
                source=source,
                to_game=False,
                to_discord=sent,
                ok=True,
                reason=reason,
            )
        )


def render_template(template: str, seconds: float, humanize) -> str:
    """予告文の {time} を残り時間に差し替える。

    {time} が無いテンプレートはそのまま使う（毎回同じ文面で流す）。
    """
    if "{time}" not in template:
        return template
    return template.replace("{time}", humanize(seconds))
