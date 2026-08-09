#!/usr/bin/env python3
"""
insta-ray / app/make_session.py

対話ログインしてセッションファイルを /data/sessions/ に作る。

これまで docker run の長い一行野郎でやっていた作業をスクリプト化したもの。

    docker compose run --rm login

パスワードは受け取ってそのまま instaloader に渡すだけで、
ファイルにも環境変数にも残さない。
"""

import getpass
import os
import sys

import config

SESSION_DIR = config.env("SESSIONS", "/data/sessions")


def main():
    import instaloader
    from instaloader.exceptions import (
        BadCredentialsException,
        TwoFactorAuthRequiredException,
        ConnectionException,
        InvalidArgumentException,
    )

    user = config.env("LOGIN_USER", "").strip()
    if not user:
        user = input("Instagram ユーザー名: ").strip()
    if not user:
        print("ユーザー名が空です。")
        return 2

    os.makedirs(SESSION_DIR, exist_ok=True)
    session_file = os.path.join(SESSION_DIR, f"session-{user}")

    if os.path.exists(session_file):
        ans = input(f"{session_file} は既にあります。上書きしますか [y/N]: ")
        if ans.strip().lower() not in ("y", "yes"):
            print("中止しました。")
            return 0

    L = instaloader.Instaloader(quiet=True, sleep=True,
                               download_pictures=False, download_videos=False,
                               download_video_thumbnails=False,
                               save_metadata=False)

    proxy = config.env("PROXY", "")
    if proxy:
        L.context._session.proxies = {"http": proxy, "https": proxy}
        print(f"プロキシ経由: {proxy.split('@')[-1]}")

    passwd = getpass.getpass(f"{user} のパスワード（表示されません）: ")

    try:
        L.login(user, passwd)
    except TwoFactorAuthRequiredException:
        code = input("2FA コード: ").strip()
        try:
            L.two_factor_login(code)
        except Exception as e:
            print(f"2FA 失敗: {e}")
            return 1
    except BadCredentialsException:
        print("ユーザー名かパスワードが違います。")
        return 1
    except (ConnectionException, InvalidArgumentException) as e:
        print(f"ログイン失敗: {e}")
        print("チャレンジ要求が出ている場合は、先にブラウザでログインして消化してください。")
        return 1
    finally:
        del passwd

    # 作った直後に有効性を確かめる。ここで通らないセッションは保存しても無駄。
    who = None
    try:
        who = L.test_login()
    except Exception as e:
        print(f"検証中にエラー: {type(e).__name__}: {e}")

    if not who:
        print("ログインはできましたが、検証クエリが通りませんでした。")
        print("アカウントがソフトブロック中の可能性があります。")
        print("セッション自体は保存しますが、しばらく待ってから使ってください。")
    else:
        print(f"ログイン確認: {who}")

    L.save_session_to_file(session_file)
    os.chmod(session_file, 0o600)
    print(f"保存しました: {session_file}")
    print("（このファイルはパスワード同等の価値があります。取り扱い注意）")
    return 0 if who else 1


if __name__ == "__main__":
    sys.exit(main())
