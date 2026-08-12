#!/usr/bin/env python3
"""画面で使うフォントを Google Fonts から取得して同梱する。

CDN から読まずに同梱するのは、本番サーバが LAN 内に置かれて
インターネットに出られない構成を想定しているため。外から取りに行く作りだと、
その環境でだけフォントが当たらず、開発機では気づけない。

Google Fonts の CSS は unicode-range でサブセットに分かれている。
日本語は約120個に分割されており、ブラウザは実際に使われた文字を含む
サブセットだけを取りに来る。全部で 7MB ほどあるが一度に落ちるわけではない。
その仕組みを壊さないよう、unicode-range はオリジナルのまま残す。

使い方:
    python3 scripts/fetch_fonts.py

フォントを差し替えたいときは FAMILIES を書き換えて実行し直す。
生成物（backend/static/fonts/）はコミットする。
"""

from __future__ import annotations

import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "backend" / "static" / "fonts"

# 画面が使うファミリとウェイト。デザイン（Issue #19）に合わせている
FAMILIES = "family=Zen+Maru+Gothic:wght@500;700;900&family=JetBrains+Mono:wght@400;500;700"

# woff2 を返してもらうために、対応しているブラウザとして名乗る。
# 古い UA だと ttf が返ってきてサイズが数倍になる
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

HEADER = """/* Zen Maru Gothic (SIL Open Font License 1.1) / JetBrains Mono (SIL Open Font License 1.1)
 *
 * Google Fonts が配信している woff2 をそのまま同梱している。CDN から読まないのは、
 * 本番サーバが LAN 内に置かれてインターネットに出られない構成を想定しているため。
 * 外から取りに行く作りだと、その環境でだけフォントが当たらない。
 *
 * unicode-range はオリジナルのまま残してある。日本語は約120個のサブセットに
 * 分割されており、ブラウザは実際に使われた文字を含むサブセットだけを取りに来る。
 *
 * このファイルは scripts/fetch_fonts.py で生成している。手で編集しないこと。
 */
"""


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def main() -> int:
    css_url = f"https://fonts.googleapis.com/css2?{FAMILIES}&display=swap"
    print(f"CSS を取得: {css_url}")
    css = fetch(css_url).decode("utf-8")

    blocks = re.findall(r"@font-face\s*\{(.*?)\}", css, re.S)
    if not blocks:
        print("@font-face が取れませんでした。URL か UA を確認してください", file=sys.stderr)
        return 1

    DEST.mkdir(parents=True, exist_ok=True)
    # 既存の woff2 を消してから入れ直す。ウェイトを減らしたときに孤児を残さないため
    for old in DEST.glob("*.woff2"):
        old.unlink()

    jobs: list[tuple[str, str, str, str, str]] = []
    counter: dict[tuple[str, str], int] = {}
    for block in blocks:
        family = re.search(r"font-family: '([^']+)'", block).group(1)
        weight = re.search(r"font-weight: (\d+)", block).group(1)
        url = re.search(r"src: url\((https://[^)]+)\)", block).group(1)
        rng = re.search(r"unicode-range: ([^;]+);", block)
        slug = family.lower().replace(" ", "")
        key = (slug, weight)
        counter[key] = counter.get(key, 0) + 1
        local = f"{slug}-{weight}-{counter[key]:03d}.woff2"
        jobs.append((url, local, family, weight, rng.group(1).strip() if rng else ""))

    print(f"{len(jobs)} 個の woff2 をダウンロードします")

    def download(job: tuple[str, str, str, str, str]) -> None:
        url, local, *_ = job
        (DEST / local).write_bytes(fetch(url))

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(download, jobs))

    parts = [HEADER]
    for _url, local, family, weight, rng in jobs:
        parts.append(
            "@font-face {\n"
            f"  font-family: '{family}';\n"
            "  font-style: normal;\n"
            f"  font-weight: {weight};\n"
            "  font-display: swap;\n"
            f"  src: url({local}) format('woff2');\n"
            f"  unicode-range: {rng};\n"
            "}"
        )
    (DEST / "fonts.css").write_text("\n".join(parts) + "\n", encoding="utf-8")

    total = sum(f.stat().st_size for f in DEST.glob("*.woff2"))
    print(f"完了: {len(jobs)} ファイル / {total / 1024 / 1024:.1f} MB -> {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
