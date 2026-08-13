"""
insta-ray / db.py

SQLite スキーマとアクセスヘルパ。
X-Ray の「キャッシュ / 永続」分離思想を踏襲しつつ、Instagram 構造へ寄せてある。

X-Ray からの主な差分:
  - retweet / quoted / self_reply 系のカラムを全削除（IGに概念が無い）
  - shortcode を主キーに（tweet_id 相当）
  - is_video / is_carousel / location / hashtags_json / typename を追加
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import config

DB_PATH = config.db_path()

SCHEMA_VERSION = 6


# --------------------------------------------------------------------------
# 接続
# --------------------------------------------------------------------------

BUSY_TIMEOUT_MS = config.env_int("BUSY_TIMEOUT_MS", 60000)


def connect(db_path=None):
    """
    WAL / 外部キー有効の接続を返す。row_factory は sqlite3.Row。

    worker（scrape）と web が同じDBを触るので、ロック待ちは長めに取る。
    WAL なので読み書きは基本ぶつからないが、スキーマ変更や
    書き込み同士は待つ必要がある。
    """
    path = db_path or DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    timeout_s = BUSY_TIMEOUT_MS / 1000.0
    conn = sqlite3.connect(path, timeout=timeout_s)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    # timeout= と同義だが、明示しておくと他経路の接続でも効く
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def session(db_path=None):
    """with ブロックを抜けるとき commit / 例外時 rollback。"""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# スキーマ
# --------------------------------------------------------------------------

DDL = [
    # 監視対象アカウント（永続・手で育てる側）
    """
    CREATE TABLE IF NOT EXISTS accounts (
        username        TEXT PRIMARY KEY,
        userid          INTEGER,
        full_name       TEXT,
        biography       TEXT,
        profile_pic_url TEXT,
        profile_pic_local TEXT,
        followers       INTEGER,
        mediacount      INTEGER,
        categories_json TEXT,
        is_enabled      INTEGER NOT NULL DEFAULT 1,
        is_muted        INTEGER NOT NULL DEFAULT 0,
        muted_at        TEXT,
        mute_reason     TEXT,
        note            TEXT,
        added_at        TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT
    )
    """,

    # 取得した投稿（キャッシュ側 = 消えても再取得できる想定）
    """
    CREATE TABLE IF NOT EXISTS posts (
        shortcode       TEXT PRIMARY KEY,
        mediaid         INTEGER,
        owner_username  TEXT NOT NULL,
        caption         TEXT,
        date_utc        TEXT NOT NULL,
        likes           INTEGER,
        comments        INTEGER,
        typename        TEXT,
        is_video        INTEGER NOT NULL DEFAULT 0,
        is_carousel     INTEGER NOT NULL DEFAULT 0,
        location        TEXT,
        hashtags_json   TEXT,
        media_json      TEXT,
        fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_posts_owner_date ON posts(owner_username, date_utc DESC)",
    "CREATE INDEX IF NOT EXISTS idx_posts_date ON posts(date_utc DESC)",

    # ブックマーク（永続側 = 消したくない）
    """
    CREATE TABLE IF NOT EXISTS bookmarks (
        shortcode      TEXT PRIMARY KEY,
        owner_username TEXT,
        caption        TEXT,
        date_utc       TEXT,
        likes          INTEGER,
        comments       INTEGER,
        typename       TEXT,
        is_video       INTEGER,
        is_carousel    INTEGER,
        location       TEXT,
        hashtags_json  TEXT,
        media_json     TEXT,
        local_media_json TEXT,
        note           TEXT,
        created_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,

    # メディア重複排除インデックス
    # 同じ画像が複数投稿に出てきても実体は1つ、という X-Ray の dedupe 思想
    """
    CREATE TABLE IF NOT EXISTS media_index (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        shortcode     TEXT NOT NULL,
        media_index   INTEGER NOT NULL,
        is_video      INTEGER NOT NULL DEFAULT 0,
        remote_url    TEXT,
        local_path    TEXT,
        sha256        TEXT,
        bytes         INTEGER,
        is_persistent INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (shortcode, media_index)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_media_sha ON media_index(sha256)",
    "CREATE INDEX IF NOT EXISTS idx_media_shortcode ON media_index(shortcode)",

    # 実行ログ
    """
    CREATE TABLE IF NOT EXISTS scrape_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        username    TEXT,
        status      TEXT NOT NULL,
        fetched     INTEGER NOT NULL DEFAULT 0,
        inserted    INTEGER NOT NULL DEFAULT 0,
        message     TEXT,
        started_at  TEXT,
        ended_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_log_user_time ON scrape_log(username, ended_at DESC)",

    # Tumblr 共有用の一時トークン
    #
    # payload は**発行時のスナップショット**（画像パス・キャプション・タグ・
    # 元投稿URL）。配信時にDBを引き直さないので、共有中に元データが変わっても
    # 公開されるのは確定した内容だけになる。
    """
    CREATE TABLE IF NOT EXISTS share_tokens (
        token       TEXT PRIMARY KEY,
        shortcode   TEXT NOT NULL,
        payload     TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_share_tokens_exp ON share_tokens(expires_at)",

    # 全件バックフィル（丸ごとバックアップ）のジョブ管理
    #
    # resume_json は instaloader の FrozenNodeIterator を JSON 化したもの。
    # プロセスをまたいで get_posts() のページ送りを再開できる（実測 511 バイト程度）。
    # login_user を持つのは、**thaw() が context_username の一致を要求する**ため。
    # 予備アカウントに切り替えたら再開情報は捨てるしかない。
    """
    CREATE TABLE IF NOT EXISTS backfill_jobs (
        username        TEXT PRIMARY KEY,
        status          TEXT NOT NULL DEFAULT 'queued',
        resume_json     TEXT,
        login_user      TEXT,
        posts_seen      INTEGER NOT NULL DEFAULT 0,
        posts_saved     INTEGER NOT NULL DEFAULT 0,
        media_saved     INTEGER NOT NULL DEFAULT 0,
        pages           INTEGER NOT NULL DEFAULT 0,
        known_streak    INTEGER NOT NULL DEFAULT 0,
        completed_runs  INTEGER NOT NULL DEFAULT 0,
        oldest_date     TEXT,
        message         TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        started_at      TEXT,
        updated_at      TEXT,
        completed_at    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_backfill_status ON backfill_jobs(status, created_at)",

    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
]


def _add_column_if_missing(conn, table, column, decl):
    """雑マイグレーション用。X-Ray と同じ手口。"""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        return True
    return False


def init_db(conn=None, db_path=None):
    """テーブル作成 + 将来のマイグレーション適用。冪等。"""
    own = conn is None
    if own:
        conn = connect(db_path)
    try:
        for stmt in DDL:
            conn.execute(stmt)

        # --- マイグレーション置き場（CREATE TABLE の後ろ、X-Ray v14 の教訓）---
        # bookmarks を非正規化コピー方式へ。既存DBにも列を足す。
        # キャッシュを消しても、ブックマークした投稿は情報が残るようにするため。
        _add_column_if_missing(conn, "accounts", "profile_pic_local", "TEXT")
        _add_column_if_missing(conn, "accounts", "is_muted",
                               "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "accounts", "muted_at", "TEXT")
        _add_column_if_missing(conn, "accounts", "mute_reason", "TEXT")

        for col, decl in [
            ("owner_username", "TEXT"), ("caption", "TEXT"), ("date_utc", "TEXT"),
            ("likes", "INTEGER"), ("comments", "INTEGER"), ("typename", "TEXT"),
            ("is_video", "INTEGER"), ("is_carousel", "INTEGER"),
            ("location", "TEXT"), ("hashtags_json", "TEXT"),
            ("media_json", "TEXT"), ("local_media_json", "TEXT"),
        ]:
            _add_column_if_missing(conn, "bookmarks", col, decl)

        # --- 新しい列に張るインデックスはマイグレーションの「後」で ---
        # DDL リストに入れると、既存DBでは列が追加される前に走って
        # "no such column" で落ちる（v3.4 で実際に踏んだ）。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bookmarks_date ON bookmarks(date_utc DESC)")

        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    finally:
        if own:
            conn.close()
    return conn if not own else None


# --------------------------------------------------------------------------
# 書き込みヘルパ
# --------------------------------------------------------------------------

def upsert_account(conn, profile_row):
    """profile_row: dict。username 必須。"""
    conn.execute(
        """
        INSERT INTO accounts
            (username, userid, full_name, biography, profile_pic_url,
             followers, mediacount, updated_at)
        VALUES (:username, :userid, :full_name, :biography, :profile_pic_url,
                :followers, :mediacount, datetime('now'))
        ON CONFLICT(username) DO UPDATE SET
            userid          = excluded.userid,
            full_name       = excluded.full_name,
            biography       = excluded.biography,
            profile_pic_url = excluded.profile_pic_url,
            followers       = excluded.followers,
            mediacount      = excluded.mediacount,
            updated_at      = datetime('now')
        """,
        {
            "username": profile_row["username"],
            "userid": profile_row.get("userid"),
            "full_name": profile_row.get("full_name"),
            "biography": profile_row.get("biography"),
            "profile_pic_url": profile_row.get("profile_pic_url"),
            "followers": profile_row.get("followers"),
            "mediacount": profile_row.get("mediacount"),
        },
    )


def merge_account(conn, row):
    """
    分かっている項目だけを accounts に反映する。

    upsert_account と違い **None は既存値を上書きしない**（COALESCE）。
    投稿データから拾えるのは username / full_name / profile_pic_url / userid
    だけなので、followers 等を消さないためにこちらを使う。
    """
    if not row.get("username"):
        return
    conn.execute(
        """
        INSERT INTO accounts
            (username, userid, full_name, profile_pic_url, profile_pic_local, updated_at)
        VALUES (:username, :userid, :full_name, :profile_pic_url, :profile_pic_local,
                datetime('now'))
        ON CONFLICT(username) DO UPDATE SET
            userid            = COALESCE(excluded.userid, accounts.userid),
            full_name         = COALESCE(excluded.full_name, accounts.full_name),
            profile_pic_url   = COALESCE(excluded.profile_pic_url, accounts.profile_pic_url),
            profile_pic_local = COALESCE(excluded.profile_pic_local, accounts.profile_pic_local),
            updated_at        = datetime('now')
        """,
        {
            "username": row["username"],
            "userid": row.get("userid"),
            "full_name": row.get("full_name"),
            "profile_pic_url": row.get("profile_pic_url"),
            "profile_pic_local": row.get("profile_pic_local"),
        },
    )


def ensure_account(conn, username):
    """
    username だけで accounts に行を確保する。既存なら何もしない。

    upsert_account と違い、既存行の followers 等を None で上書きしない。
    web_profile_info が 429 でプロフィール詳細を取れないときに使う。
    """
    conn.execute(
        "INSERT INTO accounts (username) VALUES (?) "
        "ON CONFLICT(username) DO NOTHING",
        (username,),
    )


def save_posts(conn, records):
    """
    records: post_to_record() が返す dict のリスト。
    戻り値: (処理件数, 新規挿入件数)

    冪等。既存 shortcode は likes/comments/caption 等を更新する
    （いいね数は後から増えるので上書きが正しい）。
    """
    if not records:
        return (0, 0)

    before = conn.execute("SELECT COUNT(*) AS c FROM posts").fetchone()["c"]

    conn.executemany(
        """
        INSERT INTO posts
            (shortcode, mediaid, owner_username, caption, date_utc,
             likes, comments, typename, is_video, is_carousel,
             location, hashtags_json, media_json, fetched_at)
        VALUES
            (:shortcode, :mediaid, :owner_username, :caption, :date_utc,
             :likes, :comments, :typename, :is_video, :is_carousel,
             :location, :hashtags_json, :media_json, datetime('now'))
        ON CONFLICT(shortcode) DO UPDATE SET
            caption       = excluded.caption,
            likes         = excluded.likes,
            comments      = excluded.comments,
            location      = excluded.location,
            hashtags_json = excluded.hashtags_json,
            media_json    = excluded.media_json,
            fetched_at    = datetime('now')
        """,
        records,
    )

    after = conn.execute("SELECT COUNT(*) AS c FROM posts").fetchone()["c"]
    return (len(records), after - before)


def record_media(conn, shortcode, media_index, is_video, remote_url,
                 local_path=None, sha256=None, size_bytes=None):
    """media_index テーブルへ1件。(shortcode, media_index) で冪等。"""
    conn.execute(
        """
        INSERT INTO media_index
            (shortcode, media_index, is_video, remote_url, local_path, sha256, bytes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(shortcode, media_index) DO UPDATE SET
            local_path = COALESCE(excluded.local_path, media_index.local_path),
            sha256     = COALESCE(excluded.sha256,     media_index.sha256),
            bytes      = COALESCE(excluded.bytes,      media_index.bytes)
        """,
        (shortcode, media_index, 1 if is_video else 0,
         remote_url, local_path, sha256, size_bytes),
    )


def find_media_by_sha(conn, sha256):
    """同一ハッシュの既存ローカルファイルを探す（重複DL回避用）。"""
    row = conn.execute(
        "SELECT local_path FROM media_index "
        "WHERE sha256 = ? AND local_path IS NOT NULL LIMIT 1",
        (sha256,),
    ).fetchone()
    return row["local_path"] if row else None


def add_bookmark(conn, shortcode, note=None):
    """
    posts と media_index の内容を bookmarks へコピーする。

    参照ではなくコピーにしているのは、キャッシュ削除や再取得で
    posts 側が消えてもブックマークだけは残したいから（X-Ray と同じ方針）。
    """
    post = conn.execute(
        "SELECT * FROM posts WHERE shortcode = ?", (shortcode,)
    ).fetchone()
    if post is None:
        return False

    local = [r["local_path"] for r in conn.execute(
        "SELECT local_path FROM media_index WHERE shortcode = ? ORDER BY media_index",
        (shortcode,),
    )]

    conn.execute(
        """
        INSERT INTO bookmarks
            (shortcode, owner_username, caption, date_utc, likes, comments,
             typename, is_video, is_carousel, location, hashtags_json,
             media_json, local_media_json, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(shortcode) DO UPDATE SET
            owner_username   = excluded.owner_username,
            caption          = excluded.caption,
            date_utc         = excluded.date_utc,
            likes            = excluded.likes,
            comments         = excluded.comments,
            typename         = excluded.typename,
            is_video         = excluded.is_video,
            is_carousel      = excluded.is_carousel,
            location         = excluded.location,
            hashtags_json    = excluded.hashtags_json,
            media_json       = excluded.media_json,
            local_media_json = excluded.local_media_json,
            note             = COALESCE(excluded.note, bookmarks.note)
        """,
        (post["shortcode"], post["owner_username"], post["caption"],
         post["date_utc"], post["likes"], post["comments"], post["typename"],
         post["is_video"], post["is_carousel"], post["location"],
         post["hashtags_json"], post["media_json"],
         json.dumps(local, ensure_ascii=False), note),
    )
    return True


def remove_bookmark(conn, shortcode):
    cur = conn.execute("DELETE FROM bookmarks WHERE shortcode = ?", (shortcode,))
    return cur.rowcount > 0


def bookmarked_shortcodes(conn):
    try:
        return {r["shortcode"] for r in conn.execute(
            "SELECT shortcode FROM bookmarks")}
    except Exception:
        return set()


# scrape_log のうち「異常ではない」ステータス。
#
# **新しいステータスを log_scrape に足したら必ずここにも入れること。**
# 表示側は「このリストに無いもの＝失敗」として警告を出すので、
# 入れ忘れると成功が赤字で警告される（v4.5 の backfill_done で実際にやった）。
# バックフィルの1周完了。cron の巡回とは別枠なので、
# 表示側では巡回の集計から除外しつつ、失敗としては数えない。
BACKFILL_DONE = "backfill_done"

OK_STATUSES = ("ok", "ok_degraded", BACKFILL_DONE)


def log_scrape(conn, username, status, fetched=0, inserted=0,
               message=None, started_at=None):
    conn.execute(
        """
        INSERT INTO scrape_log
            (username, status, fetched, inserted, message, started_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (username, status, fetched, inserted, message,
         started_at or datetime.now(timezone.utc).isoformat()),
    )


# --------------------------------------------------------------------------
# 読み出しヘルパ（web.py 移植時に使う）
# --------------------------------------------------------------------------

def enabled_accounts(conn):
    """巡回対象。ミュート中のアカウントは除く。"""
    return [r["username"] for r in conn.execute(
        "SELECT username FROM accounts "
        "WHERE is_enabled = 1 AND COALESCE(is_muted, 0) = 0 "
        "ORDER BY username"
    )]


def muted_usernames(conn):
    """ミュート中のアカウント名の集合。取得スキップと表示除外に使う。"""
    try:
        return {r["username"] for r in conn.execute(
            "SELECT username FROM accounts WHERE COALESCE(is_muted, 0) = 1")}
    except Exception:
        return set()


def set_mute(conn, username, muted, reason=None):
    """
    ミュートの切り替え。未登録のアカウントでも行を作る
    （共同投稿で流れてくる相手をミュートしたい場合がある）。
    """
    username = (username or "").strip().lower()
    if not username:
        return False
    conn.execute(
        "INSERT INTO accounts (username) VALUES (?) ON CONFLICT(username) DO NOTHING",
        (username,),
    )
    conn.execute(
        "UPDATE accounts SET is_muted = ?, "
        "  muted_at = CASE WHEN ? THEN datetime('now') ELSE NULL END, "
        "  mute_reason = CASE WHEN ? THEN ? ELSE NULL END "
        "WHERE username = ?",
        (1 if muted else 0, 1 if muted else 0, 1 if muted else 0, reason, username),
    )
    return True


def muted_accounts(conn):
    """ミュート一覧（表示用）。投稿数も添える。"""
    rows = conn.execute("""
        SELECT a.username, a.muted_at, a.mute_reason, a.full_name,
               a.profile_pic_local,
               (SELECT COUNT(*) FROM posts p
                 WHERE p.owner_username = a.username) AS n_posts
        FROM accounts a
        WHERE COALESCE(a.is_muted, 0) = 1
        ORDER BY a.muted_at DESC, a.username
    """).fetchall()
    return [dict(r) for r in rows]


def get_post(conn, shortcode):
    row = conn.execute(
        "SELECT * FROM posts WHERE shortcode = ?", (shortcode,)
    ).fetchone()
    return dict(row) if row else None


def recent_posts(conn, username=None, limit=50, offset=0):
    if username:
        rows = conn.execute(
            "SELECT * FROM posts WHERE owner_username = ? "
            "ORDER BY date_utc DESC LIMIT ? OFFSET ?",
            (username, limit, offset),
        )
    else:
        rows = conn.execute(
            "SELECT * FROM posts ORDER BY date_utc DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    return [dict(r) for r in rows]


def media_for(conn, shortcode):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM media_index WHERE shortcode = ? ORDER BY media_index",
        (shortcode,),
    )]


# --------------------------------------------------------------------------
# 活動ゲート（cron 巡回とバックフィルの調停）
# --------------------------------------------------------------------------

def touch_activity(conn, who):
    """
    「いま自分がAPIを叩いている」を meta に記録する。

    cron の巡回とバックフィルは同じセッション・同じ出口を共有するので、
    同時に叩くとレート予算を取り合う。**429 は全クエリ種別に波及する**ため、
    片方が食らうともう片方も巻き添えになる。
    そこで長時間走るバックフィル側が cron の巡回中は待つ、という一方向の譲り合いにする。
    （cron は数分で終わるので待たせない）
    """
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"activity:{who}", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def seconds_since_activity(conn, who):
    """最後の touch_activity からの経過秒。記録が無ければ None。"""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (f"activity:{who}",)).fetchone()
    if row is None or not row["value"]:
        return None
    try:
        t = datetime.fromisoformat(row["value"])
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds()


def set_next_run(conn, who, seconds_from_now):
    """
    次に動く予定の時刻を記録する。

    cron もバックフィルも「1周終わってから N 秒後」という動き方なので、
    **時刻は実行側が寝る直前に書かないと分からない**。
    表示側で「前回＋間隔」を計算すると、処理時間ぶんズレるし
    ワーカーが止まっていても未来の時刻を出してしまう。
    """
    when = datetime.now(timezone.utc) + timedelta(seconds=float(seconds_from_now))
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"next_run:{who}", when.isoformat()))
    conn.commit()
    return when


def clear_next_run(conn, who):
    conn.execute("DELETE FROM meta WHERE key = ?", (f"next_run:{who}",))
    conn.commit()


def get_next_run(conn, who):
    """次回予定時刻（aware datetime）。未記録なら None。"""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (f"next_run:{who}",)).fetchone()
    if row is None or not row["value"]:
        return None
    try:
        t = datetime.fromisoformat(row["value"])
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


# --------------------------------------------------------------------------
# バックフィル（全件バックアップ）
# --------------------------------------------------------------------------

BACKFILL_FIELDS = ("status", "resume_json", "login_user", "posts_seen",
                   "posts_saved", "media_saved", "pages", "known_streak",
                   "completed_runs", "oldest_date", "message",
                   "started_at", "completed_at")


def backfill_add(conn, username):
    """キューに積む。既にあれば何もしない（進捗を潰さないため）。"""
    cur = conn.execute(
        "INSERT INTO backfill_jobs (username) VALUES (?) "
        "ON CONFLICT(username) DO NOTHING", (username,))
    conn.commit()
    return cur.rowcount > 0


def backfill_get(conn, username):
    row = conn.execute(
        "SELECT * FROM backfill_jobs WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def backfill_list(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM backfill_jobs "
        "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 "
        "  WHEN 'ratelimited' THEN 2 WHEN 'error' THEN 3 WHEN 'paused' THEN 4 "
        "  ELSE 5 END, created_at")]


def backfill_update(conn, username, **fields):
    """指定したフィールドだけ更新。updated_at は常に打つ。"""
    bad = set(fields) - set(BACKFILL_FIELDS)
    if bad:
        raise ValueError(f"unknown backfill field: {sorted(bad)}")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    sets = (sets + ", " if sets else "") + "updated_at = datetime('now')"
    conn.execute(
        f"UPDATE backfill_jobs SET {sets} WHERE username = :username",
        {**fields, "username": username})
    conn.commit()


def backfill_next(conn):
    """
    次に処理すべきジョブを1件返す。running を優先するのは、
    プロセスが落ちた後の再開を先に片付けるため。
    """
    row = conn.execute(
        "SELECT * FROM backfill_jobs "
        "WHERE status IN ('running', 'queued', 'ratelimited') "
        "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'ratelimited' THEN 1 "
        "  ELSE 2 END, created_at LIMIT 1").fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# 共有トークン
# --------------------------------------------------------------------------

def create_share_token(conn, token, shortcode, payload, ttl_minutes):
    """
    共有トークンを発行する。payload は dict（JSONで保存）。

    ttl_minutes=0 を許すのはテストのため（発行直後に失効した状態を作れる）。
    """
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO share_tokens (token, shortcode, payload, created_at, expires_at) "
        "VALUES (?,?,?,?,?)",
        (token, shortcode, json.dumps(payload, ensure_ascii=False),
         now.isoformat(), (now + timedelta(minutes=float(ttl_minutes))).isoformat()))
    conn.commit()


def get_share_payload(conn, token):
    """
    有効なトークンなら payload(dict) を返す。無効・失効なら None。

    **失効判定はここで一元化する。** 呼び出し側それぞれで比較すると
    片方だけ判定を忘れて、期限切れの画像が配信され続ける事故になる。
    """
    if not token:
        return None
    row = conn.execute(
        "SELECT payload, expires_at FROM share_tokens WHERE token = ?",
        (token,)).fetchone()
    if row is None:
        return None
    try:
        exp = datetime.fromisoformat(row["expires_at"])
    except (TypeError, ValueError):
        return None
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp <= datetime.now(timezone.utc):
        return None
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def purge_expired_share_tokens(conn):
    """失効済みトークンを掃除する。発行のついでに呼ぶ。"""
    cur = conn.execute(
        "DELETE FROM share_tokens WHERE expires_at <= ?",
        (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    return cur.rowcount


def post_is_archived(conn, shortcode):
    """
    その投稿が「すでに完全に保管済み」か。

    posts に行があるだけでは足りない。**メディア実体まで落ちていること**を条件にする。
    バックフィルの目的が画像のバックアップなので、
    レコードだけあって画像が無い投稿を「既知」と数えると取りこぼす。
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "  SUM(CASE WHEN local_path IS NOT NULL THEN 1 ELSE 0 END) AS got "
        "FROM media_index WHERE shortcode = ?", (shortcode,)).fetchone()
    if not row or not row["n"]:
        return False
    return row["n"] == (row["got"] or 0)


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    init_db(db_path=target)
    print(f"initialized: {target}")
