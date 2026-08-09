"""
insta-ray / dev/diag_session.py

429 の原因切り分け。

「直近1リクエストなのに429」は、レート超過ではなく
**ログインが効いていない**ときの典型的な症状。
instaloader の load_session_from_file() はファイルを読むだけで、
そのセッションがまだ有効かは検証しない。死んだセッションを読んでも
例外は出ず、そのまま匿名アクセス扱いで進んでしまう。

このスクリプトは段階的に確かめる:
  [A] セッションファイルが存在し、pickle として読めるか
  [B] クッキーに sessionid があるか（＝ログイン済みの形をしているか）
  [C] test_login() が username を返すか（＝サーバ側でまだ有効か）
  [D] 実際にプロフィールを1件引けるか

    python dev/diag_session.py [対象アカウント名]
"""

import os
import pickle
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

SESSION_DIR = os.environ.get("IG_RAY_SESSIONS", "/data/sessions")
LOGIN_USER = os.environ.get("IG_RAY_LOGIN_USER", "")


def hr(title):
    print(f"\n{'='*56}\n{title}\n{'='*56}")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None

    if not LOGIN_USER:
        print("IG_RAY_LOGIN_USER が未設定です。")
        return 2

    session_file = os.path.join(SESSION_DIR, f"session-{LOGIN_USER}")

    # ---------------------------------------------------------------- [A]
    hr("[A] セッションファイル")
    print(f"path: {session_file}")
    if not os.path.exists(session_file):
        print("NG: ファイルが存在しません。")
        return 1
    size = os.path.getsize(session_file)
    print(f"OK: 存在します（{size} bytes）")
    if size < 100:
        print("   ※ 異常に小さい。作り直したほうがよいかも。")

    # ---------------------------------------------------------------- [B]
    hr("[B] クッキーの中身")
    try:
        with open(session_file, "rb") as f:
            data = pickle.load(f)
    except Exception as e:
        print(f"NG: pickle として読めません: {e}")
        return 1

    if isinstance(data, dict):
        keys = sorted(data.keys())
    else:
        keys = sorted(getattr(data, "keys", lambda: [])())
    print(f"クッキー名: {keys}")

    has_sessionid = "sessionid" in keys
    print(f"sessionid の有無: {'OK あり' if has_sessionid else 'NG なし'}")
    if has_sessionid:
        v = data["sessionid"] if isinstance(data, dict) else None
        if v:
            print(f"sessionid 長さ: {len(str(v))} 文字")
            if len(str(v)) < 20:
                print("   ※ 短すぎる。空セッションの可能性。")
    else:
        print("   → ログインしていない状態のセッションです。作り直してください。")
        return 1

    # ---------------------------------------------------------------- [C]
    hr("[C] サーバ側でセッションが有効か（test_login）")
    import instaloader

    # 診断ツールが 666 秒眠ったら本末転倒なので、本体と同じ fail-fast を刺す
    import ig_scraper as igs

    L = instaloader.Instaloader(quiet=True, sleep=True,
                                download_pictures=False, download_videos=False,
                                download_video_thumbnails=False, save_metadata=False,
                                max_connection_attempts=1,
                                rate_controller=igs.make_rate_controller(max_sleep=30))

    proxy = os.environ.get("IG_RAY_PROXY", "")
    if proxy:
        L.context._session.proxies = {"http": proxy, "https": proxy}
        print(f"プロキシ経由: {proxy.split('@')[-1]}")
    try:
        L.load_session_from_file(LOGIN_USER, session_file)
    except Exception as e:
        print(f"NG: load_session_from_file 失敗: {type(e).__name__}: {e}")
        return 1
    print("load_session_from_file: OK（※これはファイルを読んだだけ）")

    try:
        who = L.test_login()
    except igs.RateLimitAbort as e:
        print(f"NG: レート制限で中止 — {e}")
        who = None
    except Exception as e:
        print(f"test_login が例外: {type(e).__name__}: {e}")
        who = None

    if who:
        print(f"OK: サーバは '{who}' として認識しています。セッションは有効。")
    else:
        print("NG: test_login が None を返しました。")
        print("   考えられる原因:")
        print("     - セッション失効（作り直しで直る）")
        print("     - アカウントがチャレンジ要求中（ブラウザでログインして本人確認）")
        print("     - このIPからのアクセスがブロックされている")
        print("   → ブラウザで同アカウントにログインし、警告が出ないか確認してください。")

    print(f"context.is_logged_in: {L.context.is_logged_in}")

    # ---------------------------------------------------------------- [D]
    if not target:
        print("\n（対象アカウント名を引数で渡すと [D] も実行します）")
        return 0 if who else 1

    hr(f"[D] 実際にプロフィールを引く: {target}")
    from instaloader.exceptions import (
        TooManyRequestsException, ProfileNotExistsException,
        LoginRequiredException, ConnectionException,
    )
    try:
        p = instaloader.Profile.from_username(L.context, target)
        print(f"OK: {p.username} / followers={p.followers} / posts={p.mediacount}")
        print("→ スクレイパ本体も通るはずです。")
        return 0
    except igs.RateLimitAbort as e:
        print(f"NG: レート制限で中止 — {e}")
        print("   長時間待機を要求されました（＝サーバに強く拒否されている）。")
    except TooManyRequestsException as e:
        print(f"NG: 429 — {e}")
        print("   [C] が OK なのに 429 なら、IP が弾かれている可能性が高いです。")
        print("   別のネットワーク（自宅回線・テザリング）から試して切り分けてください。")
    except LoginRequiredException as e:
        print(f"NG: ログインが必要 — {e}")
        print("   セッションが実質効いていません。作り直してください。")
    except ProfileNotExistsException:
        print("NG: そのアカウントは存在しません（名前を確認）")
    except ConnectionException as e:
        print(f"NG: 接続エラー — {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
