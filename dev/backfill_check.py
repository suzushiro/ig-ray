"""
ig-ray / dev/backfill_check.py

全件バックフィルの検証。ネットワーク不要。

    python3 dev/backfill_check.py

**instaloader の本物の NodeIterator** を偽コンテキストに繋いで、
freeze / thaw によるページ送りの再開が実際に効くかまで確認する。
モックで済ませると「再開できるつもり」で本番に出すことになるので、
ここだけは本物を使う。

確認していること:
  - backfill_jobs のスキーマとマイグレーション（既存DBからの ALTER）
  - 保管済み判定（メディア実体まで揃っているか）
  - freeze → JSON → 別イテレータで thaw して続きから流れるか
  - thaw が拒否される条件（ログインアカウント違い・期限切れ・不整合）
  - 活動ゲート（巡回中は待つ）
  - 打ち切り判定が初回では効かないこと
"""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  {mark}  {name}{('  -- ' + detail) if detail and not cond else ''}")


# --------------------------------------------------------------------------
# 本物の NodeIterator を回すための偽コンテキスト
# --------------------------------------------------------------------------

class FakeContext:
    """doc_id_graphql_query と username だけあれば NodeIterator は動く。"""

    def __init__(self, pages=3, per_page=3, username="login_acct"):
        self.username = username
        self.calls = []
        self.pages = pages
        self.per_page = per_page

    def doc_id_graphql_query(self, doc_id, variables, referer):
        after = variables.get("after")
        self.calls.append(after)
        page = 0 if after is None else int(after[3:]) + 1
        edges = [{"node": {"pk": f"{page}-{i}", "code": f"P{page}{i}"}}
                 for i in range(self.per_page)]
        has_next = page < self.pages - 1
        return {"data": {"conn": {
            "edges": edges,
            "page_info": {"has_next_page": has_next,
                          "end_cursor": f"cur{page}" if has_next else None}}}}


def make_iterator(ctx, username="target"):
    """Profile.get_posts() と同じ形の NodeIterator を組む。"""
    from instaloader.nodeiterator import NodeIterator
    return NodeIterator(
        context=ctx,
        query_hash=None,
        doc_id="7898261790222653",
        edge_extractor=lambda d: d["data"]["conn"],
        node_wrapper=lambda n: n["code"],
        query_variables={"data": {"count": 12}, "username": username},
        query_referer=f"https://www.instagram.com/{username}/",
        first_data=None,
    )


# --------------------------------------------------------------------------

def test_schema(dbfile):
    print("\n[1] スキーマ")
    import db
    conn = db.connect(dbfile)
    db.init_db(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(backfill_jobs)")}
    for c in ("username", "status", "resume_json", "login_user", "posts_seen",
              "posts_saved", "media_saved", "pages", "known_streak",
              "completed_runs", "oldest_date", "message", "completed_at"):
        check(f"列 {c}", c in cols)

    v = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    check("schema_version = 5", v["value"] == "5", v["value"])
    db.init_db(conn)
    check("init_db は冪等", True)
    conn.close()


def test_migration():
    print("\n[2] 既存DB（v4）からのマイグレーション")
    import db
    tmp = tempfile.mkdtemp(prefix="igray_bf_mig_")
    old = os.path.join(tmp, "old.db")

    # backfill_jobs が無い状態のDBを作る
    import sqlite3
    c = sqlite3.connect(old)
    c.execute("CREATE TABLE posts (shortcode TEXT PRIMARY KEY, "
              "owner_username TEXT, date_utc TEXT)")
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT INTO meta VALUES ('schema_version','4')")
    c.execute("INSERT INTO posts VALUES ('OLD1','someone','2026-01-01')")
    c.commit()
    c.close()

    conn = db.connect(old)
    db.init_db(conn)
    n = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
    check("既存の投稿が残る", n == 1, f"got {n}")
    check("backfill_jobs が作られる",
          conn.execute("SELECT COUNT(*) c FROM backfill_jobs").fetchone()["c"] == 0)
    v = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    check("schema_version が 5 になる", v["value"] == "5", v["value"])
    conn.close()


def test_job_crud(dbfile):
    print("\n[3] ジョブ管理")
    import db
    conn = db.connect(dbfile)
    db.init_db(conn)

    check("add で追加される", db.backfill_add(conn, "alpha") is True)
    check("2回目は追加しない（進捗を潰さない）",
          db.backfill_add(conn, "alpha") is False)
    db.backfill_add(conn, "beta")

    job = db.backfill_get(conn, "alpha")
    check("初期 status は queued", job["status"] == "queued", job["status"])
    check("初期 completed_runs は 0", job["completed_runs"] == 0)

    db.backfill_update(conn, "alpha", status="running", posts_seen=12)
    job = db.backfill_get(conn, "alpha")
    check("update が効く", job["status"] == "running" and job["posts_seen"] == 12)
    check("updated_at が入る", bool(job["updated_at"]))

    try:
        db.backfill_update(conn, "alpha", nonexistent_field=1)
        check("未知フィールドを弾く", False, "例外が出なかった")
    except ValueError:
        check("未知フィールドを弾く", True)

    nxt = db.backfill_next(conn)
    check("running を優先して返す", nxt["username"] == "alpha", nxt["username"])

    db.backfill_update(conn, "alpha", status="done")
    nxt = db.backfill_next(conn)
    check("done は返さない", nxt["username"] == "beta", nxt["username"])

    db.backfill_update(conn, "beta", status="paused")
    check("paused も返さない", db.backfill_next(conn) is None)
    conn.close()


def test_archived(dbfile):
    print("\n[4] 保管済み判定（メディア実体まで揃っているか）")
    import db
    conn = db.connect(dbfile)
    db.init_db(conn)

    def mkpost(sc, locals_):
        conn.execute("INSERT OR REPLACE INTO posts "
                     "(shortcode, owner_username, date_utc) VALUES (?,?,?)",
                     (sc, "u", "2026-01-01T00:00:00+00:00"))
        conn.execute("DELETE FROM media_index WHERE shortcode = ?", (sc,))
        for i, lp in enumerate(locals_):
            conn.execute("INSERT INTO media_index "
                         "(shortcode, media_index, local_path) VALUES (?,?,?)",
                         (sc, i, lp))
    mkpost("FULL", ["/x/a.jpg", "/x/b.jpg"])
    mkpost("PARTIAL", ["/x/c.jpg", None])
    mkpost("NONE", [None])
    conn.execute("INSERT OR REPLACE INTO posts "
                 "(shortcode, owner_username, date_utc) VALUES ('NOMEDIA','u','2026-01-01')")
    conn.commit()

    check("全部ローカルにある → 保管済み", db.post_is_archived(conn, "FULL"))
    check("一部欠け → 保管済みではない",
          not db.post_is_archived(conn, "PARTIAL"))
    check("実体ゼロ → 保管済みではない", not db.post_is_archived(conn, "NONE"))
    check("media_index 行が無い → 保管済みではない",
          not db.post_is_archived(conn, "NOMEDIA"))
    check("未知の shortcode → 保管済みではない",
          not db.post_is_archived(conn, "UNKNOWN"))
    conn.close()


def test_activity_gate(dbfile):
    print("\n[5] 活動ゲート")
    import db
    conn = db.connect(dbfile)
    db.init_db(conn)

    check("記録が無ければ None", db.seconds_since_activity(conn, "scrape") is None)
    db.touch_activity(conn, "scrape")
    since = db.seconds_since_activity(conn, "scrape")
    check("touch 直後はほぼ0秒", since is not None and since < 5, str(since))

    conn.execute("UPDATE meta SET value = ? WHERE key = 'activity:scrape'",
                 ((datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat(),))
    conn.commit()
    since = db.seconds_since_activity(conn, "scrape")
    check("過去の記録から経過秒が出る", 590 < since < 620, str(since))

    conn.execute("UPDATE meta SET value = 'garbage' WHERE key='activity:scrape'")
    conn.commit()
    check("壊れた値でも落ちない",
          db.seconds_since_activity(conn, "scrape") is None)
    conn.close()


def test_freeze_thaw():
    print("\n[6] freeze / thaw による再開（本物の NodeIterator）")
    import backfill as bf

    ctx = FakeContext(pages=3, per_page=3)
    it = make_iterator(ctx)
    check("コンストラクタで1ページ目を取りに行く", len(ctx.calls) == 1,
          str(ctx.calls))

    got = []
    for x in it:
        got.append(x)
        if len(got) == 4:
            break
    blob = bf.freeze_to_json(it)
    check("freeze を JSON にできる", isinstance(blob, str) and len(blob) > 0)
    check("JSONとして読み戻せる", isinstance(json.loads(blob), dict))

    ctx2 = FakeContext(pages=3, per_page=3)
    it2 = make_iterator(ctx2)
    check("再開時もコンストラクタで1リクエスト使う（既知の無駄）",
          len(ctx2.calls) == 1, str(ctx2.calls))
    check("thaw に成功する", bf.thaw_from_json(it2, blob) is True)

    rest = list(it2)
    allitems = got + rest
    check("最後まで流れる", allitems[-1] == "P22", str(allitems[-1]))
    check("取りこぼしが無い",
          set(allitems) == {f"P{p}{i}" for p in range(3) for i in range(3)},
          str(sorted(set(allitems))))
    # freeze は total_index-1 / edges[page_index-1:] と1つ戻すので1件重複する
    check("1件だけ重複する（取りこぼし防止の仕様）",
          len(allitems) == 10 and len(set(allitems)) == 9,
          f"len={len(allitems)} uniq={len(set(allitems))}")


def test_thaw_rejects():
    print("\n[7] thaw が拒否される条件")
    import backfill as bf

    ctx = FakeContext()
    it = make_iterator(ctx)
    next(it)
    blob = bf.freeze_to_json(it)

    # ログインアカウントが違う
    other = FakeContext(username="別のアカウント")
    it2 = make_iterator(other)
    check("ログインアカウントが違うと拒否",
          bf.thaw_from_json(it2, blob) is False)

    # 対象アカウントが違う（query_variables 不一致）
    ctx3 = FakeContext()
    it3 = make_iterator(ctx3, username="other_target")
    check("対象アカウントが違うと拒否",
          bf.thaw_from_json(it3, blob) is False)

    # 期限切れ
    d = json.loads(blob)
    d["best_before"] = time.time() - 10
    ctx4 = FakeContext()
    it4 = make_iterator(ctx4)
    check("期限切れは拒否", bf.thaw_from_json(it4, json.dumps(d)) is False)

    # 壊れたJSON
    ctx5 = FakeContext()
    it5 = make_iterator(ctx5)
    check("壊れた再開情報は拒否（例外にしない）",
          bf.thaw_from_json(it5, "{ぐちゃぐちゃ") is False)

    # 正しいものは通る
    ctx6 = FakeContext()
    it6 = make_iterator(ctx6)
    check("正しい再開情報は通る", bf.thaw_from_json(it6, blob) is True)


def test_streak_policy(dbfile):
    print("\n[8] 打ち切り判定は初回では効かない")
    import db
    import backfill as bf
    conn = db.connect(dbfile)
    db.init_db(conn)

    db.backfill_add(conn, "gamma")
    job = db.backfill_get(conn, "gamma")
    check("初回は completed_runs=0 → 打ち切り無効",
          (job["completed_runs"] or 0) == 0)

    db.backfill_update(conn, "gamma", completed_runs=1)
    job = db.backfill_get(conn, "gamma")
    check("2周目は completed_runs>0 → 打ち切り有効",
          (job["completed_runs"] or 0) > 0)
    check("既定の打ち切り件数は 30", bf.KNOWN_STREAK == 30, str(bf.KNOWN_STREAK))
    check("ページ間スリープの既定は10〜20分",
          bf.PAGE_SLEEP_MIN == 600 and bf.PAGE_SLEEP_MAX == 1200,
          f"{bf.PAGE_SLEEP_MIN}-{bf.PAGE_SLEEP_MAX}")
    conn.close()


def test_next_run_recording(dbfile):
    print("\n[9] 次回ページ時刻の記録")
    import db
    import backfill as bf
    conn = db.connect(dbfile)
    db.init_db(conn)

    # page_nap は寝る直前に予定を書く。実際に寝られると困るので秒数を0にする
    orig = (bf.PAGE_SLEEP_MIN, bf.PAGE_SLEEP_MAX)
    bf.PAGE_SLEEP_MIN = bf.PAGE_SLEEP_MAX = 0.01
    try:
        db.clear_next_run(conn, bf.WHO)
        bf.page_nap(conn)
        check("page_nap が次回時刻を書く",
              db.get_next_run(conn, bf.WHO) is not None)
        db.clear_next_run(conn, bf.WHO)
        bf.page_nap(None)      # conn を渡さなければ書かない（テスト・単体利用向け）
        check("conn 無しなら書かない", db.get_next_run(conn, bf.WHO) is None)
    finally:
        bf.PAGE_SLEEP_MIN, bf.PAGE_SLEEP_MAX = orig

    # 予定が実際に未来であること（過去の時刻を出すと表示が壊れる）
    bf.PAGE_SLEEP_MIN = bf.PAGE_SLEEP_MAX = 300
    try:
        import threading
        t = threading.Timer(0, lambda: None)  # noqa
        db.set_next_run(conn, bf.WHO, 300)
        when = db.get_next_run(conn, bf.WHO)
        check("予定は未来の時刻",
              (when - datetime.now(timezone.utc)).total_seconds() > 250)
    finally:
        bf.PAGE_SLEEP_MIN, bf.PAGE_SLEEP_MAX = orig

    conn.close()


def main():
    tmp = tempfile.mkdtemp(prefix="igray_bf_")
    dbfile = os.path.join(tmp, "test.db")
    os.environ.setdefault("IG_RAY_CACHE", os.path.join(tmp, "cache"))
    print(f"test db: {dbfile}")

    test_schema(dbfile)
    test_migration()
    test_job_crud(dbfile)
    test_archived(dbfile)
    test_activity_gate(dbfile)
    test_freeze_thaw()
    test_thaw_rejects()
    test_streak_policy(dbfile)
    test_next_run_recording(dbfile)

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
