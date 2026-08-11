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
| スケジュール | サーバの**起動 / 停止 / 再起動**を予約。毎日 / 単発 / cron 式、予約ごとに予告文と予告タイミングを設定 |
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

#### 反映タイミングを選べる（サーバ稼働中でも保存できる）

**Palworld は停止時に、メモリ上の設定で `PalWorldSettings.ini` を上書きする。**
そのため稼働中に ini を直接編集しても、次にサーバを止めた時点で書き戻されて消える。

だからといって毎回サーバを止めて編集するのは現実的でないので、
**保存時に反映タイミングを選べる**ようにしてある。

| 選択肢 | 挙動 | 稼働中でも保存 |
|--------|------|----------------|
| 次にサーバが停止するとき | 予約・手動を問わず、次の停止機会に自動反映 | ✅ |
| 特定の予約のときに反映 | 選んだメンテ枠でのみ反映 | ✅ |
| 今すぐ反映 | ini に直接書く | ❌（停止中のみ） |

「次に停止するとき」「特定の予約」を選んだ場合、変更は ini に書かず
`pending-settings.json` へ退避される。停止シーケンスがサーバを止めた直後に
読み出して ini へ書き込み、そのまま起動し直す。

```
予告 → ワールド保存 → 停止 → 保留中の変更を ini へ書き込む → 起動
```

これで**管理者はいつ設定を入力してもよく、反映はメンテナンス枠で自動的に起きる**。
メンテナンス時間を事前にアナウンスしておけば、当日に人が張り付く必要はない。

守っていること:

- **反映に失敗してもサーバは必ず起動し直す。** 設定が変わらないより、サーバが落ちたままの方が困る
- **失敗した保留は消さない。** 次の停止機会に再試行される。失敗は Discord に通知する
- **保存の時点で検証する。** 範囲外の値などは反映時ではなく保存時に弾く
- **保留があるときだけ `systemctl restart` を `stop → 書き込み → start` に分ける。**
  restart 一発だと ini を書き換える隙間が作れないため。保留が無ければ従来どおり restart

`PUT /api/settings-ini`（全文編集）は従来どおり稼働中だと 409 を返す
（緊急時は `force: true`）。

#### 新しいプロパティへの追従

設定項目の情報源は3つある。

| 情報源 | 内容 | 更新 |
|--------|------|------|
| `settings_schema.py` の `FIELDS` | 82項目の型・ラベル・範囲・カテゴリ。**手書き** | このツールの更新が必要 |
| 稼働中サーバの `/v1/api/settings` | サーバ自身が持つ設定。**権威ある情報源** | 自動 |
| 設定ファイルの実際の中身 | いま書かれているキー | 自動 |

手書きスキーマは Palworld の更新に必ず遅れるので、以下で補っている。

- **ini にある未知のキー** — 値から型を推論し「未知」バッジ付きで編集できる
- **サーバが持つ未設定のキー** — `/v1/api/settings` と突き合わせて「未設定の項目」に出す
- **どこにも無いキー** — 「一覧に無い項目を自分で追加」から名前と型を指定して書き足せる

これでツールの更新を待たずに新プロパティを設定できる。
項目名は `^[A-Za-z_][A-Za-z0-9_]*$` に限定し、`OptionSettings` の構文が壊れないようにしている。

### スケジュール（起動 / 停止 / 再起動の予約）

| 動作 | 内容 | 予告 |
|------|------|------|
| 再起動 | 予告 → ワールド保存 → 停止 → 起動 | 必須 |
| 停止 | 予告 → ワールド保存 → 停止（起動しない） | 必須 |
| 起動 | 即時起動 | なし（停止中のサーバには送れないため） |

「04:00 に停止、04:30 に起動」のようにメンテ枠を組める。
daily / once は指定時刻ちょうどに動作するよう予告リードぶん手前で発火し、
予告の無い起動は指定時刻ちょうどに発火する。

**設定変更はこの枠で自動反映される。** 稼働中に「次の停止時に反映」または
「この予約で反映」を選んで保存しておけば、枠の中で ini への書き込みまで自動的に行われる。
人が張り付く必要はない。

### ログイン

`APP_PASSWORD` を設定すると管理画面にログインが必要になる（空なら無認証）。

| 経路 | 認証方法 |
|------|----------|
| ブラウザ | ログイン画面 → セッション Cookie（HttpOnly / SameSite=Lax、HTTPS 時のみ Secure） |
| curl・スクリプト | Basic 認証（`-u admin:PASS`） |
| WebSocket（ログ画面） | Cookie。Basic 認証も受け付ける |

- セッションは署名付きトークンで、サーバ側には保持しない。
  署名鍵はファイルに永続化するので、**プロセスを再起動してもログインは切れない**
- ログイン失敗が続いた接続元は一定時間受け付けない（既定 10 回 / 300 秒）
- 401 に `WWW-Authenticate` を返さない。返すとブラウザ標準のダイアログが出て、
  自前のログイン画面と二重になるため

**Basic 認証だけだった頃、WebSocket が繋がらないことがあった。**
ブラウザは WS のハンドシェイクに Basic 認証を付けないことがあり、
ログインできていてもログ画面だけ動かない。Cookie は同一オリジンの
WS ハンドシェイクにも送られるので、この問題は起きない。

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
- **消える変更を書かせない** — Palworld が停止時に ini を上書きする仕様のため、稼働中の直接編集は 409 で止め、予約による反映へ誘導する
- **設定ファイルの所有者を変えない** — 一時ファイル + rename ではなく inode を保ったまま上書きする。置き換えるとゲーム側が停止時に ini を書き戻せなくなる
- **自分を締め出せないようにする** — `AdminPassword` / `RESTAPIEnabled` / `RESTAPIPort` はフォームから変更不可。無人の予約反映で通信できなくなるのを防ぐ
- **タイムアウトを失敗と混同しない** — 応答が返らないだけで処理は続いている可能性があるため、中止の文面を分ける
- **意図的な停止で誤警報を出さない** — シーケンス進行中と起動後の猶予は「応答なし」を通知しない
- **反映に失敗してもサーバを落としたままにしない** — 設定変更より稼働継続を優先し、保留は残して次の機会に再試行する
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
│   │   ├── scheduler.py     起動/停止/再起動の予約（APScheduler）
│   │   ├── monitor.py       定期サンプリングと閾値アラート
│   │   ├── settings_ini.py  PalWorldSettings.ini の読み書きとバックアップ
│   │   ├── settings_schema.py 各設定項目の型・範囲・カテゴリ定義
│   │   ├── cache.py         ゲームサーバへの問い合わせを間引く TTL キャッシュ
│   │   ├── auth.py          ログイン認証（セッション Cookie / Basic / 試行制限）
│   │   ├── pending.py       設定変更の予約（保留中の変更）
│   │   ├── logstream.py     ログの収集と WebSocket 配信
│   │   ├── notify.py        Discord Webhook
│   │   └── services.py      ゲームサーバのプロセス制御（systemd / 開発用モック）
│   ├── static/index.html    フロントエンド（これ1枚）
│   ├── tests/               pytest（372件）
│   └── requirements.txt
├── mock/mock_palworld.py    モック Palworld REST API
├── scripts/dev.sh           ローカル開発用の一括起動
├── scripts/check_secrets.py 秘密情報の混入チェック
├── .githooks/pre-commit     コミット前に上記を走らせる
├── .github/workflows/ci.yml CI（秘密情報 + テスト + JS 構文）
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
| `mise run check-secrets` | 秘密情報が混入していないか走査 |

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

# サーバの起動/停止（systemctl 相当。停止中でも制御できる）
curl     http://127.0.0.1:8212/__mock__/status
curl -XPOST http://127.0.0.1:8212/__mock__/stop
curl -XPOST http://127.0.0.1:8212/__mock__/start

# アップデートで新プロパティが増えた状況を再現（項目発見の確認用）
curl -XPOST http://127.0.0.1:8212/__mock__/settings \
  -H 'Content-Type: application/json' -d '{"bNewFeatureFromPatch":true}'
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

# 変更を予約する（稼働中でも保存できる。次の停止機会に自動反映）
curl -XPUT http://127.0.0.1:8080/api/settings-ini/fields \
  -H 'Content-Type: application/json' \
  -d '{"values":{"ExpRate":2.5},"when":"next_stop","note":"週末イベント"}'

# 特定の予約で反映する
curl -XPUT http://127.0.0.1:8080/api/settings-ini/fields \
  -H 'Content-Type: application/json' \
  -d '{"values":{"ExpRate":2.5},"when":"schedule","schedule_id":"<予約ID>"}'

# 反映待ちの一覧 / 取り消し / 今すぐ反映（停止中のみ）
curl http://127.0.0.1:8080/api/settings-ini/pending
curl -XDELETE http://127.0.0.1:8080/api/settings-ini/pending/<ID>
curl -XPOST http://127.0.0.1:8080/api/settings-ini/pending/apply

# ファイルに無い項目の追加（additions は明示指定でのみ書き足される）
curl -XPUT http://127.0.0.1:8080/api/settings-ini/fields \
  -H 'Content-Type: application/json' \
  -d '{"additions":{"ItemWeightRate":0.5}}'

# 一覧に無い新プロパティを、名前と型を指定して追加
curl -XPUT http://127.0.0.1:8080/api/settings-ini/fields \
  -H 'Content-Type: application/json' \
  -d '{"custom_additions":[{"name":"bNewFlagFromPatch","type":"bool","value":true}]}'

# ※ いずれもゲームサーバ停止中でないと 409 になる
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

これは注意書きではなく**仕組みで止めている**。`mise run setup` が
`core.hooksPath=.githooks` を設定し、コミット前に `scripts/check_secrets.py` が走る。

| 検査 | 内容 |
|------|------|
| 見本の空値ルール | `*.env.example` の `PASSWORD` / `TOKEN` / `WEBHOOK` / `SECRET` を含むキーに値が入っていたら弾く |
| トークン検出 | 追跡ファイル全体から Discord Webhook・Bot Token、GitHub、AWS、Slack、秘密鍵を探す |

フックを `--no-verify` で回避されても CI（GitHub Actions）が追跡ファイル全体を走査する。
対象外にしているのは検出パターンの定義とその試験の2ファイルだけで、
増えていないことをテストで見張っている。

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
| `test_services.py` | モックの稼働状態、起動/停止の反映、到達不能時の判定、停止→編集→起動の一連の流れ |
| `test_settings_ini.py` | ini のパース（引用符内カンマ含む）、更新、バックアップ、復元、不正な内容の拒否、パストラバーサル防止 |
| `test_settings_schema.py` | 項目の型解釈と書式化、未知キーの型推論、範囲/選択肢の検証、フォーム経由の更新 |
| `test_check_secrets.py` | 秘密情報の検出漏れと誤検知、実際に起きた流出未遂ケース、対象外リストの肥大防止 |
| `test_hardening.py` | 実機投入前に潰したリスク（タイムアウト分離、停止待ち、inode 保持、sudo、誤警報抑止、キャッシュ） |
| `test_settings_form_js.py` | ゲーム設定フォームの入力挙動を node で実行して検証（入力中に要素が作り直されないこと） |
| `test_auth.py` | トークンの偽造・期限切れ、Cookie の属性、ログアウト、総当たり制限、WebSocket 認証 |
| `test_pending.py` | 稼働中の保存、停止シーケンスでの自動反映、予約への紐づけ、反映失敗時の復旧、永続化 |
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
