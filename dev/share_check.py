"""
ig-ray / dev/share_check.py

Tumblr 共有機能の検証。ネットワーク不要。

    python3 dev/share_check.py

X-Ray から移植した機能。**過去に一度この機能を削除している**ので、
同じ失敗を繰り返していないかをここで固定する。

固定していること:
  - `content` に渡す画像URLが**絶対URL**であること
    （Tumblr のサーバー側から取得されるので相対URLでは成立しない）
  - ローカルキャッシュの無い投稿は共有できない
    （Instagram CDN は Referer 制限があり Tumblr から取れない）
  - `local_path` のサブディレクトリが失われていないこと
    （ファイル名だけにすると配信できなくなる）
  - **公開ホスト宛に本体のルートが漏れていないこと**
  - TTL 経過後に 404 になること
  - **実ボタンの数**が投稿件数分あること
    （`'openTumblrShare' in html` はJS関数の定義にマッチして通ってしまう）
"""

import importlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

BASE = "https://share.example.com"     # RFC 2606 の予約ドメイン
HOST = "share.example.com"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  {mark}  {name}{('  -- ' + detail) if detail and not cond else ''}")


def build_env(tmp, base=BASE, host=HOST, ttl="60"):
    """環境変数を立て直してから web を読み込む（モジュール定数を作り直す）。"""
    cache = os.path.join(tmp, "cache")
    os.makedirs(os.path.join(cache, "AB"), exist_ok=True)
    os.environ["IG_RAY_DB"] = os.path.join(tmp, "t.db")
    os.environ["IG_RAY_CACHE"] = cache
    os.environ["IG_RAY_PUBLIC_SHARE_BASE_URL"] = base
    os.environ["IG_RAY_PUBLIC_SHARE_HOST"] = host
    os.environ["IG_RAY_SHARE_TOKEN_TTL_MIN"] = ttl

    import config
    importlib.reload(config)
    import db
    importlib.reload(db)
    import cache_utils
    importlib.reload(cache_utils)
    import web
    importlib.reload(web)
    web.app.testing = True
    return db, web, cache


def seed(db, cache):
    """
    投稿を3件。
      LOCAL1 … ローカル画像2枚（共有できる）
      REMOTE … ローカル実体なし（共有できない）
      VIDONLY… 動画1件のみ（共有できない）
    """
    conn = db.connect()
    db.init_db(conn)
    conn.execute("INSERT INTO accounts (username) VALUES ('alpha')")

    def mkfile(rel, content=b"\xff\xd8\xff" + b"x" * 200):
        p = os.path.join(cache, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(content)
        return p

    def mkpost(sc, media, rows):
        conn.execute(
            "INSERT INTO posts (shortcode, owner_username, date_utc, media_json) "
            "VALUES (?,?,?,?)",
            (sc, "alpha", "2026-05-01T00:00:00+00:00", json.dumps(media)))
        for idx, local, is_vid in rows:
            conn.execute(
                "INSERT INTO media_index (shortcode, media_index, local_path, is_video) "
                "VALUES (?,?,?,?)", (sc, idx, local, 1 if is_vid else 0))

    p0 = mkfile("AB/LOCAL1_0.jpg")
    p1 = mkfile("AB/LOCAL1_1.jpg")
    mkpost("LOCAL1",
           [{"index": 0, "is_video": False, "image_url": "https://cdn.example/0.jpg"},
            {"index": 1, "is_video": False, "image_url": "https://cdn.example/1.jpg"}],
           [(0, p0, False), (1, p1, False)])

    mkpost("REMOTE",
           [{"index": 0, "is_video": False, "image_url": "https://cdn.example/r.jpg"}],
           [(0, None, False)])

    pv = mkfile("AB/VIDONLY_0.jpg")
    mkpost("VIDONLY",
           [{"index": 0, "is_video": True, "image_url": "https://cdn.example/v.jpg"}],
           [(0, pv, True)])

    conn.commit()
    conn.close()
    return p0, p1


def prepare(cl, shortcode, caption="", tags=""):
    return cl.post("/api/share/prepare", json={
        "shortcode": shortcode, "caption": caption, "tags": tags})


def test_prepare(cl):
    print("\n[1] トークン発行")
    r = prepare(cl, "LOCAL1", caption="てすと", tags="a,b")
    check("200 で返る", r.status_code == 200, str(r.status_code))
    d = r.get_json()
    check("ok=True", d.get("ok") is True, str(d))
    check("share_url が返る", bool(d.get("share_url")))
    check("share_url は絶対URL", (d.get("share_url") or "").startswith(BASE))

    urls = d.get("image_urls") or []
    check("image_urls が2枚", len(urls) == 2, str(len(urls)))
    check("image_urls はすべて絶対URL",
          all(u.startswith("https://") for u in urls), str(urls))
    check("image_urls が公開ベースURL配下",
          all(u.startswith(BASE + "/share-img/") for u in urls), str(urls))
    check("連番が 0,1", urls[0].endswith("/0") and urls[1].endswith("/1"), str(urls))

    check("source_url が Instagram の元投稿",
          d.get("source_url") == "https://www.instagram.com/p/LOCAL1/",
          str(d.get("source_url")))
    check("expires_in_min が返る", d.get("expires_in_min") == 60,
          str(d.get("expires_in_min")))
    return d


def test_no_local(cl):
    print("\n[2] ローカル画像が無ければ共有できない")
    r = prepare(cl, "REMOTE")
    check("リモートのみは 400", r.status_code == 400, str(r.status_code))
    check("理由を返す", "共有" in (r.get_json() or {}).get("error", ""))

    r = prepare(cl, "VIDONLY")
    check("動画のみも 400（サムネは共有しない）", r.status_code == 400,
          str(r.status_code))

    r = prepare(cl, "NOSUCH")
    check("存在しない投稿も 400", r.status_code == 400, str(r.status_code))

    r = cl.post("/api/share/prepare", json={})
    check("shortcode 無しは 400", r.status_code == 400, str(r.status_code))


def test_share_page(cl, d):
    print("\n[3] /share/<token>（OGPプレビュー）")
    token = d["share_url"].rsplit("/", 1)[-1]
    r = cl.get(f"/share/{token}")
    check("200 で返る", r.status_code == 200, str(r.status_code))
    h = r.get_data(as_text=True)
    check("og:image がある", 'property="og:image"' in h)
    check("og:image が1枚目の絶対URL", f'content="{BASE}/share-img/{token}/0"' in h)
    check("キャプションが出る", "てすと" in h)
    check("出典リンクがある", "https://www.instagram.com/p/LOCAL1/" in h)
    check("noindex を付けている", "noindex" in h)

    check("無効なトークンは 404",
          cl.get("/share/deadbeefdeadbeef").status_code == 404)
    return token


def test_share_img(cl, token):
    print("\n[4] /share-img/<token>/<n>（Tumblr が叩く本命）")
    for n in (0, 1):
        r = cl.get(f"/share-img/{token}/{n}")
        check(f"{n}枚目が 200", r.status_code == 200, str(r.status_code))
        check(f"{n}枚目に中身がある", len(r.get_data()) > 100)

    check("範囲外は 404", cl.get(f"/share-img/{token}/2").status_code == 404)
    check("負の番号は 404（ルーティングで弾く）",
          cl.get(f"/share-img/{token}/-1").status_code == 404)
    check("無効トークンは 404",
          cl.get("/share-img/nosuchtoken/0").status_code == 404)


def test_subdirectory_preserved(db, token):
    print("\n[5] サブディレクトリが失われていないか")
    conn = db.connect()
    payload = db.get_share_payload(conn, token)
    conn.close()
    paths = payload.get("paths") or []
    check("payload にパスが入っている", len(paths) == 2, str(len(paths)))
    # ファイル名だけを保存すると配信できなくなる（X-Ray からの移植で踏みやすい）
    check("パスにサブディレクトリが残っている",
          all(os.sep + "AB" + os.sep in p for p in paths), str(paths))
    check("payload に caption が入っている", payload.get("caption") == "てすと")
    check("payload に source_url が入っている",
          payload.get("source_url") == "https://www.instagram.com/p/LOCAL1/")


def test_public_host_guard(cl, token):
    print("\n[6] 公開ホストのガード（最後の砦）")
    hdr = {"Host": HOST}
    for path in ("/", "/gallery", "/bookmarks", "/mutes", "/storage", "/backup"):
        r = cl.get(path, headers=hdr)
        check(f"公開ホスト経由の {path} は 404", r.status_code == 404,
              str(r.status_code))
    r = cl.get("/api/share/prepare", headers=hdr)
    check("公開ホスト経由の /api/... も 404", r.status_code == 404, str(r.status_code))
    r = cl.get("/cache/AB/LOCAL1_0.jpg", headers=hdr)
    check("公開ホスト経由の /cache も 404", r.status_code == 404, str(r.status_code))

    check("公開ホスト経由でも /share は 200",
          cl.get(f"/share/{token}", headers=hdr).status_code == 200)
    check("公開ホスト経由でも /share-img は 200",
          cl.get(f"/share-img/{token}/0", headers=hdr).status_code == 200)

    # ローカルホスト名では本体が見える
    check("ローカルアクセスでは / が 200",
          cl.get("/", headers={"Host": "localhost:8079"}).status_code == 200)


def test_buttons_rendered(cl):
    print("\n[7] 実ボタンが描画されているか")
    h = cl.get("/", headers={"Host": "localhost:8079"}).get_data(as_text=True)

    # **ここが要点。** 'openTumblrShare' in html だとJS関数の定義にマッチして
    # ボタンが0個でも通ってしまう（X-Ray で実際に見逃した）。
    n = h.count('onclick="openTumblrShare')
    check("t ボタンが1個以上ある", n >= 1, f"count={n}")
    # 共有できるのは LOCAL1 だけ（REMOTE と VIDONLY は出ない）
    check("共有可能な投稿の数だけ出る", n == 1, f"count={n}")
    check("LOCAL1 のボタンがある", "openTumblrShare('LOCAL1')" in h)
    check("REMOTE のボタンは出ない", "openTumblrShare('REMOTE')" not in h)
    check("VIDONLY のボタンは出ない", "openTumblrShare('VIDONLY')" not in h)
    check("共有モーダルが描画されている", 'id="tmb-dlg"' in h)

    # --- Tumblr の仕様変更対応（2026-08）---
    # 複数枚の自動添付が廃止された（1枚なら今もフェッチされる。tcpdumpで実測）。
    # 対応: モーダルで1枚選択 → content には選んだ1枚だけ渡す。
    check("1枚選択の変数がある", "tmbSelected" in h)
    check("複数枚の注記要素がある", 'id="tmb-multi-note"' in h)
    check("content に選んだ1枚だけ渡している",
          "params.set('content', pick)" in h)
    check("全枚数を join で渡す旧実装に戻っていない",
          "image_urls || []).join(',')" not in h.replace(
              "join(',') に戻すだけ", ""))
    check("復活時の戻し方がコメントに残っている", "join(',') に戻す" in h)

    # マクロの with context が効いているか（他ページでも同様）
    for path in ("/user/alpha", "/bookmarks"):
        r = cl.get(path, headers={"Host": "localhost:8079"})
        check(f"{path} が 200", r.status_code == 200, str(r.status_code))


def test_macro_with_context():
    print("\n[8] マクロの with context")
    # `{% from '_macros.html' import post_card %}` のままだと
    # context_processor で入れた share_enabled がマクロ内から見えず、
    # ボタンが一切描画されない（X-Ray で実際に踏んだ）。
    tpl_dir = os.path.join(os.path.dirname(__file__), "..", "app", "templates")
    missing = []
    for fn in os.listdir(tpl_dir):
        if not fn.endswith(".html"):
            continue
        text = open(os.path.join(tpl_dir, fn), encoding="utf-8").read()
        if "import post_card" in text and "with context" not in text:
            missing.append(fn)
    check("post_card を import する全テンプレートに with context がある",
          not missing, str(missing))


def test_disabled(tmp):
    print("\n[9] BASE_URL 未設定なら機能まるごと無効")
    db, web, cache = build_env(tmp, base="", host="")
    cl = web.app.test_client()

    check("SHARE_ENABLED が False", web.SHARE_ENABLED is False)
    r = cl.post("/api/share/prepare", json={"shortcode": "LOCAL1"})
    check("prepare が 404", r.status_code == 404, str(r.status_code))
    check("/share が 404", cl.get("/share/anything").status_code == 404)
    check("/share-img が 404", cl.get("/share-img/anything/0").status_code == 404)

    h = cl.get("/").get_data(as_text=True)
    check("t ボタンが出ない", 'onclick="openTumblrShare' not in h)
    check("モーダルも出ない", 'id="tmb-dlg"' not in h)
    check("本体は普通に見える", cl.get("/").status_code == 200)


def test_ttl(tmp):
    print("\n[10] TTL 経過後は 404")
    db, web, cache = build_env(tmp, ttl="0")     # 発行直後に失効する
    cl = web.app.test_client()
    r = prepare(cl, "LOCAL1")
    check("発行自体は成功する", r.status_code == 200, str(r.status_code))
    token = r.get_json()["share_url"].rsplit("/", 1)[-1]

    check("失効後の /share は 404", cl.get(f"/share/{token}").status_code == 404)
    check("失効後の /share-img は 404",
          cl.get(f"/share-img/{token}/0").status_code == 404)

    conn = db.connect()
    check("get_share_payload も None を返す",
          db.get_share_payload(conn, token) is None)
    n = db.purge_expired_share_tokens(conn)
    check("失効分を掃除できる", n >= 1, str(n))
    conn.close()


def main():
    tmp = tempfile.mkdtemp(prefix="igray_share_")
    print(f"test dir: {tmp}")

    db, web, cache = build_env(tmp)
    seed(db, cache)
    cl = web.app.test_client()

    d = test_prepare(cl)
    test_no_local(cl)
    token = test_share_page(cl, d)
    test_share_img(cl, token)
    test_subdirectory_preserved(db, token)
    test_public_host_guard(cl, token)
    test_buttons_rendered(cl)
    test_macro_with_context()

    # 環境を作り直す系は別ディレクトリで
    tmp2 = tempfile.mkdtemp(prefix="igray_share_off_")
    db2, web2, cache2 = build_env(tmp2, base="", host="")
    seed(db2, cache2)
    test_disabled(tmp2)

    tmp3 = tempfile.mkdtemp(prefix="igray_share_ttl_")
    db3, web3, cache3 = build_env(tmp3, ttl="0")
    seed(db3, cache3)
    test_ttl(tmp3)

    print(f"\n{'=' * 50}")
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("すべて通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
