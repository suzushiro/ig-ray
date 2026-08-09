"""
ig-ray / dev/egress_check.py

出口IP確認ツールと設定まわりの検証。ネットワーク不要。

    python3 dev/egress_check.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  -- ' + detail) if detail and not cond else ''}")


def reload_config(**env):
    for k in list(os.environ):
        if k.startswith(("IG_RAY_", "INSTA_RAY_")):
            del os.environ[k]
    os.environ.update({k: v for k, v in env.items() if v is not None})
    for m in ("config",):
        sys.modules.pop(m, None)
    import config
    return config


def test_prefix_fallback():
    print("\n[1] 新旧の環境変数プレフィックス")
    c = reload_config(IG_RAY_CACHE="/new")
    check("IG_RAY_ を読む", c.env("CACHE") == "/new", c.env("CACHE"))

    c = reload_config(INSTA_RAY_CACHE="/old")
    check("旧 INSTA_RAY_ も読む（既存.envが動く）",
          c.env("CACHE") == "/old", c.env("CACHE"))

    c = reload_config(IG_RAY_CACHE="/new", INSTA_RAY_CACHE="/old")
    check("両方あれば新を優先", c.env("CACHE") == "/new", c.env("CACHE"))

    c = reload_config()
    check("無ければ既定値", c.env("CACHE", "/data/cache") == "/data/cache")
    check("空文字は未設定扱い",
          reload_config(IG_RAY_CACHE="").env("CACHE", "/d") == "/d")

    c = reload_config(IG_RAY_POSTS_ONLY="1")
    check("bool: 1 は True", c.env_bool("POSTS_ONLY") is True)
    check("bool: 未設定は既定", reload_config().env_bool("POSTS_ONLY", False) is False)
    check("bool: true も True",
          reload_config(IG_RAY_POSTS_ONLY="true").env_bool("POSTS_ONLY") is True)
    check("int: 不正値は既定",
          reload_config(IG_RAY_BUSY_TIMEOUT_MS="abc").env_int("BUSY_TIMEOUT_MS", 60000) == 60000)


def test_db_path_migration():
    print("\n[2] DBパスの引き継ぎ")
    d = tempfile.mkdtemp()

    c = reload_config(IG_RAY_DATA_DIR=d)
    check("何も無ければ新名 ig_ray.db",
          c.db_path().endswith("ig_ray.db"), c.db_path())

    # 旧名のDBが既にある場合はそちらを使う（改名でDBを見失わないため）
    old = os.path.join(d, "insta_ray.db")
    open(old, "wb").write(b"x")
    c = reload_config(IG_RAY_DATA_DIR=d)
    check("旧名が既にあればそれを使う", c.db_path() == old, c.db_path())

    # 新名も作られていれば新を優先
    new = os.path.join(d, "ig_ray.db")
    open(new, "wb").write(b"x")
    c = reload_config(IG_RAY_DATA_DIR=d)
    check("新名があれば新を優先", c.db_path() == new, c.db_path())

    c = reload_config(IG_RAY_DB="/explicit/path.db", IG_RAY_DATA_DIR=d)
    check("明示指定が最優先", c.db_path() == "/explicit/path.db", c.db_path())


def test_fetch_validation():
    print("\n[3] 出口IPの判定")
    reload_config()
    sys.modules.pop("check_egress", None)
    import check_egress as ce

    class Resp:
        def __init__(self, text, status=200):
            self.text = text; self.status_code = status

    class Sess:
        def __init__(self, resp): self.resp = resp
        def get(self, url, **kw): return self.resp

    r = []
    ce.fetch(Sess(Resp("203.0.113.10")), "u", "IPv4", r)
    check("正しいIPを受け取る", r[-1][1] == "203.0.113.10", str(r[-1]))

    ce.fetch(Sess(Resp("2001:db8::1")), "u", "IPv6", r)
    check("IPv6も受け取る", r[-1][1] == "2001:db8::1", str(r[-1]))

    # 200 でもIP以外なら弾く（プロキシのエラーページ等）
    ce.fetch(Sess(Resp("Host not in allowlist: api.ipify.org", 403)), "u", "err", r)
    check("IP以外は None にする", r[-1][1] is None, str(r[-1]))

    ce.fetch(Sess(Resp("<html>proxy error</html>")), "u", "html", r)
    check("HTMLも弾く", r[-1][1] is None, str(r[-1]))

    ce.fetch(Sess(Resp("")), "u", "empty", r)
    check("空も弾く", r[-1][1] is None, str(r[-1]))

    # \r が混じると表示が壊れるので潰していること
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ce.fetch(Sess(Resp("<html>\r\n<center>403</center>\r\n</html>", 403)),
                 "u", "crlf", r)
    out = buf.getvalue()
    check("改行やCRを表示に出さない",
          "\r" not in out and out.count("\n") == 1, repr(out[:80]))

    class Boom:
        def get(self, url, **kw): raise RuntimeError("no route")
    ce.fetch(Boom(), "u", "boom", r)
    check("例外でも落ちない", r[-1][1] is None)


def test_compose_proxy():
    print("\n[4] compose のプロキシ設定")
    import yaml
    root = os.path.join(os.path.dirname(__file__), "..")
    d = yaml.safe_load(open(os.path.join(root, "docker-compose.yml")))

    worker = ["scrape", "cron", "probe", "diag", "import-cookies", "login", "ipcheck"]
    for name in worker:
        e = d["services"][name]["environment"]
        ok = "HTTPS_PROXY" in e and "https_proxy" in e
        check(f"{name} にプロキシ設定がある", ok, str(sorted(e)[:4]))

    check("web にはプロキシを入れない（外に出ないため）",
          not any("roxy" in k.lower() for k in d["services"]["web"]["environment"]))

    check("大文字と小文字の両方を入れる（ライブラリ差異の吸収）",
          "http_proxy" in d["services"]["cron"]["environment"])
    check("NO_PROXY の既定がある",
          "NO_PROXY" in d["services"]["cron"]["environment"])
    check("ipcheck サービスがある", "ipcheck" in d["services"])
    check("ipcheck は tools profile", d["services"]["ipcheck"]["profiles"] == ["tools"])


def test_header_stripping():
    """
    ログイン済みセッションで測定するとき、Instagram 用ヘッダを外すこと。
    外さないと Host: www.instagram.com が ipify に送られて 403 になる
    （メディアDLが全件404になったのと同じ構図）。
    測定後は必ず元に戻すこと。
    """
    print("\n[5] 測定時のヘッダ退避")
    reload_config()
    sys.modules.pop("check_egress", None)
    import check_egress as ce
    import ig_scraper as igs

    seen = {}

    class Sess:
        def __init__(self):
            self.headers = {
                "Host": "www.instagram.com",
                "Origin": "https://www.instagram.com",
                "X-IG-App-ID": "936619743392459",
                "User-Agent": "Mozilla/5.0",
                "Accept": "*/*",
            }
        def get(self, url, **kw):
            seen.update(self.headers)
            class R:
                text = "203.0.113.10"; status_code = 200
            return R()

    sess = Sess()
    before = dict(sess.headers)

    class Ctx:
        pass
    ctx = Ctx(); ctx._session = sess
    class Loader:
        context = ctx

    orig = igs.make_loader
    igs.make_loader = lambda *a, **k: (Loader(), None)
    try:
        rc = ce.main()
        check("Host を外して測定する", "Host" not in seen, str(sorted(seen)))
        check("Origin も外す", "Origin" not in seen)
        check("X-IG-App-ID も外す", "X-IG-App-ID" not in seen)
        check("User-Agent は残す（普通のリクエストとして通す）",
              "User-Agent" in seen, str(sorted(seen)))
        check("測定後にヘッダを復元する", sess.headers == before,
              f"{sorted(sess.headers)} != {sorted(before)}")
    finally:
        igs.make_loader = orig


def main():
    test_prefix_fallback()
    test_db_path_migration()
    test_fetch_validation()
    test_compose_proxy()
    test_header_stripping()

    print(f"\n{'='*50}")
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  - " + f)
        return 1
    print("すべて通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
