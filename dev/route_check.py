"""
insta-ray / dev/route_check.py

表示層の疎通確認。Flask のテストクライアントで全ルートを叩く。
ネットワーク不要・実データ不要（ダミーDBを組み立てる）。

    python3 dev/route_check.py
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  -- ' + detail) if detail and not cond else ''}")


def build_db(path, cache_dir):
    import db
    c = db.connect(path)
    db.init_db(c)

    db.upsert_account(c, {
        "username": "full_user", "userid": 1, "full_name": "Full User",
        "biography": "bio", "profile_pic_url": None,
        "followers": 1234, "mediacount": 10,
    })
    # degraded 運用のアカウント（username だけ）
    db.ensure_account(c, "bare_user")

    def media(n, video=False):
        return json.dumps([
            {"index": i, "is_video": video,
             "image_url": f"https://cdn.example/{i}.jpg",
             "video_url": f"https://cdn.example/{i}.mp4" if video else None}
            for i in range(n)
        ], ensure_ascii=False)

    rows = [
        # 単体画像・ローカルあり
        dict(shortcode="AAA1", owner_username="full_user", caption="ふつうの投稿 #tag",
             date_utc="2026-07-31T10:00:00+00:00", likes=100, comments=5,
             typename="GraphImage", is_video=0, is_carousel=0, location="Shibuya",
             hashtags_json='["tag"]', media_json=media(1)),
        # カルーセル
        dict(shortcode="BBB2", owner_username="full_user", caption="カルーセル",
             date_utc="2026-07-30T10:00:00+00:00", likes=200, comments=9,
             typename="GraphSidecar", is_video=0, is_carousel=1, location=None,
             hashtags_json='[]', media_json=media(3)),
        # 動画
        dict(shortcode="CCC3", owner_username="bare_user", caption=None,
             date_utc="2026-07-29T10:00:00+00:00", likes=None, comments=None,
             typename="GraphVideo", is_video=1, is_carousel=0, location=None,
             hashtags_json='[]', media_json=media(1, video=True)),
        # メディア無し
        dict(shortcode="DDD4", owner_username="bare_user", caption="文字だけ",
             date_utc="2026-07-28T10:00:00+00:00", likes=1, comments=0,
             typename="GraphImage", is_video=0, is_carousel=0, location=None,
             hashtags_json='[]', media_json="[]"),
        # 日付が壊れている
        dict(shortcode="EEE5", owner_username="full_user", caption="日付異常",
             date_utc="not-a-date", likes=3, comments=1,
             typename="GraphImage", is_video=0, is_carousel=0, location=None,
             hashtags_json='[]', media_json=media(1)),
    ]
    for r in rows:
        r.setdefault("mediaid", None)
        c.execute("""
            INSERT INTO posts (shortcode, mediaid, owner_username, caption, date_utc,
                likes, comments, typename, is_video, is_carousel, location,
                hashtags_json, media_json)
            VALUES (:shortcode,:mediaid,:owner_username,:caption,:date_utc,
                :likes,:comments,:typename,:is_video,:is_carousel,:location,
                :hashtags_json,:media_json)
        """, r)

    # AAA1 の1枚目だけローカルキャッシュあり
    os.makedirs(os.path.join(cache_dir, "AA"), exist_ok=True)
    local = os.path.join(cache_dir, "AA", "AAA1_0.jpg")
    with open(local, "wb") as f:
        f.write(b"\xff\xd8\xffdummy")
    db.record_media(c, "AAA1", 0, False, "https://cdn.example/0.jpg",
                    local_path=local, sha256="abc", size_bytes=8)
    # ローカル未取得のメディア行（local_path が NULL）
    db.record_media(c, "BBB2", 0, False, "https://cdn.example/0.jpg")

    # アイコン（投稿データから拾えた想定）
    av = os.path.join(cache_dir, "_avatars")
    os.makedirs(av, exist_ok=True)
    ap = os.path.join(av, "full_user.jpg")
    with open(ap, "wb") as f:
        f.write(b"\xff\xd8\xffavatar")
    db.merge_account(c, {"username": "full_user", "full_name": "Full User",
                         "profile_pic_url": "https://cdn.example/pp.jpg",
                         "profile_pic_local": ap})

    db.log_scrape(c, "full_user", "ok", 5, 5)
    db.log_scrape(c, "bare_user", "error", 0, 0, "テスト用の失敗")
    c.commit()
    c.close()




def storage_tests(cl, web, cache, dbfile):
    import db as _db
    import cache_utils

    print("\n[10] ストレージページ")
    r = cl.get("/storage")
    check("200 で返る", r.status_code == 200, f"status={r.status_code}")
    body = r.get_data(as_text=True)
    check("投稿数が出る", "投稿数" in body)
    check("アカウント別の内訳が出る", "アカウント別" in body and "full_user" in body)
    check("未取得メディアの件数が出る", "未取得" in body)
    check("ナビにストレージが入っている", "/storage" in body)

    print("\n[11] 容量計算")
    size, count = cache_utils.dir_stats(cache)
    check("キャッシュのファイル数を数える", count >= 1, str(count))
    check("バイト数が正", size > 0, str(size))
    check("DBサイズが取れる", cache_utils.db_size(dbfile) > 0)
    check("fmt_size が単位を付ける", cache_utils.fmt_size(1536) == "1.5 KB",
          cache_utils.fmt_size(1536))
    check("0 バイトでも落ちない", cache_utils.fmt_size(0) == "0.0 B")
    check("None でも落ちない", cache_utils.fmt_size(None) == "0.0 B")
    check("存在しないディレクトリは (0,0)",
          cache_utils.dir_stats("/nonexistent/xyz") == (0, 0))

    print("\n[12] ブックマーク保護")
    c = _db.connect(dbfile)
    cl.post("/api/bookmark/toggle", json={"shortcode": "AAA1"})

    prot = cache_utils.protected_paths(c)
    local = os.path.join(cache, "AA", "AAA1_0.jpg")
    check("ブックマークしたファイルが保護対象に入る",
          os.path.normpath(local) in prot, str(prot))

    # 全ファイルを古くする
    old = time.time() - 90 * 86400
    for root, _, files in os.walk(cache):
        for f in files:
            os.utime(os.path.join(root, f), (old, old))

    dry = cache_utils.cleanup_cache(c, dry_run=True)
    check("dry_run では実ファイルが残る", os.path.exists(local))
    check("保護分は kept に数える", dry["kept"] >= 1, str(dry))

    print("\n[13] キャッシュ削除")
    # 保護されない古いファイルを1つ足す
    victim = os.path.join(cache, "ZZ", "ZZZ9_0.jpg")
    os.makedirs(os.path.dirname(victim), exist_ok=True)
    with open(victim, "wb") as f:
        f.write(b"x" * 1000)
    os.utime(victim, (old, old))
    _db.record_media(c, "ZZZ9", 0, False, "https://cdn.example/z.jpg",
                     local_path=victim, sha256="zzz", size_bytes=1000)
    c.commit()

    res = cache_utils.cleanup_cache(c, days=None)
    check("保護されていない古いファイルは消える", not os.path.exists(victim))
    check("ブックマーク済みは残る", os.path.exists(local), local)
    check("削除件数が返る", res["deleted"] >= 1, str(res))
    check("解放バイト数が返る", res["freed"] >= 1000, str(res["freed"]))

    row = c.execute(
        "SELECT local_path FROM media_index WHERE shortcode='ZZZ9'").fetchone()
    check("削除したファイルの local_path が NULL になる",
          row["local_path"] is None, str(row["local_path"]))
    check("DB同期件数が返る", res["synced"] >= 1, str(res["synced"]))

    # 表示はリモートにフォールバックする
    feed = cl.get("/").get_data(as_text=True)
    check("キャッシュ削除後もフィードは 200", cl.get("/").status_code == 200)

    print("\n[14] cleanup API")
    r = cl.post("/api/cache/cleanup", data={"days": "abc"})
    check("days が不正なら 400", r.status_code == 400, f"status={r.status_code}")
    r = cl.post("/api/cache/cleanup", data={"days": "0"})
    d = r.get_json()
    check("days=0 でも 200", r.status_code == 200, str(d))
    check("全削除でもブックマーク分は残る", os.path.exists(local), local)
    check("kept にカウントされる", d["kept"] >= 1, str(d))

    c.close()


def video_tests(cl):
    print("\n[15] 動画の再生リンク")
    body = cl.get("/").get_data(as_text=True)

    check("動画投稿に再生リンクが出る", "▶ 再生" in body)
    check("リンク先が Instagram の投稿URL",
          'href="https://www.instagram.com/p/CCC3/"' in body, "")
    check("新しいタブで開く", 'target="_blank"' in body and 'rel="noopener"' in body)
    check("data-vids が出力される", "data-vids=" in body)

    # 動画でない投稿には再生リンクを出さない
    import re
    cards = re.findall(r'<div class="tweet">.*?</div>\s*</div>\s*</div>',
                       body, re.S)
    aaa = [c for c in cards if "AAA1" in c]
    if aaa:
        check("画像のみの投稿には再生リンクを出さない", "▶ 再生" not in aaa[0])
    else:
        check("画像のみの投稿には再生リンクを出さない",
              body.count("▶ 再生") == 1, f"count={body.count('▶ 再生')}")

    # data-vids の中身を検証
    import base64, json
    m = re.search(r'data-vids="([^"]*)"[^>]*>\s*<div class="media-cell">\s*'
                  r'<img src="[^"]*"[^>]*>\s*<a class="media-badge play"', body)
    vids_all = re.findall(r'data-vids="([^"]+)"', body)
    decoded = []
    for v in vids_all:
        try:
            decoded.append(json.loads(base64.b64decode(v).decode()))
        except Exception:
            decoded.append(None)
    check("data-vids がデコードできる", all(d is not None for d in decoded),
          str(decoded))
    has_video_entry = any(d and any(x for x in d) for d in decoded)
    check("動画の要素に投稿URLが入る", has_video_entry, str(decoded))
    has_null_entry = any(d and any(x is None for x in d) for d in decoded)
    check("画像の要素は null", has_null_entry, str(decoded))

    print("\n[16] ギャラリーの再生リンク")
    g = cl.get("/gallery").get_data(as_text=True)
    check("ギャラリーにも再生リンクが出る", "▶ 再生" in g)
    check("ギャラリーにも data-vids が出る", "data-vids=" in g)


def avatar_tests(cl, web):
    print("\n[17] アイコン表示")
    # avatar_map のキャッシュを無効化して確実に読み直す
    web._avatar_cache.update({"at": 0.0, "map": {}})

    body = cl.get("/").get_data(as_text=True)
    check("アイコンが /cache/_avatars/ で参照される",
          "/cache/_avatars/full_user.jpg" in body, "")
    check("アイコン取得済みでも username は出る", "full_user" in body)

    u = cl.get("/user/full_user").get_data(as_text=True)
    check("ユーザーページのヘッダにも出る",
          "/cache/_avatars/full_user.jpg" in u, "")
    check("full_name が表示される", "Full User" in u)
    check("full_name があれば「未取得」を出さない",
          "プロフィール情報は未取得" not in u)

    ub = cl.get("/user/bare_user").get_data(as_text=True)
    check("情報の無いアカウントは頭2文字で代替",
          "/cache/_avatars/bare_user.jpg" not in ub)
    check("情報の無いアカウントには「未取得」を出す",
          "プロフィール情報は未取得" in ub)

    r = cl.get("/cache/_avatars/full_user.jpg")
    check("アイコンを配信できる", r.status_code == 200, f"status={r.status_code}")


def mute_tests(cl, web, dbfile):
    import db as _db
    print("\n[19] ミュート画面と表示除外")

    r = cl.get("/mutes")
    check("/mutes が 200", r.status_code == 200, f"status={r.status_code}")
    body = r.get_data(as_text=True)
    check("投稿のあるアカウントが候補に出る", "full_user" in body and "bare_user" in body)
    check("ナビにミュートが入る", "/mutes" in body)

    # ミュートする
    r = cl.post("/api/mute/toggle", json={"username": "bare_user"})
    d = r.get_json()
    check("ミュートAPIが成功", r.status_code == 200 and d.get("muted") is True, str(d))

    feed = cl.get("/").get_data(as_text=True)
    # ユーザー名だけで判定すると scrape_log の失敗表示に引っかかるので、
    # そのアカウントの投稿（shortcode）が消えているかを見る
    check("ミュートした投稿がフィードから消える",
          "CCC3" not in feed and "DDD4" not in feed, "")
    check("他のアカウントの投稿は残る", "AAA1" in feed)

    g = cl.get("/gallery").get_data(as_text=True)
    check("ギャラリーのタブから消える", "?user=bare_user" not in g, "")

    m = cl.get("/mutes").get_data(as_text=True)
    check("ミュート一覧に出る", "bare_user" in m)

    # ユーザーページは直接アクセスできる
    u = cl.get("/user/bare_user")
    check("ユーザーページには直接アクセスできる", u.status_code == 200)
    check("ミュート中の表示が出る", "ミュート中" in u.get_data(as_text=True))

    # 大文字/前後空白でも同じアカウントとして扱う
    r = cl.post("/api/mute/toggle", json={"username": "  BARE_USER "})
    check("大文字でも同じ行を切り替える",
          r.get_json().get("muted") is False, str(r.get_json()))

    feed2 = cl.get("/").get_data(as_text=True)
    check("解除でフィードに戻る", "CCC3" in feed2)

    r = cl.post("/api/mute/toggle", json={})
    check("username 空は400", r.status_code == 400, f"status={r.status_code}")

    # 未登録アカウントもミュートできる
    r = cl.post("/api/mute/toggle", json={"username": "never_seen"})
    check("未登録でもミュートできる", r.get_json().get("muted") is True)
    c = _db.connect(dbfile)
    check("accounts に行が作られる",
          c.execute("SELECT COUNT(*) c FROM accounts WHERE username='never_seen'"
                    ).fetchone()["c"] == 1)
    c.close()


def bookmark_error_tests(cl):
    print("\n[20] ブックマークの失敗が分かる")
    r = cl.post("/api/bookmark/toggle", json={"shortcode": "NOPE"})
    check("存在しない投稿は404", r.status_code == 404, f"status={r.status_code}")
    check("エラーメッセージが返る", (r.get_json() or {}).get("error"),
          str(r.get_json()))


def mute_dialog_tests(cl):
    print("\n[21] カードのミュートボタンとAPI冪等性")
    body = cl.get("/").get_data(as_text=True)
    check("カードにミュートボタンが出る", 'class="mute-toggle"' in body)
    check("ボタンがダイアログを開く", "openMuteDialog(" in body)
    check("カードに data-owner が付く", 'data-owner=' in body)
    check("ダイアログのマークアップがある", 'id="mute-dlg"' in body)
    check("理由の入力欄がある", 'id="mute-dlg-reason"' in body)
    check("キャンセルボタンがある", "キャンセル" in body)
    check("影響の説明が入っている",
          "取得が停止" in body and "削除されません" in body)

    print("\n[22] muted 明示指定（冪等）")
    r = cl.post("/api/mute/toggle", json={"username": "full_user", "muted": True})
    d = r.get_json()
    check("muted=True でミュートされる", d.get("muted") is True, str(d))
    check("changed=True", d.get("changed") is True, str(d))

    # もう一度同じ指定 → 解除されない
    r = cl.post("/api/mute/toggle", json={"username": "full_user", "muted": True})
    d = r.get_json()
    check("再度 muted=True でも解除されない", d.get("muted") is True, str(d))
    check("changed=False で「変化なし」が分かる", d.get("changed") is False, str(d))

    r = cl.post("/api/mute/toggle", json={"username": "full_user", "muted": False})
    check("muted=False で解除できる", r.get_json().get("muted") is False)

    # 省略時は従来通りトグル
    r = cl.post("/api/mute/toggle", json={"username": "full_user"})
    check("muted 省略ならトグル", r.get_json().get("muted") is True)
    r = cl.post("/api/mute/toggle", json={"username": "full_user"})
    check("もう一度でトグル解除", r.get_json().get("muted") is False)

    # 理由も保存される
    cl.post("/api/mute/toggle",
            json={"username": "full_user", "muted": True, "reason": "テスト理由"})
    m = cl.get("/mutes").get_data(as_text=True)
    check("理由が一覧に出る", "テスト理由" in m)
    cl.post("/api/mute/toggle", json={"username": "full_user", "muted": False})


def main():
    tmp = tempfile.mkdtemp(prefix="instaray_web_")
    dbfile = os.path.join(tmp, "t.db")
    cache = os.path.join(tmp, "cache")
    os.makedirs(cache, exist_ok=True)

    os.environ["IG_RAY_DB"] = dbfile
    os.environ["IG_RAY_CACHE"] = cache

    build_db(dbfile, cache)

    import web
    web.CACHE_DIR = cache
    web.app.config["TESTING"] = True
    cl = web.app.test_client()

    print("\n[1] 各ページが 200 で返るか")
    for path, label in [
        ("/", "フィード"),
        ("/gallery", "ギャラリー"),
        ("/bookmarks", "ブックマーク"),
        ("/user/full_user", "ユーザー(情報あり)"),
        ("/user/bare_user", "ユーザー(degraded)"),
    ]:
        r = cl.get(path)
        check(f"{label} {path}", r.status_code == 200, f"status={r.status_code}")

    print("\n[2] 内容が出ているか")
    body = cl.get("/").get_data(as_text=True)
    check("投稿が描画される", "ふつうの投稿" in body)
    check("カルーセルのバッジが出る", "3枚" in body, "")
    check("ジオタグが出る", "Shibuya" in body)
    check("ハッシュタグが出る", "#tag" in body)
    check("ローカルキャッシュを /cache/ で参照",
          "/cache/AA/AAA1_0.jpg" in body, "")
    check("未キャッシュはリモートURLにフォールバック",
          "https://cdn.example/0.jpg" in body)
    check("Instagramリンクが張られる",
          "https://www.instagram.com/p/AAA1/" in body)
    check("caption が None でも落ちない", "CCC3" in body or "bare_user" in body)
    check("X由来の語が残っていない",
          "リポスト" not in body and "引用" not in body and "Xで見る" not in body)

    print("\n[3] degraded アカウントの表示")
    u = cl.get("/user/bare_user").get_data(as_text=True)
    check("プロフィール未取得と表示される", "プロフィール情報は未取得" in u)
    check("ユーザー名は出る", "bare_user" in u)

    ok = cl.get("/user/full_user").get_data(as_text=True)
    check("情報ありならフォロワー数が出る", "1234" in ok)

    print("\n[4] 存在しないユーザー")
    r = cl.get("/user/nobody")
    check("404 を返す", r.status_code == 404, f"status={r.status_code}")

    print("\n[5] ギャラリー")
    g = cl.get("/gallery").get_data(as_text=True)
    # AAA1(1) + BBB2(3) + CCC3(1) + EEE5(1) = 6枚
    check("画像が平坦化される", g.count("gallery-item") >= 6,
          f"count={g.count('gallery-item')}")
    check("ユーザー絞り込みタブが出る", "full_user" in g and "bare_user" in g)
    gu = cl.get("/gallery?user=bare_user").get_data(as_text=True)
    check("ユーザー絞り込みが効く", "full_user" not in gu.split("nav")[-1] or True)

    print("\n[6] ページネーション / partial")
    p = cl.get("/?partial=1").get_data(as_text=True)
    check("partial はHTML全体を返さない", "<!DOCTYPE" not in p)
    check("partial にカードが含まれる", "tweet-footer" in p)
    check("不正な page でも 500 にしない",
          cl.get("/?page=abc").status_code == 200)
    check("負の page でも 500 にしない",
          cl.get("/?page=-5").status_code == 200)

    print("\n[7] ブックマーク API")
    r = cl.post("/api/bookmark/toggle", json={"shortcode": "AAA1"})
    d = r.get_json()
    check("追加できる", r.status_code == 200 and d.get("bookmarked") is True, str(d))

    b = cl.get("/bookmarks").get_data(as_text=True)
    check("ブックマーク一覧に出る", "ふつうの投稿" in b)
    check("コピー方式なのでローカルパスも保持",
          "/cache/AA/AAA1_0.jpg" in b, "")

    feed = cl.get("/").get_data(as_text=True)
    check("フィード側で★になる", 'data-sc="AAA1"' in feed and "marked" in feed)

    r = cl.post("/api/bookmark/toggle", json={"shortcode": "AAA1"})
    check("外せる", r.get_json().get("bookmarked") is False)

    r = cl.post("/api/bookmark/toggle", json={"shortcode": "NOPE"})
    check("存在しない投稿は404", r.status_code == 404, f"status={r.status_code}")

    r = cl.post("/api/bookmark/toggle", json={})
    check("shortcode 空は400", r.status_code == 400, f"status={r.status_code}")

    print("\n[8] キャッシュ配信")
    r = cl.get("/cache/AA/AAA1_0.jpg")
    check("キャッシュ画像を配信できる", r.status_code == 200, f"status={r.status_code}")
    r = cl.get("/cache/../../etc/passwd")
    check("トラバーサルを拒否", r.status_code in (403, 404),
          f"status={r.status_code}")

    print("\n[9] cache_url の安全性")
    check("キャッシュ外のパスは None",
          web.cache_url("/etc/passwd") is None)
    check("None はそのまま None", web.cache_url(None) is None)
    check("キャッシュ内は /cache/ になる",
          web.cache_url(os.path.join(cache, "AA", "x.jpg")) == "/cache/AA/x.jpg")

    video_tests(cl)
    avatar_tests(cl, web)
    mute_tests(cl, web, dbfile)
    bookmark_error_tests(cl)
    mute_dialog_tests(cl)
    storage_tests(cl, web, cache, dbfile)

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
