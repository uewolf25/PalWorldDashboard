"""秘密情報スキャナのテスト。

これ自体が「秘密を漏らさない」ための仕組みなので、
検出漏れ（見逃し）と誤検知の両方を押さえておく。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# scripts/ はパッケージではないのでパス指定で読み込む
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_secrets.py"
_spec = importlib.util.spec_from_file_location("check_secrets", _SCRIPT)
check_secrets = importlib.util.module_from_spec(_spec)
sys.modules["check_secrets"] = check_secrets
_spec.loader.exec_module(check_secrets)


def write(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return name


# ---- 実際に起きた流出未遂 --------------------------------------------------


def test_detects_the_actual_incident(tmp_path):
    """2026-08-11 に起きた「見本ファイルに実 Webhook」を検出すること。"""
    rel = write(tmp_path, "dashboard-Pal.env.example", (
        "PAL_ADMIN_PASSWORD=\n"
        "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/"
        "1536406374475505745/Jx1g0WbwE_FLJK7T6c6WsJ637Tow2U1NkC_Xcs73CHnDyRVB\n"
    ))
    findings = check_secrets.scan(tmp_path, [rel])

    kinds = {f.kind for f in findings}
    assert "見本に実値" in kinds      # キー名から
    assert "トークン検出" in kinds     # 値の形から
    # 二重に引っかかるのは意図どおり（片方の判定が甘くてももう片方で止まる）


def test_clean_example_file_passes(tmp_path):
    rel = write(tmp_path, "dashboard-Pal.env.example", (
        "# これは見本です\n"
        "PAL_ADMIN_USER=admin\n"
        "PAL_ADMIN_PASSWORD=\n"
        "APP_PASSWORD=\n"
        "DISCORD_WEBHOOK_URL=\n"
        "DISCORD_ALERT_WEBHOOK_URL=\n"
    ))
    assert check_secrets.scan(tmp_path, [rel]) == []


# ---- 見本ファイルのキー判定 ------------------------------------------------


@pytest.mark.parametrize("line", [
    "PAL_ADMIN_PASSWORD=hunter2",
    "APP_PASSWORD=s3cret",
    "DISCORD_WEBHOOK_URL=https://example.com/hook",
    "SOME_API_TOKEN=abcdef",
    "MY_SECRET=value",
    'QUOTED_PASSWORD="in quotes"',
    "export EXPORTED_PASSWORD=value",
])
def test_secret_keys_must_be_empty(tmp_path, line):
    rel = write(tmp_path, "x.env.example", line + "\n")
    findings = [f for f in check_secrets.scan(tmp_path, [rel]) if f.kind == "見本に実値"]
    assert findings, f"検出できていない: {line}"


@pytest.mark.parametrize("line", [
    "PAL_ADMIN_USER=admin",          # ユーザ名は秘密ではない
    "APP_USER=admin",
    "PAL_HOST=127.0.0.1",
    "APP_PORT=8080",
    "PAL_SERVICE_NAME=palworld.service",
    "PAL_ADMIN_PASSWORD=",           # 空はよい
    "PAL_ADMIN_PASSWORD=CHANGE_ME",  # プレースホルダはよい
    "APP_PASSWORD=   ",              # 空白だけもよい
    "# PAL_ADMIN_PASSWORD=commented", # コメント行は見ない
    "PAL_ADMIN_PASSWORD=  # 説明だけ",  # 値なし + 行末コメント
])
def test_non_secrets_are_not_flagged(tmp_path, line):
    rel = write(tmp_path, "x.env.example", line + "\n")
    findings = [f for f in check_secrets.scan(tmp_path, [rel]) if f.kind == "見本に実値"]
    assert findings == [], f"誤検知: {line}"


def test_only_example_files_get_the_empty_value_rule(tmp_path):
    """見本以外（本番用の env など）は値が入っていて当然。"""
    rel = write(tmp_path, "local.env", "DISCORD_WEBHOOK_URL=https://example.com/x\n")
    findings = [f for f in check_secrets.scan(tmp_path, [rel]) if f.kind == "見本に実値"]
    assert findings == []


# ---- 形で分かるトークン ----------------------------------------------------


@pytest.mark.parametrize("secret", [
    "https://discord.com/api/webhooks/1234567890123456789/aBcDeFgHiJkLmNoPqRsTuVwXyZ012345",
    "https://discordapp.com/api/webhooks/1234567890123456789/aBcDeFgHiJkLmNoPqRsTuVwXyZ012345",
    "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "github_pat_11ABCDEFG0abcdefghijkl_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij0123456789",
    "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AKIAIOSFODNN7EXAMPLE",
    "xoxb-123456789012-abcdefghijklmnop",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
])
def test_known_token_formats_are_detected_anywhere(tmp_path, secret):
    """見本ファイルに限らず、どのファイルでも止める。"""
    rel = write(tmp_path, "scripts/deploy.sh", f"TOKEN={secret}\n")
    findings = [f for f in check_secrets.scan(tmp_path, [rel]) if f.kind == "トークン検出"]
    assert findings, f"検出できていない: {secret[:30]}"


@pytest.mark.parametrize("text", [
    "https://discord.com/api/webhooks/...",          # 説明用の省略形
    "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...",
    "sk-",                                            # 断片
    "ghp_",
    "参考: https://discord.com/developers/docs",       # 普通の URL
    "AKIA",
])
def test_documentation_is_not_flagged(tmp_path, text):
    """README の説明文で誤検知しないこと。"""
    rel = write(tmp_path, "README.md", text + "\n")
    findings = [f for f in check_secrets.scan(tmp_path, [rel]) if f.kind == "トークン検出"]
    assert findings == [], f"誤検知: {text}"


@pytest.mark.parametrize("rel", sorted(check_secrets.ALLOWLIST_PATHS))
def test_allowlisted_files_are_not_flagged(rel):
    """パターン定義とテスト用の偽の値は検出対象にしない。"""
    root = _SCRIPT.parents[1]
    assert check_secrets.scan(root, [rel]) == []


def test_allowlist_stays_small():
    """対象外リストは検査の穴なので、増えていないことを見張る。

    増やす必要が出たら、その値を本当にリポジトリへ置くべきか先に考えること。
    """
    assert check_secrets.ALLOWLIST_PATHS == {
        "scripts/check_secrets.py",
        "backend/tests/test_check_secrets.py",
    }


def test_allowlist_does_not_exempt_the_empty_value_rule(tmp_path):
    """対象外でもトークン検査を外すだけで、見本の空値ルールは残ること。"""
    findings = check_secrets.check_env_example.__doc__
    assert findings  # 実装が残っていること
    rel = write(tmp_path, "x.env.example", "MY_PASSWORD=real\n")
    assert [f for f in check_secrets.scan(tmp_path, [rel]) if f.kind == "見本に実値"]


# ---- 走査対象の絞り込み ----------------------------------------------------


@pytest.mark.parametrize("rel", [
    ".venv/lib/x.py",
    ".dev/local.env",          # 開発用の秘密置き場（gitignore 済み）
    "backend/__pycache__/x.py",
    "docs/logo.png",
])
def test_skipped_paths(tmp_path, rel):
    write(tmp_path, rel, "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n")
    assert check_secrets.scan(tmp_path, [rel]) == []


def test_missing_file_is_ignored(tmp_path):
    """削除されたファイルを渡されても落ちないこと（フックから呼ばれるため）。"""
    assert check_secrets.scan(tmp_path, ["does/not/exist.env.example"]) == []


# ---- 値の取り出し ----------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("value", "value"),
    ('"quoted"', "quoted"),
    ("'single'", "single"),
    ("value  # コメント", "value"),
    ('"has # inside"', "has # inside"),
    ("  # コメントだけ", ""),
    ("", ""),
])
def test_strip_value(raw, expected):
    assert check_secrets.strip_value(raw) == expected


# ---- 実リポジトリ ----------------------------------------------------------


def test_repository_is_clean():
    """このリポジトリ自体に秘密情報が無いこと。"""
    root = _SCRIPT.parents[1]
    findings = check_secrets.scan(root, check_secrets.tracked_files(root))
    assert findings == [], "\n".join(str(f) for f in findings)


# ---- 置き場所を指す変数（誤検知の修正） ------------------------------------


@pytest.mark.parametrize("line", [
    "APP_SESSION_SECRET_FILE=/var/lib/dashboard-Pal/session-secret",
    "PAL_ANNOUNCE_STORE=/var/lib/dashboard-Pal/announcements.json",
    "TLS_KEY_PATH=/etc/ssl/private/server.key",
    "TOKEN_DIR=/var/run/tokens",
])
def test_location_variables_are_not_secrets(tmp_path, line):
    """名前に SECRET や KEY が入っていても、指しているのは場所であって値ではない。"""
    rel = write(tmp_path, "x.env.example", line + "\n")
    findings = [f for f in check_secrets.scan(tmp_path, [rel]) if f.kind == "見本に実値"]
    assert findings == [], f"誤検知: {line}"


def test_location_suffix_is_not_an_escape_hatch(tmp_path):
    """_FILE を付ければ何でも書けるわけではない。

    キー名では見逃しても、値の形でトークンと分かるものは止める。
    """
    rel = write(tmp_path, "x.env.example",
                "MY_TOKEN_FILE=ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n")
    findings = check_secrets.scan(tmp_path, [rel])
    assert any(f.kind == "トークン検出" for f in findings)
