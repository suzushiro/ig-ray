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


# --------------------------------------------------------------------------
# 判定ロジック
#
# **テスト側に判定を書き写さない。** IP問い合わせ関数だけ差し替えて
# 本物の check_egress() を呼ぶ。X-Ray の旧テストは判定部分をテスト内に
# 再実装していたため、本体の判定を一切検証していなかった。
# --------------------------------------------------------------------------

def _with_ips(ce, proxy_ip, direct_ip):
    """
    _fetch_global_ip を差し替える。session が「直接用」かどうかで返す値を変える。

    直接用の判別は trust_env=False で行う（本体が _direct_session() で
    立てている印そのもの）。ここを文字列で判定すると本体の実装と乖離する。
    """
    def fake(session=None, url=None, timeout=20):
        is_direct = session is not None and getattr(session, "trust_env", True) is False
        return direct_ip if is_direct else proxy_ip
    ce._fetch_global_ip = fake


def test_verdicts():
    print("\n[6] 判定の切り分け")
    reload_config()
    sys.modules.pop("check_egress", None)
    import check_egress as ce
    orig = ce._fetch_global_ip

    PROXY = "http://192.0.2.10:8888"

    def run(expected, proxy_ip, direct_ip, proxy_env=PROXY):
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(k, None)
        if proxy_env:
            os.environ["HTTPS_PROXY"] = proxy_env
        _with_ips(ce, proxy_ip, direct_ip)
        return ce.check_egress(expected)

    try:
        # 1) 一致
        r = run("203.0.113.10", "203.0.113.10", "198.51.100.5")
        check("match: verdict", r["verdict"] == "match", r["verdict"])
        check("match: 続行する", r["ok"] is True)
        check("match: ipを返す", r["ip"] == "203.0.113.10", str(r["ip"]))
        check("match: 直接は測らない", r["direct_ip"] is None, str(r["direct_ip"]))

        # 2) 回線IPが変わった（proxy経由 ≠ 直接 → proxyは効いている）
        r = run("203.0.113.10", "203.0.113.99", "198.51.100.5")
        check("proxy_ip_changed: verdict",
              r["verdict"] == "proxy_ip_changed", r["verdict"])
        check("proxy_ip_changed: 続行する", r["ok"] is True)
        check("proxy_ip_changed: 新しいIPを理由に含む",
              "203.0.113.99" in r["reason"], r["reason"])
        check("proxy_ip_changed: 更新先の変数名を案内する",
              "IG_RAY_EXPECT_EGRESS_IP" in r["reason"], r["reason"])

        # 3) proxyを迂回している（proxy経由 == 直接）
        r = run("203.0.113.10", "198.51.100.5", "198.51.100.5")
        check("proxy_bypassed: verdict",
              r["verdict"] == "proxy_bypassed", r["verdict"])
        check("proxy_bypassed: 中止する", r["ok"] is False)
        check("proxy_bypassed: direct_ip を返す",
              r["direct_ip"] == "198.51.100.5", str(r["direct_ip"]))

        # 4) proxy未設定で不一致
        r = run("203.0.113.10", "198.51.100.5", "198.51.100.5", proxy_env=None)
        check("no_proxy_configured: verdict",
              r["verdict"] == "no_proxy_configured", r["verdict"])
        check("no_proxy_configured: 中止する", r["ok"] is False)
        check("no_proxy_configured: 直接は測らない",
              r["direct_ip"] is None, str(r["direct_ip"]))

        # 5) 直接の出口IPが取れない → 判定不能 → 安全側に倒す
        r = run("203.0.113.10", "198.51.100.5", None)
        check("undetermined: verdict",
              r["verdict"] == "undetermined", r["verdict"])
        check("undetermined: 中止する", r["ok"] is False)

        # 6) proxy経由の出口IPすら取れない
        r = run("203.0.113.10", None, "198.51.100.5")
        check("unreachable: verdict", r["verdict"] == "unreachable", r["verdict"])
        check("unreachable: 中止する", r["ok"] is False)

        # 7) 期待値なし → 照合しない
        r = run("", "198.51.100.5", "198.51.100.5")
        check("unchecked: verdict", r["verdict"] == "unchecked", r["verdict"])
        check("unchecked: 続行する", r["ok"] is True)
        r2 = run("   ", "198.51.100.5", "198.51.100.5")
        check("unchecked: 空白だけでも照合しない",
              r2["verdict"] == "unchecked", r2["verdict"])

        # 戻り値の形
        r = run("203.0.113.10", "203.0.113.10", None)
        for k in ("ok", "ip", "direct_ip", "expected", "proxy", "verdict", "reason"):
            check(f"戻り値に {k} がある", k in r, str(sorted(r)))
        check("reason は1行", "\n" not in r["reason"], repr(r["reason"]))
        check("続行してよい verdict は3つ",
              set(ce.CONTINUE_VERDICTS) ==
              {"match", "unchecked", "proxy_ip_changed"},
              str(ce.CONTINUE_VERDICTS))
        check("記録しない verdict は2つ",
              set(ce.QUIET_VERDICTS) == {"match", "unchecked"},
              str(ce.QUIET_VERDICTS))

        # 期待値を環境変数から拾う
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(k, None)
        os.environ["IG_RAY_EXPECT_EGRESS_IP"] = "203.0.113.77"
        sys.modules.pop("config", None)
        sys.modules.pop("check_egress", None)
        import check_egress as ce2
        _with_ips(ce2, "203.0.113.77", None)
        r = ce2.check_egress()
        check("引数なしなら .env の期待値を使う",
              r["verdict"] == "match" and r["expected"] == "203.0.113.77",
              f"{r['verdict']} / {r['expected']}")
        os.environ.pop("IG_RAY_EXPECT_EGRESS_IP", None)
    finally:
        ce._fetch_global_ip = orig


def test_direct_session():
    print("\n[7] 直接接続用セッション")
    reload_config(IG_RAY_PROXY="http://192.0.2.10:8888")
    sys.modules.pop("check_egress", None)
    import check_egress as ce
    sess = ce._direct_session()
    check("trust_env を切っている", sess.trust_env is False)
    # trust_env は環境変数しか見ない。IG_RAY_PROXY 経由で渡した分も
    # 潰さないと requests のセッション既定が残る
    check("proxies を空にしている", sess.proxies == {}, str(sess.proxies))

    os.environ["HTTPS_PROXY"] = "http://192.0.2.10:8888"
    try:
        check("プロキシ設定を環境変数から検出する",
              ce._proxy_configured() == "http://192.0.2.10:8888",
              ce._proxy_configured())
    finally:
        os.environ.pop("HTTPS_PROXY", None)
    reload_config(IG_RAY_PROXY="http://192.0.2.11:8888")
    sys.modules.pop("check_egress", None)
    import check_egress as ce3
    check("IG_RAY_PROXY だけでも検出する",
          ce3._proxy_configured() == "http://192.0.2.11:8888",
          ce3._proxy_configured())


def test_logging():
    print("\n[8] scrape_log への記録")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    reload_config(IG_RAY_DB=path, IG_RAY_CACHE=tmp)
    for m in ("db", "check_egress"):
        sys.modules.pop(m, None)
    import db
    import check_egress as ce
    conn = db.connect(path)
    db.init_db(conn)

    def rows():
        return [dict(r) for r in conn.execute(
            "SELECT username, status, message FROM scrape_log "
            "WHERE username = ? ORDER BY id", (ce.EGRESS_LOG_NAME,))]

    # 正常時は書かない
    wrote = ce.log_egress(conn, {"ok": True, "verdict": "match",
                                 "reason": "期待どおり"})
    check("match では書かない", wrote is False and not rows())
    wrote = ce.log_egress(conn, {"ok": True, "verdict": "unchecked",
                                 "reason": "未設定"})
    check("unchecked でも書かない", wrote is False and not rows())

    # 警告
    wrote = ce.log_egress(conn, {"ok": True, "verdict": "proxy_ip_changed",
                                 "reason": "回線のIPが変わりました"})
    conn.commit()
    r = rows()
    check("warn で書く", wrote is True and len(r) == 1, str(r))
    check("status は egress_warn", r and r[-1]["status"] == "egress_warn",
          str(r))
    check("message は reason そのまま",
          r and r[-1]["message"] == "回線のIPが変わりました", str(r))

    # 中止
    ce.log_egress(conn, {"ok": False, "verdict": "proxy_bypassed",
                         "reason": "迂回しています"})
    conn.commit()
    r = rows()
    check("error で書く", len(r) == 2, str(len(r)))
    check("status は egress_error", r[-1]["status"] == "egress_error",
          r[-1]["status"])
    check("擬似アカウント名は __egress__",
          r[-1]["username"] == "__egress__", r[-1]["username"])

    # 正常なステータス集合には入れない（画面が成功扱いしないこと）
    check("egress_warn は OK_STATUSES ではない",
          "egress_warn" not in db.OK_STATUSES)
    check("egress_error は OK_STATUSES ではない",
          "egress_error" not in db.OK_STATUSES)

    # 消せる
    n = ce.clear_egress_log(conn)
    check("clear_egress_log が件数を返す", n == 2, str(n))
    check("消える", not rows())
    check("もう一度消しても0", ce.clear_egress_log(conn) == 0)

    # 他のアカウントのログは巻き込まない
    db.log_scrape(conn, "someone", "error", 0, 0, "別件")
    ce.log_egress(conn, {"ok": False, "verdict": "proxy_bypassed",
                         "reason": "迂回"})
    conn.commit()
    ce.clear_egress_log(conn)
    left = [dict(r) for r in conn.execute(
        "SELECT username FROM scrape_log")]
    check("他のアカウントのログは残る",
          [r["username"] for r in left] == ["someone"], str(left))
    conn.close()


def test_banner_render():
    print("\n[9] 実レンダリング（文字列grepで済ませない）")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    cache = os.path.join(tmp, "cache")
    os.makedirs(cache, exist_ok=True)
    reload_config(IG_RAY_DB=path, IG_RAY_CACHE=cache)
    for m in ("db", "web", "check_egress"):
        sys.modules.pop(m, None)
    import db
    import check_egress as ce
    conn = db.connect(path)
    db.init_db(conn)
    conn.execute("INSERT INTO accounts (username, is_enabled, is_target) "
                 "VALUES ('u1', 1, 1)")
    conn.commit()

    import web
    web.app.config["TESTING"] = True

    check("web と check_egress で擬似アカウント名が一致",
          web.EGRESS_LOG_NAME == ce.EGRESS_LOG_NAME,
          f"{web.EGRESS_LOG_NAME} / {ce.EGRESS_LOG_NAME}")

    import re as _re

    def banner_classes(html):
        """描画された health バナーの class 属性だけを集める。

        **素の "health-error" を grep してはいけない。** _style.html の
        CSS 定義（.health-error { ... }）に一致して常に真になる。
        X-Ray がボタン0個の不具合をテストで見逃したのと同じ罠。
        """
        return _re.findall(r'<div class="(health[^"]*)"', html)

    def get(path_):
        web._target_cache.update({"at": 0.0, "set": None})
        web._muted_cache.update({"at": 0.0, "set": frozenset()})
        with web.app.test_client() as cli:
            return cli.get(path_).get_data(as_text=True)

    # 記録なし → バナーも出ない
    html = get("/")
    check("記録が無ければバナーは出ない",
          not any("health-error" in c for c in banner_classes(html)),
          str(banner_classes(html)))
    check("記録が無ければ擬似アカウント名も出ない", "__egress__" not in html)

    # 中止
    REASON = "プロキシを迂回しています（プロキシ経由 198.51.100.5 ＝ 直接 198.51.100.5）"
    ce.log_egress(conn, {"ok": False, "verdict": "proxy_bypassed",
                         "reason": REASON})
    conn.commit()
    html = get("/")
    check("error で health-error クラスが出る",
          any("health-error" in c for c in banner_classes(html)),
          str(banner_classes(html)))
    check("CSS定義ではなく実際の要素に付いている",
          'class="health health-error"' in html)
    check("error の見出しが出る", "取得を中止しています" in html)
    check("reason がそのまま出る", REASON in html, html[:0])
    check("擬似アカウント名が生で出ない", "__egress__" not in html)
    check("失敗一覧に混ざらない",
          "egress_error×" not in html and "egress_error ×" not in html)
    check("「失敗なし」と嘘をつかない",
          "直近6時間の失敗なし" in html,
          "出口IPはアカウントの失敗ではないので一覧の判定は変えない")

    # 警告（続行しているので赤ベタにはしない）
    ce.clear_egress_log(conn)
    WARN = "プロキシは効いています（経由 203.0.113.99 ≠ 直接 198.51.100.5）"
    ce.log_egress(conn, {"ok": True, "verdict": "proxy_ip_changed",
                         "reason": WARN})
    conn.commit()
    html = get("/")
    check("warn では health-error を使わない",
          not any("health-error" in c for c in banner_classes(html)),
          str(banner_classes(html)))
    check("warn で health-warn が出る",
          any("health-warn" in c for c in banner_classes(html)),
          str(banner_classes(html)))
    check("warn の reason が出る", WARN in html)
    check("warn でも擬似アカウント名は出ない", "__egress__" not in html)

    # バックアップ画面
    bk = get("/backup")
    check("backup に出口IPの行が出る", "🌐 出口IP" in bk)
    check("backup に reason が出る", WARN in bk)
    check("backup に擬似アカウント名が出ない", "__egress__" not in bk)
    check("backup に消し方の案内が出る", "--clear-log" in bk)
    check("巡回の集計に混ざらない", "egress_warn" not in bk, bk[:0])

    # 12時間より古い記録はバナーに出さない
    conn.execute("UPDATE scrape_log SET ended_at = datetime('now','-13 hours') "
                 "WHERE username = ?", (ce.EGRESS_LOG_NAME,))
    conn.commit()
    html = get("/")
    check("12時間より古い記録はトップに出さない", WARN not in html)
    bk = get("/backup")
    check("backup では古くても状態として出す", WARN in bk)
    conn.close()


def test_cli_intact():
    print("\n[10] 既存CLIが壊れていない")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    reload_config(IG_RAY_DB=path, IG_RAY_CACHE=tmp)
    for m in ("db", "check_egress"):
        sys.modules.pop(m, None)
    import io
    import contextlib
    import db
    import check_egress as ce

    # --clear-log は測定をせずに終わる（出口が壊れていても使えること）
    def boom(*a, **k):
        raise AssertionError("--clear-log で出口IPを測ってはいけない")
    orig = ce._fetch_global_ip
    ce._fetch_global_ip = boom
    argv = sys.argv
    try:
        sys.argv = ["ipcheck", "--clear-log", "--db", path]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ce.main()
        check("--clear-log は 0 で終わる", rc == 0, str(rc))
        check("件数を表示する", "記録" in buf.getvalue(), buf.getvalue())

        # 行があれば消える
        conn = db.connect(path)
        db.init_db(conn)
        db.log_scrape(conn, ce.EGRESS_LOG_NAME, "egress_error", 0, 0, "x")
        conn.commit()
        conn.close()
        sys.argv = ["ipcheck", "--clear-log", "--db", path]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ce.main()
        check("--clear-log で実際に消える", "1 件削除" in buf.getvalue(),
              buf.getvalue())

        # --verdict は判定だけ出す
        ce._fetch_global_ip = lambda session=None, url=None, timeout=20: \
            "203.0.113.10"
        sys.argv = ["ipcheck", "--verdict", "--expect", "203.0.113.10"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ce.main()
        check("--verdict は一致なら 0", rc == 0, str(rc))
        check("--verdict が verdict を出す", "match" in buf.getvalue(),
              buf.getvalue())
    finally:
        sys.argv = argv
        ce._fetch_global_ip = orig

    # 3経路を見せる既存の出力が残っていること
    src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "check_egress.py"), encoding="utf-8").read()
    for label in ("requests (IPv4)", "requests (IPv6優先)",
                  "メディアDL用セッション", "投稿取得用セッション"):
        check(f"CLI が {label} を出す", label in src)
    check("IPv6 の警告が残っている", "IPv6 で出ている経路があります" in src)
    check("経路差の警告が残っている", "経路によって出口IPが違います" in src)


def test_scraper_wiring():
    print("\n[11] スクレイパーへの組み込み")
    src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "ig_scraper.py"), encoding="utf-8").read()
    i_eg = src.find("check_egress.check_egress()")
    i_loader = src.find("loader, err = make_loader(args.login_user")
    check("判定は make_loader より前にある",
          i_eg != -1 and i_loader != -1 and i_eg < i_loader,
          f"egress={i_eg} loader={i_loader}")
    check("無効化オプションがある", "--skip-egress-check" in src)
    check("中止しても次回予定を記録する",
          'db.set_next_run(conn, "scrape", CRON_INTERVAL)' in
          src[i_eg:i_loader], src[i_eg:i_loader][-400:])
    check("sys.exit ではなく return で返す",
          "return 2" in src[i_eg:i_loader])
    check("判定結果を記録する", "log_egress" in src)


def main():
    test_prefix_fallback()
    test_db_path_migration()
    test_fetch_validation()
    test_compose_proxy()
    test_header_stripping()
    test_verdicts()
    test_direct_session()
    test_logging()
    test_banner_render()
    test_cli_intact()
    test_scraper_wiring()

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
