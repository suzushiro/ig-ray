# IG-Ray

X-Ray の Instagram 版。**完全独立フォーク**（X-Ray と共通ライブラリ化はしない）。
スクレイパ層 + Docker 運用 + 表示層（フィード / ユーザーページ / ギャラリー / ブックマーク）。

## 同梱物

```
Dockerfile.worker         worker イメージ（Python 3.12 固定・非root）
Dockerfile.web            web イメージ（Flask + waitress）
docker-compose.yml        全サービス。既定では何も常駐しない
.env.example              設定テンプレ（cp して .env に）
app/db.py                 スキーマ + DBヘルパ
app/ig_scraper.py         スクレイパ本体
app/make_session.py       対話ログインでセッション作成
app/seed_accounts.py      監視対象アカウントの管理
app/web.py                表示層（Flask）
app/cache_utils.py        キャッシュ容量集計・保持期間の掃除
app/config.py             環境変数の読み取り（旧 INSTA_RAY_* も互換で読む）
app/check_egress.py       出口IPの確認（回線分離の検証）
app/templates/            Jinja2テンプレート（X-Ray v18 から移植）
app/requirements.txt      worker 用（instaloader）
app/requirements-web.txt  web 用（flask + waitress）
dev/mock_check.py         ネットワーク不要のモック検証（コンテナ非同梱）
dev/ratelimit_check.py    レート制限 fail-fast の検証（コンテナ非同梱）
dev/loader_check.py       ログイン検証まわりのテスト（コンテナ非同梱）
dev/diag_session.py       429の原因切り分けツール（コンテナ非同梱）
dev/accounts_check.py     seed_accounts のテスト（コンテナ非同梱）
dev/fallback_check.py     web_profile_info 回避策のテスト（コンテナ非同梱）
dev/probe_posts.py        投稿の生データを覗く診断（コンテナ非同梱）
dev/route_check.py        表示層の疎通テスト（コンテナ非同梱）
dev/migration_check.py    既存DBからのマイグレーション検証（コンテナ非同梱）
dev/egress_check.py       設定まわり・出口IP判定のテスト（コンテナ非同梱）
app/backfill.py           全件バックフィル（キュー管理＋常駐ワーカー）
app/tumblr_client.py      Tumblr API v2 クライアント（OAuth1）
tools/tumblr_auth.py      Tumblrのアクセストークン取得（初回だけ・コンテナ非同梱）
dev/purge_check.py        アカウント完全削除のテスト（コンテナ非同梱）
dev/backfill_check.py     全件バックフィルのテスト（コンテナ非同梱）
dev/backup_page_check.py  バックアップ状況画面のテスト（コンテナ非同梱）
dev/video_check.py        GraphVideo 取り扱いのテスト（コンテナ非同梱）
dev/rename_db.py          DBファイル名の移行ヘルパ（コンテナ非同梱）
dev/privacy_check.py      公開ファイルへの環境固有情報の混入検査（コンテナ非同梱）
dev/share_check.py        Tumblr投稿のテスト（コンテナ非同梱）
dev/tumblr_check.py       Tumblr APIクライアントのテスト（コンテナ非同梱）
deploy/                   外部公開の設定例（Cloudflare Tunnel）
NOTES.example.md          作業メモの雛形（実物 NOTES.md は .gitignore 済み）
dev/lightbox_check.js     ライトボックスのキー操作テスト（jsdom・コンテナ非同梱）
```

zip は X-Ray と同じく**サブフォルダ無しで直下展開**。

## 動作確認（ログイン不要）

```bash
python3 dev/mock_check.py        # 59項目
python3 dev/ratelimit_check.py   # 23項目
python3 dev/loader_check.py      # 20項目
python3 dev/accounts_check.py    # 35項目
python3 dev/fallback_check.py    # 135項目
python3 dev/route_check.py       # 113項目
python3 dev/migration_check.py   # 21項目
python3 dev/egress_check.py      # 37項目
python3 dev/purge_check.py       # 39項目
python3 dev/video_check.py       # 34項目
python3 dev/backfill_check.py    # 57項目
python3 dev/backup_page_check.py # 50項目
python3 dev/share_check.py       # 100項目
python3 dev/tumblr_check.py      # 58項目
python3 dev/privacy_check.py     # 17項目（公開ファイルの情報漏れ検査）

npm i -D jsdom && node dev/lightbox_check.js   # 82項目
```

計 880 項目通過。前者が extract_media / post_to_record / save_posts /
media_index / accounts / scrape_log / init_db 冪等性、
後者が instaloader の**本物の** RateController / get_json リトライループを使った
fail-fast 検証。

## セットアップ

**`.env` の作成は必須です。** これを飛ばすと `IG_RAY_LOGIN_USER が未設定です` で止まります。

```bash
cd ~/ig-ray
cp .env.example .env

# UID/GID を自分に合わせる
sed -i "s/^IG_RAY_UID=.*/IG_RAY_UID=$(id -u)/" .env
sed -i "s/^IG_RAY_GID=.*/IG_RAY_GID=$(id -g)/" .env

# ログインに使うIGアカウント名を書く（IGアカウント名は実際の値に）
sed -i "s/^IG_RAY_LOGIN_USER=.*/IG_RAY_LOGIN_USER=IGアカウント名/" .env

# 確認
grep -E "LOGIN_USER|UID|GID" .env

mkdir -p data/sessions data/cache

# 非root(1000)で動くので、root所有のファイルが残っていると書き込めない
sudo chown -R $(id -u):$(id -g) data

# profile 付きのサービスは素の `build` ではビルドされない。
# worker イメージが古いまま動く事故になるので、必ず profile 付きで。
docker compose --profile cron --profile tools build
```

### セッション作成

```bash
docker compose run --rm login
```

パスワードを対話で聞かれます（画面に出ません）。2FA にも対応。
作成直後に `test_login()` で有効性を検証し、`/data/sessions/session-アカウント名` に保存します。

> セッションファイルはパスワード同等の価値があります。`chmod 600` で保存されますが、
> バックアップやリポジトリに紛れ込ませないこと（`.gitignore` で `data/` を除外済み）。

### ミュート

ミュートしたアカウントは**取得されず**、フィードとギャラリーからも消えます。
取得済みの投稿は削除されません（解除すれば元通り）。

```bash
docker compose run --rm accounts mute アカウント名
docker compose run --rm accounts mute アカウント名 --reason "理由"
docker compose run --rm accounts unmute アカウント名
docker compose run --rm accounts muted        # 一覧
```

**投稿カードの 🔇 ボタン**からもミュートできます。押すと確認ダイアログが出て、
理由（任意）を入れて実行すると、そのアカウントのカードが画面から消えます
（リロード不要）。`/mutes` 画面からも操作できます。

API は `muted` を明示指定でき、冪等です。省略時のみトグルします
（カードのダイアログは常に `muted: true` を送るので、
既にミュート中のアカウントを押しても誤って解除されません）。ユーザーページ `/user/名前` には
ミュート中でも直接アクセスできます。

`is_enabled`（`accounts disable`）とは別軸です。

| | 取得 | 表示 |
|---|---|---|
| 通常 | する | する |
| `disable` | しない | する |
| `mute` | しない | しない |

### 監視対象の登録

```bash
docker compose run --rm accounts add minamina_flyover
docker compose run --rm accounts add https://www.instagram.com/someone/   # URLでも可
docker compose run --rm accounts list
docker compose run --rm accounts disable someone   # 巡回から外す（データは残る）
```

### 取得

```bash
# まず疎通（1アカウント・3件・メディアなし）
docker compose run --rm scrape minamina_flyover --limit 3 --no-media

# メディア込み
docker compose run --rm scrape minamina_flyover --limit 6

# 引数なし = accounts の有効なものを巡回
docker compose run --rm scrape --limit 6
```

### 確認

```bash
docker compose run --rm shell
# コンテナ内で:
sqlite3 /data/ig_ray.db "SELECT DISTINCT typename FROM posts;"
sqlite3 /data/ig_ray.db "SELECT username, status, fetched, inserted, message \
                            FROM scrape_log ORDER BY id DESC LIMIT 5;"
```

### 定期巡回（任意・既定で無効）

```bash
docker compose --profile cron up -d cron    # サービス名を明示する
docker compose --profile cron logs -f cron
docker compose --profile cron stop cron
```

一発実行のサービス（`scrape` / `accounts` / `shell` など）には
`profiles: ["tools"]` を付けてあるので `up` では起動しません。
これが無いと `--profile cron up -d` が `scrape` も一緒に起動し、
**cron と二重に巡回**して `database is locked` や
並行アクセスによるブロックの原因になります。

**cron 稼働中に手動で `scrape` を叩かないこと。** 試すときは先に
`docker compose --profile cron stop cron` を。

**手動運用で安定を確認してから**にすること。既定は6時間間隔（`IG_RAY_INTERVAL`）。
IGは叩きすぎると即ブロックされるので、短くしないこと。

## なぜ既定で何も常駐しないのか

`docker compose up` しても取得は始まりません。全部 `docker compose run --rm` で明示実行です。
IGはXと比べてブロックが厳しく、「気づいたら裏で叩き続けていた」が一番危ないため、
意図しない実行が起きない側に倒してあります。定期巡回は `--profile cron` で明示的に有効化します。

## worker を別の場所で動かす

サーバのIPが弾かれる場合、**worker だけ別のネットワークで動かす**構成が取れます。
`Dockerfile.worker` と `data/` があれば単体で動くので、

- 自宅マシンで `docker compose run --rm scrape ...` を回す
- `data/ig_ray.db` と `data/cache/` をサーバへ同期（rsync など）
- サーバでは表示層（v2.1）だけを動かす

DB が SQLite 単一ファイルなので同期は素直です。ただし
**worker と web が同時に同じDBを書かない**ようにすること（web は読み取りのみの想定）。

## 出口を別回線に分離する（任意）

Instagram のスクレイピングは BAN / IP制限のリスクがあるため、
**普段使いの回線と IP レピュテーションを切り離す**構成を取れるようにしてある。
別回線側にプロキシを立て、worker だけそこから出す。

`.env` に書く:

```bash
HTTP_PROXY=http://<プロキシのIP>:8888
HTTPS_PROXY=http://<プロキシのIP>:8888
NO_PROXY=localhost,127.0.0.1
IG_RAY_EXPECT_EGRESS_IP=<分離先回線のグローバルIP>
```

**worker 系のサービスにだけ**入れること（`web` はローカルのファイルを
返すだけで外に出ないため）。requests / instaloader は標準でこの環境変数を見る。

プロキシ自体は tinyproxy など何でもよい。内部LAN側を Listen、
分離したい回線側をデフォルトルートにして、コンテナ内の IPv6 は無効化しておく
（IPv6 が有効だとそちらの経路で元の回線から出てしまう）。

> 実際に使っている機材・IP・LXC の設定は `NOTES.md`（git管理外）に置いてある。

### 出口の確認

```bash
docker compose run --rm ipcheck
```

3経路をそれぞれ確認する。**素の requests / メディアDL用の匿名セッション /
投稿取得用のログイン済みセッション**で、どれか1つでも別のIPから出ていれば
警告を出す。`IG_RAY_EXPECT_EGRESS_IP` を設定しておくと自動判定して
一致しないときに非ゼロ終了する。

**`.env` を旧 `INSTA_RAY_*` のままにしないこと。** `config.py` は旧名も読むが、
**compose が `${IG_RAY_*}` しか参照しない**ので、旧名の変数はコンテナまで届かない。
`sed -i 's/^INSTA_RAY_/IG_RAY_/' .env` で変換する。

**設定を変えたら毎回確認すること。** Docker ホストが IPv6 グローバルを
持っている場合、環境変数を入れ忘れたコンテナは **IPv6 で元の回線から出る**。
分離が黙って崩れるので、漏れに気づける仕組みが要る。

- 画像の一括ダウンロードも同じ出口を通る（`ipcheck` の「メディアDL用セッション」で確認できる）
- 分離先の回線が遅い場合、大量ダウンロード時のスループットは要観察
- ブラウザ自動操作を足す場合、環境変数だけでは効かないことがある
  （Playwright なら `launch` の `proxy` オプションが別途必要）

## ビルド時の注意

**`docker compose build` は profile 付きのサービスをビルドしない。**
worker 系は `profiles: ["tools"]`、cron は `profiles: ["cron"]` なので、
素の `build` では `web` しか作られない。

```bash
docker compose --profile cron --profile tools build
```

これを忘れると、**web だけ新しく worker が古いまま**という状態になる。
`app/` を変えたのに挙動が変わらないときは、まずこれを疑う。

## SQLite のロックについて

worker（`scrape` / `cron`）と web が同じDBを触るため、
**書き込みトランザクションを長く握らないこと**が重要です。

v3.7 以前はメディアDLの書き込みをアカウント1件ぶんまとめてコミットしていたため、
投稿ごとの休憩（3〜8秒）のあいだずっと書き込みロックを保持し、
別プロセスが `database is locked` で落ちていました。
現在は**1投稿ごとにコミット**します。

- `IG_RAY_BUSY_TIMEOUT_MS`（既定60秒）までロック解放を待ちます
- 1アカウントがDBエラーで落ちても巡回全体は止まりません
  （`scrape_log` に `error` として残り、次のアカウントへ進みます）
- `-wal` / `-shm` の所有者がずれていてもロックの原因になります。
  `sudo chown -R $(id -u):$(id -g) data` で揃えてください

## スキーマ変更のときの注意

**新しい列を参照する DDL は、必ずマイグレーションの後ろに置くこと。**

`db.py` の `DDL` リストは `CREATE TABLE IF NOT EXISTS` なので、既存DBでは
何もしません。列は `_add_column_if_missing()` で後から足されます。
新しい列に張る `CREATE INDEX` を DDL リストに入れると、既存DBでは
列が存在しない状態で走って `no such column` で落ちます（v3.4 で実際に発生）。

`dev/migration_check.py` が旧スキーマDBを組み立てて検証します。
スキーマを触ったら必ず走らせてください。

## 設計メモ

- **キャッシュ / 永続の分離**（X-Ray 踏襲）
  - キャッシュ側 = `posts`, `media_index`(is_persistent=0), `/data/cache`。消えても再取得できる
  - 永続側 = `accounts`, `bookmarks`。手で育てるので消したくない
- **冪等**: `posts` は shortcode 主キー + ON CONFLICT。いいね数・キャプションは後から変わるので上書き更新、`fetched_at` を更新
- **重複排除**: `media_index.sha256` で同一実体を検出し、既存ファイルを使い回す
- **レート制限 (v1.1で修正)**: `Instaloader(sleep=True)` + 5件ごと・アカウントごとに 3〜8 秒のランダム休憩。
  加えて `FailFastRateController` を差し込み、**`IG_RAY_MAX_SLEEP` を超える待機を要求されたら `RateLimitAbort` を上げて即中止**する。
  v1 では instaloader が 429 を食うと内部で 666 秒眠って自力リトライしてしまい、
  こちらの中止ロジックが一切発火しなかった（実地テストで発覚）。
  `wait_before_query()` / `handle_429()` はどちらも最終的に `sleep()` を呼ぶので、`sleep()` だけ差し替えれば両経路を押さえられる。
- **なぜ独自例外クラスなのか**: `TooManyRequestsException` は `ConnectionException` のサブクラスで
  `get_json()` の `except (ConnectionException, ...)` の捕捉対象、`AbortDownloadException` は
  `test_login()` で握り潰される。instaloader には broad な `except Exception` が無いため、
  `Exception` 直下の独自クラスなら**どの経路でも確実に呼び出し側まで抜ける**。
  （実測では `TooManyRequestsException` でも結果的には抜けるが、それは `handle_429` の
  `sleep` で再度上がるという内部実装に依存している。独自クラスはその依存が無い）
- 例外は握りつぶさず `scrape_log` に status（`ok` / `error` / `ratelimited`）で残す

## X-Ray との差分

| X-Ray | ig-ray |
|-------|-----------|
| `tweet_id` | `shortcode` |
| `screen_name` | `owner_username` |
| `content` | `caption` |
| `created_at` | `date_utc` |
| `like_count` | `likes` |
| `reply_count` | `comments` |
| `retweet_count` / `quoted_json` / self-reply | **削除**（IGに概念なし） |
| `media` | `media_json`（`get_sidecar_nodes()` 展開） |
| — | 追加: `is_video` / `is_carousel` / `location` / `hashtags_json` / `typename` |

## 運用上の注意

- instaloader は非公式スクレイパなので、**Instagram の仕様変更で普通に壊れる**。X-Ray とは別リポジトリ・別DBで隔離しているのはこのため
- セッションは無期限ではない。落ちたら作り直し
- 取得間隔と件数は控えめに。`--limit 3` の疎通確認から始め、3 → 6 → 12 と伸ばす
- **作りたてのアカウントは初回リクエストで即 429 を食うことがある**。ログイン直後にAPIを叩くのではなく、
  ブラウザ/アプリで普通にログインして少し使ってから（フォロー・スクロール・プロフィール設定）回すと通りやすい
- `ratelimited` が出た日はそれ以上回さない。翌日に持ち越す
- 対象は公開アカウントに限定するのが無難。非公開アカウントは `PrivateProfileNotFollowedException` で弾かれる（弾かれる設計のままにしておく）

## 表示層

```bash
docker compose up -d --build web
# http://localhost:8079
```

X-Ray v18 の表示層を移植したもの。画面はフィード / ユーザーページ /
ギャラリー / ブックマーク / ミュート / ストレージの6つ。ダークモード・無限スクロール・
画像拡大・一括ダウンロードは X-Ray のものがそのまま動く。

ライトボックスのキー操作:

**フィード / ユーザーページ**

| キー | 動作 |
|---|---|
| ← → | 同じ投稿内の画像を切り替え（複数枚のとき。端でループ） |
| ← → | 前後の投稿へ移動（1枚だけの投稿のとき） |
| ↑ ↓ | 前後の投稿へ移動 |
| Esc | 閉じる |

**ギャラリー**（1アイテム=1画像のグリッド）

| キー | 動作 |
|---|---|
| ← → | 隣の画像へ（行をまたぐ） |
| ↑ ↓ | 1行ぶん上下へ |
| Esc | 閉じる |

列数は `getBoundingClientRect()` から実測するので、
ウィンドウ幅が変わっても行移動が合います。

**動画は再生できません。** サムネイルしか保存していない（`IG_RAY_DOWNLOAD_VIDEOS`
が既定OFF）ため、動画には「▶ 再生」バッジを出して Instagram の投稿ページへ
リンクします。ライトボックスでも、動画を表示中はツールバーに
「▶ Instagramで再生 ↗」が出ます。

↓ で次の投稿に移ると先頭画像、↑ で戻ると末尾画像から表示する。
移動先が画面外なら `scrollIntoView` で追従するので、無限スクロールの
読み込みもそのまま誘発される。ギャラリーは1アイテム=1画像なので
↑↓ が実質「隣の画像へ」になる。

X-Ray からの差分:

- RT / 引用RT / 自己リプライ / スペースは IG に無いので**全削除**
- **カテゴリ機能は未実装**（タブ分け・R18除外なし）。ギャラリーのタブは
  アカウント単位の絞り込みに置き換えてある
- **画像削除UIは未移植**（投稿カード上の個別削除ボタン）。ただし
  **ストレージ画面からのキャッシュ一括削除は実装済み**
- X-Ray の「永続保存(/data/images)へハードリンクで昇格」は**移植しない**。
  ig-ray はキャッシュ1ディレクトリのみで、**ブックマークが投稿ごと
  コピーを持つ**ため、保護対象は「ブックマークが参照しているパス」で判定する
- メディア解決が違う。X-Ray は `media_json`(URL配列) + `local_media_json`、
  ig-ray は `media_json`(dict配列) + `media_index` テーブル。
  `resolve_media()` が吸収し、ローカルがあれば `/cache/...`、
  無ければリモートURLにフォールバックする（未取得は ☁ バッジ付き）
- **ブックマークは非正規化コピー**。`posts` が消えてもブックマークは残る
- degraded 運用でも**アイコンと表示名は取れる**（下記）。取れないのは
  followers / biography / mediacount で、その場合ユーザーページは
  「プロフィール情報は未取得」と出す

**web からの書き込みはブックマーク操作とキャッシュ削除の2つだけ**です。
それ以外は読み取り専用の想定なので、`scrape` と同時に重い書き込みを
行わないようにしてください。

### ストレージ画面

`/storage` でキャッシュ容量・アカウント別の内訳・ディスク使用状況を確認でき、
古いキャッシュの削除も行えます。

| 項目 | 内容 |
|---|---|
| 保持期間 | `IG_RAY_CACHE_RETENTION_DAYS`（既定30日） |
| 保護対象 | **ブックマークが参照している画像**（`days=0` の全削除でも消えない） |
| 削除後 | `media_index.local_path` を NULL に戻し、表示はリモートURLへフォールバック |

キャッシュを消しても投稿本文とリモートURLは残るので、表示は Instagram 側の
画像に切り替わります（☁ バッジが付く）。ブックマークは投稿ごとコピーを
持っているため、内容自体は失われません。

## アイコンと表示名は投稿データから取れる

`web_profile_info` が 429 でも、**アイコンURLと表示名は追加リクエストなしで取得できる**。

ログイン時の投稿には `iphone_struct.user` が丸ごと入っており、instaloader 自身も
`Profile.from_iphone_struct()` でここから `username` / `full_name` /
`profile_pic_url` を組み立てている。`owner_from_node()` が同じ辞書を直接読む。

- アイコンURLは署名付きで期限切れになるので、**実体を `/data/cache/_avatars/` に保存**する
  （7日で取り直し）。DBには `accounts.profile_pic_local` として持つ
- 保存には**キャッシュ削除の保護がかかる**（`_avatars` 配下は `days=0` の
  全削除でも消えない）。サイズが小さく常に必要なため
- 反映は `db.merge_account()`。**None は既存値を上書きしない**ので、
  後から `followers` が取れても潰さない
- 共同投稿では**相手側アカウントのアイコンも拾える**

取れないのは `followers` / `biography` / `mediacount`（これらは
`web_profile_info` 専用）。

## 429 が出たときの切り分け

```bash
docker compose run --rm diag 対象アカウント名
```

| 段階 | 見るもの |
|---|---|
| A | セッションファイルの存在・サイズ |
| B | クッキーに `sessionid` があるか |
| C | **`test_login()` が username を返すか**（サーバ側で有効か） |
| D | 実際にプロフィールを1件引けるか |

**「直近1リクエストなのに429」はレート超過ではない。** ほぼ確実にアカウントの
ソフトブロック。`401 "Please wait a few minutes before you try again."` が出たら
それが確定サイン。ブラウザでログインしてチャレンジを消化し、数日普通に使うこと。
`few minutes` と書いてあるが実際は数時間〜数日かかる。叩き続けると延びる。

## web_profile_info が 429 で弾かれる場合

実機で遭遇した状況。セッションは有効（`test_login` は通る）なのに、
`api/v1/users/web_profile_info/` だけが恒常的に 429 を返す。

instaloader の経路はこう分かれている:

| 処理 | エンドポイント |
|---|---|
| `Profile.from_username()` | `api/v1/users/web_profile_info/` |
| `get_posts()`（ログイン時） | `graphql/query`（doc_id POST） |

`get_posts()` は変数に `username` しか使わないので、`from_username()` を
経由しなければ投稿は取れる。そこで `minimal_profile()` で
`_obtain_metadata()` を短絡させ、投稿取得だけ通す。

**`IG_RAY_POSTS_ONLY=1` にすること。** 自動フォールバックもあるが、
一度 429 を食うとペナルティが全クエリ種別に波及するため
（`RateController.query_waittime()` の `untracked_next_request_time()` は
query_type を見ない）、最初から叩かないほうが速く確実。

この場合 `followers` / `full_name` などプロフィール詳細は取得できず、
`accounts` には username のみが入る。ステータスは `ok_degraded`。

## 投稿ごとの堅牢性

instaloader の `Post` はデータ構造の差異でプロパティが `KeyError` を投げることがある
（実機で 8 件目の投稿が `KeyError: 'id'` を出し、取得済みの 7 件ごと失われた）。対策:

- `post_to_record` の各フィールドは `_safe()` 経由で、欠けていても既定値で続行
- それでも落ちる投稿は**その1件だけスキップ**し、残りは保存する。
  スキップした shortcode と例外はコンソールと `scrape_log` に残り、status は `ok_degraded`
- **`location` は生ノードから直接読む。** instaloader の `Post.location` は
  `int(loc['id'])` を要求するが、実データの `iphone_struct.location` のキーは
  `lat / lng / name / pk / profile_pic_url` で **`id` が無い** → `KeyError: 'id'`。
  さらに `_field("location")` は fake_node に無いと `_full_metadata` を取りに行くため
  **投稿ごとに追加の HTTP リクエスト**が発生する。
  生ノードの `iphone_struct.location.name` を読めば両方回避でき、追加コストもゼロ。
  生ノードに無い場合にプロパティ経由まで試すかは `IG_RAY_FETCH_LOCATION=1`（既定OFF）

原因を特定したいときは `probe` を使う（DBに書かない）:

```bash
docker compose run --rm probe 対象アカウント名 --limit 12 --dump-node
```

## メディア取得は匿名セッションで

`scontent-*.cdninstagram.com` からの取得に**ログイン済みセッションを使い回してはいけない**。
instaloader のセッションには `Host: www.instagram.com` が固定で入っており、
CDN に送ると host 不一致で **404** になる（実機で全件失敗して判明）。

`media_session()` が `context.get_anonymous_session()` を返す。
instaloader 自身も `get_raw()` で同じことをしている。

## 動画投稿（GraphVideo）の扱い

**`Post.video_url` プロパティを使ってはいけない。**

instaloader 4.15.3 の `video_url` は、`_field('video_url')` と
`iphone_struct['video_versions']` の全解像度を候補として集め、候補が2つ以上あると
**Content-Length を比べるために候補ごとに `context.head()` を投げる**。
実データでは候補はまず複数になるので、**動画投稿1件につきCDNへHEADが2〜4回**飛ぶ。
`location` と同じ「プロパティを触ると勝手に外へ出る」型の罠。

`Post.from_iphone_struct` が組み立てる fake_node には
`video_versions[-1]['url']` が既に `video_url` として入っているので、
`video_url_of()` がそこを直接読む。追加リクエストなし。

最高画質を厳密に選びたい場合だけ `IG_RAY_VIDEO_URL_PROBE=1`。
既定の `IG_RAY_DOWNLOAD_VIDEOS=0` では動画URLは保存するだけで使わないため、
わざわざHEADを投げる意味はない。

カルーセル内の動画は `_convert_iphone_carousel` が作る namedtuple から読むので
最初から追加リクエストは起きない。

`typename` は instaloader 側が `{1:"GraphImage", 2:"GraphVideo", 8:"GraphSidecar"}` の
決め打ち辞書で決めるので、Reels でも `GraphVideo` になる。

## アカウントの削除

2種類ある。

| | accounts 行 | posts / media_index | メディア実体 | ブックマーク |
|---|---|---|---|---|
| `accounts remove` | 消す | 残る | 残る | 残る |
| `accounts purge`  | 消す | 消す | 消す | **残る** |

```bash
docker compose run --rm accounts purge 対象アカウント名 --dry-run   # 何が消えるか確認
docker compose run --rm accounts purge 対象アカウント名             # 実行
```

確認はユーザー名の再入力式（`y/n` だと誤爆しやすいため）。`--yes` で飛ばせる。

**ブックマークは消さない。** 非正規化コピーなので `bookmarks` 行はそのまま残り、
それが参照しているメディア実体も保護される。
加えて、**他アカウントの投稿からも参照されている実体**（sha256 重複排除・共同投稿）も
保護する。ここを見ないと他アカウントの表示が壊れる。

accounts 行が消えるため、ブックマーク表示のアイコンと表示名は失われる。
残したい場合は `--keep-avatar`。

## DBファイル名の移行

`config.db_path()` が旧名 `insta_ray.db` を自動で拾うので、移行しなくても動く。
名前を揃えたいときだけ:

```bash
docker compose --profile cron --profile tools down
python3 dev/rename_db.py ~/ig-ray/data --dry-run
python3 dev/rename_db.py ~/ig-ray/data
docker compose --profile cron up -d
```

`-wal` / `-shm` も一緒に移す。移す前に WAL をチェックポイントするので、
**必ずコンテナを止めてから**実行すること（動いているとロックで開けない）。

## 全件バックフィル（丸ごとバックアップ）

指定アカウントの投稿を最古まで遡って保管する。1アカウントに数日〜1週間かける前提。

```bash
docker compose run --rm backfill add 対象アカウント名
docker compose run --rm backfill add-all          # 監視対象を全部積む
docker compose run --rm backfill list             # 進捗
docker compose --profile backfill up -d           # ワーカー起動
docker compose run --rm backfill pause 対象アカウント名
```

### なぜ常駐ワーカーなのか

instaloader の `NodeIterator` は `freeze()` / `thaw()` でページ送りの途中状態を
保存・復元できる（`FrozenNodeIterator` は素のJSON、実測500バイト程度、
有効期限は `_shelf_life = 29日`）。

ただし **`thaw()` には新しいイテレータが必要で、`NodeIterator.__init__` は
コンストラクタの中で1ページ目を取りに行く**
（`Profile.get_posts()` はログイン時 `first_data=None` のため）。
`thaw()` はその結果を捨てるので、**再開1回につき1リクエストが丸損**する。

したがって「1ページ取ってプロセス終了、次回再開」はリクエストが倍増する悪手。
プロセスを生かしたままページ間でスリープし、
`freeze()` の保存は**クラッシュ保険のチェックポイント**として使う
（freeze はローカル処理なのでリクエストを消費しない）。

`freeze` / `thaw` は**1件だけ重複再生する**（`total_index - 1` と
`edges[page_index-1:]` で意図的に1つ戻す取りこぼし防止の仕様）。
`save_posts` は shortcode の UPSERT で冪等なので実害は無いが、
`inserted` の数が再開ごとに1ずれる。

`thaw()` は `query_variables` / `query_referer` / `doc_id` に加えて
**ログインアカウント名（`context_username`）の一致も要求する**。
予備アカウントへ切り替えたら再開情報は捨てて取り直しになる（自動で検出する）。

### cron との共存

同時に叩くとレート予算を取り合い、**429 は全クエリ種別に波及する**ので
片方が食らうと両方が巻き添えになる。
そこで `meta` テーブルの `activity:scrape` を巡回側が打刻し、
**バックフィル側が巡回中は待つ**という一方向の譲り合いにしている
（cron は数分で終わるので待たせない）。`IG_RAY_BACKFILL_QUIET_SEC` で調整。

### scrape_log のステータス

**新しいステータスを `log_scrape` に足したら `db.OK_STATUSES` にも入れること。**
トップの警告バナーは「このリストに無いもの＝失敗」で判定するので、
入れ忘れると**成功が赤字で警告される**（v4.5 の `backfill_done` で実際にやった）。

| | |
|---|---|
| `ok` / `ok_degraded` | 巡回の正常終了。`ok_degraded` がこの運用の通常状態 |
| `backfill_done` | バックフィルの1周完了。**正常** |
| `ratelimited` / `error` | 要確認 |

`backfill_done` は `/backup` の巡回集計からは除外している（巡回とは別枠のため）が、
失敗としては数えない。この2つは別の話なので混同しないこと。

### バックアップ状況画面（`/backup`）

巡回と全件バックフィルの状況、および**次回開始時刻**を表示する。

**次回時刻は実行側が `meta` に書いたものだけを出す。** 表示側で「前回＋間隔」を
計算しない。理由は3つ:

- cron は「1周終わってから6時間」なので、処理時間ぶんズレる
- バックフィルのページ間隔は**ランダム**（10〜20分）なので計算では当たらない
- **ワーカーが止まっていても未来の時刻を出してしまう**

記録が無ければ素直に「予定なし」と出し、起動コマンドを案内する。

- 巡回側：`ig_scraper.py --cron` のときだけ、1周終了後に
  `next_run:scrape` を書く。**手動 `scrape` では書かない**
  （嘘の予定時刻が出るため。`--cron` は compose の cron サービスだけが付ける）
- バックフィル側：`page_nap()` が**寝る直前に**実際の秒数で `next_run:backfill` を書く。
  ジョブ完了・キューが空になったら消す
- ワーカーの生存は `activity:backfill_worker` の打刻で判定し、
  15分以上音沙汰が無ければ画面に警告を出す

**進捗率（何％）は出していない。** アカウントの総投稿数が分からないため
（`mediacount` は `web_profile_info` 専用で、この運用では叩いていない）。
代わりに「どこまで遡ったか」を投稿日で表示する。

### 2周目以降の打ち切り

**完全に保管済みの投稿が `IG_RAY_BACKFILL_KNOWN_STREAK` 件連続したら打ち切る。**
「保管済み」は posts に行があるだけでなく **media_index の実体まで揃っていること**
（画像バックアップが目的なので、レコードだけある投稿を既知と数えると取りこぼす）。

**初回は必ず最後まで走る。** cron が上位N件を既に取っているので、
初回から判定を効かせると先頭で止まってしまう。
`completed_runs > 0` のときだけ有効にしている。

## Tumblr 投稿（任意）

投稿カードの `t` ボタンから Tumblr に投稿する。**2方式ある。**

| | OAuth API 方式（推奨） | シェアツール方式（旧） |
|---|---|---|
| 外部公開 | **不要** | 必要（Tumblrが画像を取りに来る） |
| 複数枚 | **可** | **不可**（2026-08にTumblr側が劣化） |
| 投稿先の選択 | 可 | 不可 |
| 下書き | 可 | 不可 |
| 操作 | ボタン1発で完了 | Tumblrの投稿画面が開く |
| 要るもの | アプリ登録のみ | ドメイン・cloudflared・常駐 |

**`TUMBLR_CONSUMER_KEY` が設定されていれば API 方式を使う。**
未設定ならシェアツール方式にフォールバックする（`_inject_share_flags()`）。

### OAuth API 方式のセットアップ

**OAuth1 を使う（OAuth2 ではない）。** OAuth2 のアクセストークンは
`expires_in` が約42分でリフレッシュ処理が必須になるが、
OAuth1 のトークンは失効しないので一度認可すれば放置できる。

1. https://www.tumblr.com/oauth/apps でアプリを登録する。
   **コールバックURLは `http://localhost:8765/callback`**（OAuth2リダイレクトURLも同じ値でよい）
2. Consumer Key / Secret を `.env` に入れる
3. アカウントごとにトークンを取得する

```bash
cd tools
python3 -m venv .venv
.venv/bin/pip install requests requests-oauthlib

export TUMBLR_CONSUMER_KEY=xxxx
export TUMBLR_CONSUMER_SECRET=yyyy

.venv/bin/python tumblr_auth.py --label main --out ../data/tumblr_accounts.json
.venv/bin/python tumblr_auth.py --label sub  --out ../data/tumblr_accounts.json
.venv/bin/python tumblr_auth.py --list --out ../data/tumblr_accounts.json
```

**別アカウントを登録するときはシークレットウィンドウを使うか一度ログアウトすること。**
同じアカウントのままだと同じトークンが取れてしまう。

最近の Ubuntu は PEP 668 で `pip install` を直接拒否するので venv を使う。

#### GUIのないサーバーで実行する場合

認可にはブラウザが要る。手元のPCから**SSHポートフォワード**を張ってから実行する。

```bash
# 手元のPC側。ホスト名は解決できないことが多いのでIP直指定
ssh -L 8765:localhost:8765 user@<サーバーのIP>
```

その接続の中で `tumblr_auth.py` を実行し、表示された認可URLを
**手元のブラウザ**にコピペする。認可後 `localhost:8765` がSSH経由で転送される。

認可後に `channel N: open failed: Connection refused` が大量に出ることがあるが、
**スクリプトが待ち受けを閉じた後のノイズ**で無害。

### 設定

```bash
TUMBLR_CONSUMER_KEY=xxxx
TUMBLR_CONSUMER_SECRET=yyyy
TUMBLR_ACCOUNTS_FILE=/data/tumblr_accounts.json
```

compose の **web サービスにだけ**渡す（worker には不要）。

**`data/tumblr_accounts.json` は実質パスワード。** `.gitignore` 済み。
`os.replace` で原子的に書き `chmod 600` する。バックアップにも含めないこと。

`USER_AGENT` は `ig-ray-archiver/1.0` の固定値。
Tumblr が一貫した値を求めているので変動させない。

### エンドポイント

| | |
|---|---|
| `GET /api/tumblr/accounts` | 投稿先の一覧。**トークンは返さない** |
| `POST /api/tumblr/post` | `shortcode` / `account` / `caption` / `tags` / `state` / `indices` |

`indices` は添付する画像の添字をカンマ区切りで指定（省略時は全部）。
`state` は `published` / `draft` / `private` / `queue`。

**キャプションの初期値は投稿者のアカウントIDが入る**（編集も削除も可）。
カード側の `.tweet[data-owner]` から取る。共同投稿では相手側の名前になるが、
その投稿の持ち主としてはそれが正しいのでそのまま使う。
出典URLは別途 `source_url` として付くので、キャプションは表示テキストだけでよい。

**`create_photo_post` に渡すのは実ファイルの絶対パス。**
ig-ray の `local_path` は `/data/cache/AB/XXX_0.jpg` のようにサブディレクトリを
含むので、ファイル名だけ取り出すと開けなくなる。

### 制限

- レート制限は **1日250投稿・250画像アップロード**。手動運用では当たらない
- Consumer Secret を人に見せない（スクショにも写さない）。
  再生成はできるが**再認可が必要**になる

### シェアツール方式（旧）

`TUMBLR_CONSUMER_KEY` が未設定のときのフォールバック。
Tumblr の[シェアツール](https://help.tumblr.com/knowledge-base/share-button-documentation/)
に URL パラメータを渡してポップアップを開く。

**`content` の画像URLは Tumblr のサーバー側から取得される**ため、
トークン付きの一時公開URLを自前で配信する必要がある
（`PUBLIC_SHARE_BASE_URL` / cloudflared / `/share` 系エンドポイント）。
詳細は `deploy/README.md`。

**2026-08 時点、複数枚の自動添付が効かない。**
`content` にカンマ区切りで複数枚渡すのが仕様だが、現在の Tumblr は
複数枚だとフェッチ自体をせず空の投稿画面になる（1枚なら今も自動添付される。
tcpdump で実測確認）。配信側・Cloudflare・URLパラメータはすべて検証して無罪。

そのためモーダルで1枚を選ばせている。「全枚数を渡す（実験）」トグルで
本来の渡し方も試せるので、Tumblr が直ったかを手でURLを組まずに確認できる。

**API方式に移行すればこの制約は消える。** cloudflared・トンネル・
`/share` 系エンドポイント・`share_tokens` テーブルも不要になるので、
API方式が実際に通るのを確認したら旧方式は消してよい。

### 移植時に踏んだ罠

**Jinja のマクロは呼び出し元のコンテキストを引き継がない。**

```jinja
{% from '_macros.html' import post_card with context %}
```

`with context` が無いと `context_processor` で入れた `share_enabled` が
マクロ内から見えず、**ボタンが一切描画されない**。全テンプレートに必要。

さらに**この不具合はテストでも見逃しやすい。**
`'openTumblrShare' in html` はJS関数の定義にマッチして通ってしまう。
検証は必ず**実ボタンの数**を数えること:

```python
html.count('onclick="openTumblrShare')   # 共有可能な投稿の数だけあるはず
```

**`<script>` 内に Jinja タグを書くと `dev/lightbox_check.js` が落ちる**
（eval が `Unexpected token '%'`）。JSは常に定義し、出し分けはHTML側だけにする。

## 既知の未確認事項

- **`GraphVideo` の実データ検証がまだ。** 手元の監視対象に動画投稿が無いため。
  ロジックは `dev/video_check.py` で instaloader の実ノード形状を再現して検証済み。
  実データで確認したい場合は、アカウントを追加せずに1投稿だけ引ける:

  ```bash
  docker compose run --rm probe --shortcode 投稿のショートコード --dump-node
  ```

  ※ `from_shortcode` は `get_posts()` とは別エンドポイントなので、
  429 のリスクは別途ある。cron が回っていない時間帯に1回だけ叩くこと。

## 公開ファイルと作業メモの分離

この README には**どこでも再現できる情報だけ**を書く。
環境固有の情報（機材構成・IPアドレス・ホスト名・回線事業者・監視対象アカウントなど）は
リポジトリに入れず、`NOTES.md`（`.gitignore` 済み）に置く。

```bash
cp NOTES.example.md NOTES.md                          # 作業メモ
cp dev/privacy_words.example.txt dev/privacy_words.txt # 自環境の要注意語
```

IPアドレスやホスト名を書きたくなったら、それは `NOTES.md` 側の内容。
**迷ったら NOTES.md に置く**（後から公開側へ移すのは簡単だが、逆は履歴に残る）。

### 混入していないかの検査

```bash
python3 dev/privacy_check.py
```

git 追跡対象のファイル（＝実際に公開されるもの）だけを走査して、

- プライベートIP / ドキュメント用レンジ以外のグローバルIP
- SSH鍵ファイル名、`sessionid` や `csrftoken` の実値
- `dev/privacy_words.txt` に書いたサイト固有の語（ホスト名・回線事業者名など）

を検出する。`NOTES.md` と `privacy_words.txt` が `.gitignore` 済みであることも確認する。

`privacy_check.py` 自体には**汎用パターンしか書かない**。
サイト固有の語を直書きすると「検査ツールがそれを公開する」ことになるため
（実際に一度やった）、そこもテストで固定している。
