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

# 設定ファイル編集画面を試せるよう、開発用の ini を用意する。
# 項目数が実機に近くないとフォーム UI の確認にならないので、
# 公式の既定値をひととおり並べてある。
if [ ! -f "$DEV/PalWorldSettings.ini" ]; then
  cat > "$DEV/PalWorldSettings.ini" <<'INI'
[/Script/Pal.PalGameWorldSettings]
OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,NightTimeSpeedRate=1.000000,ExpRate=1.000000,PalCaptureRate=1.000000,PalSpawnNumRate=1.000000,PalDamageRateAttack=1.000000,PalDamageRateDefense=1.000000,PlayerDamageRateAttack=1.000000,PlayerDamageRateDefense=1.000000,PlayerStomachDecreaceRate=1.000000,PlayerStaminaDecreaceRate=1.000000,PlayerAutoHPRegeneRate=1.000000,PlayerAutoHpRegeneRateInSleep=1.000000,PalStomachDecreaceRate=1.000000,PalStaminaDecreaceRate=1.000000,PalAutoHPRegeneRate=1.000000,PalAutoHpRegeneRateInSleep=1.000000,BuildObjectDamageRate=1.000000,BuildObjectDeteriorationDamageRate=1.000000,CollectionDropRate=1.000000,CollectionObjectHpRate=1.000000,CollectionObjectRespawnSpeedRate=1.000000,EnemyDropItemRate=1.000000,DeathPenalty=All,bEnablePlayerToPlayerDamage=False,bEnableFriendlyFire=False,bEnableInvaderEnemy=True,bActiveUNKO=False,bEnableAimAssistPad=True,bEnableAimAssistKeyboard=False,DropItemMaxNum=3000,DropItemMaxNum_UNKO=100,BaseCampMaxNum=128,BaseCampWorkerMaxNum=15,DropItemAliveMaxHours=1.000000,bAutoResetGuildNoOnlinePlayers=False,AutoResetGuildTimeNoOnlinePlayers=72.000000,GuildPlayerMaxNum=20,PalEggDefaultHatchingTime=72.000000,WorkSpeedRate=1.000000,bIsMultiplay=False,bIsPvP=False,bCanPickupOtherGuildDeathPenaltyDrop=False,bEnableNonLoginPenalty=True,bEnableFastTravel=True,bIsStartLocationSelectByMap=True,bExistPlayerAfterLogout=False,bEnableDefenseOtherGuildPlayer=False,CoopPlayerMaxNum=4,ServerPlayerMaxNum=32,ServerName="Dev Palworld Server",ServerDescription="",AdminPassword="mockpass",ServerPassword="",PublicPort=8211,PublicIP="",RCONEnabled=False,RCONPort=25575,Region="",bUseAuth=True,BanListURL="https://api.palworldgame.com/api/banlist.txt",RESTAPIEnabled=True,RESTAPIPort=8212,bShowPlayerList=False,ChatPostLimitPerMinute=10,AllowConnectPlatform=Steam,bIsUseBackupSaveData=True,LogFormatType=Text,SupplyDropSpan=180,EnablePredatorBossPal=True,MaxBuildingLimitNum=0,ServerReplicatePawnCullDistance=15000.000000,bAllowGlobalPalboxExport=True,bAllowGlobalPalboxImport=False,bInvisibleOtherGuildBaseCampAreaFX=False,bBuildAreaLimit=False,ItemWeightRate=1.000000,RandomizerType=None,RandomizerSeed="",bIsRandomizerPalLevelRandom=False)
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
# 開発では素通し。ログイン画面を試したいときは値を入れる
export APP_PASSWORD=${APP_PASSWORD:-}
export APP_SESSION_SECRET_FILE="$DEV/session-secret"
# 実機の LinuxGSM は無いので、モックサーバ自体を起動/停止するバックエンドを使う。
# これがないと「停止」しても停止扱いにならず、設定ファイルの編集を試せない
export PAL_SERVICE_BACKEND=mock
export PAL_MOCK_CONTROL_URL=http://127.0.0.1:8212
export PAL_SETTINGS_INI="$DEV/PalWorldSettings.ini"
export PAL_BACKUP_DIR="$DEV/backups"
# ワールド画面のセーブ情報とバックアップを試せるようにする。
# 中身は形だけのダミー（実際のセーブは無いので、サイズと世代管理の確認用）
export PAL_SAVE_DIR="$DEV/SaveGames"
export PAL_WORLD_BACKUP_DIR="$DEV/world-backups"
export PAL_WORLD_BACKUP_KEEP=3
export PAL_PRESENCE_STORE="$DEV/presence.json"
if [ ! -d "$DEV/SaveGames" ]; then
  mkdir -p "$DEV/SaveGames/0/DEVWORLD/Players"
  head -c 262144 /dev/urandom > "$DEV/SaveGames/0/DEVWORLD/Level.sav"
  head -c 16384  /dev/urandom > "$DEV/SaveGames/0/DEVWORLD/LocalData.sav"
  head -c 32768  /dev/urandom > "$DEV/SaveGames/0/DEVWORLD/Players/00000001.sav"
fi
export PAL_SCHEDULE_STORE="$DEV/schedules.json"
export PAL_PENDING_STORE="$DEV/pending-settings.json"
export SCHEDULE_TIMEZONE=Asia/Tokyo
# 開発では待たされたくないので予告を短くする
export RESTART_NOTICE_OFFSETS=${RESTART_NOTICE_OFFSETS:-30,10,5}
export RESTART_SHUTDOWN_WAIT=1
export RESTART_DEBOUNCE_SEC=5
export MONITOR_INTERVAL=5
# journald は無いのでアプリ自身のログだけ流す
export LOG_SOURCE=none

# 実 Webhook などの秘密情報はここに置く（.dev/ は .gitignore 済み）。
# リポジトリに追跡させないため、*.env.example には絶対に書かないこと。
if [ -f "$DEV/local.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$DEV/local.env"
  set +a
  echo "  $DEV/local.env を読み込みました"
fi

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
