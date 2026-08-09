# IG-Ray 作業メモ（雛形）

`NOTES.example.md` を `NOTES.md` にコピーして使う。
`NOTES.md` は `.gitignore` 済みなので公開されない。

```bash
cp NOTES.example.md NOTES.md
```

**方針**: README には「どこでも再現できる情報」だけを書く。
IPアドレス・ホスト名・アカウント名を書きたくなったら、それはこちら側の内容。
迷ったらこちらに置く（後から公開側へ移すのは簡単だが、逆は履歴に残る）。

---

## 母艦

| | |
|---|---|
| ホスト名 | |
| 種別 | 自宅サーバ / VPS / その他 |
| ディレクトリ | |
| セッション実体 | `<ディレクトリ>/data/sessions/session-<user>` |
| 表示層 | `http://localhost:8079` |

## 出口の分離

使っていれば書く。使っていなければこの節ごと消してよい。

| | |
|---|---|
| プロキシのホスト | |
| 内部LAN側アドレス（入口） | |
| 分離先回線側アドレス（出口） | |
| プロキシソフト / Listen | |
| 分離先のグローバルIP | |

- コンテナ内の IPv6 を無効化したか:
- `docker compose run --rm ipcheck` の実測結果:

## Git 運用

- リモート:
- SSH鍵 / `~/.ssh/config` の設定:

## 運用状況

- 巡回間隔・対象アカウント数:
- 状態確認:

```bash
sqlite3 <ディレクトリ>/data/ig_ray.db \
  "SELECT status, COUNT(*) FROM scrape_log WHERE ended_at > datetime('now','-1 day') GROUP BY status;"
```

## 取得用アカウント

- 本アカウント / 予備:
- 注意点:

## 監視対象の台帳

- どこで管理しているか（Notion / スプレッドシート等）:
- 本体と自動同期するか:

## 未着手・保留

-
