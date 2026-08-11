"""管理画面のログイン認証のテスト（Issue #15）。

Basic 認証だけだった頃の問題:
  - ブラウザは WebSocket のハンドシェイクに Basic 認証を付けないことがあり、
    ログインできていてもログ画面だけ繋がらない（移行前チェックリストの R-14）
  - ログアウトできない
Cookie 方式にするとどちらも解決する。Basic 認証は API クライアント向けに残す。
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import (
    COOKIE_NAME,
    LoginThrottle,
    check_basic_header,
    issue_token,
    load_or_create_secret,
    token_expiry,
    verify_token,
)
from app.main import create_app

PASSWORD = "s3cret-pass"


@pytest.fixture
def secured(settings, tmp_path):
    settings.app_password = PASSWORD
    settings.session_secret_file = tmp_path / "session-secret"
    return settings


@pytest.fixture
async def guest(secured, pal_client, notifier):
    """未ログインのクライアント。Cookie は保持する。"""
    app = create_app(secured, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        c.app = app
        yield c


# ---- トークン --------------------------------------------------------------


def test_token_roundtrip():
    secret = b"unit-test-secret"
    token = issue_token(secret, 60)
    assert verify_token(secret, token) is True


def test_token_is_rejected_with_another_secret():
    token = issue_token(b"one", 60)
    assert verify_token(b"two", token) is False


def test_expired_token_is_rejected():
    secret = b"unit-test-secret"
    assert verify_token(secret, issue_token(secret, -1)) is False


@pytest.mark.parametrize("bad", ["", "garbage", "a.b", "1.2.3.4", "9999999999.nonce.deadbeef"])
def test_malformed_tokens_are_rejected(bad):
    assert verify_token(b"unit-test-secret", bad) is False


def test_tampering_with_the_expiry_is_detected():
    """期限だけ書き換えても署名が合わない。"""
    secret = b"unit-test-secret"
    token = issue_token(secret, -1)
    _, nonce, sig = token.split(".")
    forged = f"{int(time.time()) + 9999}.{nonce}.{sig}"
    assert verify_token(secret, forged) is False


def test_tokens_differ_each_time():
    secret = b"unit-test-secret"
    assert issue_token(secret, 60) != issue_token(secret, 60)


def test_token_expiry_is_readable():
    token = issue_token(b"s", 100)
    assert token_expiry(token) > time.time()
    assert token_expiry("こわれた") is None


# ---- 署名鍵の永続化 --------------------------------------------------------


def test_secret_is_persisted_so_restarts_do_not_log_everyone_out(tmp_path):
    path = tmp_path / "session-secret"
    first = load_or_create_secret(path)
    second = load_or_create_secret(path)
    assert first == second
    assert path.is_file()


def test_persisted_secret_is_not_world_readable(tmp_path):
    path = tmp_path / "session-secret"
    load_or_create_secret(path)
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)


def test_configured_secret_wins(tmp_path):
    path = tmp_path / "session-secret"
    assert load_or_create_secret(path, "from-env") == b"from-env"
    assert not path.exists()   # 設定があるならファイルは作らない


def test_unwritable_location_still_works(tmp_path):
    """鍵を保存できなくても動く（再起動でログインは切れる）。"""
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("x")
    secret = load_or_create_secret(blocked / "session-secret")
    assert secret


# ---- ログイン --------------------------------------------------------------


async def test_endpoints_need_login(guest):
    for path in ("/api/config", "/api/status", "/api/players", "/api/schedules"):
        assert (await guest.get(path)).status_code == 401, path


async def test_login_then_access(guest):
    resp = await guest.post("/api/login", json={"password": PASSWORD})
    assert resp.status_code == 200
    assert COOKIE_NAME in resp.cookies

    # 以降は Cookie だけで通る
    assert (await guest.get("/api/config")).status_code == 200
    assert (await guest.get("/api/status")).status_code == 200


async def test_wrong_password_is_rejected(guest):
    resp = await guest.post("/api/login", json={"password": "nope"})
    assert resp.status_code == 401
    assert COOKIE_NAME not in resp.cookies
    assert (await guest.get("/api/config")).status_code == 401


async def test_login_cookie_is_httponly_and_samesite(guest):
    resp = await guest.post("/api/login", json={"password": PASSWORD})
    header = resp.headers["set-cookie"].lower()
    # JavaScript から読めないようにする
    assert "httponly" in header
    assert "samesite=lax" in header
    # 平文 HTTP のテストでは secure を立てない（立てるとブラウザが送らない）
    assert "secure" not in header


async def test_logout_clears_the_session(guest):
    await guest.post("/api/login", json={"password": PASSWORD})
    assert (await guest.get("/api/config")).status_code == 200

    assert (await guest.post("/api/logout")).status_code == 200
    assert (await guest.get("/api/config")).status_code == 401


async def test_auth_status_is_reachable_without_logging_in(guest):
    """ログイン画面を出すかどうかを判断するための入口。"""
    body = (await guest.get("/api/auth/status")).json()
    assert body == {"required": True, "authenticated": False}

    await guest.post("/api/login", json={"password": PASSWORD})
    assert (await guest.get("/api/auth/status")).json()["authenticated"] is True


async def test_forged_cookie_is_rejected(guest):
    guest.cookies.set(COOKIE_NAME, issue_token(b"attacker-secret", 600))
    assert (await guest.get("/api/config")).status_code == 401


async def test_expired_cookie_is_rejected(guest, secured):
    secured.app_session_ttl = -1
    app = create_app(secured, pal_client=guest.app.state.pal,
                     notifier=guest.app.state.notifier, start_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        await c.post("/api/login", json={"password": PASSWORD})
        assert (await c.get("/api/config")).status_code == 401


async def test_401_does_not_trigger_the_browser_dialog(guest):
    """自前のログイン画面と Basic 認証ダイアログが二重に出ないこと。"""
    resp = await guest.get("/api/config")
    assert resp.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in resp.headers}


# ---- Basic 認証は残す ------------------------------------------------------


async def test_basic_auth_still_works_for_api_clients(secured, pal_client, notifier):
    """curl やスクリプトからは Basic 認証の方が扱いやすい。"""
    app = create_app(secured, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://manager", auth=("admin", PASSWORD)
    ) as c:
        assert (await c.get("/api/config")).status_code == 200


async def test_basic_auth_with_a_wrong_password_fails(secured, pal_client, notifier):
    app = create_app(secured, pal_client=pal_client, notifier=notifier, start_background=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://manager", auth=("admin", "nope")
    ) as c:
        assert (await c.get("/api/config")).status_code == 401


@pytest.mark.parametrize("header", ["", "Bearer x", "Basic !!!notbase64", "Basic " + "eA=="])
def test_check_basic_header_rejects_junk(header):
    assert check_basic_header(header, "admin", "p") is False


# ---- 認証なしの構成 --------------------------------------------------------


async def test_no_password_means_no_login(client):
    """APP_PASSWORD 未設定なら従来どおり素通しで使える。"""
    assert (await client.get("/api/config")).status_code == 200
    body = (await client.get("/api/auth/status")).json()
    assert body == {"required": False, "authenticated": True}


async def test_login_is_a_noop_without_a_password(client):
    resp = await client.post("/api/login", json={"password": "whatever"})
    assert resp.status_code == 200
    assert resp.json()["required"] is False


# ---- 総当たり対策 ----------------------------------------------------------


def test_throttle_locks_after_repeated_failures():
    throttle = LoginThrottle(max_attempts=3, lockout=60)
    assert throttle.retry_after("1.2.3.4") == 0

    assert throttle.record_failure("1.2.3.4") == 0
    assert throttle.record_failure("1.2.3.4") == 0
    assert throttle.record_failure("1.2.3.4") == 60      # 3回目でロック

    assert throttle.retry_after("1.2.3.4") > 0
    assert throttle.retry_after("5.6.7.8") == 0          # 別の接続元は巻き込まない


def test_throttle_clears_on_success():
    throttle = LoginThrottle(max_attempts=3, lockout=60)
    throttle.record_failure("1.2.3.4")
    throttle.record_success("1.2.3.4")
    assert throttle.record_failure("1.2.3.4") == 0       # 数え直しになる


def test_throttle_forgets_old_failures():
    throttle = LoginThrottle(max_attempts=3, window=0.05, lockout=60)
    throttle.record_failure("1.2.3.4")
    throttle.record_failure("1.2.3.4")
    time.sleep(0.08)
    assert throttle.record_failure("1.2.3.4") == 0       # 古い分は数えない


async def test_repeated_bad_logins_get_locked_out(secured, pal_client, notifier):
    secured.app_login_max_attempts = 3
    secured.app_login_lockout_sec = 60
    app = create_app(secured, pal_client=pal_client, notifier=notifier, start_background=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        for _ in range(2):
            assert (await c.post("/api/login", json={"password": "nope"})).status_code == 401
        assert (await c.post("/api/login", json={"password": "nope"})).status_code == 429

        # ロック中は正しいパスワードでも受け付けない
        resp = await c.post("/api/login", json={"password": PASSWORD})
        assert resp.status_code == 429
        assert "秒後" in resp.json()["detail"]


async def test_successful_login_resets_the_counter(secured, pal_client, notifier):
    secured.app_login_max_attempts = 3
    app = create_app(secured, pal_client=pal_client, notifier=notifier, start_background=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manager") as c:
        await c.post("/api/login", json={"password": "nope"})
        await c.post("/api/login", json={"password": "nope"})
        assert (await c.post("/api/login", json={"password": PASSWORD})).status_code == 200
        # 数え直されているので、また2回は失敗できる
        await c.post("/api/login", json={"password": "nope"})
        assert (await c.post("/api/login", json={"password": "nope"})).status_code == 401


async def test_empty_password_is_rejected_by_validation(guest):
    assert (await guest.post("/api/login", json={"password": ""})).status_code == 422


# ---- WebSocket（R-14 の解消） ----------------------------------------------


def _expect_line(ws, wanted: str, limit: int = 20) -> bool:
    """目的の行が届くまで読む。

    購読を始めた時点で直近ログが流し込まれるので、
    最初の1件が自分の投げた行とは限らない。
    """
    for _ in range(limit):
        if ws.receive_json()["line"] == wanted:
            return True
    return False


def test_websocket_accepts_the_session_cookie(secured, pal_client, notifier):
    """Cookie は同一オリジンの WS ハンドシェイクにも送られる。

    Basic 認証だけだった頃は、ブラウザが WS にヘッダを付けないことがあり、
    ログインできていてもログ画面だけ繋がらなかった（R-14）。
    """
    from fastapi.testclient import TestClient

    app = create_app(secured, pal_client=pal_client, notifier=notifier, start_background=False)
    with TestClient(app) as client:
        resp = client.post("/api/login", json={"password": PASSWORD})
        assert resp.status_code == 200

        # TestClient は Cookie を保持するので、そのまま WS を張れる
        with client.websocket_connect("/ws/logs") as ws:
            app.state.broker.publish("ログの1行", source="app")
            assert _expect_line(ws, "ログの1行")


def test_websocket_is_refused_without_a_session(secured, pal_client, notifier):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect as WSDisconnect

    app = create_app(secured, pal_client=pal_client, notifier=notifier, start_background=False)
    with TestClient(app) as client:
        with pytest.raises(WSDisconnect):
            with client.websocket_connect("/ws/logs") as ws:
                ws.receive_json()


def test_websocket_still_accepts_basic_auth(secured, pal_client, notifier):
    """スクリプトからの接続用に Basic 認証も残す。"""
    import base64

    from fastapi.testclient import TestClient

    app = create_app(secured, pal_client=pal_client, notifier=notifier, start_background=False)
    token = base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/logs", headers={"Authorization": f"Basic {token}"}
        ) as ws:
            app.state.broker.publish("basic でも届く", source="app")
            assert _expect_line(ws, "basic でも届く")


def test_websocket_is_open_when_no_password_is_set(settings, pal_client, notifier):
    from fastapi.testclient import TestClient

    app = create_app(settings, pal_client=pal_client, notifier=notifier, start_background=False)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/logs") as ws:
            app.state.broker.publish("認証なし", source="app")
            assert _expect_line(ws, "認証なし")
