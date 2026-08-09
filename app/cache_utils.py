"""
insta-ray / cache_utils.py

キャッシュ容量の集計と、保持期間を過ぎた画像の掃除。

X-Ray の cache_utils との違い:
  X-Ray は「表示用キャッシュ(/data/cache)」と「永続保存(/data/images)」の
  2ディレクトリをハードリンクで行き来させる仕組みだった。
  insta-ray はディレクトリが /data/cache 1つだけで、代わりに
  **bookmarks が投稿ごと非正規化コピー**（local_media_json にローカルパスを保持）
  している。なので「守るべきファイル」はブックマークが参照しているパス、
  という判定になる。

  ハードリンクを使わないぶん単純だが、sha256 による重複排除で
  **1つの実体を複数の投稿が参照する**ことはある。削除時はここを踏む。
"""

import os
import json
import time
from datetime import datetime, timezone

import config

CACHE_DIR = config.env("CACHE", "/data/cache")
DB_PATH = config.db_path()
RETENTION_DAYS = config.env_int("CACHE_RETENTION_DAYS", 30)

# アイコンはサイズが小さく常に必要なので掃除対象から外す
AVATAR_DIR = os.path.join(CACHE_DIR, "_avatars")


def fmt_size(n):
    n = float(n or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def dir_stats(path=None, seen_inodes=None):
    """
    (合計バイト数, ファイル数)

    同一 inode は1回だけ数える。insta-ray ではハードリンクを張らないが、
    利用者が手で張っている可能性もあるので X-Ray と同じ扱いにしておく。
    """
    path = path or CACHE_DIR
    total = 0
    count = 0
    seen = seen_inodes if seen_inodes is not None else set()
    if os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    st = os.stat(os.path.join(root, f))
                    count += 1
                    if st.st_ino in seen:
                        continue
                    seen.add(st.st_ino)
                    total += st.st_size
                except OSError:
                    pass
    return total, count


def db_size(db_path=None):
    """WAL / SHM も含めたDB実サイズ。"""
    p = db_path or DB_PATH
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(p + suffix)
        except OSError:
            pass
    return total


def protected_paths(conn):
    """
    削除してはいけないローカルパスの集合。

    ブックマークは投稿ごとコピーしてあるので、そこが参照しているファイルは
    「キャッシュを消しても残ってほしいもの」として扱う。
    """
    out = set()
    try:
        rows = conn.execute(
            "SELECT local_media_json FROM bookmarks "
            "WHERE local_media_json IS NOT NULL").fetchall()
    except Exception:
        return out

    for r in rows:
        try:
            for p in json.loads(r["local_media_json"] or "[]"):
                if p:
                    out.add(os.path.normpath(p))
        except (TypeError, ValueError):
            continue
    return out


def _iter_cache_files():
    if not os.path.isdir(CACHE_DIR):
        return
    for root, _, files in os.walk(CACHE_DIR):
        for f in files:
            yield os.path.join(root, f)


def cleanup_cache(conn, days=None, dry_run=False):
    """
    保持期間を過ぎたキャッシュ画像を削除する。

    days=None → RETENTION_DAYS より古いもの
    days=0    → 全部（ただしブックマーク参照分は常に保護）

    削除したファイルを指している media_index.local_path は NULL に戻す。
    こうしておくと表示層はリモートURLへ自動フォールバックする。

    戻り値: dict(deleted, kept, freed, synced, days)
    """
    d = RETENTION_DAYS if days is None else max(0, int(days))
    cutoff = time.time() - d * 86400
    protected = protected_paths(conn)

    deleted, kept, freed = 0, 0, 0
    removed_paths = []

    for path in _iter_cache_files():
        norm = os.path.normpath(path)
        if norm.startswith(os.path.normpath(AVATAR_DIR) + os.sep):
            kept += 1
            continue
        if norm in protected:
            kept += 1
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        if d > 0 and st.st_mtime >= cutoff:
            kept += 1
            continue

        freed += st.st_size
        deleted += 1
        removed_paths.append(path)
        if not dry_run:
            try:
                os.remove(path)
            except OSError:
                deleted -= 1
                freed -= st.st_size
                removed_paths.pop()

    synced = 0
    if not dry_run and removed_paths:
        synced = sync_db_paths(conn, removed_paths)
        _prune_empty_dirs()

    return {"deleted": deleted, "kept": kept, "freed": freed,
            "synced": synced, "days": d}


def sync_db_paths(conn, paths=None):
    """
    実体が無いのに local_path が入っている media_index の行を NULL に戻す。

    paths を渡せばその分だけ、省略すれば全行を検査する。
    sha256 の重複排除で同じ実体を複数行が指していることがあるので、
    パスで一括更新する（1行ずつ消すと取りこぼす）。
    """
    if paths is None:
        rows = conn.execute(
            "SELECT DISTINCT local_path FROM media_index "
            "WHERE local_path IS NOT NULL").fetchall()
        targets = [r["local_path"] for r in rows
                   if r["local_path"] and not os.path.exists(r["local_path"])]
    else:
        targets = [p for p in set(paths) if not os.path.exists(p)]

    if not targets:
        return 0

    n = 0
    for i in range(0, len(targets), 400):        # SQLite の変数上限対策
        chunk = targets[i:i + 400]
        marks = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"UPDATE media_index SET local_path = NULL WHERE local_path IN ({marks})",
            chunk,
        )
        n += cur.rowcount
    conn.commit()
    return n


def _prune_empty_dirs():
    """シャーディング用の空ディレクトリを片付ける（ベストエフォート）。"""
    if not os.path.isdir(CACHE_DIR):
        return
    for root, dirs, files in os.walk(CACHE_DIR, topdown=False):
        if root == CACHE_DIR:
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass


def purge_plan(conn, username):
    """
    アカウント完全削除の「何がどうなるか」を先に算出する（副作用なし）。

    ファイル削除の判定は3段構え:
      1. ブックマークが参照しているパス → **常に保護**（本人方針）
      2. **他アカウントの投稿からも参照されているパス** → 保護
         sha256 の重複排除で1つの実体を複数の投稿が指すことがあり、
         共同投稿では相手側の投稿が同じ画像を持っている。
         ここを見ないと他アカウントの表示を壊す。
      3. 残りが削除対象

    戻り値: dict
      posts / media_rows / bookmarks / files（削除するパスのリスト）
      bytes（解放見込み）/ protected_bookmark / protected_shared / avatar
    """
    username = (username or "").strip().lower()

    shortcodes = [r["shortcode"] for r in conn.execute(
        "SELECT shortcode FROM posts WHERE owner_username = ?", (username,))]

    n_media = conn.execute(
        "SELECT COUNT(*) AS c FROM media_index m "
        "JOIN posts p ON p.shortcode = m.shortcode "
        "WHERE p.owner_username = ?", (username,)).fetchone()["c"]

    n_bookmarks = conn.execute(
        "SELECT COUNT(*) AS c FROM bookmarks WHERE owner_username = ?",
        (username,)).fetchone()["c"]

    # このアカウントの投稿が指しているローカルパス
    mine = {os.path.normpath(r["local_path"]) for r in conn.execute(
        "SELECT DISTINCT m.local_path FROM media_index m "
        "JOIN posts p ON p.shortcode = m.shortcode "
        "WHERE p.owner_username = ? AND m.local_path IS NOT NULL",
        (username,)) if r["local_path"]}

    # 他アカウントの投稿も指しているパス（重複排除・共同投稿）
    shared = {os.path.normpath(r["local_path"]) for r in conn.execute(
        "SELECT DISTINCT m.local_path FROM media_index m "
        "JOIN posts p ON p.shortcode = m.shortcode "
        "WHERE p.owner_username != ? AND m.local_path IS NOT NULL",
        (username,)) if r["local_path"]} & mine

    booked = protected_paths(conn) & mine

    doomed = sorted(mine - shared - booked)

    total = 0
    real = []
    for p in doomed:
        try:
            total += os.path.getsize(p)
            real.append(p)
        except OSError:
            # 実体が既に無いパスも media_index の掃除対象にはする
            real.append(p)

    avatar = os.path.join(AVATAR_DIR, f"{username}.jpg")
    if not os.path.exists(avatar):
        avatar = None

    return {
        "username": username,
        "posts": len(shortcodes),
        "shortcodes": shortcodes,
        "media_rows": n_media,
        "bookmarks": n_bookmarks,
        "files": real,
        "bytes": total,
        "protected_bookmark": len(booked),
        "protected_shared": len(shared),
        "avatar": avatar,
    }


def purge_account(conn, username, keep_avatar=False, dry_run=False):
    """
    アカウントに紐づくデータを削除する。**ブックマークは消さない。**

    削除するもの: メディア実体（保護分を除く）/ media_index 行 / posts 行 /
                  アイコン（keep_avatar=False のとき）/ accounts 行
    残すもの:     bookmarks 行と、それが参照しているメディア実体

    dry_run=True なら何も変更せず plan だけ返す。
    """
    plan = purge_plan(conn, username)
    if dry_run:
        plan["executed"] = False
        return plan

    deleted_files = 0
    freed = 0
    for p in plan["files"]:
        try:
            size = os.path.getsize(p)
            os.remove(p)
            deleted_files += 1
            freed += size
        except OSError:
            pass

    if plan["avatar"] and not keep_avatar:
        try:
            os.remove(plan["avatar"])
        except OSError:
            pass

    # DB側。shortcode をまたぐので変数上限（999）に当たらないよう分割する。
    codes = plan["shortcodes"]
    for i in range(0, len(codes), 400):
        chunk = codes[i:i + 400]
        marks = ",".join("?" * len(chunk))
        conn.execute(
            f"DELETE FROM media_index WHERE shortcode IN ({marks})", chunk)
    conn.execute("DELETE FROM posts WHERE owner_username = ?", (plan["username"],))
    conn.execute("DELETE FROM accounts WHERE username = ?", (plan["username"],))
    # バックフィルのキューにも残さない（再開情報だけ生き残ると
    # 消したはずのアカウントを取りに行ってしまう）
    try:
        conn.execute("DELETE FROM backfill_jobs WHERE username = ?",
                     (plan["username"],))
    except Exception:
        pass    # 旧スキーマのDBにはテーブルが無い
    conn.commit()

    _prune_empty_dirs()

    plan["executed"] = True
    plan["deleted_files"] = deleted_files
    plan["freed"] = freed
    return plan


def per_account_usage(conn):
    """
    アカウント別のキャッシュ使用量。どのアカウントが容量を食っているか見るため。
    ファイルサイズは media_index.bytes を使う（実ファイルを walk しないので速い）。
    """
    rows = conn.execute("""
        SELECT p.owner_username AS username,
               COUNT(*) AS files,
               COALESCE(SUM(m.bytes), 0) AS bytes,
               SUM(CASE WHEN m.local_path IS NULL THEN 1 ELSE 0 END) AS missing
        FROM media_index m
        JOIN posts p ON p.shortcode = m.shortcode
        GROUP BY p.owner_username
        ORDER BY bytes DESC
    """).fetchall()
    return [{"username": r["username"], "files": r["files"],
             "bytes": r["bytes"], "size": fmt_size(r["bytes"]),
             "missing": r["missing"]} for r in rows]
