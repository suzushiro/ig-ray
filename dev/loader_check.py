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


def test_verify_softblock():
    """
    実際に踏んだやつ。
    セッションは読めるが test_login() が None（401 soft block）。
    """
    print("\n[3] ソフトブロック（test_login が None）")
    tmp = tempfile.mkdtemp()
    make_session_file(tmp, "nullzebra")
    igs = with_env(IG_RAY_SESSIONS=tmp, IG_RAY_LOGIN_USER="nullzebra")

    import instaloader

    # load_session_from_file は成功、test_login は None を返す状況を作る
    orig_load = instaloader.Instaloader.load_session_from_file
    orig_test = instaloader.Instaloader.test_login
    instaloader.Instaloader.load_session_from_file = lambda self, u, f=None: None
    instaloader.Instaloader.test_login = lambda self: None
    try:
        loader, err = igs.make_loader()
        check("loader を返さない（＝先に進ませない）", loader is None,
              "v1.1 まではここで通してしまっていた")
        check("ソフトブロックの説明が出る", err and "アカウントが制限されている" in err)
        check("ブラウザでの対処を案内する", err and "ブラウザ" in err)
        check("診断スクリプトを案内する", err and "diag_session" in err)
    finally:
        instaloader.Instaloader.load_session_from_file = orig_load
        instaloader.Instaloader.test_login = orig_test


def test_verify_exception():
    print("\n[4] test_login が例外を投げる場合")
    tmp = tempfile.mkdtemp()
    make_session_file(tmp, "nullzebra")
    igs = with_env(IG_RAY_SESSIONS=tmp, IG_RAY_LOGIN_USER="nullzebra")

    import instaloader
    from instaloader.exceptions import ConnectionException

    orig_load = instaloader.Instaloader.load_session_from_file
    orig_test = instaloader.Instaloader.test_login

    def boom(self):
        raise ConnectionException("401 Unauthorized - Please wait a few minutes")

    instaloader.Instaloader.load_session_from_file = lambda self, u, f=None: None
    instaloader.Instaloader.test_login = boom
    try:
        loader, err = igs.make_loader()
        check("例外でも loader を返さない", loader is None)
        check("例外型が出る", err and "ConnectionException" in err, (err or "")[:60])
        check("ソフトブロック案内が付く", err and "アカウントが制限されている" in err)
    finally:
        instaloader.Instaloader.load_session_from_file = orig_load
        instaloader.Instaloader.test_login = orig_test


def test_verify_ok():
    print("\n[5] 検証が通る場合")
    tmp = tempfile.mkdtemp()
    make_session_file(tmp, "nullzebra")
    igs = with_env(IG_RAY_SESSIONS=tmp, IG_RAY_LOGIN_USER="nullzebra")

    import instaloader
    orig_load = instaloader.Instaloader.load_session_from_file
    orig_test = instaloader.Instaloader.test_login
    instaloader.Instaloader.load_session_from_file = lambda self, u, f=None: None
    instaloader.Instaloader.test_login = lambda self: "nullzebra"
    try:
        loader, err = igs.make_loader()
        check("loader が返る", loader is not None)
        check("エラーは無い", err is None, str(err))
        check("fail-fast コントローラが刺さっている",
              loader is not None and
              type(loader.context._rate_controller).__name__ == "FailFastRateController",
              type(loader.context._rate_controller).__name__ if loader else "no loader")
    finally:
        instaloader.Instaloader.load_session_from_file = orig_load
        instaloader.Instaloader.test_login = orig_test


def test_skip_verify():
    print("\n[6] verify=False で検証を飛ばせる")
    tmp = tempfile.mkdtemp()
    make_session_file(tmp, "nullzebra")
    igs = with_env(IG_RAY_SESSIONS=tmp, IG_RAY_LOGIN_USER="nullzebra")

    import instaloader
    orig_load = instaloader.Instaloader.load_session_from_file
    orig_test = instaloader.Instaloader.test_login

    called = []
    instaloader.Instaloader.load_session_from_file = lambda self, u, f=None: None
    instaloader.Instaloader.test_login = lambda self: called.append(1) or None
    try:
        loader, err = igs.make_loader(verify=False)
        check("検証を飛ばすと loader が返る", loader is not None)
        check("test_login は呼ばれない", not called, f"called {len(called)} times")
    finally:
        instaloader.Instaloader.load_session_from_file = orig_load
        instaloader.Instaloader.test_login = orig_test


def test_proxy():
    print("\n[7] IG_RAY_PROXY")
    tmp = tempfile.mkdtemp()
    make_session_file(tmp, "nullzebra")
    igs = with_env(IG_RAY_SESSIONS=tmp, IG_RAY_LOGIN_USER="nullzebra",
                   IG_RAY_PROXY="http://proxy.example:8080")

    import instaloader
    orig_load = instaloader.Instaloader.load_session_from_file
    orig_test = instaloader.Instaloader.test_login
    instaloader.Instaloader.load_session_from_file = lambda self, u, f=None: None
    instaloader.Instaloader.test_login = lambda self: "nullzebra"
    try:
        loader, err = igs.make_loader()
        check("loader が返る", loader is not None)
        if loader:
            px = loader.context._session.proxies
            check("http/https 両方に設定される",
                  px.get("http") == "http://proxy.example:8080" and
                  px.get("https") == "http://proxy.example:8080", str(px))
    finally:
        instaloader.Instaloader.load_session_from_file = orig_load
        instaloader.Instaloader.test_login = orig_test
        with_env(IG_RAY_PROXY=None)


def main():
    test_missing_session()
    test_no_login_user()
    test_verify_softblock()
    test_verify_exception()
    test_verify_ok()
    test_skip_verify()
    test_proxy()

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
