"""
ig-ray / dev/il416_check.py

instaloader 4.16 への移植（v4.19）の検証。

経緯:
  2026-10-05 から1か月取得が止まっていた。原因は **instaloader 4.15.3 が使う
  投稿取得クエリ（doc_id 7898261790222653）を Instagram が廃止した**こと。
  廃止されたクエリには 400 feedback_required / 401 が返り、それを
  「アカウントが制限された」と読み違えて、生きているアカウントを1か月寝かせた。

  ログイン検証（test_login、旧式 query_hash）も同時期に廃止されていて、
  4.16 でも直っていない（上流 issue #2792）。

検証すること:
  1. 4.16 が入っていて、廃止されたクエリを投げないこと
  2. 投稿の変換で追加リクエストを出さないこと（Post.comments の罠）
  3. ログイン検証に test_login() を使わないこと
  4. セッション起因の失敗で巡回・バックフィルを止め、バックオフに乗せること
  5. 巡回順が毎回同じでないこと

    python3 dev/il416_check.py
"""

import importlib.util
import os
import pickle
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          f"{('  -- ' + str(detail)) if detail and not cond else ''}")


def fresh(**env):
    for k in list(os.environ):
        if k.startswith(("IG_RAY_", "INSTA_RAY_")):
            del os.environ[k]
    os.environ.update({k: str(v) for k, v in env.items() if v is not None})
    for m in ("config", "db", "ig_scraper", "backfill"):
        sys.modules.pop(m, None)


def iphone_node(code="IMG001", comments=7):
    """4.16 の Post.from_iphone_struct が通る最小の iphone_struct。"""
    return {
        "pk": "111", "id": "111_999", "code": code, "media_type": 1,
        "taken_at": 1756000000, "like_count": 5, "comment_count": comments,
        "has_liked": False,
        "caption": {"text": "hello #tag"},
        "image_versions2": {"candidates": [
            {"url": "https://cdn.example/a.jpg", "width": 1080, "height": 1080}]},
        "user": {"pk": 999, "username": "owner_u", "full_name": "Owner U",
                 "profile_pic_url": "https://cdn.example/pp.jpg",
                 "is_private": False},
    }


# --------------------------------------------------------------------------

def test_version():
    print("\n[1] instaloader のバージョン")
    import instaloader
    v = tuple(int(x) for x in instaloader.__version__.split(".")[:2])
    check("4.16 以上が入っている", v >= (4, 16), instaloader.__version__)
    req = open(os.path.join(ROOT, "app", "requirements.txt"),
               encoding="utf-8").read()
    check("requirements.txt は 4.16 に固定", "instaloader==4.16" in req, req)
    check("4.15.x の固定が残っていない", "4.15" not in req, req)


def test_query():
    print("\n[2] 投稿取得のクエリ")
    fresh()
    import ig_scraper as igs
    import instaloader

    calls = []

    class Ctx:
        is_logged_in = True
        username = "login_acct"

        def doc_id_graphql_query(self, doc_id, variables, referer=None,
                                 web_api=False):
            calls.append((doc_id, web_api, variables.get("username")))
            return {"data": {
                "xdt_api__v1__feed__user_timeline_graphql_connection": {
                    "edges": [], "page_info": {"has_next_page": False,
                                               "end_cursor": None}}}}

        def get_json(self, *a, **k):
            raise AssertionError("web_profile_info などを叩いてはいけない")

    p = igs.minimal_profile(Ctx(), "Target_User")
    check("minimal_profile に is_private がある",
          p._node.get("is_private") is False, p._node)
    try:
        list(p.get_posts())
        ok = True
    except Exception as e:
        ok = False
        calls.append(repr(e))
    check("get_posts() が落ちない", ok, calls)
    check("問い合わせは1回だけ", len(calls) == 1, calls)
    if calls and isinstance(calls[0], tuple):
        doc_id, web_api, uname = calls[0]
        check("廃止された 7898261790222653 を使わない",
              doc_id != "7898261790222653", doc_id)
        check("api/graphql に送る（web_api=True）", web_api is True, web_api)
        check("username で問い合わせる", uname == "target_user", uname)


def test_no_extra_request():
    print("\n[3] 変換で追加リクエストを出さない")
    fresh()
    import instaloader
    import ig_scraper as igs

    L = instaloader.Instaloader(quiet=True)
    post = instaloader.Post.from_iphone_struct(L.context, iphone_node())

    fetched = []
    orig = instaloader.Post._obtain_metadata

    def boom(self):
        fetched.append(self.shortcode)
        raise AssertionError("追加リクエスト")
    instaloader.Post._obtain_metadata = boom
    try:
        rec = igs.post_to_record(post)
    finally:
        instaloader.Post._obtain_metadata = orig

    check("post_to_record が追加リクエストを出さない", not fetched, fetched)
    check("コメント数は iphone_struct から取れる", rec.get("comments") == 7,
          rec.get("comments"))
    check("いいね数も取れる", rec.get("likes") == 5, rec.get("likes"))
    check("owner が取れる", rec.get("owner_username") == "owner_u",
          rec.get("owner_username"))

    # comments_of 単体
    class P:
        _node = {"iphone_struct": {"comment_count": 0}}
    check("comment_count=0 も 0 として返す（None にしない）",
          igs.comments_of(P()) == 0, igs.comments_of(P()))

    class Q:
        _node = {"edge_media_to_comment": {"count": 3}}
    check("graphql 形式の edge からも取れる", igs.comments_of(Q()) == 3)

    class R:
        _node = {}
    check("どこにも無ければ None（取りに行かない）", igs.comments_of(R()) is None)


def test_make_loader():
    print("\n[4] ログイン検証に test_login を使わない")
    tmp = tempfile.mkdtemp()
    sess = os.path.join(tmp, "sessions")
    os.makedirs(sess)
    pickle.dump({"sessionid": "79001242633%3Aabc", "csrftoken": "c",
                 "ds_user_id": "79001242633", "mid": "m", "ig_did": "d"},
                open(os.path.join(sess, "session-good"), "wb"))
    pickle.dump({"csrftoken": "c", "mid": "m"},
                open(os.path.join(sess, "session-nosid"), "wb"))
    fresh(IG_RAY_SESSION_DIR=sess, IG_RAY_SESSIONS=sess,
          IG_RAY_DB=os.path.join(tmp, "t.db"), IG_RAY_CACHE=tmp)
    import instaloader
    import ig_scraper as igs
    igs.SESSION_DIR = sess

    called = []
    orig = instaloader.Instaloader.test_login
    instaloader.Instaloader.test_login = lambda self: called.append(1) or None
    try:
        L, err = igs.make_loader("good", verify=True)
        check("クッキーが揃っていれば通る", L is not None, err)
        check("test_login() を呼ばない", not called, len(called))
        L2, err2 = igs.make_loader("nosid", verify=True)
        check("sessionid が無ければ弾く", L2 is None, err2)
        check("理由に sessionid が出る", "sessionid" in (err2 or ""), err2)
        L3, err3 = igs.make_loader("nosid", verify=False)
        check("verify=False なら素通し", L3 is not None, err3)
    finally:
        instaloader.Instaloader.test_login = orig

    src = open(os.path.join(ROOT, "app", "ig_scraper.py"), encoding="utf-8").read()
    i = src.index("def make_loader")
    j = src.index("\ndef ", i + 10)
    check("make_loader の本体に test_login() の呼び出しが無い",
          "L.test_login()" not in src[i:j])


def test_session_failure_marks():
    print("\n[5] セッション起因の失敗の判定")
    fresh()
    import ig_scraper as igs
    real = [
        'AbortDownloadException: 400 Bad Request - "fail" status, message '
        '"feedback_required" when accessing https://www.instagram.com/graphql/query',
        'ConnectionException: JSON Query to graphql/query: 401 Unauthorized - '
        '"fail" status, message "Please wait a few minutes before you try again."',
        '400 Bad Request - "fail" status, message "checkpoint_required"',
        'LoginRequiredException: Login required to access the posts',
        # 4.16 がトップページでトークンを拾う段階で落ちる形（停止中の nullzebra で実測）
        'ConnectionException: 302 Found when accessing https://www.instagram.com/',
        'BadResponseException: The start page did not contain the token lsd.',
    ]
    for m in real:
        check(f"セッション起因: {m[:40]}…", igs.is_session_failure(m))
    for m in [None, "", "ProfileNotExistsException: Profile x does not exist",
              "DBエラー: database is locked", "429 Too Many Requests"]:
        check(f"アカウント／その他: {str(m)[:40]}", not igs.is_session_failure(m))


def test_sweep():
    print("\n[6] 巡回：セッション起因の失敗で止まり、記録とバックオフ")
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "t.db")
    fresh(IG_RAY_DB=p, IG_RAY_CACHE=os.path.join(tmp, "c"),
          IG_RAY_INTERVAL=21600)
    import db
    import ig_scraper as igs
    c = db.connect(p)
    db.init_db(c)
    for u in ("aa", "bb", "cc", "dd"):
        c.execute("INSERT INTO accounts (username,is_enabled,is_target) "
                  "VALUES (?,1,1)", (u,))
    c.commit()
    c.close()

    igs.make_loader = lambda *a, **k: (object(), None)
    igs._nap = lambda *a, **k: None
    seen = []
    mode = {"fail": True}

    def fake_scrape(conn, loader, u, **k):
        seen.append(u)
        if mode["fail"]:
            return {"status": "error", "fetched": 0, "inserted": 0,
                    "message": '取得中断: AbortDownloadException: 400 Bad Request'
                               ' - "fail" status, message "feedback_required"',
                    "started_at": None}
        return {"status": "ok_degraded", "fetched": 3, "inserted": 1,
                "message": None, "started_at": None}
    igs.scrape_user = fake_scrape

    def run(argv):
        old = sys.argv
        sys.argv = list(argv)
        try:
            return igs.main()
        finally:
            sys.argv = old

    rc = run(["s", "--cron", "--skip-egress-check"])
    check("セッション起因なら rc=2", rc == 2, rc)
    check("1件目で止まる（残りを叩かない）", len(seen) == 1, seen)
    c = db.connect(p)
    rows = [dict(r) for r in c.execute(
        "SELECT username, status FROM scrape_log ORDER BY id")]
    check("__login__ に login_error を記録", any(
        r["username"] == db.LOGIN_LOG_NAME and r["status"] == "login_error"
        for r in rows), rows)
    check("次回予定が入っている", db.get_next_run(c, "scrape") is not None)
    c.close()

    # 2回目の失敗 → バックオフで次回が延びる
    seen.clear()
    run(["s", "--cron", "--skip-egress-check"])
    c = db.connect(p)
    n = db.consecutive_failures(c, db.LOGIN_LOG_NAME, ("login_error",))
    check("連続失敗が数えられる", n == 2, n)
    c.close()

    # バックオフ中の cron は何も叩かない
    seen.clear()
    rc = run(["s", "--cron", "--skip-egress-check"])
    check("バックオフ中は叩かない", seen == [] and rc == 0, (seen, rc))

    # 手動で復旧 → login_ok で連続が切れる
    mode["fail"] = False
    seen.clear()
    run(["s", "--skip-egress-check"])
    c = db.connect(p)
    n = db.consecutive_failures(c, db.LOGIN_LOG_NAME, ("login_error",))
    check("取得できたら連続が切れる", n == 0, n)
    check("復旧後は全アカウントを回る", sorted(seen) == ["aa", "bb", "cc", "dd"],
          seen)
    c.close()

    # 巡回順はシャッフルされる（明示指定のときは指定順）
    orders = set()
    for _ in range(12):
        seen.clear()
        run(["s", "--skip-egress-check"])
        orders.add(tuple(seen))
    check("巡回順が毎回同じではない", len(orders) > 1, orders)
    seen.clear()
    run(["s", "--skip-egress-check", "dd", "aa"])
    check("明示指定は指定順のまま", seen == ["dd", "aa"], seen)


def test_backfill():
    print("\n[7] バックフィル：セッション起因で error にしない")
    src = open(os.path.join(ROOT, "app", "backfill.py"), encoding="utf-8").read()
    i = src.index("if igs.is_session_failure(msg):")
    blk = src[i:i + 600]
    check("セッション起因は queued に戻す", 'status="queued"' in blk, blk[:200])
    check("戻り値は session", 'return "session"' in blk)
    j = src.index('if result == "session":')
    blk2 = src[j:j + 700]
    check("__login__ に記録する", "_record_login_failure" in blk2)
    check("バックオフで待つ", "login_retry_delay" in blk2 and "sleep" in blk2)


def main():
    test_version()
    test_query()
    test_no_extra_request()
    test_make_loader()
    test_session_failure_marks()
    test_sweep()
    test_backfill()
    print("\n" + "=" * 50)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    for n in FAIL:
        print(f"  - {n}")
    if not FAIL:
        print("すべて通過")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
