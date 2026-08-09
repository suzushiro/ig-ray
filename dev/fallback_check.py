"""
insta-ray / dev/fallback_check.py

`api/v1/users/web_profile_info/` だけが 429 で弾かれるときの回避策を検証する。
ネットワーク不要。

実機で起きたこと:
  - graphql/query は通る（test_login が成功する）
  - web_profile_info だけ 429
  - Profile.from_username() は web_profile_info 一択なので詰む
  - しかし get_posts() は doc_id ベースの graphql で、変数は username だけ

    python3 dev/fallback_check.py
"""

import json
import os
import sys
import tempfile
import time
from collections import namedtuple
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import db                    # noqa: E402
import ig_scraper as igs     # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  -- ' + detail) if detail and not cond else ''}")


class FakeContext:
    is_logged_in = True

    def log(self, *a, **kw):
        pass

    def error(self, *a, **kw):
        pass


class FakePost:
    def __init__(self, shortcode, owner="target_user"):
        self.shortcode = shortcode
        self.mediaid = abs(hash(shortcode)) % 10**17
        self.owner_username = owner
        self.caption = "cap #tag"
        self.date_utc = datetime(2026, 7, 20, 12, 0, 0)
        self.likes = 5
        self.comments = 1
        self.typename = "GraphImage"
        self.is_video = False
        self.url = f"https://scontent.example/{shortcode}.jpg"
        self.video_url = None
        self.location = None

    @property
    def caption_hashtags(self):
        return ["tag"]

    def get_sidecar_nodes(self):
        return iter([])


def test_minimal_profile_shape():
    print("\n[1] minimal_profile の形")
    ctx = FakeContext()
    p = igs.minimal_profile(ctx, "Minamina_FlyOver")

    check("username が小文字化される", p.username == "minamina_flyover", p.username)
    check("_has_full_metadata が立っている", p._has_full_metadata is True)

    # _obtain_metadata が短絡することの確認:
    # 立っていなければ context のメソッドを呼びに行って AttributeError になる
    called = []
    orig = type(p)._obtain_metadata
    try:
        type(p)._obtain_metadata = lambda self: called.append(1)
        _ = p.username
        check("username アクセスでメタデータ取得が走らない", not called,
              f"called {len(called)}")
    finally:
        type(p)._obtain_metadata = orig

    # userid はダミーの 0。DB に書いてはいけない値。
    check("userid はダミーの 0", p.userid == 0, str(p.userid))

    # プロフィール詳細は取れない
    try:
        p.followers
        check("followers は取得できない（KeyError）", False, "例外が出なかった")
    except KeyError:
        check("followers は取得できない（KeyError）", True)
    except Exception as e:
        check("followers は取得できない（KeyError）", False, type(e).__name__)


def test_get_posts_uses_username_path():
    """
    get_posts が doc_id graphql（username ベース）を使い、
    web_profile_info を叩かないことを確認する。
    """
    print("\n[2] get_posts が graphql 経路を使うか")

    captured = {}

    class Ctx(FakeContext):
        def doc_id_graphql_query(self, doc_id, variables, referer=None):
            captured["doc_id"] = doc_id
            captured["variables"] = variables
            # 空の結果を返して NodeIterator を成立させる
            return {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {
                "edges": [], "page_info": {"has_next_page": False, "end_cursor": None}}}}

        def get_json(self, path, params=None, **kw):
            captured.setdefault("get_json_paths", []).append(path)
            raise AssertionError(f"get_json が呼ばれた: {path}")

    p = igs.minimal_profile(Ctx(), "target_user")
    it = p.get_posts()
    posts = list(it)

    check("doc_id graphql が使われる", "doc_id" in captured, str(captured.keys()))
    check("web_profile_info を叩かない", "get_json_paths" not in captured,
          str(captured.get("get_json_paths")))
    if "variables" in captured:
        v = captured["variables"]
        check("変数に username が入る", v.get("username") == "target_user", str(v))
        check("変数に userid（id）を使っていない", "id" not in v, str(v))
    check("空結果でも落ちない", posts == [])


def test_ensure_account_does_not_clobber():
    print("\n[3] ensure_account は既存データを潰さない")
    dbfile = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = db.connect(dbfile)
    db.init_db(conn)

    # 先にフル情報が入っている状態を作る
    db.upsert_account(conn, {
        "username": "target_user", "userid": 999, "full_name": "Full Name",
        "biography": "bio", "profile_pic_url": "u", "followers": 1234,
        "mediacount": 56,
    })
    conn.commit()

    db.ensure_account(conn, "target_user")
    row = conn.execute(
        "SELECT * FROM accounts WHERE username='target_user'").fetchone()
    check("followers が保持される", row["followers"] == 1234, str(row["followers"]))
    check("userid が保持される", row["userid"] == 999, str(row["userid"]))
    check("full_name が保持される", row["full_name"] == "Full Name")

    # 新規は作られる
    db.ensure_account(conn, "brand_new")
    n = conn.execute(
        "SELECT COUNT(*) c FROM accounts WHERE username='brand_new'").fetchone()["c"]
    check("未登録なら行が作られる", n == 1)
    check("新規行の is_enabled は 1",
          conn.execute("SELECT is_enabled FROM accounts WHERE username='brand_new'"
                       ).fetchone()["is_enabled"] == 1)

    conn.commit()
    conn.close()


def test_scrape_user_fallback():
    print("\n[4] scrape_user のフォールバック")
    import instaloader
    from instaloader.exceptions import TooManyRequestsException

    dbfile = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = db.connect(dbfile)
    db.init_db(conn)

    class Loader:
        context = FakeContext()

    posts = [FakePost("F001"), FakePost("F002")]

    orig_from = instaloader.Profile.from_username
    orig_min = igs.minimal_profile

    def boom(context, username):
        raise TooManyRequestsException("429 web_profile_info")

    class Stub:
        username = "target_user"

        def get_posts(self):
            return iter(posts)

    instaloader.Profile.from_username = staticmethod(boom)
    igs.minimal_profile = lambda ctx, u: Stub()
    try:
        res = igs.scrape_user(conn, Loader(), "target_user",
                              limit=5, with_media=False)
        check("フォールバックして成功する", res["status"] == "ok_degraded",
              f"{res['status']} / {res['message']}")
        check("投稿が保存される", res["inserted"] == 2, str(res["inserted"]))
        check("degraded の説明が入る",
              res["message"] and "投稿のみ" in res["message"], str(res["message"]))

        n = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        check("posts に2件", n == 2, str(n))

        acc = conn.execute(
            "SELECT * FROM accounts WHERE username='target_user'").fetchone()
        check("accounts に username だけ入る", acc is not None)
        check("userid にダミーが書かれていない",
              acc is not None and acc["userid"] is None, str(acc["userid"] if acc else None))
        check("followers にダミーが書かれていない",
              acc is not None and acc["followers"] is None)

        # --no-fallback 相当
        res2 = igs.scrape_user(conn, Loader(), "target_user",
                               limit=5, with_media=False, fallback=False)
        check("fallback=False なら ratelimited で止まる",
              res2["status"] == "ratelimited", res2["status"])
    finally:
        instaloader.Profile.from_username = orig_from
        igs.minimal_profile = orig_min
        conn.close()


def test_normal_path_unchanged():
    print("\n[5] 正常時は従来通り（回帰）")
    import instaloader

    dbfile = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = db.connect(dbfile)
    db.init_db(conn)

    class Loader:
        context = FakeContext()

    class FullProfile:
        username = "target_user"
        userid = 42
        full_name = "Name"
        biography = "bio"
        profile_pic_url = "pp"
        followers = 100
        mediacount = 7

        def get_posts(self):
            return iter([FakePost("N001")])

    orig = instaloader.Profile.from_username
    instaloader.Profile.from_username = staticmethod(lambda c, u: FullProfile())
    try:
        res = igs.scrape_user(conn, Loader(), "target_user",
                              limit=5, with_media=False)
        check("status は ok", res["status"] == "ok", res["status"])
        check("message は None", res["message"] is None, str(res["message"]))
        acc = conn.execute(
            "SELECT * FROM accounts WHERE username='target_user'").fetchone()
        check("followers が保存される", acc["followers"] == 100, str(acc["followers"]))
        check("userid が保存される", acc["userid"] == 42, str(acc["userid"]))
    finally:
        instaloader.Profile.from_username = orig
        conn.close()




def test_rate_penalty_leak():
    """
    実機で踏んだやつ。
    `other`（web_profile_info）で 429 を食うと、その待機ペナルティが
    query_type に関係なく全クエリに波及する。
    フォールバック先の graphql まで巻き添えになるので、切り替え時に解除する。
    """
    print("\n[6] 429ペナルティの波及と解除")
    import time
    import instaloader

    L = instaloader.Instaloader(
        sleep=True, quiet=True,
        download_pictures=False, download_videos=False,
        download_video_thumbnails=False, save_metadata=False,
        rate_controller=igs.make_rate_controller(max_sleep=60),
    )
    rc = L.context._rate_controller

    orig_sleep = time.sleep
    time.sleep = lambda s: None
    try:
        # other で 429 を食った状態を作る
        now = time.monotonic()
        rc._query_timestamps['other'] = [now - i * 0.1 for i in range(200)]
        try:
            rc.handle_429('other')
        except igs.RateLimitAbort:
            pass

        # 別 query_type（graphql の doc_id）の待機時間を見る
        w_before = rc.query_waittime('7898261790222653', time.monotonic(), False)
        check("429後は別クエリ種別も待たされる（＝波及する）", w_before > 60,
              f"waittime={w_before:.0f}")

        igs.clear_rate_penalty(L.context)

        w_after = rc.query_waittime('7898261790222653', time.monotonic(), False)
        check("clear_rate_penalty で解除される", w_after == 0,
              f"waittime={w_after:.0f}")
        check("グローバル値がゼロに戻る",
              rc._earliest_next_request_time == 0.0
              and rc._iphone_earliest_next_request_time == 0.0)
    finally:
        time.sleep = orig_sleep


def test_posts_only():
    print("\n[7] posts_only は web_profile_info を叩かない")
    import instaloader

    dbfile = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = db.connect(dbfile)
    db.init_db(conn)

    class Loader:
        context = FakeContext()

    called = []

    def spy(context, username):
        called.append(username)
        raise AssertionError("from_username が呼ばれた")

    class Stub:
        username = "target_user"

        def get_posts(self):
            return iter([FakePost("PO01"), FakePost("PO02")])

    orig_from = instaloader.Profile.from_username
    orig_min = igs.minimal_profile
    instaloader.Profile.from_username = staticmethod(spy)
    igs.minimal_profile = lambda ctx, u: Stub()
    try:
        res = igs.scrape_user(conn, Loader(), "target_user", limit=5,
                              with_media=False, posts_only=True)
        check("from_username を一度も呼ばない", not called, str(called))
        check("ok_degraded で返る", res["status"] == "ok_degraded", res["status"])
        check("投稿が保存される", res["inserted"] == 2, str(res["inserted"]))
        acc = conn.execute(
            "SELECT * FROM accounts WHERE username='target_user'").fetchone()
        check("accounts に行ができる", acc is not None)
        check("userid は書かれない", acc is not None and acc["userid"] is None)
    finally:
        instaloader.Profile.from_username = orig_from
        igs.minimal_profile = orig_min
        conn.close()


def test_post_error_isolation():
    """
    実機で踏んだやつ。8件目の投稿が KeyError: 'id' を投げて
    バッチ全体が落ち、取得済みの7件も保存されなかった。
    1件の失敗が全体を壊さないことを確認する。
    """
    print("\n[8] 投稿単位のエラー隔離")
    import instaloader

    dbfile = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = db.connect(dbfile)
    db.init_db(conn)

    class Loader:
        context = FakeContext()

    class BadPost:
        """shortcode の時点で壊れている投稿（_safe でも救えないケース）。"""
        def __getattr__(self, name):
            raise KeyError("id")

    good1, good2 = FakePost("OK01"), FakePost("OK02")
    bad = BadPost()

    class Stub:
        username = "target_user"

        def get_posts(self):
            return iter([good1, bad, good2])

    orig_min = igs.minimal_profile
    igs.minimal_profile = lambda ctx, u: Stub()
    try:
        res = igs.scrape_user(conn, Loader(), "target_user", limit=10,
                              with_media=False, posts_only=True)
        rows = {r["shortcode"] for r in conn.execute("SELECT shortcode FROM posts")}
        check("正常な投稿は保存される", rows == {"OK01", "OK02"}, str(rows))
        check("壊れた投稿の後も処理が続く", "OK02" in rows)
        check("status は失敗にならない",
              res["status"] in ("ok", "ok_degraded"), res["status"])
        check("スキップ件数が報告される",
              res["message"] and "スキップ" in res["message"], str(res["message"]))
        check("スキップの記録が残る",
              res["message"] and "1件スキップ" in res["message"], str(res["message"]))
    finally:
        igs.minimal_profile = orig_min
        conn.close()


def test_location_opt_in():
    """
    location は参照すると instaloader が追加の HTTP リクエストを飛ばす。
    既定では触らないこと。
    """
    print("\n[9] location は既定で参照しない")

    touched = []

    class LocPost(FakePost):
        @property
        def location(self):
            touched.append(1)
            raise KeyError("id")   # 実機で出たのと同じ例外

        @location.setter
        def location(self, value):
            pass                   # FakePost.__init__ の代入を無視する

    p = LocPost("LOC1")

    rec = igs.post_to_record(p, with_location=False)
    check("既定では location を参照しない", not touched, f"touched {len(touched)}")
    check("location は None", rec["location"] is None)
    check("他のフィールドは取れている", rec["shortcode"] == "LOC1")

    rec2 = igs.post_to_record(p, with_location=True)
    # FakePost は _node を持たないので、有効化時のみプロパティ経由を試す
    check("有効化すると参照する", len(touched) == 1, f"touched {len(touched)}")
    check("参照して失敗しても record は作れる", rec2["location"] is None)
    check("失敗しても他フィールドは無事", rec2["likes"] == 5)


def test_safe_helper():
    print("\n[10] _safe のふるまい")
    check("正常値を返す", igs._safe(lambda: 42) == 42)
    check("KeyError を既定値に", igs._safe(lambda: {}["x"], "d") == "d")
    check("AttributeError を既定値に", igs._safe(lambda: None.foo, "d") == "d")
    check("TypeError を既定値に", igs._safe(lambda: 1 + "x", "d") == "d")
    check("IndexError を既定値に", igs._safe(lambda: [][0], "d") == "d")
    check("既定値なしなら None", igs._safe(lambda: {}["x"]) is None)


def test_error_path_reports_inserted():
    """
    v2.6以前: 取得中に例外が出たとき、保存はしているのに inserted=0 と
    報告していた。データが失われたと誤解する原因になる。
    """
    print("\n[11] エラー中断時も実際の保存件数を報告する")

    dbfile = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = db.connect(dbfile)
    db.init_db(conn)

    class Loader:
        context = FakeContext()

    def gen():
        yield FakePost("E001")
        yield FakePost("E002")
        raise RuntimeError("イテレーション中に壊れた")

    class Stub:
        username = "target_user"

        def get_posts(self):
            return gen()

    orig_min = igs.minimal_profile
    igs.minimal_profile = lambda ctx, u: Stub()
    try:
        res = igs.scrape_user(conn, Loader(), "target_user", limit=10,
                              with_media=False, posts_only=True)
        rows = {r["shortcode"] for r in conn.execute("SELECT shortcode FROM posts")}
        check("中断前の投稿は保存されている", rows == {"E001", "E002"}, str(rows))
        check("inserted が実件数を報告する", res["inserted"] == 2,
              f"inserted={res['inserted']} なのに実際は {len(rows)} 件")
        check("fetched も実件数", res["fetched"] == 2, str(res["fetched"]))
        check("status は error", res["status"] == "error", res["status"])
    finally:
        igs.minimal_profile = orig_min
        conn.close()


def test_location_from_raw_node():
    """
    実データで確定した挙動:
      iphone_struct.location のキーは lat/lng/name/pk/profile_pic_url。
      'id' が無いので instaloader の Post.location は KeyError: 'id' を投げる。
      生ノードから name を直接読めば追加リクエストも例外も無い。
    """
    print("\n[12] location を生ノードから読む")

    touched = []

    class NodePost(FakePost):
        def __init__(self, sc, node):
            super().__init__(sc)
            self._node = node

        @property
        def location(self):
            touched.append(1)
            raise KeyError("id")   # instaloader の実挙動

        @location.setter
        def location(self, v):
            pass

    # 実データと同じ形（id が無く pk がある）
    real_node = {
        "shortcode": "GEO1",
        "iphone_struct": {
            "location": {"lat": 35.6, "lng": 139.7, "name": "Tokyo Tower",
                         "pk": 12345, "profile_pic_url": "u"},
        },
    }
    p = NodePost("GEO1", real_node)
    rec = igs.post_to_record(p)
    check("生ノードから地名が取れる", rec["location"] == "Tokyo Tower",
          str(rec["location"]))
    check("Post.location を触らない（追加リクエストなし）", not touched,
          f"touched {len(touched)}")

    # location が無い投稿
    p2 = NodePost("GEO2", {"shortcode": "GEO2", "iphone_struct": {}})
    check("location が無ければ None", igs.post_to_record(p2)["location"] is None)
    check("それでも例外は出ない", igs.post_to_record(p2)["shortcode"] == "GEO2")

    # 旧形式（node直下の location）
    p3 = NodePost("GEO3", {"shortcode": "GEO3",
                           "location": {"id": 1, "name": "Osaka"}})
    check("旧形式の location も読める",
          igs.post_to_record(p3)["location"] == "Osaka",
          str(igs.post_to_record(p3)["location"]))

    # 生ノードに無い + FETCH_LOCATION=True ならプロパティ経由を試す
    touched.clear()
    p4 = NodePost("GEO4", {"shortcode": "GEO4"})
    rec4 = igs.post_to_record(p4, with_location=True)
    check("有効化時のみプロパティ経由を試す", len(touched) == 1,
          f"touched {len(touched)}")
    check("プロパティが落ちても record は作れる", rec4["location"] is None)


def test_media_session_headers():
    """
    実機で踏んだやつ。ログイン済みセッションを CDN に使い回すと
    `Host: www.instagram.com` が送られて **404** になる。
    """
    print("\n[13] メディア取得は匿名セッションを使う")
    import instaloader

    L = instaloader.Instaloader(quiet=True, sleep=True,
                                download_pictures=False, download_videos=False,
                                download_video_thumbnails=False,
                                save_metadata=False)

    # ログイン済みセッションに載るヘッダ（_default_http_header の既定形）
    full = L.context._default_http_header()
    check("既定ヘッダには Host が入っている（これが CDN で 404 を招く）",
          "Host" in full, str(sorted(full)))
    check("empty_session_only では Host が落ちる",
          "Host" not in L.context._default_http_header(empty_session_only=True))

    sess = igs.media_session(L)
    h = {k.lower() for k in sess.headers}
    check("匿名セッションに Host は無い", "host" not in h, str(sorted(h)))
    check("Origin も無い", "origin" not in h, str(sorted(h)))
    check("X-Instagram-AJAX も無い", "x-instagram-ajax" not in h)
    check("X-Requested-With も無い", "x-requested-with" not in h)
    check("User-Agent はある", "user-agent" in h)

    # loader 無しでも壊れない
    bare = igs.media_session()
    hb = {k.lower() for k in bare.headers}
    check("loader なしでも生成できる", bare is not None)
    check("loader なしでも Host は付けない", "host" not in hb, str(sorted(hb)))


def test_download_media_uses_given_session():
    print("\n[14] download_media のセッション取り扱い")
    import tempfile as tf

    dbfile = os.path.join(tf.mkdtemp(), "t.db")
    conn = db.connect(dbfile)
    db.init_db(conn)

    cache = tf.mkdtemp()
    orig_cache = igs.CACHE_DIR
    igs.CACHE_DIR = cache

    used = []

    class FakeResp:
        status_code = 200
        content = b"\xff\xd8\xfffake-jpeg-bytes"

        def raise_for_status(self):
            pass

    class FakeSess:
        headers = {}

        def get(self, url, **kw):
            used.append(url)
            return FakeResp()

    try:
        media = [{"index": 0, "is_video": False,
                  "image_url": "https://scontent.example/a.jpg", "video_url": None},
                 {"index": 1, "is_video": False,
                  "image_url": "https://scontent.example/b.jpg", "video_url": None}]
        saved = igs.download_media(conn, "CARO1", media, session_obj=FakeSess())
        check("渡したセッションが使われる", len(used) == 2, str(len(used)))
        # 2枚目は sha256 が同じなので既存ファイルを使い回す = 新規書き込みは1件
        check("重複分はファイルを書かない", saved == 1, str(saved))

        rows = db.media_for(conn, "CARO1")
        check("media_index に2件", len(rows) == 2, str(len(rows)))
        check("sha256 が埋まる", all(r["sha256"] for r in rows))
        check("同一内容は同じ sha256（重複排除の前提）",
              rows[0]["sha256"] == rows[1]["sha256"])
        check("2件目は既存ファイルを使い回す",
              rows[1]["local_path"] == rows[0]["local_path"],
              f"{rows[0]['local_path']} vs {rows[1]['local_path']}")
        check("bytes が記録される", all(r["bytes"] for r in rows))

        # 404 は例外を投げず記録だけ残す
        class Sess404:
            headers = {}

            def get(self, url, **kw):
                import requests
                r = FakeResp()
                r.status_code = 404
                def boom():
                    raise requests.HTTPError("404 Client Error")
                r.raise_for_status = boom
                return r

        igs.download_media(conn, "MISS1",
                           [{"index": 0, "is_video": False,
                             "image_url": "https://scontent.example/x.jpg",
                             "video_url": None}],
                           session_obj=Sess404())
        miss = db.media_for(conn, "MISS1")
        check("404 でも media_index に行は残る", len(miss) == 1)
        check("404 なら local_path は空", miss[0]["local_path"] is None)
        conn.commit()
    finally:
        igs.CACHE_DIR = orig_cache
        conn.close()


def test_owner_from_node():
    """
    web_profile_info が 429 でも、投稿データからアイコン/表示名は取れる。
    instaloader の Profile.from_iphone_struct が読んでいるのと同じ辞書。
    """
    print("\n[15] 投稿データからオーナー情報を拾う")

    class P:
        def __init__(self, node): self._node = node

    full = P({"iphone_struct": {"user": {
        "pk": 12345, "username": "TargetUser", "full_name": "Target 太郎",
        "profile_pic_url": "https://cdn.example/pic.jpg",
        "hd_profile_pic_url_info": {"url": "https://cdn.example/pic_hd.jpg"},
    }}})
    r = igs.owner_from_node(full)
    check("username は小文字化", r["username"] == "targetuser", str(r))
    check("full_name が取れる", r["full_name"] == "Target 太郎")
    check("HD版を優先する",
          r["profile_pic_url"] == "https://cdn.example/pic_hd.jpg",
          r["profile_pic_url"])
    check("userid が取れる", r["userid"] == 12345)

    # HD が無ければ通常版
    plain = P({"iphone_struct": {"user": {
        "pk": 1, "username": "u", "full_name": "",
        "profile_pic_url": "https://cdn.example/p.jpg"}}})
    r2 = igs.owner_from_node(plain)
    check("HDが無ければ通常版", r2["profile_pic_url"] == "https://cdn.example/p.jpg")
    check("full_name が空文字なら None", r2["full_name"] is None, str(r2["full_name"]))

    # 壊れた/欠けたノード
    check("iphone_struct が無ければ None", igs.owner_from_node(P({})) is None)
    check("user が無ければ None",
          igs.owner_from_node(P({"iphone_struct": {}})) is None)
    check("username が無ければ None",
          igs.owner_from_node(P({"iphone_struct": {"user": {"pk": 1}}})) is None)
    check("_node が無くても落ちない", igs.owner_from_node(object()) is None)


def test_merge_account_keeps_existing():
    print("\n[16] merge_account は既存値を潰さない")
    dbfile = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = db.connect(dbfile)
    db.init_db(conn)

    db.upsert_account(conn, {
        "username": "u1", "userid": 9, "full_name": "Old Name",
        "biography": "bio", "profile_pic_url": "old.jpg",
        "followers": 500, "mediacount": 20,
    })
    conn.commit()

    # 投稿から拾える範囲だけ更新
    db.merge_account(conn, {"username": "u1", "full_name": "New Name",
                            "profile_pic_url": "new.jpg",
                            "profile_pic_local": "/data/cache/_avatars/u1.jpg"})
    r = conn.execute("SELECT * FROM accounts WHERE username='u1'").fetchone()
    check("full_name が更新される", r["full_name"] == "New Name", r["full_name"])
    check("アイコンURLが更新される", r["profile_pic_url"] == "new.jpg")
    check("ローカルパスが入る",
          r["profile_pic_local"] == "/data/cache/_avatars/u1.jpg")
    check("followers は保持される", r["followers"] == 500, str(r["followers"]))
    check("biography は保持される", r["biography"] == "bio")

    # None は既存を潰さない
    db.merge_account(conn, {"username": "u1", "full_name": None,
                            "profile_pic_url": None})
    r2 = conn.execute("SELECT * FROM accounts WHERE username='u1'").fetchone()
    check("None は上書きしない", r2["full_name"] == "New Name", r2["full_name"])

    # 未登録なら作られる（共同投稿の相手など）
    db.merge_account(conn, {"username": "collab", "full_name": "Collab"})
    n = conn.execute(
        "SELECT COUNT(*) c FROM accounts WHERE username='collab'").fetchone()["c"]
    check("未登録アカウントも作られる", n == 1)
    check("username 無しは無視", (db.merge_account(conn, {}) or True))

    conn.commit(); conn.close()


def test_save_avatar():
    print("\n[17] アイコンのキャッシュ保存")
    import tempfile as tf
    cache = tf.mkdtemp()
    orig = igs.CACHE_DIR
    igs.CACHE_DIR = cache
    try:
        class Resp:
            status_code = 200
            content = b"\xff\xd8\xffavatar"
        calls = []

        class Sess:
            def get(self, url, **kw):
                calls.append(url)
                return Resp()

        p = igs.save_avatar("someone", "https://cdn.example/a.jpg", session_obj=Sess())
        check("保存される", p and os.path.exists(p), str(p))
        check("_avatars 配下に置く", "_avatars" in p, str(p))
        check("1回取得した", len(calls) == 1)

        # 2回目はキャッシュを使う
        igs.save_avatar("someone", "https://cdn.example/a.jpg", session_obj=Sess())
        p2 = igs.save_avatar("someone", "https://cdn.example/a.jpg", session_obj=Sess())
        check("新しいうちは再取得しない", len(calls) == 1, f"calls={len(calls)}")

        # 古くなったら取り直す
        old = time.time() - 30 * 86400
        os.utime(p, (old, old))
        igs.save_avatar("someone", "https://cdn.example/a.jpg", session_obj=Sess())
        check("期限切れなら取り直す", len(calls) == 2, f"calls={len(calls)}")

        check("URLが無ければ None", igs.save_avatar("x", None) is None)

        # 取得失敗しても既存があれば残す
        class Fail:
            def get(self, url, **kw):
                raise RuntimeError("network down")
        keep = igs.save_avatar("someone", "https://cdn.example/a.jpg", session_obj=Fail())
        check("失敗しても既存パスを返す", keep == p, str(keep))
        check("既存ファイルは消えない", os.path.exists(p))
    finally:
        igs.CACHE_DIR = orig


def test_avatars_survive_cleanup():
    print("\n[18] アイコンはキャッシュ削除で消えない")
    import tempfile as tf
    import cache_utils

    cache = tf.mkdtemp()
    dbfile = os.path.join(tf.mkdtemp(), "t.db")
    conn = db.connect(dbfile); db.init_db(conn)

    orig_c, orig_cu, orig_av = igs.CACHE_DIR, cache_utils.CACHE_DIR, cache_utils.AVATAR_DIR
    igs.CACHE_DIR = cache
    cache_utils.CACHE_DIR = cache
    cache_utils.AVATAR_DIR = os.path.join(cache, "_avatars")
    try:
        av = os.path.join(cache, "_avatars"); os.makedirs(av, exist_ok=True)
        avatar = os.path.join(av, "someone.jpg")
        open(avatar, "wb").write(b"a" * 100)

        normal = os.path.join(cache, "AB", "X_0.jpg")
        os.makedirs(os.path.dirname(normal), exist_ok=True)
        open(normal, "wb").write(b"b" * 100)

        old = time.time() - 90 * 86400
        for f in (avatar, normal):
            os.utime(f, (old, old))

        res = cache_utils.cleanup_cache(conn, days=0)
        check("通常のキャッシュは消える", not os.path.exists(normal))
        check("アイコンは残る", os.path.exists(avatar), avatar)
        check("kept に数えられる", res["kept"] >= 1, str(res))
    finally:
        igs.CACHE_DIR = orig_c
        cache_utils.CACHE_DIR = orig_cu
        cache_utils.AVATAR_DIR = orig_av
        conn.close()


def test_mute_blocks_scraping():
    """ミュート中のアカウントは取得しない（要件そのもの）。"""
    print("\n[19] ミュートは取得をブロックする")
    dbfile = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = db.connect(dbfile)
    db.init_db(conn)

    class Loader:
        context = FakeContext()

    fetched = []

    class Stub:
        username = "muted_user"
        def get_posts(self):
            fetched.append(1)
            return iter([FakePost("M001", owner="muted_user")])

    orig_min = igs.minimal_profile
    igs.minimal_profile = lambda ctx, u: Stub()
    try:
        db.set_mute(conn, "muted_user", True, "テスト")
        conn.commit()

        res = igs.scrape_user(conn, Loader(), "muted_user", limit=5,
                              with_media=False, posts_only=True)
        check("status が muted", res["status"] == "muted", res["status"])
        check("get_posts を呼ばない", not fetched, f"called {len(fetched)}")
        check("投稿が保存されない",
              conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"] == 0)

        # 大文字で指定してもブロックされる
        res2 = igs.scrape_user(conn, Loader(), "Muted_User", limit=5,
                               with_media=False, posts_only=True)
        check("大文字指定でもブロック", res2["status"] == "muted", res2["status"])

        # 解除すれば取れる
        db.set_mute(conn, "muted_user", False)
        conn.commit()
        res3 = igs.scrape_user(conn, Loader(), "muted_user", limit=5,
                               with_media=False, posts_only=True)
        check("解除すれば取得できる",
              res3["status"] in ("ok", "ok_degraded"), res3["status"])
        check("投稿が保存される",
              conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"] == 1)
    finally:
        igs.minimal_profile = orig_min
        conn.close()


def test_mute_helpers():
    print("\n[20] ミュートのDBヘルパ")
    dbfile = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = db.connect(dbfile)
    db.init_db(conn)

    db.upsert_account(conn, {"username": "a1", "userid": 1, "full_name": "A",
                             "biography": None, "profile_pic_url": None,
                             "followers": 10, "mediacount": 1})
    db.ensure_account(conn, "a2")
    conn.commit()

    check("初期は巡回対象",
          set(db.enabled_accounts(conn)) == {"a1", "a2"},
          str(db.enabled_accounts(conn)))

    db.set_mute(conn, "a1", True, "うるさい")
    conn.commit()
    check("ミュートは巡回対象から外れる",
          db.enabled_accounts(conn) == ["a2"], str(db.enabled_accounts(conn)))
    check("muted_usernames に入る", db.muted_usernames(conn) == {"a1"})

    r = conn.execute("SELECT * FROM accounts WHERE username='a1'").fetchone()
    check("muted_at が入る", bool(r["muted_at"]))
    check("理由が入る", r["mute_reason"] == "うるさい")
    check("followers は保持される", r["followers"] == 10)
    check("is_enabled は触らない", r["is_enabled"] == 1)

    lst = db.muted_accounts(conn)
    check("一覧に出る", len(lst) == 1 and lst[0]["username"] == "a1", str(lst))

    db.set_mute(conn, "a1", False)
    conn.commit()
    r2 = conn.execute("SELECT * FROM accounts WHERE username='a1'").fetchone()
    check("解除で muted_at が消える", r2["muted_at"] is None)
    check("解除で理由も消える", r2["mute_reason"] is None)
    check("巡回対象に戻る", set(db.enabled_accounts(conn)) == {"a1", "a2"})

    # 未登録でもミュートできる（共同投稿の相手など）
    db.set_mute(conn, "NewGuy", True)
    conn.commit()
    check("未登録アカウントもミュートできる", "newguy" in db.muted_usernames(conn),
          str(db.muted_usernames(conn)))
    check("空文字は拒否", db.set_mute(conn, "", True) is False)

    conn.close()


def test_short_transactions():
    """
    メディアDL中に書き込みロックを握り続けないこと。
    握ったままだと別プロセスが "database is locked" で落ちる（実機で発生）。
    """
    print("\n[21] メディアDL中もロックを保持しない")
    import sqlite3 as _sq
    import tempfile as tf

    dbfile = os.path.join(tf.mkdtemp(), "t.db")
    cache = tf.mkdtemp()
    orig_cache = igs.CACHE_DIR
    igs.CACHE_DIR = cache

    conn = db.connect(dbfile)
    db.init_db(conn)

    # 別プロセス相当の接続（短いタイムアウトで、待たされたら即失敗する）
    other = _sq.connect(dbfile, timeout=0.3)
    other.row_factory = _sq.Row

    class Loader:
        context = FakeContext()

    posts = [FakePost(f"T{i:03d}") for i in range(3)]

    class Stub:
        username = "target_user"
        def get_posts(self):
            return iter(posts)

    class Resp:
        status_code = 200
        content = b"\xff\xd8\xffx"
        def raise_for_status(self): pass

    blocked = []

    class Sess:
        headers = {}
        def get(self, url, **kw):
            # メディア取得のたびに、別接続から書き込めるか試す
            try:
                other.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('probe','1')")
                other.commit()
            except _sq.OperationalError as e:
                blocked.append(str(e))
            return Resp()

    # まずこの検査自体がロックを検出できることを確かめる。
    # 検出力の無いテストは通っても意味がない。
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('hold','1')")
    detected = False
    try:
        other.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('probe','0')")
        other.commit()
    except _sq.OperationalError:
        detected = True
    conn.commit()
    check("検査自体がロックを検出できる（対照）", detected,
          "ロックを握っても検出されない = このテストは無意味")

    orig_min = igs.minimal_profile
    orig_sess = igs.media_session
    igs.minimal_profile = lambda ctx, u: Stub()
    igs.media_session = lambda loader=None: Sess()
    orig_nap = igs._nap
    igs._nap = lambda: None
    try:
        res = igs.scrape_user(conn, Loader(), "target_user", limit=5,
                              with_media=True, posts_only=True)
        check("取得は成功する", res["status"] in ("ok", "ok_degraded"), res["status"])
        check("メディアDL中に他接続がブロックされない",
              not blocked, f"{len(blocked)}回ブロック: {blocked[:1]}")

        n = conn.execute("SELECT COUNT(*) c FROM media_index").fetchone()["c"]
        check("メディアが記録される", n == 3, str(n))
    finally:
        igs.minimal_profile = orig_min
        igs.media_session = orig_sess
        igs._nap = orig_nap
        igs.CACHE_DIR = orig_cache
        other.close(); conn.close()


def test_busy_timeout():
    print("\n[22] busy_timeout が設定される")
    import tempfile as tf
    dbfile = os.path.join(tf.mkdtemp(), "t.db")
    c = db.connect(dbfile)
    v = c.execute("PRAGMA busy_timeout").fetchone()[0]
    check("busy_timeout が既定値以上", v >= 60000, str(v))
    check("WAL モード",
          c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
    c.close()


def main():
    test_minimal_profile_shape()
    test_get_posts_uses_username_path()
    test_ensure_account_does_not_clobber()
    test_scrape_user_fallback()
    test_normal_path_unchanged()
    test_rate_penalty_leak()
    test_posts_only()
    test_post_error_isolation()
    test_location_opt_in()
    test_safe_helper()
    test_error_path_reports_inserted()
    test_owner_from_node()
    test_merge_account_keeps_existing()
    test_save_avatar()
    test_avatars_survive_cleanup()
    test_mute_blocks_scraping()
    test_mute_helpers()
    test_short_transactions()
    test_busy_timeout()
    test_media_session_headers()
    test_download_media_uses_given_session()
    test_location_from_raw_node()

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
