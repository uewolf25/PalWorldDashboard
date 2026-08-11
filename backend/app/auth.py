"""管理画面のログイン認証。

これまでは HTTP Basic 認証だけだった。ブラウザ標準のダイアログが出るので
実装は楽だが、次の問題がある。

- **WebSocket が通らないことがある。** ブラウザは WS のハンドシェイクに
  Basic 認証の資格情報を自動では付けないため、ログイン画面が動いていても
  ログのストリーミングだけ繋がらない
- ログアウトできない（ブラウザを閉じるまで資格情報が残る）
- パスワードが毎リクエスト飛ぶ

そこでログイン画面 + セッション Cookie 方式にする。Cookie は同一オリジンの
WebSocket ハンドシェイクにも送られるので、上の1点目も解決する。

セッションはサーバ側に持たず、署名付きトークンで表す。プロセスを再起動しても
ログインが切れないようにするため、署名鍵はファイルに永続化する。
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import secrets
import time
from collections import deque
from hashlib import sha256
from pathlib import Path

logger = logging.getLogger(__name__)

COOKIE_NAME = "dashboard_pal_session"


# --------------------------------------------------------------------------
# 署名鍵
# --------------------------------------------------------------------------


def load_or_create_secret(path: Path | None, configured: str = "") -> bytes:
    """セッションの署名に使う鍵を用意する。

    APP_SESSION_SECRET が設定されていればそれを使う。無ければ生成して
    ファイルに保存する。毎回作り直すとプロセス再起動のたびに
    全員がログアウトさせられるため。
    """
    if configured:
        return configured.encode("utf-8")

    if path is not None:
        try:
            if path.is_file():
                saved = path.read_text(encoding="utf-8").strip()
                if saved:
                    return saved.encode("utf-8")
        except OSError as exc:
            logger.warning("セッション鍵を読めませんでした: %s", exc)

    generated = secrets.token_urlsafe(48)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(generated, encoding="utf-8")
            os.chmod(path, 0o600)
            logger.info("セッション鍵を生成しました: %s", path)
        except OSError as exc:
            # 保存できなくても動作はする。ただし再起動でログインが切れる
            logger.warning(
                "セッション鍵を保存できませんでした（再起動でログインが切れます）: %s", exc
            )
    return generated.encode("utf-8")


# --------------------------------------------------------------------------
# セッショントークン
# --------------------------------------------------------------------------


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode("utf-8"), sha256).hexdigest()


def issue_token(secret: bytes, ttl: float) -> str:
    """有効期限つきのトークンを発行する。

    形式は `<期限>.<ノンス>.<署名>`。ノンスを混ぜるのは、同じ秒に
    発行したトークンが同一文字列にならないようにするため。
    """
    expires = int(time.time() + max(ttl, 0))
    nonce = secrets.token_urlsafe(9)
    payload = f"{expires}.{nonce}"
    return f"{payload}.{_sign(secret, payload)}"


def verify_token(secret: bytes, token: str) -> bool:
    """トークンが本物で、まだ期限内かを確かめる。"""
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    expires_raw, nonce, signature = parts

    payload = f"{expires_raw}.{nonce}"
    if not hmac.compare_digest(_sign(secret, payload), signature):
        return False

    try:
        expires = int(expires_raw)
    except ValueError:
        return False
    return time.time() < expires


def token_expiry(token: str) -> int | None:
    """トークンの期限（UNIX 秒）。検証はしない。"""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# ログイン試行の制限
# --------------------------------------------------------------------------


class LoginThrottle:
    """総当たりを遅くする。

    LAN 内限定の想定でも、ログイン画面を出す以上は無制限に試させない。
    接続元ごとに直近の失敗を数え、上限を超えたら一定時間受け付けない。

    リバースプロキシ越しだと接続元が全部プロキシの IP になり、
    実質的に全体で1つの枠を共有することになる。緩くはなるが、
    無制限よりはよいので、そのまま採用する。
    """

    def __init__(self, max_attempts: int = 10, window: float = 300.0, lockout: float = 300.0) -> None:
        self.max_attempts = max_attempts
        self.window = window
        self.lockout = lockout
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        attempts = self._failures.setdefault(key, deque())
        while attempts and now - attempts[0] > self.window:
            attempts.popleft()
        return attempts

    def retry_after(self, key: str) -> float:
        """あと何秒待つ必要があるか。0 なら試してよい。"""
        now = time.time()
        until = self._locked_until.get(key, 0.0)
        return max(0.0, until - now)

    def record_failure(self, key: str) -> float:
        """失敗を記録する。ロックされたら残り秒数を返す。"""
        now = time.time()
        attempts = self._prune(key, now)
        attempts.append(now)
        if len(attempts) >= self.max_attempts:
            self._locked_until[key] = now + self.lockout
            attempts.clear()
            logger.warning("ログイン失敗が続いたため %s を %.0f 秒ロックします", key, self.lockout)
            return self.lockout
        return 0.0

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)


# --------------------------------------------------------------------------
# Basic 認証（API クライアント向けに残す）
# --------------------------------------------------------------------------


def check_basic_header(header: str, user: str, password: str) -> bool:
    """Authorization ヘッダの Basic 認証を検証する。

    ブラウザはログイン画面と Cookie を使うが、curl やスクリプトからは
    Basic 認証の方が扱いやすいので残しておく。
    """
    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
        got_user, _, got_pass = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(got_user, user) and hmac.compare_digest(got_pass, password)
