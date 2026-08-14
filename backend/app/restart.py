"""再起動/停止シーケンスの実行管理。

守っていること:

- 二重実行しない（asyncio.Lock + 直近実行のデバウンス）
- 予告中はキャンセルできる
- ワールド保存に失敗したら再起動を中止する（セーブデータを失わないため）
- 予告アナウンスは必ず出す（文面と予告タイミングは呼び出し側が必ず指定する）

Discord へは開始時と完了時、それに中止/キャンセル時だけ流す。
予告の途中経過まで流すとチャンネルが荒れるため、ゲーム内だけに出す。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from .announce import Announcer, render_template
from .palapi import PalApiError, PalworldClient
from .pending import ApplyResult
from .services import GameService

# サーバが完全に停止したあとに呼ばれ、保留中の設定変更を ini に書き込む。
# 引数はこの停止機会に紐づく予約 ID（手動実行なら None）。
ApplyPending = Callable[[str | None], Awaitable[ApplyResult]]
# この停止機会で反映される保留変更の件数
CountPending = Callable[[str | None], int]

logger = logging.getLogger(__name__)

# 再起動の何秒前に予告するか（降順で使う）
DEFAULT_NOTICE_OFFSETS: tuple[float, ...] = (300.0, 60.0, 30.0)
DEFAULT_RESTART_TEMPLATE = "サーバーは{time}後に再起動します。"
DEFAULT_STOP_TEMPLATE = "サーバーは{time}後に停止します。"

Phase = Literal["idle", "announcing", "saving", "restarting", "done", "failed", "cancelled"]
Mode = Literal["restart", "stop"]

MODE_LABELS: dict[str, str] = {"restart": "再起動", "stop": "停止"}


def humanize(seconds: float) -> str:
    total = int(round(seconds))
    if total >= 60 and total % 60 == 0:
        return f"{total // 60}分"
    if total >= 60:
        return f"{total // 60}分{total % 60}秒"
    return f"{total}秒"


class RestartValidationError(ValueError):
    """アナウンス文や予告タイミングの指定が不正。"""


class RestartInProgress(RuntimeError):
    pass


class RestartDebounced(RuntimeError):
    pass


class ServiceControlUnavailable(RuntimeError):
    """systemctl が通らないと分かったので、サーバを落とす前に中止した。

    落としてから気づいても起動し直せないので、これだけは
    「サーバは無事」という前提で報告してよい失敗になる。
    """


def validate_request(message: str, offsets: list[float] | tuple[float, ...] | None) -> tuple[float, ...]:
    """アナウンス文と予告タイミングを検証して、降順の offsets を返す。"""
    if not message or not message.strip():
        raise RestartValidationError("アナウンス文は必須です")
    if not offsets:
        raise RestartValidationError("予告タイミングは1つ以上指定してください")
    values: list[float] = []
    for raw in offsets:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise RestartValidationError(f"予告タイミングが数値ではありません: {raw!r}") from exc
        if value < 0:
            raise RestartValidationError("予告タイミングに負の値は指定できません")
        if value > 24 * 3600:
            raise RestartValidationError("予告タイミングは24時間以内で指定してください")
        values.append(value)
    return tuple(sorted(set(values), reverse=True))


@dataclass
class RestartStatus:
    phase: Phase = "idle"
    mode: Mode = "restart"
    reason: str = ""
    announce_message: str = ""
    # この停止機会に紐づく予約 ID（手動実行なら None）
    schedule_id: str | None = None
    # 反映した保留中の設定変更の結果
    applied: dict[str, Any] | None = None
    started_at: float | None = None
    finished_at: float | None = None
    restart_at: float | None = None
    message: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "mode": self.mode,
            "mode_label": MODE_LABELS.get(self.mode, self.mode),
            "reason": self.reason,
            "announce_message": self.announce_message,
            "schedule_id": self.schedule_id,
            "applied": self.applied,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "restart_at": self.restart_at,
            "seconds_remaining": (
                max(0.0, self.restart_at - time.time()) if self.restart_at else None
            ),
            "in_progress": self.phase in ("announcing", "saving", "restarting"),
            "cancellable": self.phase == "announcing",
            "message": self.message,
            "steps": self.steps,
        }


class RestartManager:
    def __init__(
        self,
        pal: PalworldClient,
        announcer: Announcer,
        service: GameService,
        *,
        notice_offsets: tuple[float, ...] | list[float] = DEFAULT_NOTICE_OFFSETS,
        announce_template: str = DEFAULT_RESTART_TEMPLATE,
        debounce_sec: float = 60.0,
        shutdown_waittime: int = 10,
        shutdown_grace: float = 120.0,
        poll_interval: float = 1.0,
        use_systemd: bool = True,
        apply_pending: ApplyPending | None = None,
        count_pending: CountPending | None = None,
        suppress_alerts: Callable[[float], None] | None = None,
        alert_grace: float = 180.0,
    ) -> None:
        self._pal = pal
        self._announcer = announcer
        self._service = service
        # サーバ停止後に保留中の設定変更を反映するフックと、その件数を数えるフック。
        # 件数は「再起動を stop→書き込み→start に分ける必要があるか」の判定に使う
        self._apply_pending = apply_pending
        self._count_pending = count_pending
        # 意図的に落としている間の「応答なし」通知を止めるためのフック
        self._suppress_alerts = suppress_alerts
        self.alert_grace = alert_grace
        self.notice_offsets = tuple(sorted((float(o) for o in notice_offsets), reverse=True))
        self.announce_template = announce_template
        self.debounce_sec = debounce_sec
        self.shutdown_waittime = shutdown_waittime
        # shutdown API の waittime を過ぎてから、実際に落ちるのを待つ猶予
        self.shutdown_grace = shutdown_grace
        self.poll_interval = poll_interval
        self.use_systemd = use_systemd

        self.status = RestartStatus()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._last_completed_at: float | None = None

    # ---- 状態 ----------------------------------------------------------

    @property
    def in_progress(self) -> bool:
        return self._task is not None and not self._task.done()

    def _step(self, name: str, ok: bool = True, detail: str = "") -> None:
        self.status.steps.append(
            {"name": name, "ok": ok, "detail": detail, "ts": time.time()}
        )
        logger.info("%sシーケンス: %s (ok=%s) %s",
                    MODE_LABELS.get(self.status.mode, ""), name, ok, detail)

    # ---- 起動/キャンセル ------------------------------------------------

    async def request(
        self,
        reason: str = "手動",
        *,
        announce_message: str | None = None,
        notice_offsets: tuple[float, ...] | list[float] | None = None,
        mode: Mode = "restart",
        force: bool = False,
        schedule_id: str | None = None,
    ) -> RestartStatus:
        """再起動/停止シーケンスをバックグラウンドで開始する。

        announce_message と notice_offsets は必ず指定する運用だが、
        省略時は設定由来の既定値を使う（スケジューラの後方互換のため）。
        """
        if self.in_progress:
            raise RestartInProgress(
                f"{MODE_LABELS.get(self.status.mode, '再起動')}シーケンスが既に進行中です"
            )

        template = announce_message if announce_message is not None else self.announce_template
        offsets = validate_request(
            template,
            notice_offsets if notice_offsets is not None else self.notice_offsets,
        )

        now = time.time()
        if (
            not force
            and self._last_completed_at is not None
            and (now - self._last_completed_at) < self.debounce_sec
        ):
            wait = self.debounce_sec - (now - self._last_completed_at)
            raise RestartDebounced(
                f"直前に実行したばかりです（あと {int(wait)} 秒は受け付けません）"
            )

        lead = offsets[0]
        label = MODE_LABELS.get(mode, mode)

        self.status = RestartStatus(
            phase="announcing",
            mode=mode,
            reason=reason,
            announce_message=template,
            schedule_id=schedule_id,
            started_at=now,
            restart_at=now + lead,
            message=f"{humanize(lead)}後に{label}します" if lead else f"まもなく{label}します",
        )
        self._task = asyncio.create_task(
            self._run(reason, template, offsets, mode, schedule_id), name=f"{mode}-sequence"
        )
        return self.status

    def cancel(self) -> bool:
        """予告中のシーケンスを取り消す。保存・停止に入っていたら取り消せない。"""
        if not self.in_progress:
            return False
        if self.status.phase != "announcing":
            return False
        assert self._task is not None
        self._task.cancel()
        return True

    async def wait(self) -> None:
        """テスト用: 進行中のシーケンスの完了を待つ。"""
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ---- 本体 ----------------------------------------------------------

    async def _run(
        self,
        reason: str,
        template: str,
        offsets: tuple[float, ...],
        mode: Mode,
        schedule_id: str | None = None,
    ) -> None:
        label = MODE_LABELS.get(mode, mode)
        async with self._lock:
            try:
                # 予告の開始を Discord に1度だけ知らせる。
                # この時点ではまだ実行していないので、「開始します」と書くと
                # 受け取った側は今まさに落ちると受け取ってしまう。
                # いつ実行されるのかを主語にする
                lead = offsets[0]
                pending_count = (
                    self._count_pending(schedule_id) if self._count_pending else 0
                )
                detail = f"理由: {reason}"
                if pending_count:
                    detail += f"\nこのタイミングで設定変更 {pending_count} 件を反映します。"
                detail += f"\n実行までキャンセルできます。"
                await self._announcer.discord_only(
                    f"【予告】{humanize(lead)}後にサーバーを{label}します"
                    if lead else f"サーバーを{label}します",
                    detail,
                    source=mode,
                    reason=reason,
                )

                await self._announce_countdown(template, offsets, mode, reason)

                # --- ワールド保存 ---
                self.status.phase = "saving"
                self.status.message = "ワールドを保存しています"
                try:
                    await self._pal.save()
                    self._step("world_save")
                except PalApiError as exc:
                    # タイムアウトは「保存が失敗した」ことの証明ではない。
                    # 保存できたか確認できない以上どちらでも中止するが、
                    # 原因の切り分けができるよう文面は区別する
                    cause = (
                        "ワールド保存の応答を確認できなかった"
                        if exc.timed_out else "ワールド保存に失敗した"
                    )
                    self._step("world_save", ok=False, detail=str(exc))
                    self.status.phase = "failed"
                    self.status.message = f"{cause}ため{label}を中止しました: {exc}"
                    self.status.finished_at = time.time()
                    await self._announcer.send(
                        f"{cause}ため{label}を中止しました。", source=mode, reason=reason,
                    )
                    await self._announcer.discord_only(
                        f"サーバー{label}を中止しました",
                        f"理由: {cause}\n{exc}"
                        + (
                            "\n\nPAL_SLOW_TIMEOUT を延ばすか、ワールドの肥大化を確認してください。"
                            if exc.timed_out else ""
                        ),
                        source=mode,
                        level="crit",
                        reason=reason,
                    )
                    return

                # --- 停止 → 保留中の設定変更を反映 → （再起動なら）起動 ---
                self.status.phase = "restarting"
                self.status.message = f"サーバを{label}しています"
                try:
                    await self._shutdown(mode, schedule_id)
                except ServiceControlUnavailable as exc:
                    # サーバはまだ落としていない。ここは「失敗」ではあるが
                    # 被害ゼロなので、そうと分かる書き方にする
                    self.status.phase = "failed"
                    self.status.message = (
                        f"サーバを操作できないため{label}を中止しました"
                        "（サーバは動いたままです）"
                    )
                    self.status.finished_at = time.time()
                    await self._announcer.send(
                        f"サーバーの操作ができないため{label}を取りやめました。",
                        source=mode,
                        reason=reason,
                    )
                    await self._announcer.discord_only(
                        f"サーバー{label}を中止しました",
                        f"systemctl を実行できませんでした。**サーバーは落としていません。**\n{exc}",
                        source=mode,
                        level="crit",
                        reason=reason,
                    )
                    return

                self.status.phase = "done"
                self.status.message = f"{label}が完了しました"
                self.status.finished_at = time.time()
                detail = f"理由: {reason}"
                applied = self.status.applied
                if applied and applied.get("applied"):
                    detail += f"\n設定変更 {applied['applied']} 件を反映しました: " + ", ".join(
                        applied.get("keys", [])
                    )
                elif applied and applied.get("error"):
                    detail += f"\n⚠️ 設定変更の反映に失敗しました: {applied['error']}"
                await self._announcer.discord_only(
                    f"サーバー{label}が完了しました",
                    detail,
                    source=mode,
                    reason=reason,
                )

            except asyncio.CancelledError:
                self.status.phase = "cancelled"
                self.status.message = f"{label}はキャンセルされました"
                self.status.finished_at = time.time()
                self._step("cancelled")
                await self._announcer.send(
                    f"サーバー{label}はキャンセルされました。", source=mode, reason=reason
                )
                await self._announcer.discord_only(
                    f"サーバー{label}をキャンセルしました",
                    f"理由: {reason}",
                    source=mode,
                    level="warn",
                    reason=reason,
                )
                raise
            except Exception as exc:  # pragma: no cover - 想定外
                logger.exception("%sシーケンスが異常終了", label)
                self.status.phase = "failed"
                self.status.message = f"{label}に失敗しました: {exc}"
                self.status.finished_at = time.time()
                await self._announcer.discord_only(
                    f"サーバー{label}に失敗しました", str(exc), source=mode, level="crit", reason=reason
                )
            finally:
                self._last_completed_at = time.time()

    async def _announce_countdown(
        self, template: str, offsets: tuple[float, ...], mode: Mode, reason: str
    ) -> None:
        """5分前・1分前・30秒前……とゲーム内に予告しながら待つ。

        予告の送信に失敗しても、シーケンス自体は続行する。
        """
        label = MODE_LABELS.get(mode, mode)
        prev = None
        for offset in offsets:
            delay = (prev - offset) if prev is not None else 0.0
            if delay > 0:
                await asyncio.sleep(delay)
            text = render_template(template, offset, humanize)
            record = await self._announcer.send(text, source=mode, reason=reason)
            self._step("announce", ok=record.ok, detail=f"{humanize(offset)}前: {text}")
            self.status.message = f"{humanize(offset)}後に{label}します"
            prev = offset
        if prev is not None and prev > 0:
            await asyncio.sleep(prev)

    async def _wait_until_down(self) -> None:
        """shutdown API を投げたあと、実際に落ちるまで待つ。

        以前は `min(shutdown_waittime, 5)` 秒だけ寝ていたが、
        実機のワールド保存はもっと時間がかかる。待ちが足りないまま
        systemctl stop に進むと、保存中に SIGTERM を送ることになる。

        逆に固定で長く寝ると、すぐ落ちた場合に無駄な停止時間が延びる。
        REST API に到達できなくなった時点を「落ちた」とみなして先へ進む。
        REST API が無効な構成では判定できないので、その場合は
        shutdown_waittime を待ってから進む。
        """
        deadline = self.shutdown_waittime + self.shutdown_grace
        if deadline <= 0:
            return

        loop = asyncio.get_running_loop()
        end = loop.time() + deadline
        while loop.time() < end:
            try:
                await self._pal.info()
            except PalApiError:
                elapsed = deadline - (end - loop.time())
                self._step("wait_until_down", detail=f"{elapsed:.1f}秒で応答が止まりました")
                return
            await asyncio.sleep(min(self.poll_interval, max(0.0, end - loop.time())))

        self._step(
            "wait_until_down", ok=False,
            detail=f"{deadline:.0f}秒待っても応答が止まりませんでした。停止処理に進みます",
        )

    def _suppress_downtime(self, seconds: float) -> None:
        if self._suppress_alerts is not None:
            self._suppress_alerts(seconds)

    async def _shutdown(self, mode: Mode, schedule_id: str | None = None) -> None:
        label = MODE_LABELS.get(mode, mode)

        # shutdown API を通すとゲームサーバは落ちる。そこから先で systemctl が
        # 通らないと分かっても、起動し直す手段が無いまま落ちたままになる。
        # 引き返せるうちに、サービスを操作できるかどうかだけ確かめておく
        if self.use_systemd:
            check = await self._service.preflight()
            if not check.ok:
                reason = check.stderr or check.stdout or "詳細不明"
                self._step("preflight", ok=False, detail=reason)
                raise ServiceControlUnavailable(reason)

        # ここから先はこちらの意思で落とすので、「応答なし」の通知を止める。
        # 停止待ち + 設定反映 + 起動 + 起動しきるまで、をまとめて覆う長さにする
        self._suppress_downtime(
            self.shutdown_waittime + self.shutdown_grace + self.alert_grace
        )
        try:
            await self._pal.shutdown(
                waittime=self.shutdown_waittime, message=f"サーバーを{label}します"
            )
            self._step("shutdown_api", detail=f"waittime={self.shutdown_waittime}")
        except PalApiError as exc:
            # 既に落ちている場合もあるので警告に留めて systemctl に進む
            self._step("shutdown_api", ok=False, detail=str(exc))

        if not self.use_systemd:
            self._step("systemd_skipped", detail="systemd 未使用（自動再起動に任せる）")
            return

        await self._wait_until_down()

        if mode == "stop":
            result = await self._service.stop()
            self._step("systemctl_stop", ok=result.ok, detail=result.stdout or result.stderr)
            if not result.ok:
                raise RuntimeError(f"停止に失敗: {result.stderr}")
            # 停止したので、保留中の設定変更を書き込める
            await self._apply_pending_changes(schedule_id)
            return

        # --- 再起動 ---
        # 保留中の変更があるときは restart をやめて stop → 書き込み → start に分ける。
        # ini を書き換えられるのはサーバが完全に止まっている間だけなので、
        # restart で一気に上げ直すとその隙間が作れない。
        if not self._has_pending(schedule_id):
            result = await self._service.restart()
            self._step("systemctl_restart", ok=result.ok, detail=result.stdout or result.stderr)
            self._suppress_downtime(self.alert_grace)
            if not result.ok:
                raise RuntimeError(f"再起動に失敗: {result.stderr}{await self._rescue_start()}")
            return

        result = await self._service.stop()
        self._step("systemctl_stop", ok=result.ok, detail=result.stdout or result.stderr)
        if not result.ok:
            # 止まったのか確認できていないので ini は書かない。稼働中に書いても
            # 終了時にゲーム側のメモリ上の設定で上書きされるだけで、事故になる。
            # 保留は残るので次の停止機会に回る
            raise RuntimeError(f"停止に失敗: {result.stderr}{await self._rescue_start()}")

        await self._apply_pending_changes(schedule_id)

        # 反映に失敗していてもサーバは必ず上げ直す。
        # 設定が変わらないより、サーバが落ちたままの方が困る。
        result = await self._service.start()
        self._step("systemctl_start", ok=result.ok, detail=result.stdout or result.stderr)
        # 起動コマンドは即座に返るが、実機が接続を受け付けるまでは数十秒かかる。
        # ここから改めて猶予を取り直す
        self._suppress_downtime(self.alert_grace)
        if not result.ok:
            raise RuntimeError(f"起動に失敗: {result.stderr}")

    async def _rescue_start(self) -> str:
        """systemctl での再起動/停止に失敗したあと、起動だけでも試す。

        shutdown API は既に通してあるのでゲームサーバは落ちている。ここで
        諦めると、誰も気づかないまま朝まで落ちたままになる（issue #28）。
        失敗報告に足す文字列を返す。
        """
        result = await self._service.start()
        self._step("rescue_start", ok=result.ok, detail=result.stdout or result.stderr)
        # 起動コマンドが通っても実機が受け付けるまでは時間がかかる
        self._suppress_downtime(self.alert_grace)
        if result.ok:
            return "\nサーバの起動だけは試みて、そちらは通りました。"
        return f"\nサーバの起動も試みましたが失敗しました: {result.stderr}"

    def _has_pending(self, schedule_id: str | None) -> bool:
        if self._apply_pending is None or self._count_pending is None:
            return False
        return self._count_pending(schedule_id) > 0

    async def _apply_pending_changes(self, schedule_id: str | None) -> None:
        """サーバ停止中に、保留していた設定変更を ini へ書き込む。

        ここで失敗してもシーケンスは止めない（呼び出し側が必ず起動し直す）。
        保留中の変更は消さずに残すので、次の機会に再試行できる。
        """
        if self._apply_pending is None:
            return
        try:
            result = await self._apply_pending(schedule_id)
        except Exception as exc:  # pragma: no cover - 想定外
            logger.exception("保留中の設定変更の反映で想定外のエラー")
            self.status.applied = {"ok": False, "applied": 0, "keys": [], "error": str(exc)}
            self._step("apply_settings", ok=False, detail=str(exc))
            return

        self.status.applied = result.as_dict()
        if result.count == 0 and not result.error:
            return
        self._step(
            "apply_settings",
            ok=result.ok,
            detail=(f"{result.count}件: " + ", ".join(result.keys)) if result.ok else result.error,
        )
        if not result.ok:
            await self._announcer.discord_only(
                "設定変更の反映に失敗しました",
                f"{result.error}\n保留中の変更は残してあるので、次の停止時に再試行されます。",
                source="system",
                level="crit",
            )
