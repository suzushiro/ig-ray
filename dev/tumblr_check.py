"""
ig-ray / dev/tumblr_check.py

Tumblr への直接投稿（OAuth1 API 方式）の検証。**外部ネットワークは使わない。**

    python3 dev/tumblr_check.py

**モックの Tumblr API サーバーを立てて実HTTPで検証する。**
`requests` をモンキーパッチすると「署名が付いているか」「multipart に
実バイナリが載っているか」が確認できず、本番で初めて落ちる。
`tumblr_client.API_BASE` を差し替えるだけでモックに向くので、
実際にHTTPを飛ばして受け側で中身を見る。

とくに見ているのは:
  - OAuth1 署名（Authorization: OAuth ... HMAC-SHA1）が付くこと
  - User-Agent が固定値であること（変動させるとアプリ停止の恐れ）
  - **複数枚が data[0] / data[1] … として実バイナリで載ること**
  - public_accounts() にトークンが混ざらないこと
  - 画像0枚・存在しないファイル・不正な state を弾くこと
  - APIエラー（4xx）を握り潰さないこと
"""

import importlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

PASS, FAIL = [], []
RECEIVED = []          # モックが受け取ったリクエストの記録


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  {mark}  {name}{('  -- ' + detail) if detail and not cond else ''}")


# --------------------------------------------------------------------------
# モックの Tumblr API
# --------------------------------------------------------------------------

class MockHandler(BaseHTTPRequestHandler):
    fail_next = False

    def _record(self, body=b""):
        RECEIVED.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })

    def do_GET(self):
        self._record()
        if self.path.endswith("/user/info"):
            self._json(200, {"response": {"user": {"blogs": [
                {"name": "myblog"}, {"name": "otherblog"}]}}})
        else:
            self._json(404, {"meta": {"msg": "Not Found"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        self._record(body)
        if MockHandler.fail_next:
            MockHandler.fail_next = False
            self._json(400, {"meta": {"status": 400, "msg": "Bad Request"},
                             "response": {"errors": ["画像が大きすぎます"]}})
            return
        self._json(201, {"response": {"id_string": "123456789"}})

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def start_mock():
    srv = HTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v2"


# --------------------------------------------------------------------------

def make_images(tmp, n=3):
    """サブディレクトリ付きで画像を作る（ig-ray のキャッシュ構造を模す）。"""
    out = []
    for i in range(n):
        d = os.path.join(tmp, "cache", "AB")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"SHORT1_{i}.jpg")
        with open(p, "wb") as f:
            f.write(b"\xff\xd8\xff" + bytes([i]) * 64)     # 中身を区別できるように
        out.append(p)
    return out


def setup_client(tmp, base):
    accounts = os.path.join(tmp, "tumblr_accounts.json")
    with open(accounts, "w", encoding="utf-8") as f:
        json.dump({"accounts": [
            {"label": "main", "blog": "myblog", "token": "tok1", "secret": "sec1"},
            {"label": "sub", "blog": "subblog", "token": "tok2", "secret": "sec2"},
        ]}, f)

    os.environ["TUMBLR_CONSUMER_KEY"] = "ckey"
    os.environ["TUMBLR_CONSUMER_SECRET"] = "csecret"
    os.environ["TUMBLR_ACCOUNTS_FILE"] = accounts

    import config
    importlib.reload(config)
    import tumblr_client
    importlib.reload(tumblr_client)
    tumblr_client.API_BASE = base
    return tumblr_client


def test_accounts(tc):
    print("\n[1] アカウントの読み書き")
    accs = tc.load_accounts()
    check("2件読める", len(accs) == 2, str(len(accs)))
    check("is_configured が True", tc.is_configured() is True)

    pub = tc.public_accounts()
    check("public_accounts が2件", len(pub) == 2)
    # **ここが漏れると投稿権限そのものが漏れる**
    blob = json.dumps(pub)
    check("public にトークンが含まれない",
          "tok1" not in blob and "sec1" not in blob, blob)
    check("public は label と blog だけ",
          set(pub[0].keys()) == {"label", "blog"}, str(pub[0].keys()))

    check("label で引ける", tc.find_account("sub")["blog"] == "subblog")
    check("blog名でも引ける", tc.find_account("myblog")["label"] == "main")
    check("未指定なら先頭", tc.find_account("")["label"] == "main")
    check("存在しないラベルは None", tc.find_account("nope") is None)


def test_verify(tc):
    print("\n[2] verify（トークン確認）")
    RECEIVED.clear()
    r = tc.verify(tc.find_account("main"))
    check("ok=True", r.get("ok") is True, str(r))
    check("ブログ一覧が返る", r.get("blogs") == ["myblog", "otherblog"], str(r))

    req = RECEIVED[-1]
    check("OAuth1 署名が付く",
          req["headers"].get("authorization", "").startswith("OAuth "),
          req["headers"].get("authorization", "")[:40])
    check("HMAC-SHA1 で署名している",
          "HMAC-SHA1" in req["headers"].get("authorization", ""))
    check("consumer key が載る", "ckey" in req["headers"].get("authorization", ""))
    check("User-Agent が固定値",
          req["headers"].get("user-agent") == "ig-ray-archiver/1.0",
          req["headers"].get("user-agent"))


def test_post_multi(tc, images):
    print("\n[3] 複数枚の投稿（API方式の本命）")
    RECEIVED.clear()
    r = tc.create_photo_post(
        tc.find_account("main"), images,
        caption="<a href='https://www.instagram.com/p/SHORT1/'>てすと</a>",
        tags=["photo", "テスト"], state="published",
        source_url="https://www.instagram.com/p/SHORT1/")

    check("ok=True", r.get("ok") is True, str(r))
    check("post id が返る", r.get("id") == "123456789", str(r.get("id")))
    check("post_url が組み立てられる",
          r.get("post_url") == "https://www.tumblr.com/myblog/123456789",
          str(r.get("post_url")))

    req = RECEIVED[-1]
    check("正しいブログのエンドポイントへ",
          req["path"].endswith("/blog/myblog/post"), req["path"])
    check("multipart で送っている",
          "multipart/form-data" in req["headers"].get("content-type", ""),
          req["headers"].get("content-type", ""))

    body = req["body"]
    # **枚数ぶんのフィールドが載っているか**
    for i in range(len(images)):
        check(f"data[{i}] がある", f'name="data[{i}]"'.encode() in body)
    check(f"data[{len(images)}] は無い（余分に送っていない）",
          f'name="data[{len(images)}]"'.encode() not in body)

    # **実バイナリが載っているか**（パスだけ送っていたら投稿は空になる）
    for i in range(len(images)):
        check(f"{i}枚目の実バイナリが載る",
              (b"\xff\xd8\xff" + bytes([i]) * 64) in body)

    check("type=photo", b'name="type"' in body and b"photo" in body)
    check("state が載る", b'name="state"' in body and b"published" in body)
    check("tags はカンマ区切り", "photo,テスト".encode() in body)
    check("caption が載る", "てすと".encode() in body)
    check("source_url が載る", b"instagram.com/p/SHORT1/" in body)


def test_post_single_and_subset(tc, images):
    print("\n[4] 1枚・部分指定")
    RECEIVED.clear()
    r = tc.create_photo_post(tc.find_account("sub"), images[:1], state="draft")
    check("1枚でも通る", r.get("ok") is True, str(r))
    check("指定したアカウントへ飛ぶ",
          RECEIVED[-1]["path"].endswith("/blog/subblog/post"),
          RECEIVED[-1]["path"])
    check("state=draft が載る", b"draft" in RECEIVED[-1]["body"])
    check("blog が結果に入る", r.get("blog") == "subblog", str(r.get("blog")))

    RECEIVED.clear()
    tc.create_photo_post(tc.find_account("main"), [images[0], images[2]])
    body = RECEIVED[-1]["body"]
    check("2枚に絞れる",
          b'name="data[0]"' in body and b'name="data[1]"' in body
          and b'name="data[2]"' not in body)
    check("選んだ実体が載る（0番と2番）",
          (b"\xff\xd8\xff" + bytes([0]) * 64) in body
          and (b"\xff\xd8\xff" + bytes([2]) * 64) in body)
    check("選ばなかった1番は載らない",
          (b"\xff\xd8\xff" + bytes([1]) * 64) not in body)


def test_validation(tc, images, tmp):
    print("\n[5] 入力の検証")
    n_before = len(RECEIVED)
    r = tc.create_photo_post(tc.find_account("main"), [])
    check("画像0枚は弾く", r.get("ok") is False, str(r))
    r = tc.create_photo_post(tc.find_account("main"),
                             [os.path.join(tmp, "nope.jpg")])
    check("存在しないファイルは弾く", r.get("ok") is False, str(r))
    check("理由にファイル名が出る", "nope.jpg" in r.get("error", ""), str(r))
    r = tc.create_photo_post(tc.find_account("main"), images, state="bogus")
    check("不正な state は弾く", r.get("ok") is False, str(r))
    check("弾いたときはHTTPを投げない", len(RECEIVED) == n_before,
          f"{n_before} -> {len(RECEIVED)}")


def test_api_error(tc, images):
    print("\n[6] APIエラーを握り潰さない")
    MockHandler.fail_next = True
    r = tc.create_photo_post(tc.find_account("main"), images)
    check("ok=False で返る", r.get("ok") is False, str(r))
    check("エラー内容を拾う", "画像が大きすぎます" in r.get("error", ""), str(r))
    check("HTTPステータスも出す", "400" in r.get("error", ""), str(r))


def test_not_configured(tmp, base):
    print("\n[7] 未設定なら機能ごと無効")
    os.environ["TUMBLR_CONSUMER_KEY"] = ""
    os.environ["TUMBLR_CONSUMER_SECRET"] = ""
    os.environ["TUMBLR_ACCOUNTS_FILE"] = os.path.join(tmp, "missing.json")
    import config
    importlib.reload(config)
    import tumblr_client
    importlib.reload(tumblr_client)
    check("is_configured が False", tumblr_client.is_configured() is False)
    check("アカウントが空", tumblr_client.load_accounts() == [])
    check("public も空", tumblr_client.public_accounts() == [])
    check("find_account は None", tumblr_client.find_account("main") is None)


def test_save_permissions(tmp):
    print("\n[8] トークンファイルの扱い")
    os.environ["TUMBLR_ACCOUNTS_FILE"] = os.path.join(tmp, "saved.json")
    import config
    importlib.reload(config)
    import tumblr_client
    importlib.reload(tumblr_client)
    tumblr_client.save_accounts([
        {"label": "x", "blog": "b", "token": "t", "secret": "s"}])
    p = tumblr_client.ACCOUNTS_PATH
    check("保存される", os.path.exists(p))
    mode = os.stat(p).st_mode & 0o777
    check("パーミッションが 600", mode == 0o600, oct(mode))
    check("一時ファイルが残らない", not os.path.exists(p + ".tmp"))
    check("読み戻せる", len(tumblr_client.load_accounts()) == 1)


def test_gitignore():
    print("\n[9] トークンが公開されない設定か")
    root = os.path.join(os.path.dirname(__file__), "..")
    gi = open(os.path.join(root, ".gitignore"), encoding="utf-8").read()
    check("tumblr_accounts.json が .gitignore 済み",
          "tumblr_accounts.json" in gi)
    ex = open(os.path.join(root, ".env.example"), encoding="utf-8").read()
    check("CONSUMER_KEY に実値が入っていない",
          "TUMBLR_CONSUMER_KEY=" not in ex.replace("#TUMBLR_CONSUMER_KEY=", ""))
    check("CONSUMER_SECRET に実値が入っていない",
          "TUMBLR_CONSUMER_SECRET=" not in ex.replace("#TUMBLR_CONSUMER_SECRET=", ""))


def main():
    tmp = tempfile.mkdtemp(prefix="igray_tumblr_")
    srv, base = start_mock()
    print(f"mock API: {base}")

    images = make_images(tmp, 3)
    tc = setup_client(tmp, base)

    test_accounts(tc)
    test_verify(tc)
    test_post_multi(tc, images)
    test_post_single_and_subset(tc, images)
    test_validation(tc, images, tmp)
    test_api_error(tc, images)
    test_not_configured(tmp, base)
    test_save_permissions(tmp)
    test_gitignore()

    srv.shutdown()

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
