#!/usr/bin/env python3
"""git 管理下のファイルに秘密情報が混入していないか調べる。

2026-08-11 に、実際の Discord Webhook URL が dashboard-Pal.env.example に
書かれた状態でコミット直前まで進んだ（push 前に気づいて除去した）。
同じことを人の注意力で防ぐのは無理なので、機械で止める。

チェックは2種類。

1. *.env.example の秘密キーが空であること
   見本ファイルは git 管理下なので、値を書いた時点でリポジトリに載る。
   値の置き場所は /etc/dashboard-Pal.env（本番）か .dev/local.env（開発）だけ。

2. 追跡ファイル全体に、既知フォーマットのトークンが無いこと
   Discord Webhook、GitHub トークン、秘密鍵など、形で判別できるもの。

使い方:
    python3 scripts/check_secrets.py            # 追跡ファイル全体を見る
    python3 scripts/check_secrets.py FILE...    # 指定したファイルだけ見る（フック用）

問題が見つかったら終了コード 1。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# --- 1) *.env.example で値を持ってはいけないキー ---------------------------
# 名前に含まれていたら「秘密」とみなす語。実際のキー名に合わせてある。
SECRET_KEY_HINTS = (
    "PASSWORD", "PASSWD", "SECRET", "TOKEN", "WEBHOOK",
    "APIKEY", "API_KEY", "PRIVATE_KEY", "CREDENTIAL",
)

# 秘密っぽい名前でも、値が入っていて当然のもの（誤検知を避ける）
SECRET_KEY_ALLOW = (
    "PAL_ADMIN_USER",     # ユーザ名であって秘密ではない
    "APP_USER",
)

# 名前がこれで終わるものは「置き場所」であって秘密そのものではない。
# 例: APP_SESSION_SECRET_FILE=/var/lib/.../session-secret
#
# 逃げ道にはならない。仮に MY_TOKEN_FILE=ghp_xxx と書いても、
# 下のトークン検出（値の形で判別する方）で引っかかる
LOCATION_SUFFIXES = ("_FILE", "_PATH", "_DIR", "_URL_FILE")

# 見本として書いてよいプレースホルダ
PLACEHOLDER_VALUES = {
    "", "changeme", "change_me", "CHANGE_ME", "CHANGEME",
    "your-password-here", "xxx", "***", "<change me>", "TODO",
}

# --- 2) どのファイルにあっても困る、形で分かるトークン ---------------------
TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Discord Webhook URL",
     re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-]{20,}")),
    ("Discord Bot Token",
     re.compile(r"\b[MNO][A-Za-z0-9_\-]{23,}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}\b")),
    ("GitHub token",
     re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained token",
     re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("OpenAI/Anthropic 形式の API キー",
     re.compile(r"\b(?:sk|sk-ant)-[A-Za-z0-9_\-]{24,}\b")),
    ("AWS アクセスキー",
     re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack トークン",
     re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("秘密鍵",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
]

# バイナリや生成物は見ない
SKIP_DIRS = {".git", ".venv", ".dev", "__pycache__", ".pytest_cache", "node_modules"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".woff", ".woff2"}

# トークン検査の対象外にするファイル。
# 「検出のためのパターン」と「検出されるかを試す偽の値」を持つファイルだけ。
#
# ここは意図的に短く保つこと。増やすほど検査の穴になる。
# 追加したくなったら、まずその値を本当にリポジトリへ置く必要があるか考える。
ALLOWLIST_PATHS = frozenset({
    "scripts/check_secrets.py",              # 検出パターンの定義
    "backend/tests/test_check_secrets.py",   # 検出を試すための偽の値
})

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class Finding:
    def __init__(self, path: str, line_no: int, kind: str, detail: str) -> None:
        self.path = path
        self.line_no = line_no
        self.kind = kind
        self.detail = detail

    def __str__(self) -> str:
        return f"  {self.path}:{self.line_no}  [{self.kind}] {self.detail}"


def is_secret_key(key: str) -> bool:
    if key in SECRET_KEY_ALLOW:
        return False
    upper = key.upper()
    if upper.endswith(LOCATION_SUFFIXES):
        return False
    return any(hint in upper for hint in SECRET_KEY_HINTS)


def strip_value(raw: str) -> str:
    """コメントと引用符を落として、実際に入っている値を取り出す。"""
    value = raw.strip()
    # 行末コメント（引用符の中は無視する）
    out, in_quote = [], ""
    for ch in value:
        if ch in "\"'":
            in_quote = "" if in_quote == ch else (in_quote or ch)
        elif ch == "#" and not in_quote:
            break
        out.append(ch)
    value = "".join(out).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def check_env_example(path: Path, rel: str) -> list[Finding]:
    """見本ファイルの秘密キーに値が入っていないか。"""
    findings: list[Finding] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings

    for line_no, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if not is_secret_key(key):
            continue
        value = strip_value(raw)
        if value in PLACEHOLDER_VALUES:
            continue
        findings.append(
            Finding(rel, line_no, "見本に実値",
                    f"{key} に値が入っています（見本は空にしてください）")
        )
    return findings


def check_tokens(path: Path, rel: str) -> list[Finding]:
    """形で判別できるトークンが含まれていないか。"""
    findings: list[Finding] = []
    if rel in ALLOWLIST_PATHS:
        return findings
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings

    for line_no, line in enumerate(text.splitlines(), 1):
        for label, pattern in TOKEN_PATTERNS:
            if pattern.search(line):
                findings.append(Finding(rel, line_no, "トークン検出", label))
    return findings


def tracked_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [p for p in out.split("\0") if p]


def should_skip(rel: str) -> bool:
    parts = Path(rel).parts
    if any(p in SKIP_DIRS for p in parts):
        return True
    return Path(rel).suffix.lower() in SKIP_SUFFIXES


def scan(root: Path, targets: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for rel in targets:
        if should_skip(rel):
            continue
        path = root / rel
        if not path.is_file():
            continue
        if path.name.endswith(".env.example") or path.name == ".env.example":
            findings.extend(check_env_example(path, rel))
        findings.extend(check_tokens(path, rel))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="秘密情報の混入チェック")
    parser.add_argument("files", nargs="*", help="対象ファイル（省略時は追跡ファイル全体）")
    parser.add_argument("--root", default=".", help="リポジトリのルート")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    targets = args.files or tracked_files(root)
    if not targets:
        print("チェック対象がありません")
        return 0

    findings = scan(root, targets)
    if not findings:
        print(f"秘密情報チェック: OK（{len(targets)} ファイル）")
        return 0

    print("秘密情報が混入している可能性があります。\n", file=sys.stderr)
    for f in findings:
        print(str(f), file=sys.stderr)
    print(
        "\n値の置き場所は次の2つだけです。git 管理下の見本には書かないでください。\n"
        "  本番: /etc/dashboard-Pal.env  (600 root:root)\n"
        "  開発: .dev/local.env          (.gitignore 済み。dev.sh が読み込みます)\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
