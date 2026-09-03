"""
ig-ray / dev/tumblr_web_check.py

Tumblr 投稿の **Web層**（エンドポイントとUI）の検証。外部ネットワークは使わない。

    python3 dev/tumblr_web_check.py

`dev/tumblr_check.py` が `tumblr_client` 単体（OAuth署名・multipart）を見るのに対し、
こちらは Flask のルートとテンプレートを見る。

旧 `dev/share_check.py` の後継。シェアツール方式（一時公開URL・`/share`・
`share_tokens`）は撤去したので、それらのテストは丸ごと落とし、
**マクロの `with context`** と **実ボタンの数** の検査だけ引き継いでいる。

とくに見ているのは:
  - `t` ボタンが「ローカル実体のある静止画がある投稿」にだけ出ること
  - **実ボタンの数**（`'openTumblrShare' in html` はJS定義にマッチして通る）
  - マクロを `with context` で import しているか（欠けるとボタンが消える）
  - 未設定なら機能まるごと無効になること
  - **モーダルが参照するIDが全部テンプレートに存在すること**
    （HTMLだけ直してJSが追随せず、開いた瞬間に落ちる事故を防ぐ）
"""

import importlib
import json
import os
import re
import sys
import tempfile
import threading

from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  {mark}  {name}{('  -- ' + detail) if detail and not cond else ''}")


# --------------------------------------------------------------------------

def build_env(tmp, key="ck", secret="cs", accounts=True):
    """環境変数を立て直してから web を読み込む（モジュール定数を作り直す）。"""
    cache = os.path.join(tmp, "cache")
    os.makedirs(os.path.join(cache, "AB"), exist_ok=True)
    os.environ["IG_RAY_DB"] = os.path.join(tmp, "t.db")
    os.environ["IG_RAY_CACHE"] = cache
    os.environ["TUMBLR_CONSUMER_KEY"] = key
    os.environ["TUMBLR_CONSUMER_SECRET"] = secret

    path = os.path.join(tmp, "tumblr_accounts.json")
    if accounts:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"accounts": [
                {"label": "main", "blog": "myblog", "token": "t", "secret": "s"},
                {"label": "sub", "blog": "subblog", "token": "t2", "secret": "s2"},
            ]}, f)
    elif os.path.exists(path):
        os.remove(path)
    os.environ["TUMBLR_ACCOUNTS_FILE"] = path

    import config
    importlib.reload(config)
    import db
    importlib.reload(db)
    import cache_utils
    importlib.reload(cache_utils)
    # web より先に読み直す（web が import 時のモジュール定数を見るため）
    import tumblr_client
    importlib.reload(tumblr_client)
    import web
    importlib.reload(web)
    web.app.testing = True
    return db, web, cache


def seed(db, cache):
    """
    投稿を3件。
      LOCAL1 … ローカル画像2枚（投稿できる）
      REMOTE … ローカル実体なし（投稿できない）
      VIDONLY… 動画1件のみ（投稿できない）
    """
    conn = db.connect()
    db.init_db(conn)
    conn.execute("INSERT INTO accounts (username) VALUES ('alpha')")

    def mkfile(rel):
        p = os.path.join(cache, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"x" * 200)
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

    mkpost("LOCAL1",
           [{"index": 0, "is_video": False, "image_url": "https://cdn.example/0.jpg"},
            {"index": 1, "is_video": False, "image_url": "https://cdn.example/1.jpg"}],
           [(0, mkfile("AB/LOCAL1_0.jpg"), False),
            (1, mkfile("AB/LOCAL1_1.jpg"), False)])
    mkpost("REMOTE",
           [{"index": 0, "is_video": False, "image_url": "https://cdn.example/r.jpg"}],
           [(0, None, False)])
    mkpost("VIDONLY",
           [{"index": 0, "is_video": True, "image_url": "https://cdn.example/v.jpg"}],
           [(0, mkfile("AB/VIDONLY_0.jpg"), True)])
    conn.commit()
    conn.close()


def start_mock():
    """投稿先の Tumblr API を差し替える。"""
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            hits.append({"path": self.path, "body": self.rfile.read(n)})
            raw = json.dumps({"response": {"id_string": "999"}}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits


# --------------------------------------------------------------------------

def test_accounts(cl):
    print("\n[1] /api/tumblr/accounts")
    r = cl.get("/api/tumblr/accounts")
    check("200 で返る", r.status_code == 200, str(r.status_code))
    d = r.get_json()
    check("enabled=True", d.get("enabled") is True, str(d))
    check("2件返る", len(d.get("accounts") or []) == 2, str(d))
    # **ここが漏れると投稿権限そのものが漏れる**
    blob = json.dumps(d)
    check("トークンを返さない", "token" not in blob and '"secret"' not in blob, blob)


def test_post(cl, hits):
    print("\n[2] /api/tumblr/post")
    r = cl.post("/api/tumblr/post", json={"shortcode": "LOCAL1", "account": "main"})
    check("200 で返る", r.status_code == 200,
          f"{r.status_code} {r.get_json()}")
    d = r.get_json()
    check("ok=True", d.get("ok") is True, str(d))
    check("枚数を返す", d.get("count") == 2, str(d.get("count")))
    check("投稿URLを返す", "999" in (d.get("post_url") or ""), str(d))
    check("投稿先ラベルを返す", d.get("label") == "main", str(d.get("label")))

    body = hits[-1]["body"]
    check("正しいブログへ", hits[-1]["path"].endswith("/blog/myblog/post"),
          hits[-1]["path"])
    check("2枚とも送っている",
          b'name="data[0]"' in body and b'name="data[1]"' in body)
    check("出典が付く", b"instagram.com/p/LOCAL1/" in body)

    # 投稿先を切り替えられる
    cl.post("/api/tumblr/post", json={"shortcode": "LOCAL1", "account": "sub"})
    check("別アカウントへ投稿できる",
          hits[-1]["path"].endswith("/blog/subblog/post"), hits[-1]["path"])


def test_indices(cl, hits):
    print("\n[3] 添付する画像の絞り込み")
    r = cl.post("/api/tumblr/post", json={"shortcode": "LOCAL1", "indices": "1"})
    check("indices で絞れる", r.get_json().get("count") == 1, str(r.get_json()))
    body = hits[-1]["body"]
    check("1枚だけ送る",
          b'name="data[0]"' in body and b'name="data[1]"' not in body)

    r = cl.post("/api/tumblr/post", json={"shortcode": "LOCAL1", "indices": "0,1"})
    check("全部指定でも通る", r.get_json().get("count") == 2, str(r.get_json()))

    r = cl.post("/api/tumblr/post", json={"shortcode": "LOCAL1", "indices": "x"})
    check("不正な indices は400", r.status_code == 400, str(r.status_code))


def test_state(cl, hits):
    print("\n[4] 公開状態")
    cl.post("/api/tumblr/post", json={"shortcode": "LOCAL1", "state": "draft"})
    check("draft を送れる", b"draft" in hits[-1]["body"])
    cl.post("/api/tumblr/post", json={"shortcode": "LOCAL1"})
    check("既定は published", b"published" in hits[-1]["body"])
    r = cl.post("/api/tumblr/post", json={"shortcode": "LOCAL1", "state": "bogus"})
    check("不正な state は400", r.status_code == 400, str(r.status_code))


def test_errors(cl):
    print("\n[5] 異常系")
    check("ローカル画像なしは400",
          cl.post("/api/tumblr/post", json={"shortcode": "REMOTE"}).status_code == 400)
    check("動画のみも400",
          cl.post("/api/tumblr/post", json={"shortcode": "VIDONLY"}).status_code == 400)
    check("存在しない投稿は400",
          cl.post("/api/tumblr/post", json={"shortcode": "NOPE"}).status_code == 400)
    check("shortcode なしは400",
          cl.post("/api/tumblr/post", json={}).status_code == 400)
    check("存在しないアカウントは400",
          cl.post("/api/tumblr/post",
                  json={"shortcode": "LOCAL1", "account": "nosuch"}).status_code == 400)


def test_buttons(cl):
    print("\n[6] 実ボタンが描画されているか")
    h = cl.get("/").get_data(as_text=True)

    # **ここが要点。** 'openTumblrShare' in html だとJS関数の定義にマッチして
    # ボタンが0個でも通ってしまう（X-Ray で実際に見逃した）。
    n = h.count('onclick="openTumblrShare')
    check("t ボタンが投稿の数だけ出る", n == 1, f"count={n}")
    check("LOCAL1 のボタンがある", "openTumblrShare('LOCAL1')" in h)
    check("REMOTE のボタンは出ない", "openTumblrShare('REMOTE')" not in h)
    check("VIDONLY のボタンは出ない", "openTumblrShare('VIDONLY')" not in h)
    check("モーダルが描画されている", 'id="tmb-dlg"' in h)
    check("カードに data-owner がある", 'data-owner="alpha"' in h)

    for path in ("/user/alpha", "/bookmarks", "/gallery"):
        r = cl.get(path)
        check(f"{path} が 200", r.status_code == 200, str(r.status_code))


def test_macro_with_context():
    print("\n[7] マクロの with context")
    # `{% from '_macros.html' import post_card %}` のままだと
    # context_processor で入れた share_enabled がマクロ内から見えず、
    # ボタンが一切描画されない（X-Ray で実際に踏んだ）。
    tpl = os.path.join(os.path.dirname(__file__), "..", "app", "templates")
    missing = [fn for fn in os.listdir(tpl)
               if fn.endswith(".html")
               and "import post_card" in open(os.path.join(tpl, fn),
                                              encoding="utf-8").read()
               and "with context" not in open(os.path.join(tpl, fn),
                                              encoding="utf-8").read()]
    check("post_card を import する全テンプレートに with context がある",
          not missing, str(missing))


def test_modal_ids(cl):
    print("\n[8] JSが触るIDがテンプレートに存在するか")
    # HTMLだけ直してJSが追随しないと、開いた瞬間に
    # `getElementById(...).style` が TypeError で落ちてモーダルが開かない。
    # 実際に一度この状態になった（2026-09）。
    h = cl.get("/").get_data(as_text=True)
    js = open(os.path.join(os.path.dirname(__file__), "..", "app", "templates",
                           "_scripts.html"), encoding="utf-8").read()

    # **両方向で突き合わせる。**
    # 「null チェックが無いものだけ」に絞ると、
    #   const note = getElementById('x'); if (note) note.style...
    # のような書き方を見逃す。実行時に落ちなくても、
    # IDのズレはHTMLとJSが食い違っている証拠なので落とす。
    used = set(re.findall(r"getElementById\(\s*['\"](tmb-[\w-]+)['\"]", js))
    ids = set(re.findall(r'id="(tmb-[\w-]+)"', h))

    missing = sorted(used - ids)
    check("JSが触るIDがテンプレートに全部ある", not missing, str(missing))

    # JS が触らなくてよいID（CSSセレクタや aria-labelledby の参照先）
    ok_without_js = {"tmb-dlg-title", "tmb-draft-wrap"}
    unused = sorted(ids - used - ok_without_js)
    check("テンプレートに使われないIDが残っていない", not unused, str(unused))


def test_no_share_tool(cl):
    print("\n[9] シェアツール方式が撤去されているか")
    js = open(os.path.join(os.path.dirname(__file__), "..", "app", "templates",
                           "_scripts.html"), encoding="utf-8").read()
    for dead in ("tmbOpenShareTool", "tmbSendAll", "/api/share/prepare",
                 "widgets/share/tool", "tmbApiMode"):
        check(f"JSに {dead} が残っていない", dead not in js)

    for path in ("/api/share/prepare", "/share/abc", "/share-img/abc/0"):
        r = cl.get(path)
        check(f"{path} が 404", r.status_code == 404, str(r.status_code))

    import db
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) c FROM sqlite_master "
                     "WHERE type='table' AND name='share_tokens'").fetchone()["c"]
    conn.close()
    check("share_tokens テーブルが無い", n == 0, str(n))


def test_disabled(tmp):
    print("\n[10] 未設定なら機能まるごと無効")
    db, web, cache = build_env(tmp, key="", secret="", accounts=False)
    seed(db, cache)
    cl = web.app.test_client()

    d = cl.get("/api/tumblr/accounts").get_json()
    check("enabled=False", d.get("enabled") is False, str(d))
    check("投稿は400で断る",
          cl.post("/api/tumblr/post", json={"shortcode": "LOCAL1"}).status_code == 400)

    h = cl.get("/").get_data(as_text=True)
    check("t ボタンが出ない", 'onclick="openTumblrShare' not in h)
    check("モーダルも出ない", 'id="tmb-dlg"' not in h)
    check("本体は普通に見える", cl.get("/").status_code == 200)


def main():
    tmp = tempfile.mkdtemp(prefix="igray_tmbweb_")
    print(f"test dir: {tmp}")

    srv, hits = start_mock()
    db, web, cache = build_env(tmp)
    seed(db, cache)
    import tumblr_client
    tumblr_client.API_BASE = f"http://127.0.0.1:{srv.server_port}/v2"
    cl = web.app.test_client()

    test_accounts(cl)
    test_post(cl, hits)
    test_indices(cl, hits)
    test_state(cl, hits)
    test_errors(cl)
    test_buttons(cl)
    test_macro_with_context()
    test_modal_ids(cl)
    test_no_share_tool(cl)
    srv.shutdown()

    test_disabled(tempfile.mkdtemp(prefix="igray_tmbweb_off_"))

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
