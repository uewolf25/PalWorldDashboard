#!/usr/bin/env bash
# ゲームサーバ無しでフル機能を触るための開発用起動スクリプト。
#   - モック Palworld REST API を :8212 で起動
#   - 管理ツールを :8080 で起動（モックを見るように環境変数を設定）
# Ctrl-C で両方止まる。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/backend/.venv"
DEV="$ROOT/.dev"

if [ ! -x "$VENV/bin/uvicorn" ]; then
  echo "依存がまだ入っていません。先に次を実行してください:" >&2
  echo "  mise run setup" >&2
  echo "（mise.toml が backend/.venv を自動作成し、setup が依存を入れます）" >&2
  exit 1
fi

mkdir -p "$DEV/backups"

# 設定ファイル編集画面を試せるよう、開発用の ini を用意する
if [ ! -f "$DEV/PalWorldSettings.ini" ]; then
  cat > "$DEV/PalWorldSettings.ini" <<'INI'
[/Script/Pal.PalGameWorldSettings]
OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,NightTimeSpeedRate=1.000000,ExpRate=1.000000,PalCaptureRate=1.000000,PalSpawnNumRate=1.000000,ServerPlayerMaxNum=32,ServerName="Dev Palworld Server",ServerDescription="",AdminPassword="mockpass",PublicPort=8211,RESTAPIEnabled=True,RESTAPIPort=8212)
INI
fi

export MOCK_ADMIN_PASSWORD=mockpass

export PAL_ENV=staging
export PAL_HOST=127.0.0.1
export PAL_PORT=8212
export PAL_ADMIN_USER=admin
export PAL_ADMIN_PASSWORD=mockpass
export APP_HOST=127.0.0.1
export APP_PORT=8080
export APP_PASSWORD=
export PAL_SETTINGS_INI="$DEV/PalWorldSettings.ini"
export PAL_BACKUP_DIR="$DEV/backups"
export PAL_SCHEDULE_STORE="$DEV/schedules.json"
export SCHEDULE_TIMEZONE=Asia/Tokyo
# 開発では待たされたくないので予告を短くする
export RESTART_NOTICE_OFFSETS=${RESTART_NOTICE_OFFSETS:-30,10,5}
export RESTART_SHUTDOWN_WAIT=1
export RESTART_DEBOUNCE_SEC=5
export MONITOR_INTERVAL=5
# journald は無いのでアプリ自身のログだけ流す
export LOG_SOURCE=none

cd "$ROOT"
"$VENV/bin/uvicorn" mock.mock_palworld:app --host 127.0.0.1 --port 8212 --log-level warning &
MOCK_PID=$!

cd "$ROOT/backend"
"$VENV/bin/uvicorn" app.main:app --host "$APP_HOST" --port "$APP_PORT" --log-level info &
APP_PID=$!

cleanup() { kill "$MOCK_PID" "$APP_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo
echo "  モック Palworld API : http://127.0.0.1:8212/docs"
echo "  管理画面            : http://127.0.0.1:8080/"
echo
echo "  プレイヤーを増やす  : curl -XPOST http://127.0.0.1:8212/__mock__/join"
echo "  サーバを落とす      : curl -XPOST 'http://127.0.0.1:8212/__mock__/fail?fail_all=true'"
echo

wait
