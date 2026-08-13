"""
ig-ray / dev/backup_page_check.py

バックアップ状況画面（/backup）と「次回開始時刻」の記録の検証。
ネットワーク不要。

    python3 dev/backup_page_check.py

ここで一番気にしているのは **嘘の時刻を出さないこと**。
cron もバックフィルも「1周終わってから N 秒後」という動き方なので、
表示側で「前回＋間隔」を計算すると、
  - 処理時間ぶんズレる
  - バックフィルの間隔はランダムなので当たらない
  - **ワーカーが止まっていても未来の時刻を出してしまう**
実行側が寝る直前に meta へ書いたものだけを出す、という前提を固定する。
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  {mark}  {name}{('  -- ' + detail) if detail and not cond else ''}")


def test_next_run_helpers(dbfile):
    print("\n[1] 次回時刻の記録")
    import db
    conn = db.connect(dbfile)
    db.init_db(conn)

    check("未記録なら None", db.get_next_run(conn, "scrape") is None)

    when = db.set_next_run(conn, "scrape", 3600)
    got = db.get_next_run(conn, "scrape")
    check("記録して読み戻せる", got is not None)
    check("tz-aware で返る", got.tzinfo is not None)
    delta = (got - datetime.now(timezone.utc)).total_seconds()
    check("だいたい1時間後", 3550 < delta < 3610, f"{delta:.0f}s")
    check("戻り値と一致", abs((when - got).total_seconds()) < 1)

    db.set_next_run(conn, "scrape", 60)
    d2 = (db.get_next_run(conn, "scrape") - datetime.now(timezone.utc)).total_seconds()
    check("上書きできる", d2 < 100, f"{d2:.0f}s")

    db.clear_next_run(conn, "scrape")
    check("消せる", db.get_next_run(conn, "scrape") is None)

    # 別の who と混ざらない
    db.set_next_run(conn, "backfill", 120)
    check("who ごとに独立", db.get_next_run(conn, "scrape") is None
          and db.get_next_run(conn, "backfill") is not None)

    conn.execute("UPDATE meta SET value='ぐちゃぐちゃ' WHERE key='next_run:backfill'")
    conn.commit()
    check("壊れた値でも落ちない", db.get_next_run(conn, "backfill") is None)
    conn.close()


def test_scraper_cron_flag():
    print("\n[2] --cron のときだけ次回を記録する")
    import ig_scraper as igs
    check("CRON_INTERVAL が読める", igs.CRON_INTERVAL == 21600,
          str(igs.CRON_INTERVAL))

    # 引数定義に --cron があること（手動実行では付けない前提の担保）
    import argparse
    import inspect
    src = inspect.getsource(igs.main)
    check("--cron オプションがある", '"--cron"' in src)
    check("--cron のときだけ set_next_run する",
          'if args.cron:' in src and 'set_next_run' in src)
    check("手動実行を想定した注意書きがある", "手動実行" in src)


def build(dbfile, cache_dir):
    import db
    conn = db.connect(dbfile)
    db.init_db(conn)
    for u in ("alpha", "beta", "gamma"):
        conn.execute("INSERT INTO accounts (username) VALUES (?)", (u,))
        conn.execute("INSERT INTO posts (shortcode, owner_username, date_utc) "
                     "VALUES (?,?,?)", ("S" + u, u, "2026-05-01T00:00:00+00:00"))
    conn.execute(
        "INSERT INTO backfill_jobs (username,status,posts_seen,posts_saved,"
        "media_saved,pages,completed_runs,oldest_date) "
        "VALUES ('alpha','running',480,455,1200,40,0,'2024-03-11')")
    conn.execute(
        "INSERT INTO backfill_jobs (username,status,posts_seen,posts_saved,"
        "media_saved,pages,completed_runs,oldest_date,message) "
        "VALUES ('beta','done',1203,1203,3300,101,1,'2019-08-02','打ち切り（保管済み）')")
    conn.execute("INSERT INTO backfill_jobs (username,status) VALUES ('gamma','queued')")
    db.log_scrape(conn, "alpha", "ok_degraded", 6, 2)
    db.log_scrape(conn, "beta", "ok_degraded", 6, 0)
    db.log_scrape(conn, "gamma", "backfill_done", 500, 500)
    conn.commit()
    return conn


def client(dbfile, cache_dir):
    import importlib
    os.environ["IG_RAY_DB"] = dbfile
    os.environ["IG_RAY_CACHE"] = cache_dir
    import config, db, cache_utils, web
    for m in (config, db, cache_utils, web):
        importlib.reload(m)
    web.app.testing = True
    return web.app.test_client()


def test_page(dbfile, cache_dir):
    print("\n[3] /backup の表示")
    import db
    conn = build(dbfile, cache_dir)
    db.set_next_run(conn, "scrape", 5400)
    db.set_next_run(conn, "backfill", 760)
    db.touch_activity(conn, "backfill_worker")
    conn.close()

    cl = client(dbfile, cache_dir)
    r = cl.get("/backup")
    check("200 で返る", r.status_code == 200, str(r.status_code))
    h = r.get_data(as_text=True)

    check("巡回セクションがある", "定期巡回" in h)
    check("バックフィルセクションがある", "全件バックフィル" in h)
    check("カウントダウンの要素がある", "data-countdown" in h)
    check("次回時刻をISOで渡している", h.count("data-countdown=") >= 2,
          str(h.count("data-countdown=")))
    check("ジョブの行が出る", "alpha" in h and "beta" in h and "gamma" in h)
    check("処理中を示す", "処理中" in h)
    check("遡った日付が出る", "2019-08-02" in h)
    check("メッセージが出る", "打ち切り" in h)
    check("状態バッジが出る", 'class="pill running"' in h)
    check("巡回の結果集計が出る", "ok_degraded" in h)
    check("進捗率を出せない理由を明記", "進捗率" in h and "mediacount" in h)
    check("ワーカー稼働中なら警告を出さない",
          "ワーカーが動いていないようです" not in h)
    check("ナビにリンクがある", 'href="/backup"' in h)


def test_no_data(cache_dir):
    print("\n[4] 記録が無いときは推測しない")
    import db
    tmp = tempfile.mkdtemp(prefix="igray_bp_empty_")
    dbfile = os.path.join(tmp, "t.db")
    conn = db.connect(dbfile)
    db.init_db(conn)
    conn.close()

    cl = client(dbfile, cache_dir)
    h = cl.get("/backup").get_data(as_text=True)
    check("巡回の予定なしと明示", "次回の予定は記録されていません" in h)
    check("ジョブなしと明示", "バックフィルのジョブはありません" in h)
    # JS 側にも [data-countdown] セレクタが出てくるので、
    # 「属性として書かれているか」で判定する
    check("勝手な時刻を出さない", 'data-countdown="' not in h)
    check("起動コマンドを案内する", "--profile cron up -d" in h)


def test_dead_worker(cache_dir):
    print("\n[5] ワーカーが止まっているとき")
    import db
    tmp = tempfile.mkdtemp(prefix="igray_bp_dead_")
    dbfile = os.path.join(tmp, "t.db")
    conn = db.connect(dbfile)
    db.init_db(conn)
    conn.execute("INSERT INTO backfill_jobs (username,status) VALUES ('alpha','queued')")
    conn.execute("INSERT INTO meta(key,value) VALUES('activity:backfill_worker',?)",
                 ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),))
    conn.commit()
    conn.close()

    cl = client(dbfile, cache_dir)
    h = cl.get("/backup").get_data(as_text=True)
    check("停止を警告する", "ワーカーが動いていないようです" in h)
    check("経過時間を出す", "180 分" in h, "")
    check("起動コマンドを案内する", "--profile backfill up -d" in h)


def test_legacy_schema(cache_dir):
    print("\n[6] backfill_jobs が無い旧DBでも落ちない")
    import sqlite3
    tmp = tempfile.mkdtemp(prefix="igray_bp_old_")
    dbfile = os.path.join(tmp, "old.db")
    c = sqlite3.connect(dbfile)
    c.execute("CREATE TABLE posts (shortcode TEXT PRIMARY KEY, "
              "owner_username TEXT, date_utc TEXT)")
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT INTO meta VALUES ('schema_version','4')")
    c.commit()
    c.close()

    cl = client(dbfile, cache_dir)
    r = cl.get("/backup")
    check("200 で返る", r.status_code == 200, str(r.status_code))


def test_other_pages(dbfile, cache_dir):
    print("\n[9] 既存ページの回帰")
    cl = client(dbfile, cache_dir)
    for path in ("/", "/gallery", "/bookmarks", "/mutes", "/storage"):
        check(f"{path} が 200", cl.get(path).status_code == 200)
    for path in ("/", "/gallery", "/storage"):
        h = cl.get(path).get_data(as_text=True)
        check(f"{path} のナビにバックアップが出る", 'href="/backup"' in h)


def test_backfill_done_not_a_failure(cache_dir):
    print("\n[8] backfill_done を失敗として数えない")
    # トップの警告バナーは「OK_STATUSES に無いもの＝失敗」で判定する。
    # log_scrape にステータスを足したときここへの追加を忘れると、
    # **成功が赤字の警告として出る**（v4.5 の backfill_done で実際にやった）。
    import db
    tmp = tempfile.mkdtemp(prefix="igray_bp_status_")
    dbfile = os.path.join(tmp, "t.db")
    conn = db.connect(dbfile)
    db.init_db(conn)
    conn.execute("INSERT INTO accounts (username) VALUES ('nemonagi')")
    db.log_scrape(conn, "nemonagi", db.BACKFILL_DONE, fetched=500, inserted=500)
    db.log_scrape(conn, "alpha", "ok_degraded", 6, 1)
    conn.commit()
    conn.close()

    check("BACKFILL_DONE が OK_STATUSES に入っている",
          db.BACKFILL_DONE in db.OK_STATUSES, str(db.OK_STATUSES))
    check("ok / ok_degraded も入っている",
          "ok" in db.OK_STATUSES and "ok_degraded" in db.OK_STATUSES)

    cl = client(dbfile, cache_dir)
    h = cl.get("/").get_data(as_text=True)
    check("トップに警告が出ない", "backfill_done" not in h)
    check("成功が失敗として表示されない", "nemonagi" not in h or "×1" not in h)

    # 本物の失敗はちゃんと出る（判定を緩めすぎていないか）
    conn = db.connect(dbfile)
    db.log_scrape(conn, "beta", "ratelimited", 0, 0)
    conn.commit()
    conn.close()
    cl = client(dbfile, cache_dir)
    h = cl.get("/").get_data(as_text=True)
    check("本物の失敗は警告に出る", "ratelimited" in h)
    check("そのアカウント名も出る", "beta" in h)

    # /backup 側は巡回の集計から除外しつつ、ジョブとしては見える
    check("/backup が 200", cl.get("/backup").status_code == 200)


def main():
    tmp = tempfile.mkdtemp(prefix="igray_bp_")
    dbfile = os.path.join(tmp, "t.db")
    cache_dir = os.path.join(tmp, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    print(f"test db: {dbfile}")

    test_next_run_helpers(dbfile)
    test_scraper_cron_flag()
    test_page(dbfile, cache_dir)
    test_no_data(cache_dir)
    test_dead_worker(cache_dir)
    test_legacy_schema(cache_dir)
    test_backfill_done_not_a_failure(cache_dir)
    test_other_pages(dbfile, cache_dir)

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
