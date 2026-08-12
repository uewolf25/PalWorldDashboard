"""プレイヤーの入退室と滞在時間。

**Palworld の REST API に入退室イベントは無い。** 取れるのは
`/v1/api/players` のスナップショットだけなので、前回見た顔ぶれとの差分から
JOIN / LEFT を組み立てる。

そのため、これは**イベントログではなく「観測できた範囲での出入り」**になる。
サンプリング間隔より短い出入りは丸ごと落ちるし、管理ツールが止まっている間の
出入りも見えない。画面でもそのつもりで扱うこと。

滞在時間は JOIN を見た時刻から数える。管理ツールを再起動しても続けて数えられるよう、
接続中の顔ぶれと開始時刻もイベントと一緒に永続化する。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PresenceEvent:
    ts: float
    kind: str            # "join" / "leave"
    userid: str
    name: str
    # LEFT のときだけ入る。JOIN を見ていない相手（管理ツールの起動前から
    # 繋がっていた等）では None になる
    stay_seconds: float | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind_label"] = "参加" if self.kind == "join" else "退出"
        return data


@dataclass
class _Session:
    name: str
    since: float


@dataclass
class PresenceState:
    """永続化する内容。"""

    online: dict[str, _Session] = field(default_factory=dict)
    events: list[PresenceEvent] = field(default_factory=list)


class PresenceTracker:
    """接続中プレイヤーの差分から入退室を組み立てる。"""

    def __init__(self, store_path: Path | None = None, limit: int = 500) -> None:
        self.store_path = Path(store_path) if store_path else None
        self.limit = limit
        self._online: dict[str, _Session] = {}
        self._events: list[PresenceEvent] = []   # 新しいものが先頭
        # 一度も observe していない状態と「誰もいない」を区別する。
        # 起動直後の1回目で全員に JOIN を出すと、実際には前から居た人まで
        # 「いま入った」ことにしてしまう
        self._seeded = False
        self._load()

    # ---- 永続化 --------------------------------------------------------

    def _load(self) -> None:
        if not self.store_path or not self.store_path.is_file():
            return
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("入退室履歴の読み込みに失敗: %s", exc)
            return
        for uid, item in (raw.get("online") or {}).items():
            try:
                self._online[uid] = _Session(name=item["name"], since=float(item["since"]))
            except (KeyError, TypeError, ValueError):
                continue
        for item in raw.get("events") or []:
            try:
                self._events.append(PresenceEvent(**item))
            except TypeError:
                continue
        # 復元できたなら初回扱いにしない。再起動をまたいで差分を続けられる
        self._seeded = bool(self._online or self._events)

    def _save(self) -> None:
        if not self.store_path:
            return
        payload = {
            "online": {uid: asdict(s) for uid, s in self._online.items()},
            "events": [asdict(e) for e in self._events[: self.limit]],
        }
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.store_path)
        except OSError as exc:
            logger.warning("入退室履歴の保存に失敗: %s", exc)

    # ---- 観測 ----------------------------------------------------------

    def observe(self, players: list[dict[str, Any]]) -> list[PresenceEvent]:
        """いまの顔ぶれを渡す。前回との差分でイベントを作って返す。"""
        now = time.time()
        seen: dict[str, str] = {}
        for p in players:
            uid = str(p.get("userId") or p.get("playerId") or "").strip()
            if not uid:
                continue
            seen[uid] = str(p.get("name") or "?")

        new_events: list[PresenceEvent] = []

        # --- 増えた人 ---
        for uid, name in seen.items():
            if uid in self._online:
                # 名前が変わることはまず無いが、変わったなら追従しておく
                self._online[uid].name = name
                continue
            self._online[uid] = _Session(name=name, since=now)
            if self._seeded:
                new_events.append(PresenceEvent(ts=now, kind="join", userid=uid, name=name))

        # --- 消えた人 ---
        for uid in [u for u in self._online if u not in seen]:
            session = self._online.pop(uid)
            if self._seeded:
                new_events.append(
                    PresenceEvent(
                        ts=now,
                        kind="leave",
                        userid=uid,
                        name=session.name,
                        stay_seconds=max(0.0, now - session.since),
                    )
                )

        if not self._seeded:
            # 初回は「いま見えている人がいる」という事実だけ取り込む
            self._seeded = True

        if new_events:
            # 新しいものが先頭
            self._events = new_events[::-1] + self._events
            del self._events[self.limit:]
        if new_events or self.store_path:
            self._save()
        return new_events

    def forget(self, userid: str) -> None:
        """キック/BAN の直後など、消えることが分かっている相手を落とす。

        次の observe を待たずに退出として確定させたいときに使う。
        """
        session = self._online.pop(userid, None)
        if session is None:
            return
        now = time.time()
        event = PresenceEvent(
            ts=now, kind="leave", userid=userid, name=session.name,
            stay_seconds=max(0.0, now - session.since),
        )
        self._events.insert(0, event)
        del self._events[self.limit:]
        self._save()

    # ---- 参照 ----------------------------------------------------------

    def since(self, userid: str) -> float | None:
        session = self._online.get(userid)
        return session.since if session else None

    def stay_seconds(self, userid: str) -> float | None:
        since = self.since(userid)
        return None if since is None else max(0.0, time.time() - since)

    def annotate(self, players: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """プレイヤー一覧に滞在時間を添える。"""
        out = []
        for p in players:
            uid = str(p.get("userId") or p.get("playerId") or "")
            out.append({**p, "joined_at": self.since(uid), "stay_seconds": self.stay_seconds(uid)})
        return out

    def list(self, limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
        events = self._events
        if kind:
            events = [e for e in events if e.kind == kind]
        return [e.as_dict() for e in events[:limit]]

    def clear(self) -> int:
        n = len(self._events)
        self._events = []
        self._save()
        return n

    def __len__(self) -> int:
        return len(self._events)
