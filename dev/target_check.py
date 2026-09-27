"""
ig-ray / dev/target_check.py

監視対象（is_target）の検査。

v4.16 で入れた仕様:
  * 共同投稿の相手など、投稿データから自動でできた accounts 行は
    **巡回対象にも表示対象にもしない**
  * 表示は「監視対象のみ」。ミュートは従来どおり別軸
  * ブックマークは非正規化コピーなので監視対象外でも残す
  * /user/<name> は監視対象外でも直接アクセスできる（リンクを切らない）

    python3 dev/target_check.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          f"{('  -- ' + detail) if detail and not cond else ''}")


def media(n=1):
    return json.dumps([
        {"index": i, "is_video": False,
         "image_url": f"https://cdn.example/{i}.jpg", "video_url": None}
        for i in range(n)
    ], ensure_ascii=False)


def build_db(path):
    """監視対象2件 ＋ 自動でできた相手2件。それぞれ投稿を持たせる。"""
    import db
    c = db.connect(path)
    db.init_db(c)

    # 意図して登録した対象
    for u in ("target_a", "target_b"):
        c.execute("INSERT INTO accounts (username, is_enabled, is_target) "
                  "VALUES (?, 1, 1)", (u,))
    # 共同投稿で流れてきた相手（scraper と同じ経路で作る）
    db.merge_account(c, {"username": "coauthor_x", "userid": 9,
                         "full_name": "Co X", "profile_pic_url": None,
                         "profile_pic_local": None})
    db.ensure_account(c, "related_y")

    rows = []
    for u, sc in (("target_a", "TA1"), ("target_b", "TB1"),
                  ("coauthor_x", "CX1"), ("related_y", "RY1")):
        rows.append(dict(
            shortcode=sc, mediaid=None, owner_username=u, caption=u,
            date_utc="2026-07-30T10:00:00+00:00", likes=1, comments=0,
            typename="GraphImage", is_video=0, is_carousel=0, location=None,
            hashtags_json="[]", media_json=media(1), local_media_json=None))
    db.save_posts(c, rows)
    c.commit()
    return c


# --------------------------------------------------------------------------

def test_db_layer(db, c):
    print("\n[1] DB層: 自動でできた行は監視対象にならない")
    check("merge_account の行は is_target=0",
          not db.is_target(c, "coauthor_x"))
    check("ensure_account の行も is_target=0",
          not db.is_target(c, "related_y"))
    check("merge_account の行は is_enabled も 0",
          c.execute("SELECT is_enabled FROM accounts WHERE username='coauthor_x'"
                    ).fetchone()["is_enabled"] == 0)

    check("巡回対象は監視対象だけ",
          set(db.enabled_accounts(c)) == {"target_a", "target_b"},
          str(db.enabled_accounts(c)))
    check("target_usernames も同じ",
          db.target_usernames(c) == {"target_a", "target_b"},
          str(db.target_usernames(c)))

    print("\n[2] 昇格・降格")
    db.set_target(c, "coauthor_x", True)
    c.commit()
    check("target で巡回対象に入る",
          "coauthor_x" in db.enabled_accounts(c))
    check("target は is_enabled も立てる",
          c.execute("SELECT is_enabled FROM accounts WHERE username='coauthor_x'"
                    ).fetchone()["is_enabled"] == 1)

    db.set_target(c, "coauthor_x", False)
    c.commit()
    check("untarget で巡回対象から外れる",
          "coauthor_x" not in db.enabled_accounts(c))
    check("untarget しても行は残る",
          c.execute("SELECT COUNT(*) n FROM accounts WHERE username='coauthor_x'"
                    ).fetchone()["n"] == 1)
    check("untarget しても投稿は残る",
          c.execute("SELECT COUNT(*) n FROM posts WHERE owner_username='coauthor_x'"
                    ).fetchone()["n"] == 1)

    print("\n[3] disable と is_target は別軸")
    # 巡回だけ止めた対象は、表示には出したい
    c.execute("UPDATE accounts SET is_enabled = 0 WHERE username='target_b'")
    c.commit()
    check("disable は巡回対象から外れる",
          "target_b" not in db.enabled_accounts(c))
    check("disable でも表示対象のまま",
          "target_b" in db.target_usernames(c))

    c.execute("UPDATE accounts SET is_enabled = 1 WHERE username='target_b'")
    c.commit()

    print("\n[4] strays / target_accounts")
    strays = {r["username"]: r for r in db.stray_accounts(c)}
    check("coauthor_x は stray", "coauthor_x" in strays)
    check("related_y は stray", "related_y" in strays)
    check("target_a は stray ではない", "target_a" not in strays)
    check("stray の投稿数が入る",
          strays.get("coauthor_x", {}).get("n_posts") == 1,
          str(strays.get("coauthor_x")))
    tgts = {r["username"] for r in db.target_accounts(c)}
    check("target_accounts は対象だけ", tgts == {"target_a", "target_b"}, str(tgts))


def test_web_layer(cli, db, c):
    print("\n[5] フィード: 監視対象外は出ない")
    body = cli.get("/").get_data(as_text=True)
    check("target_a の投稿が出る", "TA1" in body)
    check("target_b の投稿が出る", "TB1" in body)
    check("coauthor_x の投稿は出ない", "CX1" not in body)
    check("related_y の投稿は出ない", "RY1" not in body)
    check("非表示件数が出る", "非表示 2件" in body, body[:0])

    print("\n[6] ギャラリー")
    g = cli.get("/gallery").get_data(as_text=True)
    check("対象の画像は出る", "TA1" in g)
    check("対象外の画像は出ない", "CX1" not in g)
    check("プルダウンに対象外は出ない",
          ">coauthor_x<" not in g and "value=\"coauthor_x\"" not in g)
    # 個別指定なら見せる（ブックマークからの導線を切らない）
    g2 = cli.get("/gallery?user=coauthor_x").get_data(as_text=True)
    check("user 指定なら対象外でも見える", "CX1" in g2)

    print("\n[7] ユーザーページは対象外でも開ける")
    r = cli.get("/user/coauthor_x")
    check("404 にしない", r.status_code == 200, str(r.status_code))
    ub = r.get_data(as_text=True)
    check("監視対象外の注記が出る", "監視対象外" in ub)
    check("投稿自体は見える", "CX1" in ub)
    ua = cli.get("/user/target_a").get_data(as_text=True)
    check("対象には注記が出ない", "監視対象外" not in ua)

    print("\n[8] ブックマークは監視対象外でも残る")
    db.add_bookmark(c, "CX1")
    c.commit()
    b = cli.get("/bookmarks").get_data(as_text=True)
    check("対象外の投稿もブックマークに出る", "CX1" in b)

    print("\n[9] ミュート画面")
    m = cli.get("/mutes").get_data(as_text=True)
    check("監視対象外の節が出る", "監視対象外のアカウント" in m)
    check("ミュート候補に対象外は出ない",
          "onclick=\"toggleMute('coauthor_x'" not in m)
    check("ミュート候補に対象は出る",
          "onclick=\"toggleMute('target_a'" in m)

    print("\n[10] 監視対象が0件なら絞り込まない（全部消える事故を防ぐ）")
    c.execute("UPDATE accounts SET is_target = 0")
    c.commit()
    import web
    web._target_cache.update({"at": 0.0, "set": None})
    web._muted_cache.update({"at": 0.0, "set": frozenset()})
    body = cli.get("/").get_data(as_text=True)
    check("対象0件でも投稿は出る", "TA1" in body and "CX1" in body)

    print("\n[11] ミュートは監視対象でも効く")
    c.execute("UPDATE accounts SET is_target = 1 "
              "WHERE username IN ('target_a','target_b')")
    c.commit()
    db.set_mute(c, "target_b", True)
    c.commit()
    web._target_cache.update({"at": 0.0, "set": None})
    web._muted_cache.update({"at": 0.0, "set": frozenset()})
    body = cli.get("/").get_data(as_text=True)
    check("ミュートした対象は出ない", "TB1" not in body)
    check("ほかの対象は出る", "TA1" in body)


def test_column_missing(db):
    """is_target 列が無い旧DBでも落ちない（空集合と None を混同しない）。"""
    print("\n[12] 旧DB互換")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "old.db")
    c = db.connect(path)
    c.execute("CREATE TABLE accounts (username TEXT PRIMARY KEY, is_enabled INTEGER)")
    c.commit()
    check("列が無ければ None を返す（=絞り込みなし）",
          db.target_usernames(c) is None, str(db.target_usernames(c)))
    c.close()


def test_migration(db):
    print("\n[13] 既存DBからのマイグレーション")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "mig.db")
    c = db.connect(path)
    # is_target が無い時代の accounts を作る
    c.execute("""CREATE TABLE accounts (
        username TEXT PRIMARY KEY, userid INTEGER, full_name TEXT,
        biography TEXT, profile_pic_url TEXT, followers INTEGER,
        mediacount INTEGER, categories_json TEXT,
        is_enabled INTEGER NOT NULL DEFAULT 1, note TEXT,
        added_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT)""")
    c.execute("INSERT INTO accounts (username, is_enabled) VALUES ('kept', 1)")
    c.execute("INSERT INTO accounts (username, is_enabled) VALUES ('off', 0)")
    c.commit()
    db.init_db(c)

    check("列が追加される",
          "is_target" in {r["name"] for r in c.execute("PRAGMA table_info(accounts)")})
    check("is_enabled=1 は監視対象として引き継ぐ", db.is_target(c, "kept"))
    check("is_enabled=0 は引き継がない", not db.is_target(c, "off"))
    check("巡回対象は変わらない", db.enabled_accounts(c) == ["kept"],
          str(db.enabled_accounts(c)))
    check("schema_version が上がる",
          c.execute("SELECT value FROM meta WHERE key='schema_version'"
                    ).fetchone()["value"] == str(db.SCHEMA_VERSION))
    check("share_tokens は落とされる",
          not c.execute("SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='share_tokens'").fetchone())
    c.close()


def test_cli(db):
    print("\n[14] CLI（add / target / untarget / retarget）")
    import seed_accounts as sa
    import argparse
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "cli.db")
    c = db.connect(path)
    db.init_db(c)

    sa.cmd_add(c, argparse.Namespace(usernames=["NewTarget"], note=None))
    check("add は監視対象にする", db.is_target(c, "newtarget"))
    check("add は is_enabled も立てる",
          c.execute("SELECT is_enabled FROM accounts WHERE username='newtarget'"
                    ).fetchone()["is_enabled"] == 1)

    # 自動でできた行を後から対象にする
    db.merge_account(c, {"username": "late", "userid": None, "full_name": None,
                         "profile_pic_url": None, "profile_pic_local": None})
    c.commit()
    check("自動行は最初は対象外", not db.is_target(c, "late"))
    sa.cmd_add(c, argparse.Namespace(usernames=["late"], note=None))
    check("add で自動行を昇格できる", db.is_target(c, "late"))

    sa.cmd_untarget(c, argparse.Namespace(usernames=["late"]))
    check("untarget で外れる", not db.is_target(c, "late"))

    # retarget: ファイルの内容で置き換える
    lst = os.path.join(tmp, "targets.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.write("# コメント\nnewtarget\nfrom_file\n")
    sa.cmd_retarget(c, argparse.Namespace(path=lst, apply=False))
    check("--apply 無しでは変えない", not db.is_target(c, "from_file"))
    sa.cmd_retarget(c, argparse.Namespace(path=lst, apply=True))
    check("ファイルの名前が対象になる", db.is_target(c, "from_file"))
    check("既存の対象も維持される", db.is_target(c, "newtarget"))

    # ファイルから消えた対象は外れる
    with open(lst, "w", encoding="utf-8") as f:
        f.write("from_file\n")
    sa.cmd_retarget(c, argparse.Namespace(path=lst, apply=True))
    check("ファイルに無い対象は外れる", not db.is_target(c, "newtarget"))
    check("行は消えない",
          c.execute("SELECT COUNT(*) n FROM accounts WHERE username='newtarget'"
                    ).fetchone()["n"] == 1)

    # targets --plain → 編集 → retarget の往復
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sa.cmd_targets(c, argparse.Namespace(plain=True))
    names = [l for l in buf.getvalue().splitlines() if l.strip()]
    check("targets --plain は1行1名のみ",
          names == sorted(db.is_target(c, n) and n for n in names) or
          all(db.is_target(c, n) for n in names), str(names))
    check("targets --plain に対象外が混ざらない",
          all(db.is_target(c, n) for n in names), str(names))
    seed = os.path.join(tmp, "seed.txt")
    with open(seed, "w", encoding="utf-8") as f:
        f.write("\n".join(names) + "\n")
    sa.cmd_retarget(c, argparse.Namespace(path=seed, apply=True))
    check("書き出したリストを食わせても差分が出ない",
          {r["username"] for r in db.target_accounts(c)} == set(names),
          str(db.target_accounts(c)))

    rc = sa.cmd_retarget(c, argparse.Namespace(
        path=os.path.join(tmp, "nope.txt"), apply=True))
    check("ファイルが無ければエラー終了", rc == 1)

    # 空ファイルで全部外れる事故を防ぐ
    empty = os.path.join(tmp, "empty.txt")
    open(empty, "w").close()
    rc = sa.cmd_retarget(c, argparse.Namespace(path=empty, apply=True))
    check("空ファイルは中止する", rc == 1)
    check("空ファイルでは対象が消えない", db.is_target(c, "from_file"))
    c.close()


def main():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    cache = os.path.join(tmp, "cache")
    os.makedirs(cache, exist_ok=True)

    os.environ["IG_RAY_DB"] = path
    os.environ["IG_RAY_CACHE"] = cache
    os.environ.pop("IG_RAY_PUBLIC_SHARE_HOST", None)

    import db
    c = build_db(path)

    test_db_layer(db, c)

    import web
    web.app.config["TESTING"] = True
    web._target_cache.update({"at": 0.0, "set": None})
    web._muted_cache.update({"at": 0.0, "set": frozenset()})
    with web.app.test_client() as cli:
        test_web_layer(cli, db, c)
    c.close()

    test_column_missing(db)
    test_migration(db)
    test_cli(db)

    print("\n" + "=" * 50)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    for n in FAIL:
        print(f"  - {n}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
