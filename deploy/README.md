# deploy/

外部公開まわりの設定例。**実ドメイン・実IPは置かない**（`NOTES.md` 側に書く）。

## cloudflared-config.example.yml

Tumblr 共有機能で `/share` と `/share-img` だけを外部公開するための ingress 例。

### ハマりどころ

- **path は1ルール1パターン。** `^/share(-img)?/.*` のようにまとめると
  マッチせず全部404になる
- **`--config` は `tunnel` の直後に置く。**
  サブコマンドの後ろだと "flag provided but not defined" になる

  ```bash
  cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
  ```

- **systemd 化すると設定は `/etc/cloudflared/`。** `~/.cloudflared/` のままだと
  root から読めず起動に失敗する。認証情報の json も一緒に移し、
  `credentials-file` のパスも書き換えること
- `cloudflared tunnel run` はフォアグラウンド常駐。
  ログが流れて止まって見えるのは正常

### 動作確認

```bash
# ローカル直（Tunnel を通さない）
curl -s http://localhost:8079/share/$TOKEN | grep og:image
curl -so /dev/null -w "%{http_code}\n" http://localhost:8079/share-img/$TOKEN/0

# Tunnel 経由
curl -s https://<公開ホスト>/share/$TOKEN | grep og:image
curl -so /dev/null -w "%{http_code}\n" https://<公開ホスト>/share-img/$TOKEN/0

# 本体が漏れていないこと（すべて 404 になるはず）
curl -so /dev/null -w "%{http_code}\n" https://<公開ホスト>/
curl -so /dev/null -w "%{http_code}\n" https://<公開ホスト>/backup
```

ローカルが 200 で Tunnel 経由が 404 なら、原因は ingress の path 指定。

path 指定で消耗するなら全通しにしてもよい。
`IG_RAY_PUBLIC_SHARE_HOST` を設定しておけば、アプリ側の
`before_request` ガードが `/share` 系以外を 404 にするので本体は守られる。
