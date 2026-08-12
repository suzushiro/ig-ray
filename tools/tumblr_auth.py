#!/usr/bin/env python3
"""
Tumblr のアクセストークンを取得して data/tumblr_accounts.json に保存する。

初回だけ実行すればよい（OAuth1のトークンは失効しない）。
アカウントごとに1回ずつ実行する。メイン用・サブ用で2回。

前提:
  https://www.tumblr.com/oauth/apps でアプリを登録し、
  コールバックURLに http://localhost:8765/callback を設定しておくこと。

使い方:
    export TUMBLR_CONSUMER_KEY=xxxx
    export TUMBLR_CONSUMER_SECRET=yyyy

    # メインアカウントで認可（ブラウザが開く）
    python tumblr_auth.py --label main --out ../data/tumblr_accounts.json

    # サブアカウントで認可（別アカウントでログインしてから許可する）
    python tumblr_auth.py --label sub --out ../data/tumblr_accounts.json

    # 登録済みの確認
    python tumblr_auth.py --list --out ../data/tumblr_accounts.json
"""
import argparse
import json
import os
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

REQUEST_TOKEN_URL = "https://www.tumblr.com/oauth/request_token"
AUTHORIZE_URL = "https://www.tumblr.com/oauth/authorize"
ACCESS_TOKEN_URL = "https://www.tumblr.com/oauth/access_token"
API_BASE = "https://api.tumblr.com/v2"
USER_AGENT = "ig-ray-archiver/1.0"

_received = {}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        _received.update({k: v[0] for k, v in q.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "oauth_verifier" in _received
        body = ("<h2>認可できました。ターミナルに戻ってください。</h2>" if ok
                else "<h2>認可に失敗しました。ターミナルを確認してください。</h2>")
        self.wfile.write(f"<html><body style='font-family:sans-serif'>{body}</body></html>"
                         .encode("utf-8"))

    def log_message(self, *a):
        pass    # アクセスログを出さない


def load_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("accounts", []) if isinstance(data, dict) else (data or [])
    except (OSError, ValueError):
        return []


def save_file(path, accounts):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"accounts": accounts,
                   "updated_at": datetime.now(timezone.utc).isoformat()},
                  f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tumblrのアクセストークンを取得する")
    ap.add_argument("--label", help="このアカウントの表示名（例: main / sub）")
    ap.add_argument("--blog", help="投稿先ブログ名。省略時は認可後に選択")
    ap.add_argument("--out", default="../data/tumblr_accounts.json",
                    help="保存先 (既定: ../data/tumblr_accounts.json)。"
                         "コンテナから見える data/ に置くこと")
    ap.add_argument("--port", type=int, default=8765,
                    help="コールバック待受ポート（アプリ登録時のURLと合わせる）")
    ap.add_argument("--list", action="store_true", help="登録済みを表示して終了")
    ap.add_argument("--remove", metavar="LABEL", help="登録済みを削除")
    args = ap.parse_args(argv)

    accounts = load_file(args.out)

    if args.list:
        if not accounts:
            print("登録なし")
        for a in accounts:
            print(f"  {a.get('label'):12} → {a.get('blog')}")
        return 0

    if args.remove:
        before = len(accounts)
        accounts = [a for a in accounts if a.get("label") != args.remove]
        if len(accounts) == before:
            print(f"[!] '{args.remove}' は見つかりません")
            return 1
        save_file(args.out, accounts)
        print(f"[+] '{args.remove}' を削除しました")
        return 0

    if not args.label:
        print("[!] --label が必要です（例: --label main）")
        return 2

    key = os.environ.get("TUMBLR_CONSUMER_KEY", "").strip()
    secret = os.environ.get("TUMBLR_CONSUMER_SECRET", "").strip()
    if not key or not secret:
        print("[!] TUMBLR_CONSUMER_KEY / TUMBLR_CONSUMER_SECRET を環境変数に設定してください")
        return 2

    try:
        from requests_oauthlib import OAuth1Session
    except ImportError:
        print("[!] requests-oauthlib が未インストールです\n"
              "    pip install requests requests-oauthlib")
        return 3

    callback = f"http://localhost:{args.port}/callback"

    # 1. 一時トークン
    print("[*] 一時トークンを取得中...")
    oauth = OAuth1Session(key, client_secret=secret, callback_uri=callback)
    try:
        fetch = oauth.fetch_request_token(REQUEST_TOKEN_URL)
    except Exception as e:
        print(f"[!] 失敗: {e}")
        print("    コールバックURLがアプリ登録の値と一致しているか確認してください:")
        print(f"    {callback}")
        return 1

    # 2. ブラウザで認可
    auth_url = oauth.authorization_url(AUTHORIZE_URL)
    print(f"[*] ブラウザで認可してください（{args.label} で使うアカウントでログインすること）")
    print(f"    {auth_url}")

    _received.clear()
    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print("[*] 認可待ち... (Ctrl+C で中断)")
    try:
        import time
        while "oauth_verifier" not in _received:
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n[!] 中断しました")
        return 1
    finally:
        server.shutdown()

    verifier = _received.get("oauth_verifier")
    print("[+] 認可を受け取りました")

    # 3. アクセストークンに交換
    oauth = OAuth1Session(
        key, client_secret=secret,
        resource_owner_key=fetch.get("oauth_token"),
        resource_owner_secret=fetch.get("oauth_token_secret"),
        verifier=verifier)
    try:
        tok = oauth.fetch_access_token(ACCESS_TOKEN_URL)
    except Exception as e:
        print(f"[!] アクセストークンの取得に失敗: {e}")
        return 1

    token = tok.get("oauth_token")
    token_secret = tok.get("oauth_token_secret")

    # 4. ブログ一覧を取得して投稿先を決める
    s = OAuth1Session(key, client_secret=secret,
                      resource_owner_key=token, resource_owner_secret=token_secret)
    blog = args.blog
    try:
        r = s.get(f"{API_BASE}/user/info", headers={"User-Agent": USER_AGENT}, timeout=20)
        blogs = [b.get("name") for b in
                 r.json().get("response", {}).get("user", {}).get("blogs", [])]
    except Exception as e:
        blogs = []
        print(f"[!] ブログ一覧の取得に失敗（続行します）: {e}")

    if not blog:
        if len(blogs) == 1:
            blog = blogs[0]
        elif blogs:
            print("\nこのアカウントのブログ:")
            for i, b in enumerate(blogs, 1):
                print(f"  {i}. {b}")
            while True:
                sel = input("投稿先の番号: ").strip()
                if sel.isdigit() and 1 <= int(sel) <= len(blogs):
                    blog = blogs[int(sel) - 1]
                    break
        else:
            blog = input("投稿先のブログ名: ").strip()

    if not blog:
        print("[!] 投稿先ブログが決まりませんでした")
        return 1

    accounts = [a for a in accounts if a.get("label") != args.label]
    accounts.append({"label": args.label, "blog": blog,
                     "token": token, "secret": token_secret})
    save_file(args.out, accounts)

    print(f"\n[+] 保存しました: {args.out}")
    print(f"    {args.label} → {blog}")
    print(f"    登録済み: {', '.join(a['label'] for a in accounts)}")
    print("\nこのファイルにはトークンが入っています。gitに載せないこと。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
