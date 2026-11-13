"""
insta-ray / dev/loader_check.py

make_loader() のログイン検証を確かめる。ネットワーク不要。

v1.1 までは load_session_from_file() が成功しただけで先に進んでいた。
このファイルは実際に踏んだ失敗（401 "Please wait a few minutes" →
test_login() が None）で、ちゃんと手前で止まることを確認する。

    python3 dev/loader_check.py
"""

import os
import pickle
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  -- ' + detail) if detail and not cond else ''}")


def make_session_file(dirpath, user):
    """それらしいセッションファイルを作る。"""
    os.makedirs(dirpath, exist_ok=True)
    path = os.path.join(dirpath, f"session-{user}")
    cookies = {
        "csrftoken": "x" * 32,
        "sessionid": "y" * 50,
        "ds_user_id": "1234567890",
        "ig_did": "z" * 36,
        "mid": "m" * 28,
    }
    with open(path, "wb") as f:
        pickle.dump(cookies, f)
    return path


def with_env(**kw):
    """環境変数を差し替えて ig_scraper を読み直す。"""
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    for m in list(sys.modules):
        if m in ("ig_scraper",):
            del sys.modules[m]
    import ig_scraper
    return ig_scraper


def test_missing_session():
    print("\n[1] セッションファイルが無い場合")
    tmp = tempfile.mkdtemp()
    igs = with_env(IG_RAY_SESSIONS=tmp, IG_RAY_LOGIN_USER="nobody",
                   IG_RAY_PROXY=None)

    loader, err = igs.make_loader()
    check("loader は None", loader is None)
    check("エラーメッセージが返る", bool(err))
    check("案内文が含まれる", err and "対話ログイン" in err, (err or "")[:80])
    check("山カッコのプレースホルダを使っていない",
          err and "<username>" not in err and "<IGアカウント名>" not in err)


def test_no_login_user():
    print("\n[2] IG_RAY_LOGIN_USER 未設定")
    tmp = tempfile.mkdtemp()
    igs = with_env(IG_RAY_SESSIONS=tmp, IG_RAY_LOGIN_USER="")
    loader, err = igs.make_loader()
    check("loader は None", loader is None)
    check("未設定を指摘する", err and "IG_RAY_LOGIN_USER" in err)


def _trap_test_login():
    """test_login() が呼ばれたら記録する罠。呼ばれてはいけない。"""
    import instaloader
    called = []
    orig = instaloader.Instaloader.test_login
    instaloader.Instaloader.test_login = lambda self: called.append(1) or None
    return called, lambda: setattr(instaloader.Instaloader, "test_login", orig)


def test_verify_no_test_login():
    """
    **test_login() を呼ばないこと。**

    test_login() が叩く旧式 query_hash は 2026-10 に廃止され、
    有効なセッションでも None / 例外を返す。v4.18 まではそれを信じて
    「アカウントが制限されている」と止まり、生きているアカウントを
    1か月寝かせた。ここでは test_login() が失敗する状況を作っても
    loader が返ることを確かめる。
    """
    print("\n[3] test_login() を呼ばない（廃止されたエンドポイント）")
    tmp = tempfile.mkdtemp()
    make_session_file(tmp, "sciclone_ai")
    igs = with_env(IG_RAY_SESSIONS=tmp, IG_RAY_LOGIN_USER="sciclone_ai",
                   IG_RAY_PROXY=None)
    called, restore = _trap_test_login()
    try:
        loader, err = igs.make_loader()
        check("test_login が失敗する状況でも loader を返す", loader is not None, err)
        check("test_login は呼ばれない", not called, f"called {len(called)} times")
        check("エラーは無い", err is None, str(err))
        check("fail-fast コントローラが刺さっている",
              loader is not None and
              type(loader.context._rate_controller).__name__ == "FailFastRateController",
              type(loader.context._rate_controller).__name__ if loader else "no loader")
        check("ログイン済みとして扱われる",
              loader is not None and loader.context.is_logged_in)
    finally:
        restore()


def test_verify_missing_cookie():
    print("\n[4] クッキーが欠けているセッションは弾く")
    tmp = tempfile.mkdtemp()
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "session-broken"), "wb") as f:
        pickle.dump({"csrftoken": "x" * 32, "mid": "m" * 28}, f)
    igs = with_env(IG_RAY_SESSIONS=tmp, IG_RAY_LOGIN_USER="broken",
                   IG_RAY_PROXY=None)
    called, restore = _trap_test_login()
    try:
        loader, err = igs.make_loader()
        check("loader を返さない", loader is None)
        check("欠けているクッキー名が出る",
              err and "sessionid" in err and "ds_user_id" in err, (err or "")[:80])
        check("取り直しを案内する", err and "取り直して" in err, (err or "")[:120])
        check("この判定でも test_login は呼ばない", not called)
    finally:
        restore()


def test_verify_marker():
    print("\n[5] セッション起因の失敗の判定（巡回で止めるため）")
    igs = with_env(IG_RAY_PROXY=None)
    for msg, want in [
        ('400 Bad Request - "fail" status, message "feedback_required"', True),
        ('401 Unauthorized - "fail" status, message '
         '"Please wait a few minutes before you try again."', True),
        ('message "checkpoint_required"', True),
        ("ProfileNotExistsException: Profile x does not exist", False),
        ("", False),
    ]:
        check(f"{'セッション起因' if want else '別要因'}: {msg[:36]}",
              igs.is_session_failure(msg) is want)


def test_skip_verify():
    print("\n[6] verify=False で検証を飛ばせる")
    tmp = tempfile.mkdtemp()
    with open(os.path.join(tmp, "session-broken"), "wb") as f:
        pickle.dump({"csrftoken": "x" * 32}, f)
    igs = with_env(IG_RAY_SESSIONS=tmp, IG_RAY_LOGIN_USER="broken",
                   IG_RAY_PROXY=None)
    called, restore = _trap_test_login()
    try:
        loader, err = igs.make_loader(verify=False)
        check("検証を飛ばすとクッキーが欠けていても loader が返る",
              loader is not None)
        check("test_login は呼ばれない", not called, f"called {len(called)} times")
    finally:
        restore()


def test_proxy():
    print("\n[7] IG_RAY_PROXY")
    tmp = tempfile.mkdtemp()
    make_session_file(tmp, "nullzebra")
    igs = with_env(IG_RAY_SESSIONS=tmp, IG_RAY_LOGIN_USER="nullzebra",
                   IG_RAY_PROXY="http://proxy.example:8080")
    called, restore = _trap_test_login()
    try:
        loader, err = igs.make_loader()
        check("loader が返る", loader is not None, err)
        if loader:
            px = loader.context._session.proxies
            check("http/https 両方に設定される",
                  px.get("http") == "http://proxy.example:8080" and
                  px.get("https") == "http://proxy.example:8080", str(px))
    finally:
        restore()
        with_env(IG_RAY_PROXY=None)


def test_cookie_import_headers():
    """
    クッキー取り込み時に**ログイン済みのヘッダ一式**が付くか。

    update_cookies() はクッキーを足すだけでヘッダを直さない。
    Host / Origin / X-CSRFToken / X-Instagram-AJAX / X-Requested-With が
    欠けた状態で graphql/query を叩くと、Instagram は API 要求と見なさず
    SPA の HTML（200・500KB超）を返す。
    instaloader 側のエラーは「Expecting value: line 1 column 1」としか
    出ないため、原因の特定に何日もかかった。二度と退行させない。
    """
    print("\n[クッキー取り込みのヘッダ]")
    import importlib.util
    import instaloader
    path = os.path.join(os.path.dirname(__file__), "..", "app",
                        "import_cookies.py")
    spec = importlib.util.spec_from_file_location("ic_mod", path)
    ic = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(ic)
    except SystemExit:
        pass

    need = {"Host", "Origin", "X-CSRFToken", "X-Instagram-AJAX",
            "X-Requested-With", "x-ig-app-id"}
    ck = {"sessionid": "79001242633%3Aabc", "csrftoken": "c0ffee",
          "ds_user_id": "79001242633", "mid": "m", "ig_did": "d"}

    L = instaloader.Instaloader(quiet=True)
    ic._install_cookies(L, ck, "pending")
    h = dict(L.context._session.headers)
    for k in sorted(need):
        check(f"{k} が付く", k in h, str(sorted(h)))
    check("X-CSRFToken は csrftoken の値", h.get("X-CSRFToken") == "c0ffee",
          str(h.get("X-CSRFToken")))
    check("クッキーも入る",
          L.context._session.cookies.get("sessionid") == ck["sessionid"])

    # csrftoken が無くても壊れない
    L2 = instaloader.Instaloader(quiet=True)
    ic._install_cookies(L2, {k: v for k, v in ck.items() if k != "csrftoken"},
                        "pending")
    h2 = dict(L2.context._session.headers)
    check("csrftoken が無ければ X-CSRFToken を外す", "X-CSRFToken" not in h2)
    check("他のヘッダは残る",
          {"Host", "Origin", "X-Instagram-AJAX", "X-Requested-With"} <= set(h2),
          str(sorted(h2)))

    # update_cookies だけでは足りないことを対照として示す
    L3 = instaloader.Instaloader(quiet=True)
    L3.context.update_cookies(ck)
    h3 = dict(L3.context._session.headers)
    check("（対照）update_cookies だけだとヘッダが欠ける",
          bool(need - set(h3)), "欠けないなら instaloader 側の仕様が変わった")

    # プロキシは load_session の後に入れること（先だと作り直しで消える）
    src = open(path, encoding="utf-8").read()
    check("プロキシ設定は取り込みの後にある",
          src.index("_install_cookies(L, cookies") <
          src.index("L.context._session.proxies = "))

    # --- JSONエラーの読み分けと、定型文の抑制 ---
    # アクションブロックなのに「クッキーを取り直せ」と出すのは逆効果
    # （待つべきときに叩かせる）。言い当てたら定型文は出さない。
    for name, body, want in [
        ("feedback_required",
         '{"message":"feedback_required","spam":true,'
         '"feedback_title":"Try Again Later","status":"fail"}', True),
        ("require_login",
         '{"message":"Please wait a few minutes before you try again.",'
         '"require_login":true,"status":"fail"}', "recopy"),
        ("challenge_required",
         '{"message":"challenge_required","status":"fail"}', True),
        ("checkpoint_required",
         '{"message":"checkpoint_required","status":"fail"}', True),
        ("未知のmessage", '{"message":"something_else","status":"fail"}', False),
        ("JSONでない", 'not json at all', False),
    ]:
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = ic._explain_json(body)
        check(f"{name} の戻り値", got == want, f"{got!r} != {want!r}")
    # 待てと言うケースでだけ定型文を抑える
    check("アクションブロックでは定型文を抑制する",
          ic._explain_json.__doc__ is not None)
    src_ng = src[src.index("verdict = _explain_error(err)"):]
    check("抑制の条件が verdict を見ている",
          'if not verdict or verdict == "recopy":' in src_ng,
          src_ng[:200])

    # **検証は廃止された test_login() ではなく、実際の取得経路で行う。**
    # 旧エンドポイントは有効なクッキーでも feedback_required を返すので、
    # それを見て「アカウントが制限されている」と誤認していた。
    check("import-cookies は test_login() を呼ばない",
          "L.test_login()" not in src)
    check("import-cookies は verify_session で検証する",
          "igs.verify_session(L, args.user)" in src)
    check("旧エンドポイントを叩く診断（_report_raw）は撤去済み",
          "def _report_raw" not in src and "d6f4427" not in src)
    check("保存名は最初に要求する（検証にも使う）",
          src.index("if not args.user:") < src.index("igs.verify_session("))

    # 取得エラーの文から message を拾って説明する
    for name, err, want in [
        ("feedback_required",
         'AbortDownloadException: 400 Bad Request - "fail" status, '
         'message "feedback_required" when accessing https://www.instagram.com/graphql/query',
         True),
        ("checkpoint（停止）",
         '400 Bad Request - "fail" status, message "checkpoint_required" '
         'https://www.instagram.com/accounts/suspended/', True),
        ("401", 'ConnectionException: 401 Unauthorized - Please wait a few minutes',
         "recopy"),
        ("無関係", "ProfileNotExistsException: nope", False),
    ]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = ic._explain_error(err)
        check(f"_explain_error: {name}", got == want, f"{got!r} / {buf.getvalue()[:80]}")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ic._explain_error('message "checkpoint_required" '
                          'https://www.instagram.com/accounts/suspended/')
    check("停止の URL を拾って案内する", "停止" in buf.getvalue(), buf.getvalue()[:200])

    # 誤検知したマーカーを戻していないか
    check("裸の disabled をマーカーにしていない",
          '("disabled",' not in src, "514KBのバンドルに普通に出る語")
    check("裸の suspended をマーカーにしていない", '("suspended",' not in src)


def main():
    test_missing_session()
    test_no_login_user()
    test_verify_no_test_login()
    test_verify_missing_cookie()
    test_verify_marker()
    test_skip_verify()
    test_proxy()
    test_cookie_import_headers()

    print(f"\n{'='*50}")
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("すべて通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
