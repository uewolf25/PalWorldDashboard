"""環境変数から読み込む設定。

/etc/dashboard-Pal.env を systemd の EnvironmentFile で読ませる想定。
開発時はプロセス環境変数をそのまま使う。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


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
    pal_timeout: float = field(default_factory=lambda: _env_float("PAL_TIMEOUT", 5.0))

    # --- この管理ツール自身 ---
    app_host: str = field(default_factory=lambda: _env("APP_HOST", "0.0.0.0"))
    app_port: int = field(default_factory=lambda: _env_int("APP_PORT", 8080))
    # 設定すると管理 UI / API に Basic 認証がかかる（未設定なら無認証）
    app_password: str = field(default_factory=lambda: _env("APP_PASSWORD", ""))
    app_user: str = field(default_factory=lambda: _env("APP_USER", "admin"))

    # --- ゲームサーバのプロセス制御 ---
    pal_service_name: str = field(
        default_factory=lambda: _env("PAL_SERVICE_NAME", "palworld.service")
    )
    # systemd / mock。mock は開発用で、モックサーバを起動/停止する
    pal_service_backend: str = field(
        default_factory=lambda: _env("PAL_SERVICE_BACKEND", "systemd")
    )
    pal_mock_control_url: str = field(
        default_factory=lambda: _env("PAL_MOCK_CONTROL_URL", "http://127.0.0.1:8212")
    )

    # --- 設定ファイル ---
    pal_settings_ini: Path = field(
        default_factory=lambda: Path(
            _env(
                "PAL_SETTINGS_INI",
                "/home/steam/Steam/steamapps/common/PalServer/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini",
            )
        )
    )
    backup_dir: Path = field(
        default_factory=lambda: Path(_env("PAL_BACKUP_DIR", "/var/lib/dashboard-Pal/backups"))
    )
    backup_keep: int = field(default_factory=lambda: _env_int("PAL_BACKUP_KEEP", 30))

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
    # 直前に再起動した直後は受け付けない秒数
    restart_debounce_sec: float = field(
        default_factory=lambda: _env_float("RESTART_DEBOUNCE_SEC", 60.0)
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

    # --- 監視 ---
    monitor_interval: float = field(default_factory=lambda: _env_float("MONITOR_INTERVAL", 30.0))
    history_size: int = field(default_factory=lambda: _env_int("HISTORY_SIZE", 2880))
    mem_warn_percent: float = field(default_factory=lambda: _env_float("MEM_WARN_PERCENT", 80.0))
    mem_crit_percent: float = field(default_factory=lambda: _env_float("MEM_CRIT_PERCENT", 90.0))
    alert_cooldown_sec: float = field(
        default_factory=lambda: _env_float("ALERT_COOLDOWN_SEC", 1800.0)
    )

    # --- Discord ---
    discord_webhook_url: str = field(default_factory=lambda: _env("DISCORD_WEBHOOK_URL", ""))
    discord_alert_webhook_url: str = field(
        default_factory=lambda: _env("DISCORD_ALERT_WEBHOOK_URL", "")
    )

    # --- ログストリーム ---
    # journald / file / none
    log_source: str = field(default_factory=lambda: _env("LOG_SOURCE", "journald"))
    log_file: Path = field(default_factory=lambda: Path(_env("LOG_FILE", "/var/log/palworld.log")))

    # 破壊的操作（kick/ban/shutdown/stop）を実行せず記録だけする
    dry_run: bool = field(default_factory=lambda: _env_bool("PAL_DRY_RUN", False))

    @property
    def pal_base_url(self) -> str:
        return f"http://{self.pal_host}:{self.pal_port}"

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

    def public_dict(self) -> dict:
        """UI に返す設定。秘密情報は伏字化する。"""
        return {
            "env": self.env,
            "pal_base_url": self.pal_base_url,
            "pal_admin_user": self.pal_admin_user,
            "pal_admin_password": mask_secret(self.pal_admin_password),
            "pal_service_name": self.pal_service_name,
            "pal_service_backend": self.pal_service_backend,
            "pal_settings_ini": str(self.pal_settings_ini),
            "schedule_timezone": self.schedule_timezone,
            "restart_announce_template": self.restart_announce_template,
            "stop_announce_template": self.stop_announce_template,
            "notice_offsets": list(self.notice_offsets),
            "monitor_interval": self.monitor_interval,
            "mem_warn_percent": self.mem_warn_percent,
            "mem_crit_percent": self.mem_crit_percent,
            "discord_webhook_url": mask_secret(self.discord_webhook_url),
            "discord_alert_webhook_url": mask_secret(self.discord_alert_webhook_url),
            "log_source": self.log_source,
            "dry_run": self.dry_run,
        }


def load_settings() -> Settings:
    return Settings()
