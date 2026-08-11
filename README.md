# dashboard-Pal

Palworld 専用サーバをブラウザから管理する Web UI。

- バックエンド: Python 3.13 / FastAPI / APScheduler / httpx / psutil
- フロントエンド: 単一 HTML + Vanilla JS（外部 CDN もライブラリも不使用）
- 配備: systemd ユニット

**ゲームサーバが無くても動く**のが特徴で、同梱のモック Palworld REST API を相手に
UI も API も一通り動作確認できる。

## 機能

| 画面 | 内容 |
|------|------|
| ダッシュボード | 1秒ごとに FPS / プレイヤー数 / 稼働時間 / CPU・メモリを更新。アナウンス送信と**送信履歴**、進行中シーケンスの状況 |
| プレイヤー | 接続中プレイヤーの Lv・Ping・座標・建築数を表示。キック / BAN |
| ワールド | プレイヤー座標のマップ表示と異常検知（高 Ping、建築数過多） |
| 稼働履歴 | 一定間隔で記録した FPS・人数・メモリをグラフ表示 |
| サーバ設定 | **サーバ操作の集約先** — 起動 / 再起動 / 停止 / ワールド保存。PalWorldSettings.ini の全文編集（保存前に自動バックアップ、世代管理と復元） |
| ゲーム設定 | PalWorldSettings.ini を**項目ごとのフォームで編集**。カテゴリ分け・検索・差分確認つき |
| 再起動予約 | 毎日 / 単発 / cron 式。予約ごとに予告文と予告タイミングを設定 |
| ログ | WebSocket で journalctl の出力と管理ツール自身のログをストリーミング |

破壊的な操作（起動 / 再起動 / 停止 / キック / BAN / ini 上書き / 復元 / 予約削除）は
すべてモーダルで最終確認してから実行する。

**サーバに対する操作は「サーバ設定」タブに集約している。** ダッシュボードには操作ボタンを置かず、
進行中シーケンスの状況表示にとどめている（ただし予告中のキャンセルだけは、
カウントダウンを見ている場所からすぐ止められるようダッシュボードにも残してある）。

### アナウンス

- 再起動と停止では、**アナウンス文と予告タイミングの指定が必須**。無告知でサーバを落とせない
- 文面の `{time}` は残り時間（「5分」「30秒」）に置き換わる
- 予告タイミングは 10分前 / 5分前 / 3分前 / 1分前 / 30秒前 / 10秒前 から選択、任意の秒数も追加可能
- 送信したアナウンスはすべて履歴に残る（手動 / 再起動 / 停止 / 予約 / システムの区分つき、送信失敗も記録）

### ゲーム設定（項目ごとの編集）

`PalWorldSettings.ini` は全項目が1行に詰まった独自形式なので、全文編集だと事故りやすい。
「ゲーム設定」タブでは項目ごとに型に応じた入力欄を出す。

- **有効/無効はプルダウン**、難易度やデスペナルティは選択肢のプルダウン
- 倍率や上限値は範囲（min/max/step）つきの数値入力。範囲外はサーバ側で 400 で弾く
- パスワード項目は伏字表示（ボタンで表示切替）
- 8カテゴリに分類 + 項目名での絞り込み + 「変更した項目だけ表示」
- 保存前に**変更点の一覧（変更前 → 変更後）をモーダルで確認**。変更した項目だけを書き戻す
- 値が変わっていなければ書き込まない（無駄なバックアップを増やさない）

**設定ファイルに書かれていない項目も設定できる。** Palworld は未記載の項目を内蔵の既定値で動かすため、
`bIsPvP` を有効にしたいのにファイルに行が無い、ということが起きる。
そうした項目は「未設定の項目」として別枠に出し、**「追加」を押したものだけ**書き足す。
黙って書き足さないのは、ゲーム側の既定値が変わったときに追従できなくなるため。
公式の既定値が確認できている項目はそれを初期値として提示し、裏取りできていない項目は
「既定値は未確認」と明示する（推測値を既定値として出すと事故になる）。

**Palworld の更新で項目が増えても壊れない。** スキーマに無いキーは ini 上の値から
型を推論して「未知」バッジ付きで編集できるようにしてある。
知らない項目を UI から消してしまうと保存時に消滅するのが一番まずいので、そこは必ず通す。
ただし**追加**できるのはスキーマに定義がある項目だけ（タイプミスで妙なキーを増やさないため）。

### Discord 通知

Discord Webhook に対応（Bot 連携は未実装）。流すのは次のとき。

| 種類 | タイミング |
|------|-----------|
| 再起動 / 停止 | 開始時と完了時のみ（予告の途中経過は流さない） |
| 再起動の中止 / キャンセル | 毎回（保存失敗による中止を見逃さないため） |
| メモリ閾値超過 | 警告 80% / 危険 90%、cooldown 付き |
| サーバの応答なし / 復帰 | 状態が変わった瞬間のみ |
| 管理ツールの起動 / 停止 | 毎回 |
| 手動アナウンス | チェックを入れたときだけ |

## 設計上気をつけた点

- **再起動を二重に走らせない** — `asyncio.Lock` に加えて、直前の再起動からの経過時間でデバウンス（`force: true` で上書き可）。再起動と停止は同じロックを共有する
- **ワールド保存に失敗したら再起動を中止する** — セーブデータを失わないため。中止はゲーム内と Discord の両方に通知する
- **予告中はキャンセルできる** — 保存・停止に入った後は受け付けない
- **無告知でサーバを落とさない** — アナウンス文と予告タイミングを API レベルで必須にしている（既定値へのフォールバックなし）
- **予告の失敗でシーケンスを止めない** — アナウンスが届かなくても保存と停止は続行し、失敗は履歴に残す
- **設定ファイルを壊さない** — `OptionSettings` 行が無い内容は 400 で弾き、書き込みは一時ファイル経由で原子的に置換
- **ゲームサーバが落ちていても画面は出る** — `/api/status` は常に 200 を返し、`online: false` で表現する
- **XSS 対策** — 値の描画は必ず `textContent`。`innerHTML` は使わない
- **秘密情報の伏字化** — Webhook URL と AdminPassword は `/api/config` で伏字にして返す

## ディレクトリ構成

```
~/work/Pal/dashboard-Pal/
├── backend/
│   ├── app/
│   │   ├── main.py          FastAPI アプリ本体（ルーティング）
│   │   ├── config.py        環境変数の読み込み
│   │   ├── palapi.py        Palworld REST API クライアント
│   │   ├── announce.py      アナウンスの送信口と履歴
│   │   ├── restart.py       再起動/停止シーケンス（予告・保存・停止）
│   │   ├── scheduler.py     再起動予約（APScheduler）
│   │   ├── monitor.py       定期サンプリングと閾値アラート
│   │   ├── settings_ini.py  PalWorldSettings.ini の読み書きとバックアップ
│   │   ├── settings_schema.py 各設定項目の型・範囲・カテゴリ定義
│   │   ├── logstream.py     ログの収集と WebSocket 配信
│   │   ├── notify.py        Discord Webhook
│   │   └── services.py      systemd ユニット操作
│   ├── static/index.html    フロントエンド（これ1枚）
│   ├── tests/               pytest（157件）
│   └── requirements.txt
├── mock/mock_palworld.py    モック Palworld REST API
├── scripts/dev.sh           ローカル開発用の一括起動
├── dashboard-Pal.env.example
└── dashboard-Pal.service
```

## ローカルで動かす（ゲームサーバ不要）

言語のバージョンは [mise](https://mise.jdx.dev/) で管理している（`mise.toml`）。
このディレクトリに入れば Python 3.13 に切り替わり、`backend/.venv` も自動で作られる。

```bash
cd ~/work/Pal/dashboard-Pal
mise trust      # 初回のみ（mise.toml を信頼する）
mise run setup  # 依存をインストール
mise run dev    # モックサーバ + 管理ツールを起動
```

| タスク | 内容 |
|--------|------|
| `mise run setup` | 依存パッケージのインストール |
| `mise run dev` | モック Palworld API と管理ツールを起動 |
| `mise run test` | pytest |
| `mise run checkjs` | フロントエンドの JS を `node --check` で構文チェック |

`mise.toml` の `_.python.venv` で venv が自動有効化されるので、
このディレクトリ内では `.venv/bin/` を書かずに `pytest` や `uvicorn` を叩ける。
node はフロントエンドの構文チェックにだけ使う（アプリの実行には不要）。

- 管理画面: http://127.0.0.1:8080/
- モック API: http://127.0.0.1:8212/docs

モックには状況を作るための操作エンドポイントがある（実機には無い）。

```bash
curl -XPOST http://127.0.0.1:8212/__mock__/join            # プレイヤーを1人増やす
curl -XPOST http://127.0.0.1:8212/__mock__/leave           # 1人減らす
curl -XPOST 'http://127.0.0.1:8212/__mock__/fail?fail_all=true'   # サーバ応答なしを再現
curl -XPOST 'http://127.0.0.1:8212/__mock__/fail?fail_save=true'  # ワールド保存だけ失敗させる
curl -XPOST 'http://127.0.0.1:8212/__mock__/fps?value=12'  # FPS を固定
curl -XPOST http://127.0.0.1:8212/__mock__/reset           # 初期状態に戻す
```

`dev.sh` では予告間隔を 30/10/5 秒に縮めてあるので、再起動シーケンスもすぐ確認できる。
API から直接指定することもできる。

```bash
# 再起動（アナウンス文と予告タイミングは必須）
curl -XPOST http://127.0.0.1:8080/api/restart \
  -H 'Content-Type: application/json' \
  -d '{"reason":"動作確認","announce_message":"{time}後に再起動します","notice_offsets":[3,2,1]}'

# 停止（起動はしない）
curl -XPOST http://127.0.0.1:8080/api/shutdown \
  -H 'Content-Type: application/json' \
  -d '{"announce_message":"{time}後に停止します","notice_offsets":[3,1]}'

# 起動
curl -XPOST http://127.0.0.1:8080/api/service/start -H 'Content-Type: application/json' -d '{}'

# アナウンス履歴
curl 'http://127.0.0.1:8080/api/announcements?limit=20'

# ゲーム設定をフォーム定義として取得
curl http://127.0.0.1:8080/api/settings-ini/fields

# 既存項目の変更（書式化と検証はサーバ側で行う）
curl -XPUT http://127.0.0.1:8080/api/settings-ini/fields \
  -H 'Content-Type: application/json' \
  -d '{"values":{"ExpRate":2.5,"bIsPvP":true,"Difficulty":"Hard"}}'

# ファイルに無い項目の追加（additions は明示指定でのみ書き足される）
curl -XPUT http://127.0.0.1:8080/api/settings-ini/fields \
  -H 'Content-Type: application/json' \
  -d '{"additions":{"ItemWeightRate":0.5}}'
```

## 設定ファイル（.env）の置き場所

アプリは**環境変数しか見ない**（`config.py` が `os.environ` を読むだけ）。
ファイルから環境変数への読み込みは、開発なら `dev.sh`、本番なら systemd が担当する。

| ファイル | 用途 | git | 権限 | 読む人 |
|----------|------|-----|------|--------|
| `dashboard-Pal.env.example` | **見本。キーと説明だけで値は空** | 追跡する | 644 | 人間 |
| `.dev/local.env` | **開発用の秘密情報** | 追跡しない | 600 | `dev.sh` |
| `/etc/dashboard-Pal.env` | **本番の全設定** | サーバ上にのみ存在 | 600 root:root | systemd |

### どこに何を書くか

| 書きたいもの | 開発 | 本番 |
|--------------|------|------|
| Discord Webhook URL、各種パスワード | `.dev/local.env` | `/etc/dashboard-Pal.env` |
| ポート、パス、予告間隔などの非秘密設定 | `scripts/dev.sh`（既に設定済み。変えたいときだけ `.dev/local.env` で上書き） | `/etc/dashboard-Pal.env` |
| 新しい環境変数を増やしたとき | 上記に加えて `dashboard-Pal.env.example` に**キーと説明だけ**追記 | 同左 |

**`*.env.example` に実際の値を書かないこと。** git に追跡されているので、
Webhook URL やパスワードを書くとそのままリポジトリに載る。
値の要る場所は `.dev/local.env` か `/etc/dashboard-Pal.env` のどちらかしかない。

```bash
# 開発用の秘密情報を置く（.dev/ は .gitignore 済み）
cat > .dev/local.env <<'EOF'
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...
EOF
chmod 600 .dev/local.env
```

`dev.sh` は自分の `export` を済ませたあとに `.dev/local.env` を読む。
つまり `local.env` に書いた値が優先される（`PAL_ENV` などの上書きも可能）。

## テスト

```bash
mise run test
```

モック API に ASGI 直結で繋ぐので、ポートもネットワークも使わない。

| ファイル | 内容 |
|----------|------|
| `test_dashboard.py` | ステータス、プレイヤー一覧、キック/BAN/UNBAN、ワールド、履歴、Basic 認証、秘密情報の伏字化 |
| `test_restart.py` | 予告→保存→停止の順序、保存失敗時の中止、キャンセル、二重実行の拒否、デバウンス、アナウンス必須化、停止シーケンス、Discord の流量 |
| `test_announce.py` | アナウンス履歴の記録・永続化・上限・フィルタ、送信失敗の記録、サービス操作 |
| `test_settings_ini.py` | ini のパース（引用符内カンマ含む）、更新、バックアップ、復元、不正な内容の拒否、パストラバーサル防止 |
| `test_settings_schema.py` | 項目の型解釈と書式化、未知キーの型推論、範囲/選択肢の検証、フォーム経由の更新 |
| `test_scheduler.py` | 予約の CRUD、バリデーション、永続化と再読み込み、発火から再起動への連動、予約ごとの予告時間 |
| `test_monitor.py` | メトリクス記録、メモリ閾値アラートと cooldown、サーバ up/down 検知、ログ配信 |

## 本番デプロイ

### 1. Palworld 側で REST API を有効にする

`PalWorldSettings.ini` の `OptionSettings` に以下を設定してサーバを再起動する。

```
RESTAPIEnabled=True,RESTAPIPort=8212,AdminPassword="任意のパスワード"
```

> REST API はインターネットに直接公開しない前提で作られている（Pocketpair の注意書き）。
> LAN 内、もしくは VPN 越しでのみ到達できるようにすること。

### 2. 配置

```bash
sudo useradd -r -s /usr/sbin/nologin palmanager
sudo mkdir -p /opt/dashboard-Pal
sudo rsync -a --exclude .venv --exclude .dev ./ /opt/dashboard-Pal/
cd /opt/dashboard-Pal/backend
sudo python3.13 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo chown -R palmanager:palmanager /opt/dashboard-Pal
```

### 3. 環境変数

```bash
sudo install -o root -g root -m 600 dashboard-Pal.env.example /etc/dashboard-Pal.env
sudo nano /etc/dashboard-Pal.env   # PAL_ADMIN_PASSWORD などを埋める
```

### 4. systemd 登録

```bash
sudo cp dashboard-Pal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dashboard-Pal
sudo systemctl status dashboard-Pal
```

http://\<サーバIP\>:8080/ で管理画面が開く。

### 5. 権限まわり（ここは環境に合わせて調整が必要）

管理ツールは `palmanager` ユーザで動くので、そのままでは次の2つができない。

**ゲームサーバの systemctl 操作** — sudoers で必要な操作だけ許可する。

```
# /etc/sudoers.d/dashboard-Pal
palmanager ALL=(root) NOPASSWD: /bin/systemctl restart palworld.service, \
                                /bin/systemctl start palworld.service, \
                                /bin/systemctl stop palworld.service, \
                                /bin/systemctl is-active palworld.service
```

この場合 `app/services.py` の `systemctl` 呼び出しを `sudo systemctl` に変える必要がある。
あるいは管理ツール自体を root で動かす（`dashboard-Pal.service` の `User=` を消す）。

**`PalWorldSettings.ini` の書き込み** — ファイルのグループを `palmanager` にして
グループ書き込みを許可するか、`ReadWritePaths` の設定と合わせて調整する。

```bash
sudo chgrp palmanager /path/to/PalWorldSettings.ini
sudo chmod g+w /path/to/PalWorldSettings.ini
```

**journalctl の閲覧** — ログ画面を使うなら `systemd-journal` グループに入れる。

```bash
sudo usermod -aG systemd-journal palmanager
```

## 注意点・既知の制約

- **パル/NPC の位置は取得できない。** Palworld の REST API が公開しているのは接続中プレイヤーの座標のみなので、
  ワールド画面のマップにはプレイヤーしか出ない。記事にあるパル・NPC の表示は
  セーブデータ解析など別の手段が必要になる。
- **座標の表示範囲は決め打ち**（±200000）。実際のワールド座標系に合わせて
  `index.html` の `RANGE` を調整すること。
- **管理画面自体の認証は Basic 認証のみ。** `APP_PASSWORD` 未設定だと無認証で誰でも操作できる。
  LAN 外に出すなら、リバースプロキシで TLS と認証を付けること。
- **cron 予約のみ挙動が異なる。** daily / once は指定時刻ちょうどに再起動されるよう
  予告リードぶん手前で発火するが、cron 式はその時刻から予告が始まるため、
  実際の再起動はリード時間ぶん後になる。画面の「次回再起動」列には実際の時刻を出している。
- **Discord Bot 連携は未実装。** アナウンスは Webhook にのみ送る。
  Bot から喋らせたい場合は `app/notify.py` に Bot Token + チャンネル ID の
  送信先を足す形になる（`DiscordNotifier.send` の呼び出し側は変更不要）。
- **`systemctl` が無い環境（macOS など）ではサービス操作をスキップする。**
  成功扱いで `simulated: true` を返すので、ローカル開発では再起動シーケンスが最後まで通る。
