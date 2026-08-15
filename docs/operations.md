# 運用メモ — 何かあったときにどこを見るか

障害のときに探し回らずに済むよう、ファイルの所在と切り分け手順をまとめる。
設計の意図は [README](../README.md)、動作確認の手順は [テスト仕様書](test-plan.md)。

## 構成の全体像

**誰がゲームサーバのプロセスを持っているか**が、この構成でいちばん重要な分岐点になる。
本番は **LinuxGSM に一本化**した（ゲームサーバの systemd 運用は廃止）。

```
┌─ dashboard-Pal.service (systemd / mntuser) ──────────┐
│   uvicorn → 管理ツール本体                              │
│     予告・ワールド保存・ini 編集・予約・監視・アナウンス      │
│     プロセス制御は PAL_SERVICE_BACKEND に委譲            │
└──────────────────────────────────────────────────┘
                          │
                PAL_SERVICE_BACKEND=lgsm
                          │
        /home/mntuser/pwserver start|stop|restart
                          │
              LinuxGSM（tmux の中でゲームが動く）
```

管理ツール自身は今も systemd ユニットとして動く。廃止したのは**ゲームサーバ側**。

**プロセスの管理者は必ず1人にすること。** LinuxGSM の上に systemd を重ねると、
「落ちていたら起こす」役目が LinuxGSM の `monitor` と systemd の `Restart=` の
二人になり、管理ツールが意図的に止めている最中に横から起こされる。

構成が戻っていないかは次で確かめる。**`palworld.service` は無効のままであること。**

```bash
ls -d ~/lgsm 2>/dev/null && echo "LinuxGSM 構成"
curl -s localhost:8080/api/config | python3 -m json.tool | grep -E "pal_service_backend|pal_service_command"
systemctl is-enabled palworld.service 2>&1     # not-found / disabled が正
```

> `PAL_SERVICE_BACKEND=systemd` は開発用に残してあるだけ。本番で選ばれていると
> 起動時に警告が出る（`journalctl -u dashboard-Pal | grep 廃止`）。

## ログの所在

### 管理ツール (dashboard-Pal)

| 何 | どこ | 備考 |
|---|---|---|
| 管理ツール自身のログ | `journalctl -u dashboard-Pal` | **原因を追うときはここ。** 再起動シーケンスの各ステップ、systemctl の実行と失敗、例外のトレースバック |
| 同上（直近のみ） | ログ画面の `app` 区分 / `GET /api/logs` | メモリ上200行だけ。**プロセス再起動で消える** |
| ゲームサーバのログ取り込み | ログ画面の `server` 区分 | 取り込み元は `LOG_SOURCE`。本番は `file`（`LOG_FILE` の console ログ） |
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
| `PalWorldSettings.ini` | ゲーム側 | 場所は `PAL_SETTINGS_INI` |

### ゲームサーバ (LinuxGSM)

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
| `... がありません` | `PAL_SERVICE_COMMAND` のパスが違う | `ls -l /home/mntuser/pwserver` |
| `... に実行権限がありません` | 管理スクリプトが別ユーザ所有 | 管理ツールと同じユーザで実行できるようにする |
| `pwserver start` は通るのにサーバが上がらない | `dashboard-Pal.service` のサンドボックス | `PrivateTmp=false` と `ReadWritePaths=/home/mntuser`（[下記](#linuxgsm-構成で-pwserver-start-が黙って失敗する)） |
| `LoadState=not-found` | 廃止したはずの systemd 構成に戻っている | `PAL_SERVICE_BACKEND=lgsm` と `PAL_SERVICE_COMMAND` を設定する |

> **sudo / sudoers は使わない。** ゲームサーバの systemd 運用を廃止したので、
> `/etc/sudoers.d/dashboard-Pal` は不要（あるなら消してよい）。
> `sudo: The "no new privileges" flag is set` や `sudo: a password is required`
> が出るなら、構成が systemd に戻っている（issue #28 の症状）。

### シーケンスのステップ名と、その実体

journal と `/api/restart` に出るステップ名は**バックエンド中立**にしてある。
プロセスの操作を実際に何でやるかは `PAL_SERVICE_BACKEND` で決まる。

| ステップ | 何をしているか | 本番（lgsm）の実体 |
|---|---|---|
| `preflight` | 操作できるか先に確かめる | 管理スクリプトの存在と実行権限を見る（**叩かない**） |
| `world_save` | ワールド保存 | REST API `/v1/api/save` |
| `shutdown_api` | ゲームに終了を指示 | REST API `/v1/api/shutdown` |
| `wait_until_down` | 応答が止まるのを待つ | REST API への到達性 |
| `service_stop` | プロセスを止める | `pwserver stop` |
| `service_start` | プロセスを起こす | `pwserver start` |
| `service_restart` | プロセスを再起動 | `pwserver restart` |
| `apply_settings` | 停止中に ini を反映 | ファイル書き込み |
| `wait_until_up` | 応答が返るのを待つ | REST API への到達性 |
| `rescue_start` | 失敗後に起動だけ試す | `pwserver start` |
| `released` | 進行状態を手で解除した | — |

`service_*` は開発用の systemd バックエンドを選んだ場合だけ `systemctl <action> <unit>` になる。

`systemctl_stop` / `systemctl_start` / `systemctl_restart` は**旧名**（systemd しか
無かった頃の名残）。LinuxGSM 構成でも `systemctl` と表示されていて紛らわしかったので
`service_*` に改名した。古いログを grep するときだけ旧名も要る。

どの経路で動いているかは設定を開かずに確認できる。

```bash
curl -s localhost:8080/api/config | python3 -m json.tool | grep -E "pal_service_backend|pal_service_command"
```

### 「再起動シーケンス進行中」のまま戻らない（issue #34）

サーバは落ちて起動しているのに、画面が「(restarting) …」のまま止まっている状態。
このとき**停止も再起動も「既に進行中です」で弾かれる**ので、まず表示を解除する。

進行中バナーの「解除」ボタン（予告を過ぎると出る）か、次で解除できる。

```bash
curl -XPOST -u admin:*** localhost:8080/api/restart/release -H 'Content-Type: application/json' \
     -d '{"reason":"進行中のまま戻らない"}'
```

**解除してもサーバに送った指示は取り消されない。** 管理ツール側の進行状態を
手放すだけなので、解除したあとに必ずサーバの生死を確認すること。

```bash
curl -s localhost:8080/api/status | python3 -m json.tool | grep -E '"online"|"phase"'
```

どこで止まったかは、シーケンスのステップが journal に残っているので追える。
**最後に出たステップの次で止まっている。**

```bash
journalctl -u dashboard-Pal --since "-30min" | grep シーケンス
```

| 最後のステップ | 止まっている場所 | 見るところ |
|---|---|---|
| `world_save` | shutdown API の応答待ち | `PAL_SLOW_TIMEOUT`（既定120秒） |
| `shutdown_api` | サーバが落ちるのを待っている | `RESTART_SHUTDOWN_GRACE`（既定120秒） |
| `wait_until_down` / `apply_settings` | 起動・停止コマンドが返ってこない | `PAL_SERVICE_TIMEOUT`（既定300秒）。**ステップ間がちょうど 300 秒空いていたらこれ** |
| `service_start` / `service_restart` | サーバが応答を返すのを待っている | `RESTART_STARTUP_TIMEOUT`（既定180秒） |

放っておいても `RESTART_SEQUENCE_TIMEOUT`（既定900秒）で打ち切られ、
`failed` になって操作を受け付けるようになる。それより早く戻したいときに上の解除を使う。

> **既知の原因（修正済み）。** 管理ツールは以前、コマンドの標準出力をパイプで
> 受けていた。パイプは書き口が全部閉じるまで終わらないので、LinuxGSM のように
> ゲームを tmux に預けて自分は終了するスクリプトだと、**コマンドが終わっても
> 常駐側が握ったままで読み終わらない**。`pwserver start` が実際には数秒で
> 終わっているのに 300 秒（`PAL_SERVICE_TIMEOUT`）待たされ、成功した再起動を
> 失敗として報告していた（issue #34）。今は一時ファイルに受けてプロセスの
> 終了だけを待つ。**同じ症状が出たら、まず動いている版を確かめること。**

```bash
sudo -u mntuser git -C /opt/dashboard-Pal log --oneline -3
```

### ログ画面の `server` 区分が空

```bash
sudo grep -E "LOG_SOURCE|LOG_FILE|PAL_SERVICE_NAME" /etc/dashboard-Pal.env
```

- `LOG_SOURCE=journald` になっている → **LinuxGSM は journald に書かない。** `file` に切り替える（上記）。
  この組み合わせは起動時に警告が出る（`journalctl -u dashboard-Pal | grep LOG_SOURCE`）
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

sudo を使わない構成なので `NoNewPrivileges=true` を付けてある（配布している `dashboard-Pal.service` は設定済み）。

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
何もしない**。本番では `lgsm` であること（`systemd` は廃止した）。`dry_run` も `false` であること。
