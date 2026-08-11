"""サーバ状態変更のスケジューラ。

予約できる動作:
  restart : 予告 → ワールド保存 → 停止 → 起動
  stop    : 予告 → ワールド保存 → 停止（起動しない）
  start   : 即時起動（停止中のサーバには予告を送れないので予告なし）

繰り返し方:
  daily : 毎日 HH:MM
  once  : 単発（ISO8601 日時）
  cron  : cron 式（分 時 日 月 曜）

daily / once は「実際に動作が起きる時刻」を指定する。予告のリード時間ぶん
手前でジョブを起動するので、指定時刻ちょうどに実行される。
cron は式を素直にトリガとして使うため、その時刻から予告が始まり、
実際の実行はリード時間ぶん後になる。start は予告が無いのでズレない。

スケジュール定義は JSON に永続化する（APScheduler のジョブストアは使わない。
人が読める形で /var/lib に置いておきたいため）。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .announce import Announcer
from .restart import (
    RestartDebounced,
    RestartInProgress,
    RestartManager,
    RestartValidationError,
    validate_request,
)
from .services import GameService

logger = logging.getLogger(__name__)

ScheduleKind = Literal["daily", "once", "cron"]
ScheduleAction = Literal["restart", "stop", "start"]

ACTIONS: tuple[str, ...] = ("restart", "stop", "start")
ACTION_LABELS: dict[str, str] = {"restart": "再起動", "stop": "停止", "start": "起動"}
# 予告アナウンスを伴う動作。start は停止中のサーバに送れないので含まない
ANNOUNCED_ACTIONS: tuple[str, ...] = ("restart", "stop")


class ScheduleError(ValueError):
    pass


@dataclass
class Schedule:
    id: str
    kind: ScheduleKind
    spec: str
    label: str = ""
    enabled: bool = True
    # 何をするか。既定は restart（動作種別を持たなかった頃の定義との互換）
    action: ScheduleAction = "restart"
    # 予告アナウンスの文面と、何秒前に流すか。
    # 空/None のときは RestartManager の既定値を使う（古い定義の読み込み用）。
    announce_message: str = ""
    notice_offsets: list[float] | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_store(self) -> dict[str, Any]:
        """永続化用。dataclass のフィールドだけを書き出す。

        表示用の派生フィールドを混ぜると、読み戻すときに
        Schedule(**item) が TypeError になって定義が丸ごと捨てられる。
        """
        return asdict(self)

    def as_dict(self) -> dict[str, Any]:
        """API/画面用。表示に使う派生フィールドを足す。"""
        data = asdict(self)
        data["action_label"] = ACTION_LABELS.get(self.action, self.action)
        data["announced"] = self.action in ANNOUNCED_ACTIONS
        return data


def _validate(kind: str, spec: str, tz: ZoneInfo) -> None:
    if kind == "daily":
        try:
            hh, mm = spec.split(":")
            dtime(int(hh), int(mm))
        except (ValueError, TypeError) as exc:
            raise ScheduleError(f"daily の指定は HH:MM 形式です: {spec!r}") from exc
    elif kind == "once":
        try:
            when = datetime.fromisoformat(spec)
        except ValueError as exc:
            raise ScheduleError(f"once の指定は ISO8601 日時です: {spec!r}") from exc
        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        if when <= datetime.now(tz):
            raise ScheduleError("once の指定時刻が過去です")
    elif kind == "cron":
        try:
            CronTrigger.from_crontab(spec, timezone=tz)
        except (ValueError, KeyError) as exc:
            raise ScheduleError(f"cron 式が不正です: {spec!r} ({exc})") from exc
    else:
        raise ScheduleError(f"未知の種別です: {kind!r}")


class ServerScheduler:
    """サーバの起動 / 停止 / 再起動を予約する。"""

    def __init__(
        self,
        restart_manager: RestartManager,
        service: GameService,
        announcer: Announcer,
        *,
        timezone: str = "Asia/Tokyo",
        store_path: Path | None = None,
    ) -> None:
        self._restart = restart_manager
        self._service = service
        self._announcer = announcer
        self.tz = ZoneInfo(timezone)
        self.store_path = Path(store_path) if store_path else None
        self._scheduler = AsyncIOScheduler(timezone=self.tz)
        self._schedules: dict[str, Schedule] = {}

    # ---- ライフサイクル -------------------------------------------------

    def start(self) -> None:
        self._load()
        if not self._scheduler.running:
            self._scheduler.start()
        for sched in self._schedules.values():
            if sched.enabled:
                self._register(sched)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # ---- 永続化 --------------------------------------------------------

    def _load(self) -> None:
        if not self.store_path or not self.store_path.is_file():
            return
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("スケジュールの読み込みに失敗: %s", exc)
            return
        for item in raw if isinstance(raw, list) else []:
            try:
                sched = Schedule(**item)
            except TypeError:
                logger.warning("壊れたスケジュール定義を無視: %r", item)
                continue
            self._schedules[sched.id] = sched

    def _save(self) -> None:
        if not self.store_path:
            return
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps([s.to_store() for s in self._schedules.values()], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.store_path)
        except OSError as exc:
            logger.warning("スケジュールの保存に失敗: %s", exc)

    # ---- ジョブ登録 ----------------------------------------------------

    def _lead(self, sched: Schedule) -> int:
        """予告開始から実際の動作までのリード秒数。

        予約ごとに予告タイミングを持てるので、その最大値を使う。
        未設定の予約だけ RestartManager の既定値にフォールバックする。
        cron / daily のトリガは秒単位までしか表現できないため整数にする。
        起動は予告を伴わないので 0。
        """
        if sched.action not in ANNOUNCED_ACTIONS:
            return 0
        offsets = sched.notice_offsets or list(self._restart.notice_offsets)
        return int(max(offsets)) if offsets else 0

    def _trigger_for(self, sched: Schedule):
        lead = self._lead(sched)
        if sched.kind == "daily":
            hh, mm = (int(x) for x in sched.spec.split(":"))
            # 指定時刻ちょうどに再起動したいので、予告リードぶん手前に起動する
            target = datetime.combine(datetime.now(self.tz).date(), dtime(hh, mm), tzinfo=self.tz)
            fire = target - timedelta(seconds=lead)
            return CronTrigger(hour=fire.hour, minute=fire.minute, second=fire.second, timezone=self.tz)
        if sched.kind == "once":
            when = datetime.fromisoformat(sched.spec)
            if when.tzinfo is None:
                when = when.replace(tzinfo=self.tz)
            return DateTrigger(run_date=when - timedelta(seconds=lead), timezone=self.tz)
        return CronTrigger.from_crontab(sched.spec, timezone=self.tz)

    def _register(self, sched: Schedule) -> None:
        self._scheduler.add_job(
            self._fire,
            trigger=self._trigger_for(sched),
            args=[sched.id],
            id=sched.id,
            replace_existing=True,
            misfire_grace_time=120,
            coalesce=True,
            max_instances=1,
        )

    def _unregister(self, schedule_id: str) -> None:
        if self._scheduler.get_job(schedule_id):
            self._scheduler.remove_job(schedule_id)

    async def _fire(self, schedule_id: str) -> None:
        sched = self._schedules.get(schedule_id)
        if sched is None:  # pragma: no cover - 削除と発火が競合した場合
            return
        label = sched.label or sched.spec
        reason = f"スケジュール({label})"

        if sched.action == "start":
            # 停止中のサーバにはアナウンスを送れないので、そのまま起動する
            result = await self._service.start()
            await self._announcer.discord_only(
                "サーバーを起動しました" if result.ok else "サーバーの起動に失敗しました",
                f"予約: {label}" + ("" if result.ok else f"\n{result.stderr}"),
                source="schedule",
                level="info" if result.ok else "crit",
                reason=reason,
            )
            if not result.ok:
                logger.warning("スケジュール %s の起動に失敗: %s", schedule_id, result.stderr)
        else:
            try:
                await self._restart.request(
                    reason=reason,
                    announce_message=sched.announce_message or None,
                    notice_offsets=sched.notice_offsets or None,
                    mode=sched.action,  # type: ignore[arg-type]
                    # この予約に紐づけた設定変更を反映させるために渡す
                    schedule_id=sched.id,
                )
            except (RestartInProgress, RestartDebounced) as exc:
                logger.info("スケジュール %s をスキップ: %s", schedule_id, exc)
                return

        # 単発は実行後に自動で無効化する
        if sched.kind == "once":
            sched.enabled = False
            self._save()

    # ---- CRUD ----------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        out = []
        for sched in self._schedules.values():
            job = self._scheduler.get_job(sched.id)
            next_run = getattr(job, "next_run_time", None) if job else None
            item = sched.as_dict()
            item["next_fire_at"] = next_run.isoformat() if next_run else None
            # 実際に動作が起きる時刻（発火してから予告リードぶん後）
            item["next_action_at"] = (
                (next_run + timedelta(seconds=self._lead(sched))).isoformat() if next_run else None
            )
            out.append(item)
        return sorted(out, key=lambda i: (i["next_fire_at"] is None, i["next_fire_at"] or ""))

    def _validate_announce(
        self, action: str, announce_message: str, notice_offsets: list[float] | None
    ) -> list[float] | None:
        """予告の文面/タイミングを再起動と同じ規則で検証する。

        起動は予告を伴わないので何も持たせない。
        再起動/停止は、どちらも未指定なら RestartManager の既定値に任せる
        （動作種別を持たなかった頃の定義を読み込むため）。
        """
        if action not in ANNOUNCED_ACTIONS:
            return None
        if not announce_message and not notice_offsets:
            return None
        try:
            return list(
                validate_request(
                    announce_message, notice_offsets or self._restart.notice_offsets
                )
            )
        except RestartValidationError as exc:
            raise ScheduleError(str(exc)) from exc

    def add(
        self,
        kind: str,
        spec: str,
        label: str = "",
        enabled: bool = True,
        announce_message: str = "",
        notice_offsets: list[float] | None = None,
        action: str = "restart",
    ) -> dict[str, Any]:
        if action not in ACTIONS:
            raise ScheduleError(f"動作は {'/'.join(ACTIONS)} から選んでください: {action!r}")
        # 無告知でサーバを落とさない。既定値へのフォールバックは、
        # 動作種別を持たなかった頃の定義を読み込むときだけに限る
        if action in ANNOUNCED_ACTIONS and (not announce_message or not notice_offsets):
            raise ScheduleError(
                f"{ACTION_LABELS[action]}の予約にはアナウンス文と予告タイミングが必要です"
            )
        _validate(kind, spec, self.tz)
        notice_offsets = self._validate_announce(action, announce_message, notice_offsets)
        if action not in ANNOUNCED_ACTIONS:
            # 起動には予告が無いので、渡されても持たせない
            announce_message = ""
        sched = Schedule(
            id=uuid.uuid4().hex[:12],
            kind=kind,  # type: ignore[arg-type]
            spec=spec,
            label=label,
            enabled=enabled,
            action=action,  # type: ignore[arg-type]
            announce_message=announce_message,
            notice_offsets=notice_offsets,
        )
        self._schedules[sched.id] = sched
        if enabled and self._scheduler.running:
            self._register(sched)
        self._save()
        return sched.as_dict()

    def update(self, schedule_id: str, **changes: Any) -> dict[str, Any]:
        sched = self._schedules.get(schedule_id)
        if sched is None:
            raise ScheduleError(f"スケジュールが見つかりません: {schedule_id}")
        kind = changes.get("kind", sched.kind)
        spec = changes.get("spec", sched.spec)
        action = changes.get("action", sched.action)
        if action not in ACTIONS:
            raise ScheduleError(f"動作は {'/'.join(ACTIONS)} から選んでください: {action!r}")
        _validate(kind, spec, self.tz)
        sched.kind = kind
        sched.spec = spec
        if "label" in changes and changes["label"] is not None:
            sched.label = changes["label"]
        if "enabled" in changes and changes["enabled"] is not None:
            sched.enabled = bool(changes["enabled"])
        # 動作を変えると予告の要否も変わるので、まとめて検証し直す。
        # ここを飛ばすと「起動の予約を作ってから停止に変える」だけで
        # 無告知の停止予約が作れてしまう
        if action != sched.action or "announce_message" in changes or "notice_offsets" in changes:
            message = changes.get("announce_message", sched.announce_message) or ""
            offsets = changes.get("notice_offsets", sched.notice_offsets)
            if action in ANNOUNCED_ACTIONS and (not message or not offsets):
                raise ScheduleError(
                    f"{ACTION_LABELS[action]}の予約にはアナウンス文と予告タイミングが必要です"
                )
            sched.notice_offsets = self._validate_announce(action, message, offsets)
            sched.announce_message = message if action in ANNOUNCED_ACTIONS else ""
        sched.action = action
        self._unregister(sched.id)
        if sched.enabled and self._scheduler.running:
            self._register(sched)
        self._save()
        return sched.as_dict()

    def remove(self, schedule_id: str) -> None:
        if schedule_id not in self._schedules:
            raise ScheduleError(f"スケジュールが見つかりません: {schedule_id}")
        self._unregister(schedule_id)
        del self._schedules[schedule_id]
        self._save()
