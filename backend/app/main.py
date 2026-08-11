"""Palworld Server Manager - FastAPI アプリ本体。"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .announce import AnnouncementLog, Announcer
from .config import Settings, load_settings
from .logstream import BrokerLogHandler, LogBroker
from .monitor import Monitor
from .notify import DiscordNotifier
from .palapi import PalApiError, PalworldClient
from .restart import (
    RestartDebounced,
    RestartInProgress,
    RestartManager,
    RestartValidationError,
)
from .scheduler import RestartScheduler, ScheduleError
from .services import SystemdService
from .settings_ini import SettingsIniError, SettingsIniStore
from .settings_schema import CATEGORIES, add_fields, build_updates, describe, missing_fields

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# --------------------------------------------------------------------------
# リクエストモデル
# --------------------------------------------------------------------------


class AnnounceBody(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    # Discord にも同じ内容を流すか
    to_discord: bool = False


class PlayerActionBody(BaseModel):
    userid: str = Field(min_length=1)
    message: str = ""


class RestartBody(BaseModel):
    """再起動/停止の実行要求。

    アナウンス文と予告タイミングは必須。無告知でサーバを落とさないため、
    既定値へのフォールバックは用意しない。
    """

    reason: str = "手動"
    announce_message: str = Field(min_length=1, max_length=500)
    notice_offsets: list[float] = Field(min_length=1)
    force: bool = False


class SettingsIniBody(BaseModel):
    text: str | None = None
    options: dict[str, str] | None = None


class RestoreBody(BaseModel):
    name: str


class SettingsFieldsBody(BaseModel):
    """フォームから来た項目単位の値。ini への書式化はサーバ側で行う。

    values    : 既に設定ファイルにある項目の変更
    additions : 設定ファイルに無い項目の追加（明示操作でのみ送られる）
    """

    values: dict[str, Any] = Field(default_factory=dict)
    additions: dict[str, Any] = Field(default_factory=dict)


class ScheduleBody(BaseModel):
    kind: str
    spec: str
    label: str = ""
    enabled: bool = True
    announce_message: str = Field(min_length=1, max_length=500)
    notice_offsets: list[float] = Field(min_length=1)


class SchedulePatchBody(BaseModel):
    kind: str | None = None
    spec: str | None = None
    label: str | None = None
    enabled: bool | None = None
    announce_message: str | None = None
    notice_offsets: list[float] | None = None


class ServiceActionBody(BaseModel):
    reason: str = "手動"


# --------------------------------------------------------------------------
# アプリ生成
# --------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    *,
    pal_client: PalworldClient | None = None,
    notifier: DiscordNotifier | None = None,
    start_background: bool = True,
) -> FastAPI:
    """アプリを組み立てる。

    テストからは pal_client / notifier を差し替え、start_background=False で
    監視ループとスケジューラを止めた状態で使う。
    """
    cfg = settings or load_settings()

    pal = pal_client or PalworldClient(
        cfg.pal_base_url,
        cfg.pal_admin_user,
        cfg.pal_admin_password,
        timeout=cfg.pal_timeout,
    )
    notify = notifier or DiscordNotifier(cfg.discord_webhook_url, cfg.discord_alert_webhook_url)
    service = SystemdService(cfg.pal_service_name, dry_run=cfg.dry_run)
    ini_store = SettingsIniStore(cfg.pal_settings_ini, cfg.backup_dir, keep=cfg.backup_keep)
    monitor = Monitor(
        pal,
        notify,
        interval=cfg.monitor_interval,
        history_size=cfg.history_size,
        mem_warn_percent=cfg.mem_warn_percent,
        mem_crit_percent=cfg.mem_crit_percent,
        alert_cooldown_sec=cfg.alert_cooldown_sec,
    )
    announce_log = AnnouncementLog(cfg.announce_store, limit=cfg.announce_history_limit)
    announcer = Announcer(pal, notify, announce_log)
    restart_manager = RestartManager(
        pal,
        announcer,
        service,
        notice_offsets=cfg.notice_offsets,
        announce_template=cfg.restart_announce_template,
        debounce_sec=cfg.restart_debounce_sec,
        shutdown_waittime=cfg.restart_shutdown_wait,
    )
    scheduler = RestartScheduler(
        restart_manager,
        timezone=cfg.schedule_timezone,
        store_path=cfg.schedule_store,
    )
    broker = LogBroker()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        broker.bind_loop()
        handler = BrokerLogHandler(broker)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        handler.setLevel(logging.INFO)
        app_logger = logging.getLogger("app")
        app_logger.addHandler(handler)
        # 既定ではルートの WARNING を継承してしまい、
        # 再起動シーケンスの INFO ログがハンドラに届かない
        previous_level = app_logger.level
        app_logger.setLevel(logging.INFO)
        if start_background:
            monitor.start()
            try:
                scheduler.start()
            except Exception:  # pragma: no cover - タイムゾーン設定ミス等
                logger.exception("スケジューラの起動に失敗しました")
            await broker.start(cfg.log_source, unit=cfg.pal_service_name, path=str(cfg.log_file))
            await announcer.discord_only(
                "管理ツールを起動しました", f"環境: {cfg.env}", source="system"
            )
        try:
            yield
        finally:
            if start_background:
                await announcer.discord_only(
                    "管理ツールを停止しました", f"環境: {cfg.env}", source="system", level="warn"
                )
                await monitor.stop()
                scheduler.shutdown()
                await broker.stop()
            app_logger.removeHandler(handler)
            app_logger.setLevel(previous_level)
            await pal.aclose()
            await notify.aclose()

    app = FastAPI(title="Palworld Server Manager", version="0.1.0", lifespan=lifespan)

    # 依存から触れるようにまとめて持たせる
    app.state.cfg = cfg
    app.state.pal = pal
    app.state.notifier = notify
    app.state.service = service
    app.state.ini_store = ini_store
    app.state.monitor = monitor
    app.state.announcer = announcer
    app.state.announce_log = announce_log
    app.state.restart = restart_manager
    app.state.scheduler = scheduler
    app.state.broker = broker

    _security = HTTPBasic(auto_error=False)

    def require_auth(creds: HTTPBasicCredentials | None = Depends(_security)) -> None:
        if not cfg.app_password:
            return
        if creds is None:
            raise HTTPException(401, "認証が必要です", headers={"WWW-Authenticate": "Basic"})
        ok = secrets.compare_digest(creds.username, cfg.app_user) and secrets.compare_digest(
            creds.password, cfg.app_password
        )
        if not ok:
            raise HTTPException(401, "認証に失敗しました", headers={"WWW-Authenticate": "Basic"})

    auth = [Depends(require_auth)]

    @app.exception_handler(PalApiError)
    async def _pal_error_handler(request: Request, exc: PalApiError) -> JSONResponse:
        # ゲームサーバ側の障害は 502 として返し、UI で「サーバ応答なし」を出す
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    # ---- 画面 ----------------------------------------------------------

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        index_path = STATIC_DIR / "index.html"
        if not index_path.is_file():
            raise HTTPException(404, "index.html がありません")
        return FileResponse(index_path)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ---- 基本情報 ------------------------------------------------------

    @app.get("/api/config", dependencies=auth)
    async def get_config() -> dict[str, Any]:
        return cfg.public_dict()

    @app.get("/api/status", dependencies=auth)
    async def get_status() -> dict[str, Any]:
        """ダッシュボードが1秒ごとに叩く。ゲームサーバが落ちていても 200 を返す。"""
        online = True
        error: str | None = None
        info: dict[str, Any] = {}
        metrics: dict[str, Any] = {}
        try:
            info = await pal.info()
            metrics = await pal.metrics()
        except PalApiError as exc:
            online = False
            error = str(exc)

        host = monitor._host_stats()
        return {
            "online": online,
            "error": error,
            "env": cfg.env,
            "info": info,
            "metrics": metrics,
            "host": host,
            "restart": restart_manager.status.as_dict(),
            "log_subscribers": broker.subscriber_count,
        }

    @app.get("/api/history", dependencies=auth)
    async def get_history(minutes: int = Query(60, ge=1, le=1440)) -> dict[str, Any]:
        return {
            "interval": cfg.monitor_interval,
            "records": monitor.history_since(minutes * 60),
        }

    @app.post("/api/sample", dependencies=auth)
    async def force_sample() -> dict[str, Any]:
        """履歴を今すぐ1点取る（テスト・手動確認用）。"""
        return await monitor.sample()

    # ---- プレイヤー ----------------------------------------------------

    @app.get("/api/players", dependencies=auth)
    async def get_players() -> dict[str, Any]:
        players = await pal.players()
        return {"players": players, "count": len(players)}

    @app.post("/api/players/kick", dependencies=auth)
    async def kick_player(body: PlayerActionBody) -> dict[str, Any]:
        if cfg.dry_run:
            return {"result": "skipped", "reason": "dry_run", "userid": body.userid}
        await pal.kick(body.userid, body.message)
        await notify.send("プレイヤーをキックしました", f"{body.userid}\n{body.message}", "warn")
        return {"result": "ok", "userid": body.userid}

    @app.post("/api/players/ban", dependencies=auth)
    async def ban_player(body: PlayerActionBody) -> dict[str, Any]:
        if cfg.dry_run:
            return {"result": "skipped", "reason": "dry_run", "userid": body.userid}
        await pal.ban(body.userid, body.message)
        await notify.send("プレイヤーをBANしました", f"{body.userid}\n{body.message}", "warn")
        return {"result": "ok", "userid": body.userid}

    @app.post("/api/players/unban", dependencies=auth)
    async def unban_player(body: PlayerActionBody) -> dict[str, Any]:
        await pal.unban(body.userid)
        return {"result": "ok", "userid": body.userid}

    @app.get("/api/world", dependencies=auth)
    async def get_world() -> dict[str, Any]:
        """マップ表示用。座標が取れるのは接続中プレイヤーのみ。

        REST API はパル/NPC の位置を公開していないため、
        ここで返すのはプレイヤーの座標と異常検知のみ。
        """
        players = await pal.players()
        points = []
        anomalies = []
        for p in players:
            points.append(
                {
                    "name": p.get("name", "?"),
                    "userId": p.get("userId", ""),
                    "x": p.get("location_x"),
                    "y": p.get("location_y"),
                    "level": p.get("level"),
                    "ping": p.get("ping"),
                    "building_count": p.get("building_count"),
                }
            )
            ping = p.get("ping")
            if isinstance(ping, (int, float)) and ping >= 150:
                anomalies.append({"type": "high_ping", "name": p.get("name"), "value": ping})
            buildings = p.get("building_count")
            if isinstance(buildings, (int, float)) and buildings >= 500:
                anomalies.append(
                    {"type": "many_buildings", "name": p.get("name"), "value": buildings}
                )
        return {"points": points, "anomalies": anomalies, "count": len(points)}

    # ---- サーバ操作 ----------------------------------------------------

    @app.post("/api/announce", dependencies=auth)
    async def announce(body: AnnounceBody) -> dict[str, Any]:
        record = await announcer.send(
            body.message,
            source="manual",
            to_discord=body.to_discord,
            raise_on_error=True,
        )
        return {"result": "ok", "record": record.as_dict()}

    @app.get("/api/announcements", dependencies=auth)
    async def list_announcements(
        limit: int = Query(100, ge=1, le=500),
        source: str | None = Query(None),
    ) -> dict[str, Any]:
        return {"records": announce_log.list(limit=limit, source=source), "total": len(announce_log)}

    @app.delete("/api/announcements", dependencies=auth)
    async def clear_announcements() -> dict[str, int]:
        return {"cleared": announce_log.clear()}

    @app.post("/api/save", dependencies=auth)
    async def save_world() -> dict[str, str]:
        await pal.save()
        return {"result": "ok"}

    @app.get("/api/settings", dependencies=auth)
    async def get_server_settings() -> dict[str, Any]:
        return await pal.settings()

    @app.get("/api/restart", dependencies=auth)
    async def get_restart_status() -> dict[str, Any]:
        return restart_manager.status.as_dict()

    async def _run_sequence(body: RestartBody, mode: str) -> dict[str, Any]:
        try:
            status = await restart_manager.request(
                reason=body.reason,
                announce_message=body.announce_message,
                notice_offsets=body.notice_offsets,
                mode=mode,  # type: ignore[arg-type]
                force=body.force,
            )
        except RestartValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RestartInProgress as exc:
            raise HTTPException(409, str(exc)) from exc
        except RestartDebounced as exc:
            raise HTTPException(429, str(exc)) from exc
        return status.as_dict()

    @app.post("/api/restart", dependencies=auth)
    async def start_restart(body: RestartBody) -> dict[str, Any]:
        """予告 → ワールド保存 → 停止 → 起動。"""
        return await _run_sequence(body, "restart")

    @app.post("/api/shutdown", dependencies=auth)
    async def start_shutdown(body: RestartBody) -> dict[str, Any]:
        """予告 → ワールド保存 → 停止（起動はしない）。"""
        return await _run_sequence(body, "stop")

    @app.post("/api/restart/cancel", dependencies=auth)
    async def cancel_restart() -> dict[str, Any]:
        cancelled = restart_manager.cancel()
        if not cancelled:
            raise HTTPException(409, "キャンセルできる再起動予告がありません")
        # キャンセル処理が status に反映されるまで少しだけ待つ
        await asyncio.sleep(0)
        return {"result": "cancelled"}

    @app.post("/api/service/{action}", dependencies=auth)
    async def service_action(action: str, body: ServiceActionBody | None = None) -> dict[str, Any]:
        """systemd ユニットを直接操作する。

        停止中のサーバにはアナウンスを送れないので、起動はここから即時実行する。
        停止と再起動は予告アナウンスを伴う /api/shutdown と /api/restart を使うこと
        （緊急時のためにここからも叩けるようにはしてある）。
        """
        if action not in ("start", "stop", "restart"):
            raise HTTPException(400, "action は start/stop/restart のいずれかです")
        reason = body.reason if body else "手動"
        result = await getattr(service, action)()
        if not result.ok:
            raise HTTPException(500, result.stderr or "systemctl の実行に失敗しました")
        labels = {"start": "起動", "stop": "停止", "restart": "再起動"}
        await announcer.discord_only(
            f"サーバーを{labels[action]}しました",
            f"systemctl {action} {cfg.pal_service_name}\n理由: {reason}",
            source="system",
            reason=reason,
        )
        return result.as_dict()

    # ---- PalWorldSettings.ini ------------------------------------------

    @app.get("/api/settings-ini", dependencies=auth)
    async def get_ini() -> dict[str, Any]:
        if not ini_store.exists():
            return {
                "exists": False,
                "path": str(cfg.pal_settings_ini),
                "text": "",
                "options": {},
                "backups": [],
            }
        text = ini_store.read_text()
        return {
            "exists": True,
            "path": str(cfg.pal_settings_ini),
            "text": text,
            "options": ini_store.read_options(),
            "backups": [
                {"name": b.name, "size": b.size, "created_at": b.created_at}
                for b in ini_store.list_backups()
            ],
        }

    @app.put("/api/settings-ini", dependencies=auth)
    async def put_ini(body: SettingsIniBody) -> dict[str, Any]:
        if body.text is None and body.options is None:
            raise HTTPException(400, "text か options のどちらかを指定してください")
        try:
            if body.text is not None:
                backup = ini_store.write_text(body.text)
            else:
                backup = ini_store.update_options(body.options or {})
        except SettingsIniError as exc:
            raise HTTPException(400, str(exc)) from exc
        await notify.send(
            "サーバ設定を更新しました",
            f"バックアップ: {backup.name}\n反映にはサーバの再起動が必要です。",
            "info",
        )
        return {"result": "ok", "backup": backup.name, "restart_required": True}

    @app.get("/api/settings-ini/fields", dependencies=auth)
    async def get_ini_fields() -> dict[str, Any]:
        """ini を項目ごとのフォーム定義として返す。

        返すのは実際にファイルへ書かれているキーだけ。
        スキーマに無い項目も値から型を推論して編集できるようにする。
        """
        if not ini_store.exists():
            return {
                "exists": False,
                "path": str(cfg.pal_settings_ini),
                "categories": [],
                "category_labels": [{"key": k, "label": v} for k, v in CATEGORIES],
            }
        options = ini_store.read_options()
        return {
            "exists": True,
            "path": str(cfg.pal_settings_ini),
            "categories": describe(options),
            # 設定ファイルに書かれていない項目。ゲーム側の既定値で動いているので、
            # 変えたいならユーザーが明示的に追加する
            "available": missing_fields(options),
            "category_labels": [{"key": k, "label": v} for k, v in CATEGORIES],
        }

    @app.put("/api/settings-ini/fields", dependencies=auth)
    async def put_ini_fields(body: SettingsFieldsBody) -> dict[str, Any]:
        """変更した項目だけを受け取って ini に反映する。"""
        if not ini_store.exists():
            raise HTTPException(404, f"設定ファイルが見つかりません: {cfg.pal_settings_ini}")

        if not body.values and not body.additions:
            raise HTTPException(400, "更新する項目がありません")

        options = ini_store.read_options()
        updates, errors = build_updates(body.values, options)
        added, add_errors = add_fields(body.additions, options)
        errors += add_errors
        if errors:
            raise HTTPException(400, " / ".join(errors))

        # 値が変わっていないものは書かない（無駄なバックアップを増やさない）
        changed = {k: v for k, v in updates.items() if options.get(k) != v}
        changed.update(added)
        if not changed:
            return {"result": "unchanged", "changed": {}, "added": [], "restart_required": False}

        try:
            backup = ini_store.update_options(changed)
        except SettingsIniError as exc:
            raise HTTPException(400, str(exc)) from exc

        detail = "変更: " + ", ".join(f"{k}={v}" for k, v in changed.items())
        if added:
            detail += "\n（うち新規追加: " + ", ".join(added) + "）"
        await notify.send(
            "サーバ設定を更新しました",
            detail + f"\nバックアップ: {backup.name}\n反映にはサーバの再起動が必要です。",
            "info",
        )
        return {
            "result": "ok",
            "backup": backup.name,
            "changed": changed,
            "added": sorted(added),
            "restart_required": True,
        }

    @app.post("/api/settings-ini/restore", dependencies=auth)
    async def restore_ini(body: RestoreBody) -> dict[str, Any]:
        try:
            ini_store.restore(body.name)
        except SettingsIniError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"result": "ok", "restored": body.name, "restart_required": True}

    # ---- スケジュール --------------------------------------------------

    @app.get("/api/schedules", dependencies=auth)
    async def list_schedules() -> dict[str, Any]:
        return {"schedules": scheduler.list(), "timezone": cfg.schedule_timezone}

    @app.post("/api/schedules", dependencies=auth)
    async def add_schedule(body: ScheduleBody) -> dict[str, Any]:
        try:
            return scheduler.add(
                body.kind,
                body.spec,
                body.label,
                body.enabled,
                announce_message=body.announce_message,
                notice_offsets=body.notice_offsets,
            )
        except ScheduleError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.patch("/api/schedules/{schedule_id}", dependencies=auth)
    async def patch_schedule(schedule_id: str, body: SchedulePatchBody) -> dict[str, Any]:
        try:
            return scheduler.update(schedule_id, **body.model_dump(exclude_none=True))
        except ScheduleError as exc:
            code = 404 if "見つかりません" in str(exc) else 400
            raise HTTPException(code, str(exc)) from exc

    @app.delete("/api/schedules/{schedule_id}", dependencies=auth)
    async def delete_schedule(schedule_id: str) -> dict[str, str]:
        try:
            scheduler.remove(schedule_id)
        except ScheduleError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"result": "ok"}

    # ---- ログ ----------------------------------------------------------

    @app.get("/api/logs", dependencies=auth)
    async def get_logs() -> dict[str, Any]:
        return {"lines": broker.backlog()}

    @app.websocket("/ws/logs")
    async def ws_logs(websocket: WebSocket) -> None:
        if cfg.app_password:
            # WebSocket では Basic 認証ダイアログが使えないので、
            # ブラウザが自動付与する Authorization ヘッダを検証する
            header = websocket.headers.get("authorization", "")
            if not _check_basic_header(header, cfg.app_user, cfg.app_password):
                await websocket.close(code=1008)
                return
        await websocket.accept()
        try:
            async for record in broker.subscribe():
                await websocket.send_json(record)
        except WebSocketDisconnect:
            pass
        except (RuntimeError, asyncio.CancelledError):
            pass

    return app


def _check_basic_header(header: str, user: str, password: str) -> bool:
    import base64

    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
        got_user, _, got_pass = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(got_user, user) and secrets.compare_digest(got_pass, password)


app = create_app()
