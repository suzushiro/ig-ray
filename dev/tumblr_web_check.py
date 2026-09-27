"""
ig-ray / dev/tumblr_web_check.py

Tumblr 投稿の**表示層**の検査（share_check.py の後継）。

v4.16 で投稿経路は OAuth API 直叩きのみになり、
一時公開URL（/share, /share-img, /api/share/prepare）は撤去した。
ここではその撤去と、モーダルの HTML と JS が食い違っていないかを見る。

**HTML と JS の ID 突き合わせを両方向でやるのが本命。**
v4.15 で「バックエンドと HTML は直したが JS が古い」状態を出荷して
openTumblrShare が TypeError で落ちた。片方向だけでは拾えない。

    python3 dev/tumblr_web_check.py
"""

import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
TPL = os.path.join(ROOT, "app", "templates")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          f"{('  -- ' + detail) if detail and not cond else ''}")


def read(name):
    with open(os.path.join(TPL, name), encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------
# 1. 旧方式が本当に消えているか
# --------------------------------------------------------------------------

def test_legacy_gone():
    print("\n[1] 旧シェアツール方式の撤去")
    py = os.path.join(ROOT, "app", "web.py")
    with open(py, encoding="utf-8") as f:
        web_src = f.read()

    for token in ("/api/share/prepare", '@app.route("/share/<token>")',
                  "share-img", "SHARE_ENABLED", "PUBLIC_SHARE_BASE_URL",
                  "SHARE_TOKEN_TTL_MIN", "widgets/share/tool"):
        check(f"web.py に {token} が残っていない", token not in web_src)

    with open(os.path.join(ROOT, "app", "db.py"), encoding="utf-8") as f:
        db_src = f.read()
    for token in ("create_share_token", "get_share_payload",
                  "purge_expired_share_tokens",
                  "CREATE TABLE IF NOT EXISTS share_tokens"):
        check(f"db.py に {token} が残っていない", token not in db_src)
    check("db.py は share_tokens を落とす",
          "DROP TABLE IF EXISTS share_tokens" in db_src)

    check("share.html が消えている",
          not os.path.exists(os.path.join(TPL, "share.html")))
    check("dev/share_check.py が消えている",
          not os.path.exists(os.path.join(HERE, "share_check.py")))

    js = read("_scripts.html")
    for token in ("tmbOpenShareTool", "tmbSendAll", "tmbSelected",
                  "tmb-all", "tmb-link-note", "all-mode",
                  "widgets/share/tool"):
        check(f"_scripts.html に {token} が残っていない", token not in js)

    compose = os.path.join(ROOT, "docker-compose.yml")
    with open(compose, encoding="utf-8") as f:
        cs = f.read()
    check("compose に PUBLIC_SHARE_BASE_URL が無い",
          "PUBLIC_SHARE_BASE_URL" not in cs)
    check("compose に SHARE_TOKEN_TTL_MIN が無い",
          "SHARE_TOKEN_TTL_MIN" not in cs)
    check("compose は TUMBLR_CONSUMER_KEY を渡す", "TUMBLR_CONSUMER_KEY" in cs)


# --------------------------------------------------------------------------
# 2. HTML と JS の ID 突き合わせ（両方向）
# --------------------------------------------------------------------------

def test_id_crosscheck():
    print("\n[2] モーダルの ID を HTML と JS で突き合わせる")
    src = read("_scripts.html")

    # モーダルの markup 部分だけを切り出す
    i = src.find('<div class="dlg-wrap" id="tmb-dlg">')
    j = src.find("<script>", i)
    check("モーダルの markup が見つかる", i != -1 and j > i)
    if i == -1 or j <= i:
        return
    markup, script = src[i:j], src[j:]

    html_ids = set(re.findall(r'id="(tmb-[a-z0-9-]+)"', markup))
    # JS が触る ID（getElementById / tmbEl / tmbShow の引数）
    js_ids = set(re.findall(
        r"(?:getElementById|tmbEl|tmbShow)\(\s*'(tmb-[a-z0-9-]+)'", script))

    check("HTML 側の ID が取れている", len(html_ids) >= 8, str(sorted(html_ids)))
    check("JS 側の ID が取れている", len(js_ids) >= 8, str(sorted(js_ids)))

    # → JS が存在しない ID を触っていないか（v4.15 で踏んだ向き）
    missing = sorted(js_ids - html_ids)
    check("JS が触る ID はすべて HTML にある", not missing, str(missing))

    # ← HTML にあるのに JS が一切触らない ID（消し忘れの markup）
    # 静的に置いてあるだけの要素は除く
    static_ok = {"tmb-dlg-title"}
    unused = sorted(html_ids - js_ids - static_ok)
    check("HTML の ID はすべて JS から使われる", not unused, str(unused))

    # onclick で呼ぶ関数がすべて定義されているか
    handlers = set(re.findall(r'on(?:click|change)="(tmb[A-Za-z]+)\(', markup))
    handlers |= set(re.findall(r'on(?:click|change)="(\w*[Tt]umblr\w*)\(', markup))
    defined = set(re.findall(r"function\s+(\w+)\s*\(", script))
    defined |= set(re.findall(r"async\s+function\s+(\w+)\s*\(", script))
    undef = sorted(h for h in handlers if h not in defined)
    check("onclick/onchange の関数はすべて定義済み", not undef, str(undef))

    # マクロ側の呼び出しも同じ関数集合で解決できるか
    macros = read("_macros.html")
    called = set(re.findall(r'onclick="(\w+)\(', macros))
    tmb_called = {c for c in called if "umblr" in c or c.startswith("tmb")}
    check("マクロの openTumblrShare が定義済み",
          tmb_called <= defined, str(sorted(tmb_called - defined)))


# --------------------------------------------------------------------------
# 3. 新しい挙動が markup / JS に入っているか
# --------------------------------------------------------------------------

def test_new_behaviour():
    print("\n[3] 全選択・自動下書きの実装")
    src = read("_scripts.html")

    check("全部選ぶボタンがある", 'onclick="tmbSelectAll()"' in src)
    check("1枚目だけボタンがある", 'onclick="tmbSelectFirst()"' in src)
    check("選択枚数の表示欄がある", 'id="tmb-sel-count"' in src)
    check("自動下書きの注記欄がある", 'id="tmb-draft-auto"' in src)

    check("既定は全選択（selected を付けて生成）",
          "t.className = 'selected';" in src)
    check("しきい値が定数になっている", "TMB_DRAFT_THRESHOLD = 3" in src)
    check("しきい値は3枚", re.search(r"TMB_DRAFT_THRESHOLD\s*=\s*3\b", src))
    check("自動下書きの判定がしきい値を使う",
          "n >= TMB_DRAFT_THRESHOLD" in src)
    check("手動操作を記録する変数がある", "tmbDraftManual" in src)
    check("手動操作後は自動で書き換えない",
          "if (!tmbDraftManual) cb.checked" in src)
    check("onchange は manual を渡す",
          'onchange="tmbDraftToggled(true)"' in src)

    check("空の選択は全部として扱う", "if (!tmbChosen.size)" in src)
    check("全部外すのは不可", "tmbChosen.add(i);   // 全部外すのは不可" in src)
    check("絞ったときだけ indices を送る",
          "eff.length < tmbCount" in src)

    # 任意要素は tmbEl/tmbShow 経由（null で落ちない）
    direct = re.findall(r"document\.getElementById\('(tmb-[a-z0-9-]+)'\)\.", src)
    allowed = {"tmb-dlg"}      # 存在を先に確認しているもの
    bad = sorted(set(direct) - allowed)
    check("任意要素は直接 .style/.value を触らない", not bad, str(bad))

    print("\n[4] CSS")
    css = read("_style.html")
    check("--bg-card が light で定義されている",
          re.search(r":root\s*\{[^}]*--bg-card:", css, re.S))
    check("--bg-card が dark でも定義されている",
          re.search(r'\[data-theme="dark"\]\s*\{[^}]*--bg-card:', css, re.S))
    check(".tmb-sel-bar がある", ".tmb-sel-bar" in css)
    check(".tmb-sel-btn がある", ".tmb-sel-btn" in css)
    check(".all-mode が残っていない", "all-mode" not in css)

    # var(--x) で使っている変数がすべて定義されているか（透過バグの再発防止）
    used = set(re.findall(r"var\(--([a-z0-9-]+)", css))
    for name in ("_style.html",):
        pass
    declared = set(re.findall(r"--([a-z0-9-]+)\s*:", css))
    for other in ("backup.html", "mutes.html", "storage.html", "user.html",
                  "index.html", "gallery.html", "bookmarks.html",
                  "_macros.html", "_scripts.html", "_nav.html",
                  "_feed.html", "_gallery_items.html"):
        used |= set(re.findall(r"var\(--([a-z0-9-]+)", read(other)))
    # フォールバック付き var(--x, y) は未定義でも成立する
    undef = sorted(u for u in used - declared
                   if not re.search(r"var\(--" + re.escape(u) + r"\s*,", css))
    check("使っている CSS 変数はすべて定義済み", not undef, str(undef))


# --------------------------------------------------------------------------
# 4. ルート
# --------------------------------------------------------------------------

def build_db(path):
    import db
    c = db.connect(path)
    db.init_db(c)
    c.execute("INSERT INTO accounts (username, is_enabled, is_target) "
              "VALUES ('u1', 1, 1)")
    db.save_posts(c, [dict(
        shortcode="SC1", mediaid=None, owner_username="u1", caption="x",
        date_utc="2026-07-30T10:00:00+00:00", likes=1, comments=0,
        typename="GraphImage", is_video=0, is_carousel=0, location=None,
        hashtags_json="[]",
        media_json=json.dumps([{"index": 0, "is_video": False,
                                "image_url": "https://cdn.example/0.jpg",
                                "video_url": None}]),
        local_media_json=None)])
    # `t` ボタンは**ローカルに静止画の実体がある投稿だけ**に出る。
    # 実体を作らないと has_local=False でボタンが出ず、
    # 「出ないのが正しい」のか「壊れている」のか区別できない。
    cache = os.environ["IG_RAY_CACHE"]
    os.makedirs(os.path.join(cache, "SC"), exist_ok=True)
    local = os.path.join(cache, "SC", "SC1_0.jpg")
    with open(local, "wb") as f:
        f.write(b"\xff\xd8\xff\xd9")      # 最小の JPEG っぽいバイト列
    c.execute(
        "INSERT INTO media_index (shortcode, media_index, is_video, "
        "  remote_url, local_path) VALUES ('SC1', 0, 0, ?, ?)",
        ("https://cdn.example/0.jpg", local))
    c.commit()
    return c


def test_routes(cli):
    print("\n[5] ルート")
    for path in ("/share/abc", "/share-img/abc/0"):
        check(f"{path} は 404", cli.get(path).status_code == 404)
    r = cli.post("/api/share/prepare", json={"shortcode": "SC1"})
    check("/api/share/prepare は 404/405",
          r.status_code in (404, 405), str(r.status_code))

    r = cli.get("/api/tumblr/accounts")
    check("/api/tumblr/accounts は 200", r.status_code == 200)
    data = r.get_json()
    check("enabled を返す", "enabled" in data, str(data))
    check("未設定なら enabled=False", data.get("enabled") is False, str(data))
    check("トークンを返さない", "token" not in json.dumps(data))

    r = cli.post("/api/tumblr/post", json={"shortcode": "SC1"})
    check("未設定なら投稿は 400", r.status_code == 400, str(r.status_code))

    print("\n[6] 未設定なら t ボタンを出さない")
    body = cli.get("/").get_data(as_text=True)
    check("tmb-btn が出ない", 'class="tmb-btn"' not in body)
    check("モーダルも描画されない", 'id="tmb-dlg"' not in body)


def test_enabled_flags():
    print("\n[7] 設定済みならボタンとモーダルが出る")
    import web
    import tumblr_client
    orig = tumblr_client.is_configured
    tumblr_client.is_configured = lambda: True
    try:
        with web.app.test_request_context("/"):
            flags = web._inject_share_flags()
        check("share_enabled が True", flags["share_enabled"] is True, str(flags))
        check("tumblr_api_enabled が True", flags["tumblr_api_enabled"] is True)
        check("share_link_enabled は無くなった", "share_link_enabled" not in flags)
        with web.app.test_client() as cli:
            body = cli.get("/").get_data(as_text=True)
        check("t ボタンが出る", 'class="tmb-btn"' in body)
        check("モーダルが描画される", 'id="tmb-dlg"' in body)
        check("選択バーが描画される", 'id="tmb-sel-bar"' in body)
        check("自動下書きの注記が描画される", 'id="tmb-draft-auto"' in body)
    finally:
        tumblr_client.is_configured = orig


def test_public_host_guard():
    print("\n[8] 公開ホスト名で来たら全部404（撤去の保険）")
    import importlib
    os.environ["IG_RAY_PUBLIC_SHARE_HOST"] = "share.example.com"
    import config
    import web as web_mod
    importlib.reload(config)
    web = importlib.reload(web_mod)
    web.app.config["TESTING"] = True
    try:
        with web.app.test_client() as cli:
            for path in ("/", "/gallery", "/backup", "/api/tumblr/accounts"):
                r = cli.get(path, headers={"Host": "share.example.com"})
                check(f"公開ホストの {path} は 404",
                      r.status_code == 404, str(r.status_code))
            r = cli.get("/", headers={"Host": "localhost"})
            check("内部ホストなら通る", r.status_code == 200, str(r.status_code))
    finally:
        os.environ.pop("IG_RAY_PUBLIC_SHARE_HOST", None)
        importlib.reload(config)
        importlib.reload(web_mod)


def main():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    cache = os.path.join(tmp, "cache")
    os.makedirs(cache, exist_ok=True)
    os.environ["IG_RAY_DB"] = path
    os.environ["IG_RAY_CACHE"] = cache
    os.environ.pop("IG_RAY_PUBLIC_SHARE_HOST", None)
    os.environ.pop("TUMBLR_CONSUMER_KEY", None)
    os.environ.pop("TUMBLR_CONSUMER_SECRET", None)

    test_legacy_gone()
    test_id_crosscheck()
    test_new_behaviour()

    c = build_db(path)
    import web
    web.app.config["TESTING"] = True
    web._target_cache.update({"at": 0.0, "set": None})
    web._muted_cache.update({"at": 0.0, "set": frozenset()})
    with web.app.test_client() as cli:
        test_routes(cli)
    test_enabled_flags()
    c.close()
    test_public_host_guard()

    print("\n" + "=" * 50)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    for n in FAIL:
        print(f"  - {n}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
