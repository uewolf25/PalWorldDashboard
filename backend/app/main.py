"""Palworld Server Manager - FastAPI アプリ本体。"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi import Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .announce import AnnouncementLog, Announcer
from .auth import (
    COOKIE_NAME,
    LoginThrottle,
    issue_token,
    load_or_create_secret,
    token_expiry,
    verify_token,
)
from .cache import TTLCache
from .config import Settings, load_settings
from .health import ServerHealth
from .logstream import BrokerLogHandler, LogBroker, configure_logging
from .monitor import Monitor
from .notify import DiscordNotifier
from .palapi import PalApiError, PalworldClient
from .pending import ApplyResult, PendingChangeStore
from .presence import PresenceTracker
from .restart import (
    RestartDebounced,
    RestartInProgress,
    RestartManager,
    RestartValidationError,
)
from .scheduler import ACTION_LABELS, ACTIONS, ScheduleError, ServerScheduler
from .services import build_service
from .settings_ini import SettingsIniError, SettingsIniStore
from .world import WorldBackupError, WorldStore
from .settings_schema import (
    CATEGORIES,
    add_custom_fields,
    add_fields,
    build_updates,
    describe,
    discovered_fields,
    mask_ini_text,
    mask_values,
    missing_fields,
)

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
    # ゲームサーバ稼働中でも書き込む
    force: bool = False


class RestoreBody(BaseModel):
    name: str


class CustomFieldBody(BaseModel):
    """スキーマに無い新しいプロパティ。型はユーザーが指定する。"""

    name: str = Field(min_length=1, max_length=100)
    type: str
    value: Any = None


class SettingsFieldsBody(BaseModel):
    """フォームから来た項目単位の値。ini への書式化はサーバ側で行う。

    values            : 既に設定ファイルにある項目の変更
    additions         : スキーマ定義のある未設定項目の追加
    custom_additions  : スキーマに無い新しいプロパティの追加（名前と型を明示）
    force             : ゲームサーバ稼働中でも書き込む（下の注意を参照）
    """

    values: dict[str, Any] = Field(default_factory=dict)
    additions: dict[str, Any] = Field(default_factory=dict)
    custom_additions: list[CustomFieldBody] = Field(default_factory=list)
    force: bool = False

    # いつ反映するか
    #   now       : すぐ ini に書く（サーバ停止中のみ）
    #   next_stop : 次にサーバが停止したときに反映（予約・手動どちらでも）
    #   schedule  : 指定した予約のときに反映
    when: Literal["now", "next_stop", "schedule"] = "now"
    schedule_id: str | None = None
    note: str = ""


class ScheduleBody(BaseModel):
    """サーバ状態変更の予約。

    action が restart / stop のときはアナウンス文と予告タイミングが必須
    （無告知でサーバを落とさないため）。start は停止中のサーバに
    アナウンスを送れないので不要。必須判定はスケジューラ側で行う。
    """

    kind: str
    spec: str
    label: str = ""
    enabled: bool = True
    action: str = "restart"
    announce_message: str = Field("", max_length=500)
    notice_offsets: list[float] = Field(default_factory=list)


class SchedulePatchBody(BaseModel):
    kind: str | None = None
    spec: str | None = None
    label: str | None = None
    enabled: bool | None = None
    action: str | None = None
    announce_message: str | None = None
    notice_offsets: list[float] | None = None
    # 次の1回だけ見送る。無効化と違って、その次からはまた動く
    skip_next: bool | None = None


class ServiceActionBody(BaseModel):
    reason: str = "手動"


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


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

    if start_background:
        # テストでは付けない。pytest の捕捉と二重になるだけで得が無い
        configure_logging(cfg.log_level)

    pal = pal_client or PalworldClient(
        cfg.pal_base_url,
        cfg.pal_admin_user,
        cfg.pal_admin_password,
        timeout=cfg.pal_timeout,
        slow_timeout=cfg.pal_slow_timeout,
    )
    notify = notifier or DiscordNotifier(cfg.discord_webhook_url, cfg.discord_alert_webhook_url)
    service = build_service(
        cfg.pal_service_backend,
        unit=cfg.pal_service_name,
        dry_run=cfg.dry_run,
        mock_control_url=cfg.pal_mock_control_url,
        use_sudo=cfg.pal_systemctl_sudo,
        command=cfg.pal_service_command,
        timeout=cfg.pal_service_timeout,
    )
    # LinuxGSM は journald ではなく自前のログファイルに書く。組み合わせが
    # ちぐはぐだと、ログ画面の server 区分が黙って空になるだけで気づけない
    if cfg.pal_service_backend == "lgsm" and cfg.log_source == "journald":
        logger.warning(
            "LOG_SOURCE=journald ですが LinuxGSM 構成です。ゲームサーバは journald に"
            "書かないので、ログ画面の server 区分は空のままになります。"
            "LOG_SOURCE=file と LOG_FILE=<LinuxGSM の console ログ> にしてください"
        )

    ini_store = SettingsIniStore(cfg.pal_settings_ini, cfg.backup_dir, keep=cfg.backup_keep)
    world_store = WorldStore(
        cfg.pal_save_dir, cfg.world_backup_dir, keep=cfg.world_backup_keep
    )
    # ログインのセッション。鍵はファイルに永続化して、再起動でログインが
    # 切れないようにする
    session_secret = load_or_create_secret(cfg.session_secret_file, cfg.app_session_secret)
    throttle = LoginThrottle(
        max_attempts=cfg.app_login_max_attempts,
        lockout=cfg.app_login_lockout_sec,
    )
    monitor = Monitor(
        pal,
        notify,
        interval=cfg.monitor_interval,
        history_size=cfg.history_size,
        mem_warn_percent=cfg.mem_warn_percent,
        mem_crit_percent=cfg.mem_crit_percent,
        alert_cooldown_sec=cfg.alert_cooldown_sec,
    )
    # ゲームサーバへの問い合わせを間引く。画面のタブ数に比例して
    # 負荷が増えないよう、TTL 内は結果を使い回し、同時アクセスは合流させる
    status_cache: TTLCache[dict[str, Any]] = TTLCache(cfg.status_cache_sec)
    players_cache: TTLCache[list[dict[str, Any]]] = TTLCache(cfg.status_cache_sec)

    announce_log = AnnouncementLog(cfg.announce_store, limit=cfg.announce_history_limit)
    announcer = Announcer(pal, notify, announce_log)
    pending = PendingChangeStore(cfg.pending_store, limit=cfg.pending_limit)
    presence = PresenceTracker(cfg.presence_store, limit=cfg.presence_history_limit)

    async def _fetch_players() -> list[dict[str, Any]]:
        """プレイヤー一覧の取得口。ここを通れば必ず入退室が記録される。

        REST API に入退室イベントは無いので、スナップショットの差分で作るしかない。
        取得口を1つに絞っておかないと、経路によって記録されたりされなかったりする。
        """
        players = await pal.players()
        presence.observe(players)
        return players

    async def apply_pending_changes(schedule_id: str | None) -> ApplyResult:
        """サーバ停止中に呼ばれ、保留していた設定変更を ini へ書き込む。

        失敗しても例外は投げない。呼び出し側（停止シーケンス）は
        この結果に関わらずサーバを起動し直す必要がある。
        """
        items = pending.due_for(schedule_id)
        if not items:
            return ApplyResult(ok=True)

        updates = pending.merged(items)
        try:
            backup = ini_store.update_options(updates)
        except SettingsIniError as exc:
            logger.warning("保留中の設定変更を反映できませんでした: %s", exc)
            # 保留は消さない。次の停止機会に再試行する
            return ApplyResult(ok=False, keys=sorted(updates), error=str(exc))

        pending.remove_many([i.id for i in items])
        return ApplyResult(
            ok=True,
            applied_ids=[i.id for i in items],
            keys=sorted(updates),
            backup=backup.name,
        )

    # 「サーバが生きているか」の判定はここ1つに寄せる。停止待ち・起動待ち・
    # ini を書いてよいかの判断が、同じ材料と同じ解釈を使うようにするため
    health = ServerHealth(pal, service)

    restart_manager = RestartManager(
        pal,
        announcer,
        service,
        health=health,
        notice_offsets=cfg.notice_offsets,
        announce_template=cfg.restart_announce_template,
        debounce_sec=cfg.restart_debounce_sec,
        shutdown_waittime=cfg.restart_shutdown_wait,
        shutdown_grace=cfg.restart_shutdown_grace,
        sequence_timeout=cfg.restart_sequence_timeout,
        apply_pending=apply_pending_changes,
        count_pending=lambda sid: len(pending.due_for(sid)),
        # 意図的に落としている間は「応答なし」を通知しない
        suppress_alerts=monitor.suppress_downtime_alerts,
        alert_grace=cfg.restart_alert_grace,
        startup_timeout=cfg.restart_startup_timeout,
    )
    # シーケンス進行中も同様に抑止する（猶予の計算に取りこぼしがあっても効くように）
    monitor.set_maintenance_probe(lambda: restart_manager.in_progress)

    async def _observe_presence() -> None:
        """監視ループから入退室を観測する。

        画面のポーリング任せにすると、誰も開いていない間の出入りが丸ごと
        抜ける。サーバが落ちているときは取れないので黙って諦める。
        """
        try:
            await players_cache.get(_fetch_players)
        except PalApiError:
            pass

    monitor.set_after_sample(_observe_presence)
    scheduler = ServerScheduler(
        restart_manager,
        service,
        announcer,
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
        # 再起動シーケンスの INFO ログがハンドラに届かない。
        # LOG_LEVEL=DEBUG で立てている場合はそれを潰さない
        previous_level = app_logger.level
        if previous_level == logging.NOTSET or previous_level > logging.INFO:
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
            await service.aclose()

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
    app.state.pending = pending
    app.state.throttle = throttle
    app.state.status_cache = status_cache
    app.state.players_cache = players_cache
    app.state.restart = restart_manager
    app.state.scheduler = scheduler
    app.state.broker = broker

    _security = HTTPBasic(auto_error=False)

    def _session_ok(request: Request) -> bool:
        token = request.cookies.get(COOKIE_NAME, "")
        return verify_token(session_secret, token)

    def _basic_ok(creds: HTTPBasicCredentials | None) -> bool:
        if creds is None:
            return False
        return secrets.compare_digest(creds.username, cfg.app_user) and secrets.compare_digest(
            creds.password, cfg.app_password
        )

    def is_authenticated(
        request: Request, creds: HTTPBasicCredentials | None = Depends(_security)
    ) -> bool:
        """ログイン済みのセッション、または Basic 認証が通っているか。

        ブラウザはログイン画面で Cookie を得る。curl やスクリプトからは
        Basic 認証の方が扱いやすいので、そちらも受け付ける。

        認証を掛けていない構成（APP_PASSWORD 未設定）では常に True。
        """
        if not cfg.app_password:
            return True
        return _session_ok(request) or _basic_ok(creds)

    def require_auth(authenticated: bool = Depends(is_authenticated)) -> None:
        """操作系のエンドポイントに掛ける。

        未ログインでも閲覧はできる（Issue #15）。状態が変わるものだけを止める。
        閲覧系のうちパスワードを含むものは、止めるのではなく値を伏せる。
        """
        if authenticated:
            return
        # WWW-Authenticate を返すとブラウザが標準ダイアログを出してしまい、
        # 自前のログイン画面と二重になる。ここでは付けない
        raise HTTPException(401, "この操作にはログインが必要です")

    # 操作系に付ける。閲覧系には付けない
    auth = [Depends(require_auth)]

    async def game_server_running() -> bool:
        """ゲームサーバが動いているか。

        ini の書き換えは停止中にしか安全に行えないため、その判定に使う。
        判定そのものは ServerHealth に一本化してある（app/health.py）。
        """
        return await health.running()

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

    # ---- ログイン ------------------------------------------------------

    def _client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _cookie_secure(request: Request) -> bool:
        # リバースプロキシ越しの TLS も見る
        forwarded = request.headers.get("x-forwarded-proto", "")
        return request.url.scheme == "https" or forwarded.split(",")[0].strip() == "https"

    @app.get("/api/auth/status")
    async def auth_status(request: Request) -> dict[str, Any]:
        """ログイン画面を出すかどうかを画面が判断するために使う。

        ここだけは未認証で叩ける（認証が要るかどうかを知る手段が要るため）。
        """
        return {
            "required": bool(cfg.app_password),
            "authenticated": (not cfg.app_password) or _session_ok(request),
        }

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        if not cfg.app_password:
            return {"result": "ok", "required": False}

        key = _client_key(request)
        wait = throttle.retry_after(key)
        if wait > 0:
            raise HTTPException(
                429, f"ログインの試行が多すぎます。{int(wait)} 秒後にもう一度お試しください"
            )

        # 両方を必ず比較する。先に ID を判定して打ち切ると、
        # 応答の速さから「ID は合っている」ことが読み取れてしまう
        user_ok = secrets.compare_digest(body.username, cfg.app_user)
        password_ok = secrets.compare_digest(body.password, cfg.app_password)
        if not (user_ok and password_ok):
            locked = throttle.record_failure(key)
            logger.warning("ログインに失敗しました (%s)", key)
            if locked:
                raise HTTPException(
                    429, f"ログインの試行が多すぎます。{int(locked)} 秒後にもう一度お試しください"
                )
            # どちらが違うかは伝えない。ID の総当たりに手掛かりを与えないため
            raise HTTPException(401, "ログインIDまたはパスワードが違います")

        throttle.record_success(key)
        token = issue_token(session_secret, cfg.app_session_ttl)
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=int(cfg.app_session_ttl),
            httponly=True,           # JavaScript から読めないようにする
            samesite="lax",
            secure=_cookie_secure(request),
            path="/",
        )
        logger.info("ログインしました (%s)", key)
        return {"result": "ok", "expires_at": token_expiry(token)}

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, str]:
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"result": "ok"}

    # ---- 基本情報 ------------------------------------------------------

    @app.get("/api/config")
    async def get_config(authenticated: bool = Depends(is_authenticated)) -> dict[str, Any]:
        return cfg.public_dict(authenticated)

    async def _fetch_status() -> dict[str, Any]:
        """ゲームサーバへの問い合わせ部分だけ。キャッシュの対象。"""
        try:
            return {"online": True, "error": None,
                    "info": await pal.info(), "metrics": await pal.metrics()}
        except PalApiError as exc:
            return {"online": False, "error": str(exc), "info": {}, "metrics": {}}

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        """ダッシュボードが1秒ごとに叩く。ゲームサーバが落ちていても 200 を返す。

        画面のタブを何枚開いてもゲームサーバへの問い合わせが増えないよう、
        ここだけ短時間キャッシュする。ホスト側の統計は安いのでそのまま取る。
        """
        upstream = await status_cache.get(_fetch_status)
        online, error = upstream["online"], upstream["error"]
        info, metrics = upstream["info"], upstream["metrics"]

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

    @app.get("/api/history")
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

    @app.get("/api/players")
    async def get_players() -> dict[str, Any]:
        # プレイヤータブとワールドタブが同時に見るので、ここも合流させる
        players = await players_cache.get(_fetch_players)
        return {
            "players": presence.annotate(players),
            "count": len(players),
        }

    @app.get("/api/players/events")
    async def get_presence_events(
        limit: int = Query(50, ge=1, le=500),
        kind: str | None = Query(None, pattern="^(join|leave)$"),
    ) -> dict[str, Any]:
        """入退室の履歴。

        REST API に入退室イベントは無いため、一覧のスナップショットの差分から
        組み立てている。取得の間隔より短い出入りは記録されない。
        """
        return {
            "events": presence.list(limit=limit, kind=kind),
            "total": len(presence),
            # 画面で「取りこぼしがありうる」ことを説明するために返す
            "poll_interval": cfg.monitor_interval,
        }

    @app.post("/api/players/kick", dependencies=auth)
    async def kick_player(body: PlayerActionBody) -> dict[str, Any]:
        if cfg.dry_run:
            return {"result": "skipped", "reason": "dry_run", "userid": body.userid}
        await pal.kick(body.userid, body.message)
        players_cache.invalidate()   # 消えた直後に古い一覧を見せない
        presence.forget(body.userid)  # 次の観測を待たずに退出として確定させる
        await notify.send("プレイヤーをキックしました", f"{body.userid}\n{body.message}", "warn")
        return {"result": "ok", "userid": body.userid}

    @app.post("/api/players/ban", dependencies=auth)
    async def ban_player(body: PlayerActionBody) -> dict[str, Any]:
        if cfg.dry_run:
            return {"result": "skipped", "reason": "dry_run", "userid": body.userid}
        await pal.ban(body.userid, body.message)
        players_cache.invalidate()
        presence.forget(body.userid)
        await notify.send("プレイヤーをBANしました", f"{body.userid}\n{body.message}", "warn")
        return {"result": "ok", "userid": body.userid}

    @app.post("/api/players/unban", dependencies=auth)
    async def unban_player(body: PlayerActionBody) -> dict[str, Any]:
        await pal.unban(body.userid)
        return {"result": "ok", "userid": body.userid}

    @app.get("/api/world")
    async def get_world() -> dict[str, Any]:
        """マップ表示用。座標が取れるのは接続中プレイヤーのみ。

        REST API はパル/NPC の位置を公開していないため、
        ここで返すのはプレイヤーの座標と異常検知のみ。
        """
        players = await players_cache.get(_fetch_players)
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

    @app.get("/api/announcements")
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

    # ---- ワールドセーブ --------------------------------------------------

    @app.get("/api/world/backups")
    async def list_world_backups() -> dict[str, Any]:
        """セーブの状況とバックアップ一覧。

        サイズの集計はディレクトリ全体を歩くので、数百MBあると数十ms〜かかる。
        イベントループを止めないようスレッドに逃がす。
        """
        stats = await asyncio.to_thread(world_store.stats)
        backups = await asyncio.to_thread(world_store.list_backups)
        return {
            **stats,
            "backups": [b.as_dict() for b in backups],
            "keep": cfg.world_backup_keep,
            # 復元できるかの判定に使う
            "server_running": await game_server_running(),
        }

    @app.post("/api/world/backups", dependencies=auth)
    async def create_world_backup() -> dict[str, Any]:
        """セーブディレクトリを固める。

        稼働中でも取れるようにしてあるが、その場合は先にワールド保存を挟む。
        ゲームが書いている最中のファイルを固めると中身が食い違うため。
        それでも停止中に取る方が確実であることは画面に出している。
        """
        saved = False
        if await game_server_running():
            try:
                await pal.save()
                saved = True
            except PalApiError as exc:
                # 保存できなくてもバックアップ自体は取る。
                # 「取れなかった」より「少し古いかもしれない」方がまし
                logger.warning("バックアップ前のワールド保存に失敗: %s", exc)

        try:
            backup = await asyncio.to_thread(world_store.create)
        except WorldBackupError as exc:
            raise HTTPException(400, str(exc)) from exc

        await notify.send(
            "ワールドをバックアップしました",
            f"{backup.name}\n{backup.size / 1024 / 1024:.1f} MB",
            "info",
        )
        return {"result": "ok", "backup": backup.as_dict(), "world_saved_first": saved}

    @app.get("/api/world/backups/{name}/download", dependencies=auth)
    async def download_world_backup(name: str) -> FileResponse:
        """バックアップをダウンロードする。

        中身はワールドそのもので、伏字にできる類のものでもない。
        閲覧しかできない相手には渡さないので、ここは認証を要求する。
        """
        try:
            path = world_store.resolve(name)
        except WorldBackupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(path, filename=path.name, media_type="application/gzip")

    @app.post("/api/world/backups/{name}/restore", dependencies=auth)
    async def restore_world_backup(name: str) -> dict[str, Any]:
        """バックアップでセーブを置き換える。**停止中のみ。**

        稼働中に差し替えても、ゲームが持っているメモリ上の状態で
        上書きされて消える。ini と同じ理屈。
        """
        if await game_server_running():
            raise HTTPException(
                409,
                "ゲームサーバが稼働中です。稼働中に差し替えても、"
                "サーバが持っている状態で上書きされて失われます。"
                "サーバを停止してから復元してください。",
            )
        try:
            await asyncio.to_thread(world_store.restore, name)
        except WorldBackupError as exc:
            raise HTTPException(400, str(exc)) from exc

        await notify.send(
            "ワールドを復元しました",
            f"{name}\nサーバを起動すると反映されます。",
            "warn",
        )
        return {"result": "ok", "restored": name, "start_required": True}

    @app.delete("/api/world/backups/{name}", dependencies=auth)
    async def delete_world_backup(name: str) -> dict[str, str]:
        try:
            await asyncio.to_thread(world_store.delete, name)
        except WorldBackupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"result": "ok", "deleted": name}

    @app.get("/api/settings")
    async def get_server_settings(
        authenticated: bool = Depends(is_authenticated),
    ) -> dict[str, Any]:
        settings = await pal.settings()
        return settings if authenticated else mask_values(settings)

    @app.get("/api/restart")
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

    @app.post("/api/restart/release", dependencies=auth)
    async def release_restart(body: ServiceActionBody | None = None) -> dict[str, Any]:
        """固まったシーケンスを打ち切り、次の操作を受け付けられるようにする。

        サーバに送った操作は取り消せない。ここで解除するのは管理ツール側の
        状態だけで、サーバがどうなっているかは操作者が確かめる必要がある。
        進行中の表示が残るとその後の停止も起動も弾かれてしまうため、
        管理ツールから抜け出す口をひとつ用意しておく（issue #34）。
        """
        reason = body.reason if body else "手動"
        status = restart_manager.status.as_dict()
        if not restart_manager.release(reason):
            raise HTTPException(409, "解除できる進行中のシーケンスがありません")
        await announcer.discord_only(
            "シーケンスの進行状態を解除しました",
            f"{status['mode_label']}シーケンス（{status['phase']}）を打ち切りました。\n"
            f"理由: {reason}\n**サーバの状態は確認してください。**",
            source="system",
            level="warn",
            reason=reason,
        )
        return {"result": "released", "restart": restart_manager.status.as_dict()}

    @app.post("/api/service/{action}", dependencies=auth)
    async def service_action(action: str, body: ServiceActionBody | None = None) -> dict[str, Any]:
        """ゲームサーバのプロセスを直接操作する。

        何で操作するかは PAL_SERVICE_BACKEND 次第（LinuxGSM の管理スクリプト /
        systemctl）。この口はその違いを見せない。

        停止中のサーバにはアナウンスを送れないので、起動はここから即時実行する。
        停止と再起動は予告アナウンスを伴う /api/shutdown と /api/restart を使うこと
        （緊急時のためにここからも叩けるようにはしてある）。
        """
        if action not in ("start", "stop", "restart"):
            raise HTTPException(400, "action は start/stop/restart のいずれかです")
        reason = body.reason if body else "手動"
        command = service.describe(action)
        logger.info("サーバ操作 %s を実行します (%s, 理由: %s)", action, command, reason)
        result = await getattr(service, action)()
        if not result.ok:
            logger.warning("サーバ操作 %s に失敗しました: %s", action, result.stderr)
            raise HTTPException(500, result.stderr or f"{service.label} の実行に失敗しました")
        labels = {"start": "起動", "stop": "停止", "restart": "再起動"}
        await announcer.discord_only(
            f"サーバーを{labels[action]}しました",
            # 実際に走ったコマンドを残す。構成を移行している最中に
            # 「どちらの経路で操作したのか」を後から辿れるようにするため
            f"{command}\n理由: {reason}",
            source="system",
            reason=reason,
        )
        return result.as_dict()

    # ---- PalWorldSettings.ini ------------------------------------------

    @app.get("/api/settings-ini")
    async def get_ini(authenticated: bool = Depends(is_authenticated)) -> dict[str, Any]:
        if not ini_store.exists():
            return {
                "exists": False,
                "path": str(cfg.pal_settings_ini),
                "text": "",
                "options": {},
                "backups": [],
            }
        text = ini_store.read_text()
        options = ini_store.read_options()
        if not authenticated:
            # 全文編集は ini をそのまま見せる画面なので、ここで伏せないと素通しになる
            text = mask_ini_text(text)
            options = mask_values(options)
        return {
            "exists": True,
            "path": str(cfg.pal_settings_ini),
            "text": text,
            "options": options,
            "backups": [
                {"name": b.name, "size": b.size, "created_at": b.created_at}
                for b in ini_store.list_backups()
            ],
        }

    @app.put("/api/settings-ini", dependencies=auth)
    async def put_ini(body: SettingsIniBody) -> dict[str, Any]:
        if body.text is None and body.options is None:
            raise HTTPException(400, "text か options のどちらかを指定してください")
        if not body.force and await game_server_running():
            raise HTTPException(
                409,
                "ゲームサーバが稼働中です。Palworld は停止時に ini を上書きするため、"
                "稼働中に書き換えても次の停止で失われます。"
                "サーバを停止してから保存してください。",
            )
        try:
            if body.text is not None:
                backup = ini_store.write_text(body.text)
            else:
                backup = ini_store.update_options(body.options or {})
        except SettingsIniError as exc:
            raise HTTPException(400, str(exc)) from exc
        await notify.send(
            "サーバ設定を更新しました",
            f"バックアップ: {backup.name}\nサーバを起動すると反映されます。",
            "info",
        )
        return {"result": "ok", "backup": backup.name, "start_required": True}

    @app.get("/api/settings-ini/fields")
    async def get_ini_fields(authenticated: bool = Depends(is_authenticated)) -> dict[str, Any]:
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

        # 稼働中のサーバが持っている設定を、新しいプロパティの発見源として使う。
        # 落ちていても画面は出したいので、失敗は握りつぶす。
        server_settings: dict[str, Any] = {}
        try:
            server_settings = await pal.settings()
        except PalApiError:
            pass

        if not authenticated:
            # describe() に渡す前に伏せる。value と raw の両方に効かせるため
            options = mask_values(options)
            server_settings = mask_values(server_settings)

        return {
            "exists": True,
            "path": str(cfg.pal_settings_ini),
            "categories": describe(options),
            # 設定ファイルに書かれていない項目。ゲーム側の既定値で動いているので、
            # 変えたいならユーザーが明示的に追加する
            "available": missing_fields(options),
            # ini にもスキーマにも無いが、サーバが持っている項目
            "discovered": discovered_fields(server_settings, options),
            "category_labels": [{"key": k, "label": v} for k, v in CATEGORIES],
            # ini はサーバ停止中でないと安全に書き換えられない
            "server_running": await game_server_running(),
            # 反映待ちの変更（稼働中でも保存できるのはこの仕組みがあるため）
            "pending_total": len(pending),
        }

    @app.put("/api/settings-ini/fields", dependencies=auth)
    async def put_ini_fields(body: SettingsFieldsBody) -> dict[str, Any]:
        """変更した項目だけを受け取って ini に反映する。"""
        if not ini_store.exists():
            raise HTTPException(404, f"設定ファイルが見つかりません: {cfg.pal_settings_ini}")

        if not body.values and not body.additions and not body.custom_additions:
            raise HTTPException(400, "更新する項目がありません")

        schedule_id: str | None = None
        if body.when == "schedule":
            if not body.schedule_id:
                raise HTTPException(400, "反映先の予約を指定してください")
            if not any(s["id"] == body.schedule_id for s in scheduler.list()):
                raise HTTPException(404, f"予約が見つかりません: {body.schedule_id}")
            schedule_id = body.schedule_id

        # すぐ反映する場合だけ、稼働中かどうかを気にする。
        # Palworld は停止時に ini を上書きするので、稼働中に書いても消えるため。
        # 予約して反映する場合は ini に触らないので、稼働中でも受け付ける。
        if body.when == "now" and not body.force and await game_server_running():
            raise HTTPException(
                409,
                "ゲームサーバが稼働中です。Palworld は停止時に ini を上書きするため、"
                "稼働中に書き換えても次の停止で失われます。"
                "サーバを停止してから保存するか、反映タイミングを予約してください。",
            )

        options = ini_store.read_options()
        updates, errors, warnings = build_updates(body.values, options)
        added, add_errors, add_warnings = add_fields(body.additions, options)
        custom, custom_errors, custom_warnings = add_custom_fields(
            [c.model_dump() for c in body.custom_additions], options
        )
        added.update(custom)
        errors += add_errors + custom_errors
        warnings += add_warnings + custom_warnings
        if errors:
            raise HTTPException(400, " / ".join(errors))

        # 値が変わっていないものは書かない（無駄なバックアップを増やさない）
        changed = {k: v for k, v in updates.items() if options.get(k) != v}
        changed.update(added)
        if not changed:
            return {"result": "unchanged", "changed": {}, "added": [],
                    "warnings": warnings, "start_required": False}

        detail = "変更: " + ", ".join(f"{k}={v}" for k, v in changed.items())
        if added:
            detail += "\n（うち新規追加: " + ", ".join(added) + "）"

        # --- 予約して反映する ---
        if body.when != "now":
            try:
                item = pending.add(changed, schedule_id=schedule_id, note=body.note)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

            target = "次にサーバが停止するとき"
            if schedule_id:
                sched = next((s for s in scheduler.list() if s["id"] == schedule_id), None)
                if sched:
                    target = f"予約「{sched['label'] or sched['spec']}」（{sched['action_label']}）"
            await notify.send(
                "サーバ設定の変更を予約しました",
                detail + f"\n反映タイミング: {target}",
                "info",
            )
            return {
                "result": "scheduled",
                "pending_id": item.id,
                "changed": changed,
                "added": sorted(added),
                "when": body.when,
                "schedule_id": schedule_id,
                "warnings": warnings,
                "pending_total": len(pending),
            }

        # --- すぐ反映する（サーバ停止中） ---
        try:
            backup = ini_store.update_options(changed)
        except SettingsIniError as exc:
            raise HTTPException(400, str(exc)) from exc

        await notify.send(
            "サーバ設定を更新しました",
            detail + f"\nバックアップ: {backup.name}\nサーバを起動すると反映されます。",
            "info",
        )
        return {
            "result": "ok",
            "backup": backup.name,
            "changed": changed,
            "added": sorted(added),
            "warnings": warnings,
            # 停止中に書いた前提なので、あとは起動すれば反映される
            "start_required": True,
        }

    # ---- 保留中の設定変更 ------------------------------------------------

    @app.get("/api/settings-ini/pending")
    async def list_pending(authenticated: bool = Depends(is_authenticated)) -> dict[str, Any]:
        """反映待ちの設定変更。どの予約で反映されるかも併せて返す。"""
        schedules = {s["id"]: s for s in scheduler.list()}
        items = []
        for item in pending.list():
            sched = schedules.get(item["schedule_id"]) if item["schedule_id"] else None
            # 反映待ちの中身にも新しいパスワードが入りうる
            updates = item["updates"] if authenticated else mask_values(item["updates"])
            items.append(
                {
                    **item,
                    "updates": updates,
                    "target_label": (
                        f"{sched['label'] or sched['spec']}（{sched['action_label']}）"
                        if sched else "次にサーバが停止するとき"
                    ),
                    # 紐づけ先の予約が消えていたら、次の停止で反映される
                    "target_missing": bool(item["schedule_id"]) and sched is None,
                    "next_action_at": sched["next_action_at"] if sched else None,
                }
            )
        return {"pending": items, "total": len(pending)}

    @app.delete("/api/settings-ini/pending/{change_id}", dependencies=auth)
    async def cancel_pending(change_id: str) -> dict[str, str]:
        if not pending.remove(change_id):
            raise HTTPException(404, f"保留中の変更が見つかりません: {change_id}")
        return {"result": "ok"}

    @app.delete("/api/settings-ini/pending", dependencies=auth)
    async def clear_pending() -> dict[str, int]:
        return {"cleared": pending.clear()}

    @app.post("/api/settings-ini/pending/apply", dependencies=auth)
    async def apply_pending_now() -> dict[str, Any]:
        """保留中の変更を今すぐ反映する（サーバ停止中のみ）。"""
        if await game_server_running():
            raise HTTPException(
                409,
                "ゲームサーバが稼働中です。反映はサーバ停止中にのみ行えます。",
            )
        result = await apply_pending_changes(None)
        if not result.ok:
            raise HTTPException(400, result.error)
        return result.as_dict()

    @app.post("/api/settings-ini/restore", dependencies=auth)
    async def restore_ini(body: RestoreBody) -> dict[str, Any]:
        try:
            ini_store.restore(body.name)
        except SettingsIniError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"result": "ok", "restored": body.name, "restart_required": True}

    # ---- スケジュール --------------------------------------------------

    @app.get("/api/schedules")
    async def list_schedules() -> dict[str, Any]:
        schedules = scheduler.list()
        # この予約で反映される設定変更の件数を添える
        for sched in schedules:
            sched["pending_changes"] = (
                len(pending.due_for(sched["id"])) if sched["action"] != "start" else 0
            )
        return {
            "schedules": schedules,
            "timezone": cfg.schedule_timezone,
            "actions": [{"value": a, "label": ACTION_LABELS[a]} for a in ACTIONS],
            "pending_total": len(pending),
        }

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
                action=body.action,
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

    @app.get("/api/logs")
    async def get_logs(
        level: str | None = Query(None, pattern="^(info|warn|error)$"),
    ) -> dict[str, Any]:
        return {
            "lines": broker.backlog(level),
            "counts": broker.level_counts(),
            "total": len(broker.backlog()),
        }

    @app.websocket("/ws/logs")
    async def ws_logs(websocket: WebSocket) -> None:
        # ログ画面は未ログインでも閲覧できる（Issue #15）。
        # 認証の有無で流す内容は変えていない。ログは配信するだけで何も変えられない
        await websocket.accept()
        try:
            async for record in broker.subscribe():
                await websocket.send_json(record)
        except WebSocketDisconnect:
            pass
        except (RuntimeError, asyncio.CancelledError):
            pass

    return app


app = create_app()
