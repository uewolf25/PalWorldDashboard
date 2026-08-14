# 運用メモ — 何かあったときにどこを見るか

障害のときに探し回らずに済むよう、ファイルの所在と切り分け手順をまとめる。
設計の意図は [README](../README.md)、動作確認の手順は [テスト仕様書](test-plan.md)。

## 構成の全体像

**誰がゲームサーバのプロセスを持っているか**が、この構成でいちばん重要な分岐点になる。

```
┌─ dashboard-Pal.service (systemd / mntuser) ──────────┐
│   uvicorn → 管理ツール本体                              │
│     予告・ワールド保存・ini 編集・予約・監視・アナウンス      │
│     プロセス制御は PAL_SERVICE_BACKEND に委譲            │
└──────────────────────────────────────────────────┘
                          │
        ┌─────────────────┴─────────────────┐
        │                                   │
  PAL_SERVICE_BACKEND=systemd         PAL_SERVICE_BACKEND=lgsm
        │                                   │
  sudo -n systemctl ... palworld.service   /home/mntuser/pwserver start|stop|restart
        │                                   │
  素の SteamCMD 構成                    LinuxGSM 構成（tmux の中でゲームが動く）
```

**プロセスの管理者は必ず1人にすること。** LinuxGSM の上に systemd を重ねると、
「落ちていたら起こす」役目が LinuxGSM の `monitor` と systemd の `Restart=` の
二人になり、管理ツールが意図的に止めている最中に横から起こされる。

どちらの構成かは次で判別できる。

```bash
ls -d ~/lgsm 2>/dev/null && echo "LinuxGSM 構成" || echo "素の構成の可能性"
systemctl status palworld.service
```

## ログの所在

### 管理ツール (dashboard-Pal)

| 何 | どこ | 備考 |
|---|---|---|
| 管理ツール自身のログ | `journalctl -u dashboard-Pal` | **原因を追うときはここ。** 再起動シーケンスの各ステップ、systemctl の実行と失敗、例外のトレースバック |
| 同上（直近のみ） | ログ画面の `app` 区分 / `GET /api/logs` | メモリ上200行だけ。**プロセス再起動で消える** |
| ゲームサーバのログ取り込み | ログ画面の `server` 区分 | 取り込み元は `LOG_SOURCE`。`journald` なら `journalctl -u ${PAL_SERVICE_NAME}`、`file` なら `LOG_FILE` |
| アクセスログ | `journalctl -u dashboard-Pal` | uvicorn が出す |

詳しさは `LOG_LEVEL`（既定 `INFO`）。切り分けのときだけ `DEBUG` に落とす。

```bash
journalctl -u dashboard-Pal -f
journalctl -u dashboard-Pal --since "2026-08-13 20:00" --until "2026-08-14 00:00"
journalctl -u dashboard-Pal -p warning --since today      # 警告以上だけ
journalctl -t sudo --since today                          # 誰がどこから systemctl を打ったか
```

> journal が揮発（`/var/log/journal` が無い）だと VM 再起動で消える。永続化する:
> ```bash
> sudo mkdir -p /var/log/journal && sudo systemd-journal-flush
> ```

### 管理ツールが持つ状態ファイル

すべて `StateDirectory=dashboard-Pal`（＝ `/var/lib/dashboard-Pal/`）の下。
ログではないが、障害時に「何が予約されていたか」を確認するのに要る。

| ファイル | 中身 | 環境変数 |
|---|---|---|
| `schedules.json` | 予約（起動 / 停止 / 再起動） | `PAL_SCHEDULE_STORE` |
| `announcements.json` | アナウンス送信履歴（失敗も記録） | `PAL_ANNOUNCE_STORE` |
| `pending-settings.json` | 反映待ちの設定変更 | `PAL_PENDING_STORE` |
| `presence.json` | プレイヤーの入退室履歴 | `PAL_PRESENCE_STORE` |
| `session-secret` | ログインセッションの署名鍵 | `APP_SESSION_SECRET_FILE` |
| `backups/` | `PalWorldSettings.ini` の世代バックアップ | `PAL_BACKUP_DIR` |
| `world-backups/` | ワールドセーブのバックアップ | `PAL_WORLD_BACKUP_DIR` |

```bash
sudo ls -la /var/lib/dashboard-Pal/
sudo cat /var/lib/dashboard-Pal/schedules.json | python3 -m json.tool
```

### 設定ファイル

| ファイル | 権限 | 備考 |
|---|---|---|
| `/etc/dashboard-Pal.env` | `600 root:root` | 全設定。**パスワードを含むので中身を貼らないこと** |
| `/etc/systemd/system/dashboard-Pal.service` | `644 root:root` | `ReadWritePaths` を ini の実際の置き場に直す必要がある |
| `/etc/sudoers.d/dashboard-Pal` | `440 root:root` | systemd 構成のみ。lgsm 構成では不要 |
| `PalWorldSettings.ini` | ゲーム側 | 場所は `PAL_SETTINGS_INI` |

### ゲームサーバ (LinuxGSM 構成)

LinuxGSM は journald ではなく自前のログファイルに書く。**`${rootdir}` は
LinuxGSM を導入したユーザの home**（この環境では `/home/mntuser`）。

| 何 | 場所 |
|---|---|
| **ゲームのコンソール出力（現行）** | `~/log/console/pwserver-console.log` |
| 同（起動ごとに保存される） | `~/log/console/pwserver-console-YYYY-MM-DD-HH:MM:SS.log` |
| **LinuxGSM の動作ログ（現行）** | `~/log/script/pwserver-script.log` |
| 同（起動ごと） | `~/log/script/pwserver-script-YYYY-MM-DD-HH:MM:SS.log` |
| LinuxGSM のアラート | `~/log/script/pwserver-alert.log` |
| steamcmd（アップデート）のログ | `~/log/script/pwserver-steamcmd.log` |
| tmux ソケット | `/tmp/tmux-<uid>/pwserver-<8桁の乱数>` |
| LinuxGSM の設定 | `~/lgsm/config-lgsm/pwserver/common.cfg` / `pwserver.cfg` |
| ゲーム本体 | `~/serverfiles/` |
| `PalWorldSettings.ini` | `~/serverfiles/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini` |

Palworld (Unreal) 自身のログ（`~/serverfiles/Pal/Saved/Logs/`）は
この構成では出ていない。コンソール出力の方を見ること。

**tmux セッションを直接触らないこと。** ソケット名は起動のたびに変わり、
LinuxGSM が自分で控えている。起動・停止は必ず `pwserver` 経由にする。

```bash
find ~/log -name '*.log' -printf '%TY-%Tm-%Td %10s  %p\n' | sort -r | head -20
tail -f ~/log/console/pwserver-console.log
ls -la /tmp/tmux-$(id -u)/
```

> **`-servername=` に注意。** LinuxGSM は起動パラメータに
> `-servername='...'` を渡す（`~/lgsm/config-lgsm/pwserver/pwserver.cfg` の
> `startparameters`）。ini の `ServerName` を管理ツールから変えても、
> こちらが優先されて反映されないことがある。
>
> ```bash
> grep -n "startparameters" ~/lgsm/config-lgsm/pwserver/*.cfg
> pgrep -af PalServer-Linux-Shipping
> ```

**ログ画面に流すなら `LOG_SOURCE=file` + `LOG_FILE=<console ログのパス>` にする。**
LinuxGSM 構成で `LOG_SOURCE=journald` のままだと、存在しないユニットを tail し続けて
1行も出ない（`journalctl -u` は存在しないユニット名でもエラーにならない）。

### 外部スクリプト（この構成に固有）

| 何 | 場所 |
|---|---|
| アップデート検知 | `~/batch/update-watch.sh`（cron から10分おき） |
| そのログ | `~/batch/Log_update-watch/log_YYYYMMDD.log` |

## 切り分け

### サーバ操作（起動 / 停止 / 再起動）が失敗する

管理ツールはシーケンスの冒頭で**操作できるかを確かめてから**サーバを落とす。
中止されたときは通知に「サーバーは落としていません」と入る。

```bash
journalctl -u dashboard-Pal -p warning --since today | grep -iE "preflight|systemctl|pwserver"
```

| 症状 | 原因 | 対処 |
|---|---|---|
| `sudo: The "no new privileges" flag is set` | `dashboard-Pal.service` に `NoNewPrivileges=true` がある | その行を消す。setuid の sudo は原理的に昇格できない（issue #28） |
| `sudo: a password is required` | sudoers のパスが実体とずれている | `command -v systemctl` の結果と `/etc/sudoers.d/dashboard-Pal` を突き合わせる |
| `LoadState=not-found` | `PAL_SERVICE_NAME` が実在しないユニットを指している | ユニットを作るか、`PAL_SERVICE_BACKEND=lgsm` に切り替える |
| `... に実行権限がありません` | `PAL_SERVICE_COMMAND` が別ユーザ所有 | 管理ツールと同じユーザで実行できるようにする |

sudoers を検証するときは、**ユニットと同じ条件で**確かめること。
`sudo -u mntuser ...` はログインシェル経由なのでサンドボックスを再現せず、
通っても本番で落ちる。

```bash
sudo systemd-run --uid=mntuser --pipe --wait sudo -n systemctl is-active palworld.service
```

### ログ画面の `server` 区分が空

```bash
sudo grep -E "LOG_SOURCE|LOG_FILE|PAL_SERVICE_NAME" /etc/dashboard-Pal.env
```

- `LOG_SOURCE=journald` なのにユニットが無い → `file` に切り替える（上記）
- `LOG_SOURCE=journald` でユニットもある → `mntuser` が `systemd-journal` グループに入っているか
- `LOG_SOURCE=file` → `LOG_FILE` のパスと読み取り権限

### LinuxGSM 構成で `pwserver start` が黙って失敗する

`dashboard-Pal.service` のサンドボックスに阻まれている可能性が高い。

```bash
sudo grep -E "PrivateTmp|ProtectHome|ReadWritePaths" /etc/systemd/system/dashboard-Pal.service
```

| 設定 | 症状 |
|---|---|
| `PrivateTmp=true` | tmux のソケット（`/tmp/tmux-<uid>/pwserver-<乱数>`）が見えなくなる。管理ツールから見える `/tmp` が別の名前空間になるため、**SSH から起動したセッションを掴めず、停止も状態確認もすれ違う**。`false` にすること |
| `ReadWritePaths` が ini のディレクトリだけ | LinuxGSM は rootdir 配下にログ・ロック・serverfiles を書く。`ReadWritePaths=/home/mntuser` まで開けること |

tmux のソケットがどこにあるかは次で確認できる。

```bash
ls -la /tmp/tmux-$(id -u)/ 2>/dev/null
echo "${TMUX_TMPDIR:-/tmp}"
```

sudo を使わない構成なので、代わりに `NoNewPrivileges=true` を足してよい。

### 設定変更が ini に書き込まれない

- **稼働中は書かない仕様。** Palworld が停止時にメモリ上の設定で ini を上書きするため、
  稼働中の変更は `pending-settings.json` に退避し、次の停止機会に反映する
- `ProtectHome=read-only` が効いているので、`ReadWritePaths` が ini の実際の
  ディレクトリを含んでいないと**書き込みだけ失敗する**
- ini ファイル自体にグループ書き込み権限が要る（inode を保ったまま上書きするため）

```bash
sudo grep ReadWritePaths /etc/systemd/system/dashboard-Pal.service
sudo grep PAL_SETTINGS_INI /etc/dashboard-Pal.env
ls -l "$(sudo grep PAL_SETTINGS_INI /etc/dashboard-Pal.env | cut -d= -f2)"
```

### 身に覚えのない「サーバ応答なし」通知

管理ツールは**自分が起こした停止**の間だけ警報を抑止する。外部スクリプト
（LinuxGSM の `monitor`、`update-watch.sh` のアップデート適用など）が
サーバを止めている間は抑止が効かないので、偽の警報が飛ぶ。

```bash
crontab -l
tail -50 ~/batch/Log_update-watch/log_$(date +%Y%m%d).log
```

### 誰がサーバを起動したのか分からない

```bash
journalctl -t sudo --since "2026-08-13" --until "2026-08-14"   # TTY=pts/N なら人が手で打った
journalctl -u dashboard-Pal --since "2026-08-13" | grep -iE "サーバ操作|systemctl|pwserver"
grep -iE "start|monitor" ~/log/script/pwserver-script.log | tail -20
sudo cat /var/lib/dashboard-Pal/announcements.json | python3 -m json.tool | tail -40
```

管理ツール経由の操作なら、アナウンス履歴と Discord の両方に区分つきで残る。
どちらにも無ければ、管理ツールの外から操作されている。

## 状態を一望する

```bash
systemctl status dashboard-Pal

# どの経路でサーバを操作しているか（設定ファイルを開かずに確認できる）
curl -s localhost:8080/api/config | python3 -m json.tool \
  | grep -E "pal_service_backend|pal_service_command|pal_service_name|log_source|dry_run|env"

# 現在の稼働状況と、進行中シーケンスの状態
curl -s localhost:8080/api/status | python3 -m json.tool | head -30
```

`pal_service_backend` が `mock` や `simulated` になっていると、**操作が成功を装って
何もしない**。本番では `systemd` か `lgsm` であること。`dry_run` も `false` であること。
