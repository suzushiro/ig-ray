#!/usr/bin/env python3
"""
insta-ray / app/import_cookies.py

ブラウザのクッキーからセッションファイルを作る。

なぜこれが要るか:
  instaloader 自身の login() で作ったセッションを Instagram が拒否する一方、
  ブラウザのセッションは普通に通る、という状況がある。
  サーバ側にブラウザが無いので `--load-cookies` は使えないため、
  開発者ツールから値を手で持ってきて組み立てる。
  X-Ray で auth_token / ct0 を cookies.txt に貼っているのと同じ発想。

取り方（ブラウザで instagram.com にログインした状態で）:
  Firefox : F12 → ストレージ → Cookie → https://www.instagram.com
  Chrome  : F12 → Application → Storage → Cookies → https://www.instagram.com

  必要なのは sessionid（必須）と csrftoken / ds_user_id / mid / ig_did。
  sessionid だけでも動くことが多いが、揃っているほど自然に見える。

使い方:
  # 対話で貼り付ける
  docker compose run --rm import-cookies

  # 値を直接渡す
  docker compose run --rm import-cookies --sessionid "..." --csrftoken "..."

  # ブラウザのある端末で実行する場合（要 browser_cookie3）
  python import_cookies.py --from-browser firefox

※ sessionid はパスワード同等です。履歴に残したくない場合は対話モードを使ってください。
"""

import argparse
import getpass
import os
import sys

import config

SESSION_DIR = config.env("SESSIONS", "/data/sessions")

# sessionid は "<ds_user_id>%3A<...>" 形式。ここから user id を拾える。
COOKIE_FIELDS = ["sessionid", "csrftoken", "ds_user_id", "mid", "ig_did"]


def prompt_cookies():
    print("ブラウザの開発者ツールから値をコピーして貼り付けてください。")
    print("（sessionid 以外は空Enterでスキップ可）\n")
    cookies = {}

    # sessionid は伏せて入力
    v = getpass.getpass("sessionid（表示されません・必須）: ").strip()
    if not v:
        print("sessionid は必須です。")
        return None
    cookies["sessionid"] = v

    for name in COOKIE_FIELDS[1:]:
        v = input(f"{name}: ").strip()
        if v:
            cookies[name] = v
    return cookies


def cookies_from_browser(browser):
    try:
        import browser_cookie3  # noqa: F401
    except ImportError:
        print("browser_cookie3 が必要です: pip install browser_cookie3", file=sys.stderr)
        return None
    from instaloader.__main__ import get_cookies_from_instagram
    from instaloader.exceptions import InvalidArgumentException, LoginException
    try:
        return get_cookies_from_instagram("instagram", browser, None)
    except (InvalidArgumentException, LoginException) as e:
        print(f"取得失敗: {e}", file=sys.stderr)
        return None


def guess_username_from_cookies(cookies):
    """sessionid の先頭は ds_user_id。ユーザー名そのものは入っていない。"""
    uid = cookies.get("ds_user_id")
    if not uid and "sessionid" in cookies:
        head = cookies["sessionid"].split("%3A")[0].split(":")[0]
        if head.isdigit():
            uid = head
    return uid


def main():
    ap = argparse.ArgumentParser(
        description="ブラウザのクッキーから insta-ray 用セッションを作る")
    ap.add_argument("--user", default=config.env("LOGIN_USER", ""),
                    help="IGアカウント名（保存ファイル名に使う）")
    ap.add_argument("--from-browser", default=None,
                    help="firefox / chrome / chromium / edge / brave / safari など")
    for f in COOKIE_FIELDS:
        ap.add_argument(f"--{f.replace('_','-')}", dest=f, default=None)
    ap.add_argument("--sessions-dir", default=SESSION_DIR)
    ap.add_argument("--force", action="store_true", help="既存ファイルを確認なしで上書き")
    args = ap.parse_args()

    # --- クッキーを集める ---
    if args.from_browser:
        cookies = cookies_from_browser(args.from_browser)
    elif any(getattr(args, f) for f in COOKIE_FIELDS):
        cookies = {f: getattr(args, f) for f in COOKIE_FIELDS if getattr(args, f)}
    else:
        cookies = prompt_cookies()

    if not cookies:
        return 1
    if "sessionid" not in cookies:
        print("sessionid がありません。", file=sys.stderr)
        return 1

    print(f"\n取り込むクッキー: {sorted(cookies.keys())}")
    uid = guess_username_from_cookies(cookies)
    if uid:
        print(f"ds_user_id: {uid}")

    # --- セッションに載せて検証 ---
    import instaloader
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ig_scraper as igs

    L = instaloader.Instaloader(
        quiet=True, sleep=True,
        download_pictures=False, download_videos=False,
        download_video_thumbnails=False, save_metadata=False,
        max_connection_attempts=1,
        rate_controller=igs.make_rate_controller(max_sleep=30),
    )

    proxy = config.env("PROXY", "")
    if proxy:
        L.context._session.proxies = {"http": proxy, "https": proxy}
        print(f"プロキシ経由: {proxy.split('@')[-1]}")

    L.context.update_cookies(cookies)

    print("\n検証中...")
    who = None
    try:
        who = L.test_login()
    except igs.RateLimitAbort as e:
        print(f"レート制限で中止: {e}")
    except Exception as e:
        print(f"検証エラー: {type(e).__name__}: {e}")

    if not who:
        print("\nNG: このクッキーでもログインとして認められませんでした。")
        print("  - ブラウザで instagram.com にログイン済みか確認")
        print("  - sessionid をコピーし損ねていないか（長い文字列です）")
        print("  - ブラウザ側でログアウトすると sessionid は即座に無効になります")
        return 1

    print(f"OK: '{who}' としてログインが認められました。")

    user = args.user or who
    L.context.username = who

    os.makedirs(args.sessions_dir, exist_ok=True)
    path = os.path.join(args.sessions_dir, f"session-{user}")

    if os.path.exists(path) and not args.force:
        ans = input(f"{path} は既にあります。上書きしますか [y/N]: ")
        if ans.strip().lower() not in ("y", "yes"):
            print("中止しました。")
            return 0

    L.save_session_to_file(path)
    os.chmod(path, 0o600)
    print(f"保存しました: {path}")
    if user != who:
        print(f"※ ファイル名は '{user}' ですが、中身は '{who}' のセッションです。")
        print(f"   IG_RAY_LOGIN_USER は '{user}' のままで一致します。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
