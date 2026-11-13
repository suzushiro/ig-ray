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
  [C] そのセッションで自分の投稿を1ページ引けるか（＝巡回と同じ経路）
  [D] 対象アカウントの投稿を1ページ引けるか

**test_login() と Profile.from_username() は使わない。**
  test_login() が叩く旧式 query_hash は 2026-10 に廃止され、有効なセッションでも
  必ず失敗する（instaloader 4.16 でも未修正、上流 issue #2792）。
  from_username() は web_profile_info を叩き、ここは恒常的に 429。
  どちらも「セッションが死んでいる」と誤診させるので、巡回が実際に使う
  get_posts() の1ページ目だけで判定する。

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
    hr(f"[C] 自分の投稿を1ページ引けるか（{LOGIN_USER}・巡回と同じ経路）")
    import instaloader

    # 診断ツールが長時間眠ったら本末転倒なので、本体と同じ fail-fast を刺す
    import ig_scraper as igs

    print(f"instaloader: {instaloader.__version__}")
    L = instaloader.Instaloader(quiet=True, sleep=True,
                                download_pictures=False, download_videos=False,
                                download_video_thumbnails=False, save_metadata=False,
                                max_connection_attempts=1,
                                rate_controller=igs.make_rate_controller(max_sleep=30))
    try:
        L.load_session_from_file(LOGIN_USER, session_file)
    except Exception as e:
        print(f"NG: load_session_from_file 失敗: {type(e).__name__}: {e}")
        return 1
    # **プロキシは load_session の後に入れる**（_session が作り直されるため）
    proxy = os.environ.get("IG_RAY_PROXY", "")
    if proxy:
        L.context._session.proxies = {"http": proxy, "https": proxy}
        print(f"プロキシ経由: {proxy.split('@')[-1]}")

    ok, err = igs.verify_session(L, LOGIN_USER)
    if ok:
        print("OK: 投稿を引けました。セッションは有効です。")
    else:
        print(f"NG: {err}")
        if igs.is_session_failure(err):
            print("   セッション起因の失敗です。")
            print("   ブラウザで同じアカウントにログインし、警告が出ていないか確認して、")
            print("   クッキーを取り直してください（import-cookies）。")
        else:
            print("   セッション以外の失敗です。エラー文をそのまま確認してください。")

    # ---------------------------------------------------------------- [D]
    if not target:
        print("\n（対象アカウント名を引数で渡すと [D] も実行します）")
        return 0 if ok else 1

    hr(f"[D] 対象の投稿を1ページ引く: {target}")
    try:
        p = igs.minimal_profile(L.context, target)
        posts = []
        for post in p.get_posts():
            posts.append(post)
            if len(posts) >= 3:
                break
        print(f"OK: {len(posts)} 件取れました")
        for post in posts:
            print(f"   {post.shortcode} {post.typename} {post.date_utc:%Y-%m-%d}")
        print("→ スクレイパ本体も通るはずです。")
        return 0
    except igs.RateLimitAbort as e:
        print(f"NG: レート制限で中止 — {e}")
    except Exception as e:
        print(f"NG: {type(e).__name__}: {e}")
        if igs.is_session_failure(str(e)):
            print("   セッション起因の失敗です（[C] も確認してください）。")
    return 1

if __name__ == "__main__":
    sys.exit(main())
