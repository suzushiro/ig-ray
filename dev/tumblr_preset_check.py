"""
ig-ray / dev/tumblr_preset_check.py

Tumblr 投稿のキャプション・タグを**投稿主（Instagram 側）ごとに覚える**機能の検証。

仕様:
  * 投稿できたら、そのときのキャプションとタグを投稿主ごとに上書き保存する
  * キャプションは**リンクで包む前**の生のテキストを保存する
    （投稿時に元投稿へのリンクで包むが、リンク先は投稿ごとに違う）
  * 投稿主は画面から送られた値ではなく、DB の posts から引く
  * 失敗した投稿の入力は保存しない
  * モーダルを開いたら /api/tumblr/preset で読んで初期値にする

    python3 dev/tumblr_preset_check.py
"""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

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
    for m in ("config", "db", "web", "tumblr_client"):
        sys.modules.pop(m, None)


def media(n=1):
    return json.dumps([{"index": i, "is_video": False,
                        "image_url": f"https://cdn.example/{i}.jpg",
                        "video_url": None} for i in range(n)])


def build(path, cache):
    import db
    c = db.connect(path)
    db.init_db(c)
    c.execute("INSERT INTO accounts (username, is_enabled, is_target) "
              "VALUES ('Owner_A', 1, 1)")
    rows = []
    for sc, owner in (("SC_A1", "owner_a"), ("SC_A2", "owner_a"),
                      ("SC_B1", "owner_b")):
        rows.append(dict(
            shortcode=sc, mediaid=None, owner_username=owner, caption="x",
            date_utc="2026-11-01T10:00:00+00:00", likes=1, comments=0,
            typename="GraphImage", is_video=0, is_carousel=0, location=None,
            hashtags_json="[]", media_json=media(), local_media_json=None))
    db.save_posts(c, rows)
    # ローカル画像（投稿に必要）
    for sc in ("SC_A1", "SC_A2", "SC_B1"):
        d = os.path.join(cache, sc[:2])
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{sc}_0.jpg")
        open(p, "wb").write(b"\xff\xd8\xff\xd9")
        c.execute("INSERT INTO media_index (shortcode, media_index, is_video, "
                  "remote_url, local_path) VALUES (?, 0, 0, ?, ?)",
                  (sc, "https://cdn.example/0.jpg", p))
    c.commit()
    return c


# --------------------------------------------------------------------------

def test_db():
    print("\n[1] 保存と読み出し")
    tmp = tempfile.mkdtemp()
    fresh(IG_RAY_DB=os.path.join(tmp, "t.db"), IG_RAY_CACHE=tmp)
    import db
    c = db.connect(os.path.join(tmp, "t.db"))
    db.init_db(c)

    check("schema_version は 9", c.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] == "9")
    check("無ければ None", db.get_tumblr_preset(c, "nobody") is None)
    check("空の owner は保存しない", db.save_tumblr_preset(c, "", "x", "y") is False)

    db.save_tumblr_preset(c, "Owner_A", "キャプション", "photo, #film,  photo , Film")
    p = db.get_tumblr_preset(c, "owner_a")
    check("小文字で引ける", p is not None)
    check("キャプションがそのまま入る", p and p["caption"] == "キャプション", p)
    check("タグは整える（# を落とし、重複を大文字小文字無視で除く）",
          p and p["tags"] == "photo, film", p and p["tags"])
    check("大文字で引いても同じ", db.get_tumblr_preset(c, "OWNER_A") == p)

    db.save_tumblr_preset(c, "owner_a", "", ["a", "b"])
    p = db.get_tumblr_preset(c, "owner_a")
    check("上書きされる（1投稿主1件）", c.execute(
        "SELECT COUNT(*) n FROM tumblr_presets").fetchone()["n"] == 1)
    check("空のキャプションも「空にした」として保存する", p["caption"] == "", p)
    check("タグはリストでも受ける", p["tags"] == "a, b", p["tags"])

    check("normalize_tags: 空", db.normalize_tags("") == [] and db.normalize_tags(None) == [])
    check("normalize_tags: 入力順を保つ",
          db.normalize_tags("z, a, #m") == ["z", "a", "m"])

    check("削除できる", db.delete_tumblr_preset(c, "OWNER_A") is True and
          db.get_tumblr_preset(c, "owner_a") is None)
    c.close()


def test_migration():
    print("\n[2] 既存DB（v8）から")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "old.db")
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    raw.execute("INSERT INTO meta VALUES ('schema_version','8')")
    raw.commit()
    raw.close()
    fresh(IG_RAY_DB=path, IG_RAY_CACHE=tmp)
    import db
    c = db.connect(path)
    db.init_db(c)
    check("tumblr_presets が作られる", c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tumblr_presets'"
    ).fetchone() is not None)
    check("schema_version が 9 に上がる", c.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] == "9")
    db.save_tumblr_preset(c, "x", "y", "z")
    check("移行後に保存できる", db.get_tumblr_preset(c, "x") is not None)
    c.close()


def test_api():
    print("\n[3] 投稿と定型の保存・読み出し（Flask で実際に通す）")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    cache = os.path.join(tmp, "cache")
    os.makedirs(cache)
    fresh(IG_RAY_DB=path, IG_RAY_CACHE=cache)
    c = build(path, cache)
    import db
    import web
    import tumblr_client
    web.app.config["TESTING"] = True

    sent = []
    result = {"ok": True, "post_url": "https://example.tumblr.com/post/1"}
    tumblr_client.is_configured = lambda: True
    tumblr_client.find_account = lambda label: {"label": "main", "blog": "ex"}

    def fake_post(account, paths, caption="", tags=None, state="published",
                  source_url=None):
        sent.append({"caption": caption, "tags": tags, "state": state})
        return dict(result)
    tumblr_client.create_photo_post = fake_post

    cl = web.app.test_client()

    r = cl.get("/api/tumblr/preset?shortcode=SC_A1").get_json()
    check("定型が無ければ saved=false", r["ok"] and r["saved"] is False, r)
    check("投稿主は DB から引ける", r["owner"] == "owner_a", r)

    # 投稿する。画面から嘘の owner を送っても無視されること
    r = cl.post("/api/tumblr/post", json={
        "shortcode": "SC_A1", "account": "main", "state": "draft",
        "caption": "  みなみさん  ", "tags": "portrait, #film, portrait",
        "owner": "someone_else"}).get_json()
    check("投稿は成功する", r.get("ok"), r)
    check("Tumblr にはリンクで包んだキャプションを送る",
          sent and sent[-1]["caption"].startswith('<a href="https://www.instagram.com/p/SC_A1/')
          and "みなみさん" in sent[-1]["caption"], sent and sent[-1]["caption"])
    check("Tumblr にはタグを整えて送る", sent and sent[-1]["tags"] == ["portrait", "film"],
          sent and sent[-1]["tags"])
    check("どの投稿主で覚えたかを返す", r.get("preset_saved") == "owner_a", r)

    p = db.get_tumblr_preset(c, "owner_a")
    check("**保存するのはリンクで包む前のテキスト**",
          p and p["caption"] == "みなみさん" and "<a" not in p["caption"], p)
    check("保存するタグは整えたもの", p and p["tags"] == "portrait, film", p)
    check("画面から送られた owner には保存しない",
          db.get_tumblr_preset(c, "someone_else") is None)

    # 同じ投稿主の別の投稿で読める
    r = cl.get("/api/tumblr/preset?shortcode=SC_A2").get_json()
    check("同じ投稿主の別の投稿で前回の内容が返る",
          r["saved"] and r["caption"] == "みなみさん" and r["tags"] == "portrait, film", r)
    check("返す値にリンクが入っていない",
          "instagram.com" not in json.dumps(r, ensure_ascii=False), r)

    # 別の投稿主には混ざらない
    r = cl.get("/api/tumblr/preset?shortcode=SC_B1").get_json()
    check("別の投稿主には混ざらない", r["owner"] == "owner_b" and r["saved"] is False, r)

    # 上書き
    cl.post("/api/tumblr/post", json={
        "shortcode": "SC_A2", "account": "main", "caption": "新しい", "tags": "x"})
    r = cl.get("/api/tumblr/preset?shortcode=SC_A1").get_json()
    check("次の投稿で上書きされる", r["caption"] == "新しい" and r["tags"] == "x", r)

    # 失敗した投稿は覚えない
    result.update({"ok": False, "error": "boom"})
    rr = cl.post("/api/tumblr/post", json={
        "shortcode": "SC_A1", "account": "main", "caption": "失敗した入力",
        "tags": "bad"})
    check("失敗は 502", rr.status_code == 502, rr.status_code)
    r = cl.get("/api/tumblr/preset?shortcode=SC_A1").get_json()
    check("**失敗した投稿の入力は覚えない**", r["caption"] == "新しい", r)
    result.update({"ok": True})
    result.pop("error", None)

    # 不正な要求
    check("shortcode なしは 400",
          cl.get("/api/tumblr/preset").status_code == 400)
    r = cl.get("/api/tumblr/preset?shortcode=NOPE").get_json()
    check("知らない投稿は owner なし・saved=false",
          r["ok"] and r["owner"] is None and r["saved"] is False, r)

    # ブックマークだけ残っている投稿（posts から消えた）でも投稿主が分かる
    c.execute("INSERT INTO bookmarks (shortcode, owner_username) VALUES ('SC_GONE', 'owner_a')")
    c.commit()
    r = cl.get("/api/tumblr/preset?shortcode=SC_GONE").get_json()
    check("ブックマークだけの投稿でも投稿主の定型が返る",
          r["owner"] == "owner_a" and r["saved"], r)
    c.close()


def test_template():
    print("\n[4] 画面の部品")
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "templates",
                            "_scripts.html"), encoding="utf-8").read()
    check("復元の注記がある", 'id="tmb-preset-note"' in src)
    check("「初期値に戻す」がある", "tmbPresetReset()" in src)
    check("開く前に定型を読む（アカウント一覧と並行）",
          "Promise.all([tmbLoadAccounts(), tmbLoadPreset(shortcode)])" in src)
    check("画面から owner を送っていない（保存先はサーバが決める）",
          "owner:" not in src[src.index("async function tmbPost"):
                              src.index("async function tmbPost") + 2500])


def main():
    test_db()
    test_migration()
    test_api()
    test_template()
    print("\n" + "=" * 50)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    for n in FAIL:
        print(f"  - {n}")
    if not FAIL:
        print("すべて通過")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
