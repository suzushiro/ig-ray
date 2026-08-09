"""
insta-ray / dev/migration_check.py

**既存DBからのマイグレーション**を検証する。

v3.4 で `no such column: date_utc` を踏んだ。
bookmarks の新しい列に張るインデックスを DDL リストに入れていたため、
既存DBでは ALTER TABLE より先に CREATE INDEX が走って落ちていた。
新しい列を参照する DDL は必ずマイグレーションの後ろに置くこと。

    python3 dev/migration_check.py
"""

import os, sys, sqlite3, tempfile, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import db

PASS=[];FAIL=[]
def check(n,c,d=""):
    (PASS if c else FAIL).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}{('  -- '+d) if d and not c else ''}")

def make_v1_db(path):
    """v1〜v2.9 相当（bookmarks は shortcode/note/created_at のみ）"""
    c=sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE accounts (username TEXT PRIMARY KEY, userid INTEGER, full_name TEXT,
        biography TEXT, profile_pic_url TEXT, followers INTEGER, mediacount INTEGER,
        categories_json TEXT, is_enabled INTEGER NOT NULL DEFAULT 1, note TEXT,
        added_at TEXT, updated_at TEXT);
    CREATE TABLE posts (shortcode TEXT PRIMARY KEY, mediaid INTEGER, owner_username TEXT,
        caption TEXT, date_utc TEXT, likes INTEGER, comments INTEGER, typename TEXT,
        is_video INTEGER, is_carousel INTEGER, location TEXT, hashtags_json TEXT,
        media_json TEXT, fetched_at TEXT);
    CREATE TABLE bookmarks (shortcode TEXT PRIMARY KEY, note TEXT, created_at TEXT);
    CREATE TABLE media_index (id INTEGER PRIMARY KEY AUTOINCREMENT, shortcode TEXT,
        media_index INTEGER, is_video INTEGER, remote_url TEXT, local_path TEXT,
        sha256 TEXT, bytes INTEGER, is_persistent INTEGER DEFAULT 0, created_at TEXT,
        UNIQUE(shortcode, media_index));
    CREATE TABLE scrape_log (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT,
        status TEXT, fetched INTEGER, inserted INTEGER, message TEXT,
        started_at TEXT, ended_at TEXT);
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    c.execute("INSERT INTO accounts (username, followers) VALUES ('legacy', 777)")
    c.execute("INSERT INTO posts (shortcode, owner_username, date_utc, media_json) "
              "VALUES ('OLD1','legacy','2026-07-01T00:00:00+00:00','[]')")
    c.execute("INSERT INTO bookmarks (shortcode, note) VALUES ('OLD1','古いブックマーク')")
    c.commit(); c.close()

print("[1] 旧スキーマDBの初期化")
f = os.path.join(tempfile.mkdtemp(), "old.db")
make_v1_db(f)
conn = db.connect(f)
err = None
try:
    db.init_db(conn)
except Exception as e:
    err = e
check("init_db が例外を出さない", err is None, f"{type(err).__name__}: {err}" if err else "")

if err is None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bookmarks)")}
    check("bookmarks に date_utc が追加される", "date_utc" in cols)
    check("bookmarks に media_json が追加される", "media_json" in cols)
    check("bookmarks に local_media_json が追加される", "local_media_json" in cols)
    acols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
    check("accounts に profile_pic_local が追加される", "profile_pic_local" in acols)
    check("accounts に is_muted が追加される", "is_muted" in acols)
    check("accounts に muted_at が追加される", "muted_at" in acols)
    check("旧データの is_muted は 0 扱い",
          conn.execute("SELECT COALESCE(is_muted,0) m FROM accounts "
                       "WHERE username='legacy'").fetchone()["m"] == 0)
    check("ミュートしていなければ巡回対象",
          "legacy" in db.enabled_accounts(conn), str(db.enabled_accounts(conn)))

    idx = {r[1] for r in conn.execute("PRAGMA index_list(bookmarks)")}
    check("date_utc のインデックスが張られる",
          any("idx_bookmarks_date" in i for i in idx), str(idx))

    print("\n[2] 既存データが壊れない")
    check("旧ブックマークが残る",
          conn.execute("SELECT COUNT(*) c FROM bookmarks").fetchone()["c"] == 1)
    check("旧ブックマークの note が残る",
          conn.execute("SELECT note FROM bookmarks").fetchone()["note"] == "古いブックマーク")
    check("accounts の followers が残る",
          conn.execute("SELECT followers FROM accounts WHERE username='legacy'"
                       ).fetchone()["followers"] == 777)
    check("posts が残る",
          conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"] == 1)
    check("schema_version が最新に更新される",
          conn.execute("SELECT value FROM meta WHERE key='schema_version'"
                       ).fetchone()["value"] == str(db.SCHEMA_VERSION))

    print("\n[3] マイグレーション後に機能が動く")
    db.merge_account(conn, {"username":"legacy","full_name":"Legacy",
                            "profile_pic_local":"/data/cache/_avatars/legacy.jpg"})
    r = conn.execute("SELECT * FROM accounts WHERE username='legacy'").fetchone()
    check("merge_account が効く", r["full_name"] == "Legacy")
    check("followers を潰さない", r["followers"] == 777)

    ok = db.add_bookmark(conn, "OLD1")
    check("旧ブックマークを上書き登録できる", ok)
    b = conn.execute("SELECT * FROM bookmarks WHERE shortcode='OLD1'").fetchone()
    check("date_utc が埋まる", bool(b["date_utc"]), str(b["date_utc"]))
    check("owner_username が埋まる", b["owner_username"] == "legacy")

    print("\n[4] 二度目の init_db も通る（冪等）")
    err2 = None
    try:
        db.init_db(conn)
    except Exception as e:
        err2 = e
    check("再実行で落ちない", err2 is None, str(err2))
    conn.close()

print(f"\n{'='*50}")
print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("すべて通過")
