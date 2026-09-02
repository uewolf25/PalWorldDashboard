"""環境変数から読み込む設定。

/etc/dashboard-Pal.env を systemd の EnvironmentFile で読ませる想定。
開発時はプロセス環境変数をそのまま使う。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = _env(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def mask_secret(value: str) -> str:
    """Webhook URL やパスワードを画面/ログに出す際の伏字化。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-4:]}"


@dataclass
class Settings:
    # 実行環境ラベル（staging / production）。UI のバッジ表示に使う
    env: str = field(default_factory=lambda: _env("PAL_ENV", "staging"))

    # --- Palworld 専用サーバの REST API ---
    pal_host: str = field(default_factory=lambda: _env("PAL_HOST", "127.0.0.1"))
    pal_port: int = field(default_factory=lambda: _env_int("PAL_PORT", 8212))
    pal_admin_user: str = field(default_factory=lambda: _env("PAL_ADMIN_USER", "admin"))
    pal_admin_password: str = field(default_factory=lambda: _env("PAL_ADMIN_PASSWORD", ""))
    # 参照系（info / metrics / players / settings）のタイムアウト。
    # ダッシュボードが 1 秒ごとに叩くので短くしておく
    pal_timeout: float = field(default_factory=lambda: _env_float("PAL_TIMEOUT", 5.0))
    # 実行系（save / shutdown / stop）のタイムアウト。
    # 実機のワールド保存は数十秒かかることがあるため長めに取る
    pal_slow_timeout: float = field(default_factory=lambda: _env_float("PAL_SLOW_TIMEOUT", 120.0))

    # --- この管理ツール自身 ---
    app_host: str = field(default_factory=lambda: _env("APP_HOST", "0.0.0.0"))
    app_port: int = field(default_factory=lambda: _env_int("APP_PORT", 8080))
    # 設定すると管理画面にログインが必要になる（未設定なら無認証）。
    # ブラウザはログイン画面 + セッション Cookie、API クライアントは Basic 認証
    app_password: str = field(default_factory=lambda: _env("APP_PASSWORD", ""))
    app_user: str = field(default_factory=lambda: _env("APP_USER", "admin"))
    # セッションの署名鍵。未設定なら生成してファイルに保存する
    # （毎回作り直すと再起動のたびに全員ログアウトになる）
    app_session_secret: str = field(default_factory=lambda: _env("APP_SESSION_SECRET", ""))
    session_secret_file: Path = field(
        default_factory=lambda: Path(
            _env("APP_SESSION_SECRET_FILE", "/var/lib/dashboard-Pal/session-secret")
        )
    )
    # ログイン状態を保つ時間（秒）。既定は7日
    app_session_ttl: float = field(
        default_factory=lambda: _env_float("APP_SESSION_TTL", 604800.0)
    )
    # ログイン失敗が続いたときに一時的に受け付けなくする
    app_login_max_attempts: int = field(
        default_factory=lambda: _env_int("APP_LOGIN_MAX_ATTEMPTS", 10)
    )
    app_login_lockout_sec: float = field(
        default_factory=lambda: _env_float("APP_LOGIN_LOCKOUT_SEC", 300.0)
    )

    # --- ゲームサーバのプロセス制御 ---
    # systemd バックエンド専用（本番では使わない）。journald からログを
    # 取り込む構成でも参照する
    pal_service_name: str = field(
        default_factory=lambda: _env("PAL_SERVICE_NAME", "palworld.service")
    )
    # lgsm / mock / simulated / systemd。
    #   lgsm      … LinuxGSM の管理スクリプトを呼ぶ。**本番はこれ**
    #   mock      … 同梱モックサーバを起動/停止（開発）
    #   simulated … 空回し（テスト）
    #   systemd   … systemctl でユニットを操作する。**本番では廃止。**
    #                手元で systemd 管理のサーバを触りたいときだけ使う
    pal_service_backend: str = field(
        default_factory=lambda: _env("PAL_SERVICE_BACKEND", "lgsm")
    )
    # lgsm バックエンドで叩く LinuxGSM の管理スクリプト
    pal_service_command: str = field(
        default_factory=lambda: _env("PAL_SERVICE_COMMAND", "")
    )
    # 起動/停止コマンドの待ち時間。LinuxGSM の start/stop は数十秒で終わるので、
    # ここまで待たされるのは異常（systemd を使う場合は TimeoutStopSec 以上にする）
    pal_service_timeout: float = field(
        default_factory=lambda: _env_float("PAL_SERVICE_TIMEOUT", 300.0)
    )
    pal_mock_control_url: str = field(
        default_factory=lambda: _env("PAL_MOCK_CONTROL_URL", "http://127.0.0.1:8212")
    )
    # systemd バックエンド専用（本番では使わない）。専用ユーザで動かす場合、
    # systemctl は sudo 経由でないと実行できないため、sudoers に NOPASSWD で
    # 登録したうえで true にする。lgsm では特権昇格そのものを使わない
    pal_systemctl_sudo: bool = field(
        default_factory=lambda: _env_bool("PAL_SYSTEMCTL_SUDO", False)
    )

    # --- 設定ファイル ---
    # 既定は LinuxGSM 構成の置き場（${rootdir}/serverfiles/...）。
    # 素の SteamCMD 構成では場所が違うので、その場合は必ず明示する
    pal_settings_ini: Path = field(
        default_factory=lambda: Path(
            _env(
                "PAL_SETTINGS_INI",
                "/home/mntuser/serverfiles/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini",
            )
        )
    )
    backup_dir: Path = field(
        default_factory=lambda: Path(_env("PAL_BACKUP_DIR", "/var/lib/dashboard-Pal/backups"))
    )
    backup_keep: int = field(default_factory=lambda: _env_int("PAL_BACKUP_KEEP", 30))

    # --- ワールドセーブ ---
    # 空なら ini の場所から推測する（Saved/Config/... と Saved/SaveGames は兄弟）
    pal_save_dir_raw: str = field(default_factory=lambda: _env("PAL_SAVE_DIR", ""))
    world_backup_dir: Path = field(
        default_factory=lambda: Path(
            _env("PAL_WORLD_BACKUP_DIR", "/var/lib/dashboard-Pal/world-backups")
        )
    )
    # ini と違って1つ数百MBになるので、既定は少なめにしておく
    world_backup_keep: int = field(default_factory=lambda: _env_int("PAL_WORLD_BACKUP_KEEP", 5))

    # --- スケジューラ ---
    schedule_timezone: str = field(
        default_factory=lambda: _env("SCHEDULE_TIMEZONE", "Asia/Tokyo")
    )
    schedule_store: Path = field(
        default_factory=lambda: Path(
            _env("PAL_SCHEDULE_STORE", "/var/lib/dashboard-Pal/schedules.json")
        )
    )

    # --- 再起動シーケンス ---
    # 再起動の何秒前に予告するか（カンマ区切り）
    restart_notice_offsets: str = field(
        default_factory=lambda: _env("RESTART_NOTICE_OFFSETS", "300,60,30")
    )
    # shutdown API に渡す待ち時間（秒）
    restart_shutdown_wait: int = field(
        default_factory=lambda: _env_int("RESTART_SHUTDOWN_WAIT", 10)
    )
    # 上の待ち時間を過ぎてから、実際にサーバが落ちるのを待つ猶予（秒）。
    # ワールド保存に時間がかかるので、ここを短くすると保存中に停止処理へ進んでしまう
    restart_shutdown_grace: float = field(
        default_factory=lambda: _env_float("RESTART_SHUTDOWN_GRACE", 120.0)
    )
    # 再起動後、サーバが起動しきるまで「応答なし」を通知しない時間（秒）
    restart_alert_grace: float = field(
        default_factory=lambda: _env_float("RESTART_ALERT_GRACE", 180.0)
    )
    # 予告が終わってから完了までの打ち切り時間（秒）。0 で無効。
    # 保存・shutdown API・停止待ち・プロセス制御コマンドはそれぞれ自前の
    # タイムアウトを持っているので、ここはそのどれもが返らなかったときにだけ
    # 効く最後の砦。短くすると正常な再起動を途中で失敗扱いにしてしまう
    restart_sequence_timeout: float = field(
        default_factory=lambda: _env_float("RESTART_SEQUENCE_TIMEOUT", 900.0)
    )
    # 起動コマンドのあと、サーバが応答を返すまで待つ上限（秒）。0 で待たない。
    # ここまで見て初めて「再起動が完了した」と言える（コマンドが通っただけでは
    # プレイヤーはまだ入れない）。確認できなければ完了通知に警告を添える
    restart_startup_timeout: float = field(
        default_factory=lambda: _env_float("RESTART_STARTUP_TIMEOUT", 180.0)
    )
    # 直前に再起動した直後は受け付けない秒数
    restart_debounce_sec: float = field(
        default_factory=lambda: _env_float("RESTART_DEBOUNCE_SEC", 60.0)
    )
    # シーケンスの進行状態の保存先。
    # 管理ツールは自分の意思と無関係に落とされることがある（OS のパッケージ更新に
    # 伴う needrestart の自動再起動 / issue #41）。メモリ上の進行状態だけだと
    # 「サーバを止めたが起こす前に管理ツールが消えた」場合に誰も気づけないので、
    # 次の起動で拾えるようディスクに残す
    restart_state_store: Path = field(
        default_factory=lambda: Path(
            _env("PAL_RESTART_STATE", "/var/lib/dashboard-Pal/restart-state.json")
        )
    )
    # 管理ツールの停止時、サーバに触っている最中のシーケンスを待つ上限（秒）。
    # ★ systemd の TimeoutStopSec より必ず短くすること。長いと待っている途中で
    #   SIGKILL され、待った意味が無くなる
    restart_drain_timeout: float = field(
        default_factory=lambda: _env_float("RESTART_DRAIN_TIMEOUT", 45.0)
    )
    # 中断されたシーケンスを救済起動してよい上限（秒）。これより古い中断は
    # 通知だけにする。何時間も前に止めたサーバを、管理ツールの都合で
    # 勝手に起こさないため
    restart_recover_max_age: float = field(
        default_factory=lambda: _env_float("RESTART_RECOVER_MAX_AGE", 3600.0)
    )
    # 管理ツール自身の稼働記録（前回いつ止まったか）の保存先。
    # 短時間の停止→起動を「外部要因の可能性」として見分けるために使う
    runtime_state_store: Path = field(
        default_factory=lambda: Path(
            _env("PAL_RUNTIME_STATE", "/var/lib/dashboard-Pal/runtime-state.json")
        )
    )
    # 停止から何秒以内の復帰を「短時間での再起動」とみなすか
    quick_restart_sec: float = field(
        default_factory=lambda: _env_float("PAL_QUICK_RESTART_SEC", 60.0)
    )
    # 予告アナウンスの既定文面。{time} が残り時間に置き換わる
    restart_announce_template: str = field(
        default_factory=lambda: _env(
            "RESTART_ANNOUNCE_TEMPLATE", "サーバーは{time}後に再起動します。"
        )
    )
    stop_announce_template: str = field(
        default_factory=lambda: _env(
            "STOP_ANNOUNCE_TEMPLATE", "サーバーは{time}後に停止します。"
        )
    )

    # --- アナウンス履歴 ---
    announce_store: Path = field(
        default_factory=lambda: Path(
            _env("PAL_ANNOUNCE_STORE", "/var/lib/dashboard-Pal/announcements.json")
        )
    )
    announce_history_limit: int = field(
        default_factory=lambda: _env_int("ANNOUNCE_HISTORY_LIMIT", 500)
    )

    # --- 設定変更の予約（保留中の変更） ---
    pending_store: Path = field(
        default_factory=lambda: Path(
            _env("PAL_PENDING_STORE", "/var/lib/dashboard-Pal/pending-settings.json")
        )
    )
    pending_limit: int = field(default_factory=lambda: _env_int("PAL_PENDING_LIMIT", 50))

    # --- プレイヤーの入退室 ---
    presence_store: Path = field(
        default_factory=lambda: Path(
            _env("PAL_PRESENCE_STORE", "/var/lib/dashboard-Pal/presence.json")
        )
    )
    presence_history_limit: int = field(
        default_factory=lambda: _env_int("PRESENCE_HISTORY_LIMIT", 500)
    )

    # ゲームサーバへの問い合わせをまとめる秒数。
    # 画面のタブを何枚開いても、この間隔以上には問い合わせが増えない
    status_cache_sec: float = field(default_factory=lambda: _env_float("STATUS_CACHE_SEC", 1.0))

    # --- 監視 ---
    monitor_interval: float = field(default_factory=lambda: _env_float("MONITOR_INTERVAL", 30.0))
    history_size: int = field(default_factory=lambda: _env_int("HISTORY_SIZE", 2880))
    mem_warn_percent: float = field(default_factory=lambda: _env_float("MEM_WARN_PERCENT", 80.0))
    mem_crit_percent: float = field(default_factory=lambda: _env_float("MEM_CRIT_PERCENT", 90.0))
    alert_cooldown_sec: float = field(
        default_factory=lambda: _env_float("ALERT_COOLDOWN_SEC", 1800.0)
    )

    # --- Steam アップデートの検知（issue #30） ---
    # check-update を叩く間隔（秒）。cron の update-watch.sh が */10 で回って
    # いる間は、それより短くしても意味が無い
    update_check_interval: float = field(
        default_factory=lambda: _env_float("PAL_UPDATE_CHECK_INTERVAL", 600.0)
    )
    # 検知の状態。再起動をまたいで通知が重複しないように永続化する
    update_state_store: Path = field(
        default_factory=lambda: Path(
            _env("PAL_UPDATE_STATE", "/var/lib/dashboard-Pal/update-state.json")
        )
    )
    # 何回続けて失敗したら Discord に流すか。
    # 「黙って検知が止まる」を作らないための仕掛け
    update_fail_alert_threshold: int = field(
        default_factory=lambda: _env_int("PAL_UPDATE_FAIL_ALERTS", 3)
    )

    # --- Discord ---
    discord_webhook_url: str = field(default_factory=lambda: _env("DISCORD_WEBHOOK_URL", ""))
    discord_alert_webhook_url: str = field(
        default_factory=lambda: _env("DISCORD_ALERT_WEBHOOK_URL", "")
    )

    # --- ログストリーム ---
    # ゲームサーバ側のログの取り込み元。file / journald / none。
    # LinuxGSM は journald ではなく自前のログファイルに書くので、
    # 本番の既定は file（LOG_FILE に console ログのパスを入れる）
    log_source: str = field(default_factory=lambda: _env("LOG_SOURCE", "file"))
    log_file: Path = field(default_factory=lambda: Path(_env("LOG_FILE", "/var/log/palworld.log")))
    # 管理ツール自身のログの詳しさ（journald と画面の両方に効く）
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    # 破壊的操作（kick/ban/shutdown/stop）を実行せず記録だけする
    dry_run: bool = field(default_factory=lambda: _env_bool("PAL_DRY_RUN", False))

    @property
    def pal_base_url(self) -> str:
        return f"http://{self.pal_host}:{self.pal_port}"

    @property
    def pal_save_dir(self) -> Path:
        """ワールドセーブの置き場。

        PAL_SAVE_DIR が空なら ini の場所から推測する。標準的な配置では
        `Pal/Saved/Config/LinuxServer/PalWorldSettings.ini` に対して
        `Pal/Saved/SaveGames` が兄弟になる。推測が外れる構成もあるので、
        そのときは PAL_SAVE_DIR で明示する。
        """
        if self.pal_save_dir_raw:
            return Path(self.pal_save_dir_raw)
        parents = self.pal_settings_ini.resolve().parents
        if len(parents) >= 3 and parents[1].name == "Config":
            return parents[2] / "SaveGames"
        return self.pal_settings_ini.parent / "SaveGames"

    @property
    def notice_offsets(self) -> tuple[float, ...]:
        out: list[float] = []
        for part in self.restart_notice_offsets.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(float(part))
            except ValueError:
                continue
        return tuple(sorted(out, reverse=True)) or (300.0, 60.0, 30.0)

    def public_dict(self, authenticated: bool = True) -> dict:
        """UI に返す設定。秘密情報は伏字化する。

        未ログインの相手には mask_secret すら使わない。前後4文字を残す形式なので、
        Webhook URL のように構造が決まっているものだと当たりを付けられてしまう。
        """
        def secret(value: str) -> str:
            if not value:
                return ""
            return mask_secret(value) if authenticated else "********"

        return {
            # 実機に何が入っているかを画面から見えるようにする。伏せる理由は無い
            "version": __version__,
            "env": self.env,
            "pal_base_url": self.pal_base_url,
            "pal_admin_user": self.pal_admin_user if authenticated else "",
            "pal_admin_password": secret(self.pal_admin_password),
            "pal_service_name": self.pal_service_name,
            "pal_service_backend": self.pal_service_backend,
            # どちらの経路でサーバを操作しているかを、設定値の目視以外でも辿れるように
            "pal_service_command": self.pal_service_command,
            "pal_settings_ini": str(self.pal_settings_ini),
            "schedule_timezone": self.schedule_timezone,
            "restart_announce_template": self.restart_announce_template,
            "stop_announce_template": self.stop_announce_template,
            "notice_offsets": list(self.notice_offsets),
            "monitor_interval": self.monitor_interval,
            "mem_warn_percent": self.mem_warn_percent,
            "mem_crit_percent": self.mem_crit_percent,
            "discord_webhook_url": secret(self.discord_webhook_url),
            "discord_alert_webhook_url": secret(self.discord_alert_webhook_url),
            "auth_required": bool(self.app_password),
            # 画面が「閲覧専用で出すか、操作もさせるか」を決めるために使う
            "authenticated": authenticated,
            # サイドバーに出すログイン名。ログイン前には教えない
            "app_user": self.app_user if authenticated else "",
            "update_check_interval": self.update_check_interval,
            "log_source": self.log_source,
            "dry_run": self.dry_run,
        }


def load_settings() -> Settings:
    return Settings()
