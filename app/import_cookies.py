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


def _install_cookies(L, cookies, username):
    """
    instaloader の load_session() と同じ形でセッションを組む。

    update_cookies() はクッキーを足すだけでヘッダを直さない。
    ログイン済みのヘッダ一式が無いと Instagram は API 要求と見なさない。
    csrftoken が無くても動くよう、ヘッダ側だけ外して続行する。
    """
    cj = dict(cookies)
    has_csrf = bool(cj.get("csrftoken"))
    cj.setdefault("csrftoken", "")      # load_session が参照するので必ず入れる
    L.context.load_session(username, cj)
    if not has_csrf:
        L.context._session.headers.pop("X-CSRFToken", None)
        print("  ⚠ csrftoken が無いので X-CSRFToken ヘッダを付けられません。"
              "弾かれる場合は csrftoken も貼ってください。")


def _explain_json(body):
    """
    Instagram の JSON エラーを日本語で説明する。

    **同じ「ログインできない」でも中身で打ち手が正反対になる。**
      require_login    … セッションが通っていない → 取り直す
      feedback_required … セッションは通っている・行為がブロック → **待つ**
      challenge_required … 本人確認待ち → ブラウザで消化する
    ここを読み違えると、待つべきときに叩き続けて悪化させる。
    """
    import json as _json
    try:
        d = _json.loads(body)
    except Exception:
        return False
    if not isinstance(d, dict):
        return False
    msg = str(d.get("message", ""))

    if d.get("spam") or msg == "feedback_required":
        print("      **アクションブロック（feedback_required / spam）です。**")
        print("      セッションは通っています。クッキーの問題ではありません。")
        print("      自動化とみなされて一時的に行為を制限されている状態です。")
        print("      → **叩かないこと。** 試すほど延びます。")
        print("         数時間〜数日で自然に解けます。24時間以上空けてから1回だけ再試行を。")
        if d.get("feedback_title"):
            print(f"      （Instagram の文言: {d['feedback_title']}）")
        return True
    elif d.get("require_login") or msg == "login_required":
        print("      **require_login です。**"
              "このクッキーはログインとして認められていません。")
        print("      → ブラウザでログインし直してクッキーを取り直してください。")
        return "recopy"
    elif "challenge" in msg or d.get("challenge"):
        print("      **チャレンジ要求です。** 本人確認が保留になっています。")
        print("      → ブラウザで同じアカウントにログインして消化してください。")
        return True
    elif "checkpoint" in msg:
        print("      **チェックポイント要求です。** アカウント確認が保留です。")
        # **checkpoint_url が最重要。** どの手続きが要るかはこのURLで決まる。
        # /accounts/suspended/ ならアカウント停止で、本人確認とは話が別。
        url = d.get("checkpoint_url") or ""
        if url:
            print(f"      checkpoint_url: {url}")
            if "suspended" in url:
                print("      → **アカウントが停止されています。**"
                      "セッションやクッキーの問題ではありません。")
                print("         ブラウザでログインした状態で上のURLを開き、"
                      "停止理由と異議申し立ての導線を確認してください。")
                print("         SMS の認証コードを待っても解決しません。")
            elif "disabled" in url:
                print("      → アカウントが無効化されています。")
            else:
                print("      → ブラウザでログインした状態で上のURLを開き、"
                      "手続きを済ませてください。")
        if d.get("lock"):
            print("      lock: true（恒久的なロックの可能性）")
        return True
    elif msg:
        print(f"      message: {msg}")
    return False


def _explain_error(err):
    """
    取得エラーの文から Instagram の message を拾い、_explain_json に回す。

    以前は旧エンドポイント（test_login と同じ query_hash）を別に叩いて
    生の応答を見ていたが、そこは廃止されていて**常に feedback_required を返す**。
    それを見て「アカウントが制限されている」と誤認していたので、
    実際の取得エラーそのものを読む。
    """
    import json as _json
    import re as _re
    text = err or ""
    m = _re.search(r'message "([^"]+)"', text)
    d = {"message": m.group(1)} if m else {}
    if "feedback_required" in text:
        d.setdefault("message", "feedback_required")
        d["spam"] = True
    if "require_login" in text or "login_required" in text or \
            "LoginRequiredException" in text:
        d["require_login"] = True
    url = _re.search(r'(https://www\.instagram\.com/(?:accounts|challenge)/[^\s"\']*)',
                     text)
    if url:
        d["checkpoint_url"] = url.group(1)
    if not d:
        if "Please wait a few minutes" in text or "401" in text:
            print("判定: 401。セッションがログインとして通っていない可能性。")
            return "recopy"
        print("判定: 上のエラー文を確認してください。")
        return False
    print("判定:")
    return _explain_json(_json.dumps(d))


def _check_sessionid_shape(cookies):
    """
    sessionid の形を確かめる。

    **sessionid は `<ds_user_id>%3A<トークン>…` という形**で、
    先頭にユーザーIDが入っている。ここが ds_user_id と食い違っていたら、
    別のクッキーを貼ったか、コピーを取り損ねている。
    貼り間違いは「ログインとして認められない」という同じ症状になるので、
    入口で弾いておかないと原因の切り分けに何日もかかる。
    """
    sid = (cookies.get("sessionid") or "").strip()
    uid = (cookies.get("ds_user_id") or "").strip()
    if not sid:
        return
    print(f"sessionid 長さ: {len(sid)} 文字")
    if len(sid) < 30:
        print("  ⚠ 短すぎます。コピーを取り損ねている可能性が高いです。")
    if not uid:
        return
    # URLエンコードされていることも、生のコロンのこともある
    if sid.startswith(f"{uid}%3A") or sid.startswith(f"{uid}:"):
        print(f"  OK: 先頭が ds_user_id（{uid}）と一致しています。")
    else:
        head = sid.split("%3A")[0].split(":")[0][:24]
        print(f"  ⚠ **先頭が ds_user_id と一致しません**"
              f"（sessionid の先頭は '{head}'、ds_user_id は '{uid}'）。")
        print("     別のクッキーを貼ったか、コピーを取り損ねている可能性があります。")
        print("     開発者ツールで sessionid の値を選び直してください。")


def _explain_json(body):
    """
    Instagram の JSON エラーを日本語で説明する。

    **同じ「ログインできない」でも中身で打ち手が正反対になる。**
      require_login    … セッションが通っていない → 取り直す
      feedback_required … セッションは通っている・行為がブロック → **待つ**
      challenge_required … 本人確認待ち → ブラウザで消化する
    ここを読み違えると、待つべきときに叩き続けて悪化させる。
    """
    import json as _json
    try:
        d = _json.loads(body)
    except Exception:
        return False
    if not isinstance(d, dict):
        return False
    msg = str(d.get("message", ""))

    if d.get("spam") or msg == "feedback_required":
        print("      **アクションブロック（feedback_required / spam）です。**")
        print("      セッションは通っています。クッキーの問題ではありません。")
        print("      自動化とみなされて一時的に行為を制限されている状態です。")
        print("      → **叩かないこと。** 試すほど延びます。")
        print("         数時間〜数日で自然に解けます。24時間以上空けてから1回だけ再試行を。")
        if d.get("feedback_title"):
            print(f"      （Instagram の文言: {d['feedback_title']}）")
        return True
    elif d.get("require_login") or msg == "login_required":
        print("      **require_login です。**"
              "このクッキーはログインとして認められていません。")
        print("      → ブラウザでログインし直してクッキーを取り直してください。")
        return "recopy"
    elif "challenge" in msg or d.get("challenge"):
        print("      **チャレンジ要求です。** 本人確認が保留になっています。")
        print("      → ブラウザで同じアカウントにログインして消化してください。")
        return True
    elif "checkpoint" in msg:
        print("      **チェックポイント要求です。** アカウント確認が保留です。")
        # **checkpoint_url が最重要。** どの手続きが要るかはこのURLで決まる。
        # /accounts/suspended/ ならアカウント停止で、本人確認とは話が別。
        url = d.get("checkpoint_url") or ""
        if url:
            print(f"      checkpoint_url: {url}")
            if "suspended" in url:
                print("      → **アカウントが停止されています。**"
                      "セッションやクッキーの問題ではありません。")
                print("         ブラウザでログインした状態で上のURLを開き、"
                      "停止理由と異議申し立ての導線を確認してください。")
                print("         SMS の認証コードを待っても解決しません。")
            elif "disabled" in url:
                print("      → アカウントが無効化されています。")
            else:
                print("      → ブラウザでログインした状態で上のURLを開き、"
                      "手続きを済ませてください。")
        if d.get("lock"):
            print("      lock: true（恒久的なロックの可能性）")
        return True
    elif msg:
        print(f"      message: {msg}")
    return False


def _explain_error(err):
    """
    取得エラーの文から Instagram の message を拾い、_explain_json に回す。

    以前は旧エンドポイント（test_login と同じ query_hash）を別に叩いて
    生の応答を見ていたが、そこは廃止されていて**常に feedback_required を返す**。
    それを見て「アカウントが制限されている」と誤認していたので、
    実際の取得エラーそのものを読む。
    """
    import json as _json
    import re as _re
    text = err or ""
    m = _re.search(r'message "([^"]+)"', text)
    d = {"message": m.group(1)} if m else {}
    if "feedback_required" in text:
        d.setdefault("message", "feedback_required")
        d["spam"] = True
    if "require_login" in text or "login_required" in text or \
            "LoginRequiredException" in text:
        d["require_login"] = True
    url = _re.search(r'(https://www\.instagram\.com/(?:accounts|challenge)/[^\s"\']*)',
                     text)
    if url:
        d["checkpoint_url"] = url.group(1)
    if not d:
        if "Please wait a few minutes" in text or "401" in text:
            print("判定: 401。セッションがログインとして通っていない可能性。")
            return "recopy"
        print("判定: 上のエラー文を確認してください。")
        return False
    print("判定:")
    return _explain_json(_json.dumps(d))


def _check_sessionid_shape(cookies):
    """
    sessionid の形を確かめる。

    **sessionid は `<ds_user_id>%3A<トークン>…` という形**で、
    先頭にユーザーIDが入っている。ここが ds_user_id と食い違っていたら、
    別のクッキーを貼ったか、コピーを取り損ねている。
    貼り間違いは「ログインとして認められない」という同じ症状になるので、
    入口で弾いておかないと原因の切り分けに何日もかかる。
    """
    sid = (cookies.get("sessionid") or "").strip()
    uid = (cookies.get("ds_user_id") or "").strip()
    if not sid:
        return
    print(f"sessionid 長さ: {len(sid)} 文字")
    if len(sid) < 30:
        print("  ⚠ 短すぎます。コピーを取り損ねている可能性が高いです。")
    if not uid:
        return
    # URLエンコードされていることも、生のコロンのこともある
    if sid.startswith(f"{uid}%3A") or sid.startswith(f"{uid}:"):
        print(f"  OK: 先頭が ds_user_id（{uid}）と一致しています。")
    else:
        head = sid.split("%3A")[0].split(":")[0][:24]
        print(f"  ⚠ **先頭が ds_user_id と一致しません**"
              f"（sessionid の先頭は '{head}'、ds_user_id は '{uid}'）。")
        print("     別のクッキーを貼ったか、コピーを取り損ねている可能性があります。")
        print("     開発者ツールで sessionid の値を選び直してください。")


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
    ap.add_argument("--save-anyway", action="store_true",
                    help="検証に落ちても保存する"
                         "（dev/diag_session.py で中身を調べたいとき用）")
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

    # **保存名（アカウント名）は最初に決める。**
    # 検証はそのアカウント自身の投稿を1ページ引いて行うので名前が要る。
    # 以前は test_login() が名前を返していたが、そのエンドポイントは廃止された。
    if not args.user:
        print("アカウント名が要ります。--user <アカウント名> を付けるか、"
              ".env の IG_RAY_LOGIN_USER を設定してください。", file=sys.stderr)
        return 1

    print(f"\n取り込むクッキー: {sorted(cookies.keys())}")
    _check_sessionid_shape(cookies)
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

    # **update_cookies() ではヘッダが匿名セッションのまま。**
    # Host / Origin / X-CSRFToken / X-Instagram-AJAX / X-Requested-With が
    # 付かず、その状態で graphql/query を叩くと Instagram は API 要求と
    # 見なさずに SPA の HTML（200・500KB超）を返す。
    # 「JSONじゃなかった（Expecting value: line 1 column 1）」の正体がこれだった。
    # load_session() と同じ組み方にする。
    _install_cookies(L, cookies, args.user or "pending")

    # **プロキシは load_session の後に入れる。**
    # load_session は _session を作り直すので、先に入れると消える。
    proxy = config.env("PROXY", "")
    if proxy:
        L.context._session.proxies = {"http": proxy, "https": proxy}
        print(f"プロキシ経由: {proxy.split('@')[-1]}")

    print(f"\n検証中...（{args.user} の投稿を1ページだけ引きます）")
    ok, err = igs.verify_session(L, args.user)

    if not ok:
        print(f"\nNG: このクッキーでは投稿を引けませんでした。\n  {err}")
        verdict = _explain_error(err)
        # **原因を言い当てられたら定型文は出さない。**
        # アクションブロックなのに「クッキーを取り直せ」と出すのは
        # 逆効果（待つべきときに叩かせる）。
        if not verdict or verdict == "recopy":
            print("\n  - ブラウザで instagram.com にログイン済みか確認")
            print("  - sessionid をコピーし損ねていないか（長い文字列です）")
            print("  - ブラウザ側でログアウトすると sessionid は即座に無効になります")
        if not args.save_anyway:
            print("\n  ※ 検証に落ちたのでセッションは保存していません。")
            print("     それでも保存したい場合は --save-anyway を付けてください。")
            return 1
        print("\n  --save-anyway が指定されたので、検証に落ちたまま保存します。")
    else:
        print(f"OK: '{args.user}' として投稿を引けました。")

    user = args.user
    L.context.username = user

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
    if (config.env("LOGIN_USER", "") or "") != user:
        print(f"※ .env の IG_RAY_LOGIN_USER を '{user}' にすると巡回で使われます。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
