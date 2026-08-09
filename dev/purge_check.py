"""
ig-ray / dev/purge_check.py

`accounts purge`（アカウント完全削除）の検証。
実ファイルを一時ディレクトリに作って、消える/残るの判定を確かめる。
ネットワーク不要。

    python3 dev/purge_check.py

とくに見ているのは「消してはいけないものを消していないか」:
  - ブックマークが参照している実体
  - 他アカウントの投稿も参照している実体（sha256重複排除・共同投稿）
  - 他アカウントの posts / media_index / accounts 行
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  {mark}  {name}{('  -- ' + detail) if detail and not cond else ''}")


def build(tmp):
    """テスト用のDBとキャッシュ実体を作る。"""
    cache = os.path.join(tmp, "cache")
    os.makedirs(os.path.join(cache, "_avatars"), exist_ok=True)
    os.environ["IG_RAY_CACHE"] = cache
    os.environ["IG_RAY_DB"] = os.path.join(tmp, "test.db")

    # config を読む前に環境変数を立てる必要があるのでここで import
    import importlib
    import config
    importlib.reload(config)
    import db
    importlib.reload(db)
    import cache_utils
    importlib.reload(cache_utils)

    conn = db.connect()
    db.init_db(conn)

    def mkfile(name, content=b"x" * 100):
        p = os.path.join(cache, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(content)
        return p

    # --- アカウント3件 ---
    for u in ("target", "other", "keeper"):
        conn.execute("INSERT INTO accounts (username) VALUES (?)", (u,))

    paths = {
        "plain": mkfile("aa/AAA_0.jpg"),        # target 専用 → 消える
        "plain2": mkfile("aa/AAA_1.jpg"),       # target 専用 → 消える
        "booked": mkfile("bb/BBB_0.jpg"),       # ブックマーク参照 → 残る
        "shared": mkfile("cc/CCC_0.jpg"),       # other も参照 → 残る
        "others": mkfile("dd/DDD_0.jpg"),       # other 専用 → 無関係
    }
    avatar = mkfile("_avatars/target.jpg")
    keeper_avatar = mkfile("_avatars/keeper.jpg")

    def mkpost(sc, owner, media):
        conn.execute(
            "INSERT INTO posts (shortcode, owner_username, date_utc, media_json) "
            "VALUES (?,?,?,?)",
            (sc, owner, "2026-07-01T00:00:00+00:00", json.dumps([])))
        for i, path in enumerate(media):
            conn.execute(
                "INSERT INTO media_index (shortcode, media_index, local_path, bytes) "
                "VALUES (?,?,?,?)", (sc, i, path, 100))

    mkpost("AAA", "target", [paths["plain"], paths["plain2"]])
    mkpost("BBB", "target", [paths["booked"]])
    mkpost("CCC", "target", [paths["shared"]])
    mkpost("DDD", "other", [paths["others"], paths["shared"]])   # 実体を共有

    # BBB をブックマーク（非正規化コピー）
    conn.execute(
        "INSERT INTO bookmarks (shortcode, owner_username, local_media_json) "
        "VALUES (?,?,?)",
        ("BBB", "target", json.dumps([paths["booked"]])))
    for u in ("target", "other"):
        conn.execute("INSERT INTO backfill_jobs (username) VALUES (?)", (u,))
    conn.commit()
    return conn, cache_utils, db, paths, avatar, keeper_avatar


def test_plan(conn, cache_utils, paths):
    print("\n[1] purge_plan（副作用なしの算出）")
    plan = cache_utils.purge_plan(conn, "target")

    check("投稿数を数える", plan["posts"] == 3, f"got {plan['posts']}")
    check("media_index 行数を数える", plan["media_rows"] == 4,
          f"got {plan['media_rows']}")
    check("ブックマーク数を数える", plan["bookmarks"] == 1,
          f"got {plan['bookmarks']}")

    files = {os.path.normpath(p) for p in plan["files"]}
    check("target 専用ファイルは削除対象",
          os.path.normpath(paths["plain"]) in files and
          os.path.normpath(paths["plain2"]) in files)
    check("ブックマーク参照は削除対象に入らない",
          os.path.normpath(paths["booked"]) not in files)
    check("他アカウントも参照する実体は削除対象に入らない",
          os.path.normpath(paths["shared"]) not in files)
    check("他アカウント専用は最初から無関係",
          os.path.normpath(paths["others"]) not in files)
    check("保護カウント（ブックマーク）", plan["protected_bookmark"] == 1,
          f"got {plan['protected_bookmark']}")
    check("保護カウント（共有）", plan["protected_shared"] == 1,
          f"got {plan['protected_shared']}")
    check("解放見込みバイト数", plan["bytes"] == 200, f"got {plan['bytes']}")
    check("アイコンを検出", plan["avatar"] is not None)


def test_dry_run(conn, cache_utils, paths):
    print("\n[2] dry_run では何も消えない")
    before = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
    r = cache_utils.purge_account(conn, "target", dry_run=True)
    after = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
    check("executed=False", r["executed"] is False)
    check("posts が減らない", before == after, f"{before} -> {after}")
    check("ファイルが残っている", os.path.exists(paths["plain"]))


def test_execute(conn, cache_utils, paths, avatar, keeper_avatar):
    print("\n[3] 実行")
    r = cache_utils.purge_account(conn, "target")

    check("executed=True", r["executed"] is True)
    check("削除ファイル数", r["deleted_files"] == 2, f"got {r['deleted_files']}")
    check("解放バイト数", r["freed"] == 200, f"got {r['freed']}")

    check("target 専用ファイルが消えた",
          not os.path.exists(paths["plain"]) and not os.path.exists(paths["plain2"]))
    check("ブックマーク参照の実体は残る", os.path.exists(paths["booked"]))
    check("共有実体は残る", os.path.exists(paths["shared"]))
    check("他アカウントの実体は残る", os.path.exists(paths["others"]))
    check("アイコンが消えた", not os.path.exists(avatar))
    check("他アカウントのアイコンは無事", os.path.exists(keeper_avatar))

    n = conn.execute(
        "SELECT COUNT(*) c FROM posts WHERE owner_username='target'").fetchone()["c"]
    check("target の posts が消えた", n == 0, f"got {n}")
    n = conn.execute(
        "SELECT COUNT(*) c FROM posts WHERE owner_username='other'").fetchone()["c"]
    check("other の posts は残る", n == 1, f"got {n}")

    n = conn.execute(
        "SELECT COUNT(*) c FROM media_index WHERE shortcode IN ('AAA','BBB','CCC')"
    ).fetchone()["c"]
    check("target の media_index が消えた", n == 0, f"got {n}")
    n = conn.execute(
        "SELECT COUNT(*) c FROM media_index WHERE shortcode='DDD'").fetchone()["c"]
    check("other の media_index は残る", n == 2, f"got {n}")

    n = conn.execute(
        "SELECT COUNT(*) c FROM accounts WHERE username='target'").fetchone()["c"]
    check("accounts 行が消えた", n == 0, f"got {n}")
    n = conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
    check("他アカウントの accounts 行は残る", n == 2, f"got {n}")

    n = conn.execute(
        "SELECT COUNT(*) c FROM backfill_jobs WHERE username='target'").fetchone()["c"]
    check("バックフィルのジョブも消える", n == 0, f"got {n}")
    n = conn.execute(
        "SELECT COUNT(*) c FROM backfill_jobs WHERE username='other'").fetchone()["c"]
    check("他アカウントのジョブは残る", n == 1, f"got {n}")

    n = conn.execute("SELECT COUNT(*) c FROM bookmarks").fetchone()["c"]
    check("ブックマークは残る", n == 1, f"got {n}")
    row = conn.execute(
        "SELECT owner_username, local_media_json FROM bookmarks").fetchone()
    check("ブックマークの owner_username も残る", row["owner_username"] == "target")
    check("ブックマークのローカルパスが有効なまま",
          os.path.exists(json.loads(row["local_media_json"])[0]))


def test_idempotent(conn, cache_utils):
    print("\n[4] 冪等・存在しないアカウント")
    r = cache_utils.purge_account(conn, "target")
    check("2回目でも落ちない", r["posts"] == 0 and r["deleted_files"] == 0)
    r = cache_utils.purge_account(conn, "no_such_user_xyz")
    check("未登録でも落ちない", r["posts"] == 0)
    r = cache_utils.purge_plan(conn, "OTHER")
    check("大文字でも正規化して当たる", r["posts"] == 1, f"got {r['posts']}")


def test_keep_avatar(tmpbase):
    print("\n[5] --keep-avatar")
    tmp = tempfile.mkdtemp(prefix="igray_purge2_", dir=tmpbase)
    conn, cache_utils, _db, paths, avatar, _k = build(tmp)
    cache_utils.purge_account(conn, "target", keep_avatar=True)
    check("keep_avatar でアイコンが残る", os.path.exists(avatar))
    check("それでも投稿は消えている",
          conn.execute("SELECT COUNT(*) c FROM posts WHERE owner_username='target'"
                       ).fetchone()["c"] == 0)
    conn.close()


def main():
    tmpbase = tempfile.mkdtemp(prefix="igray_purge_base_")
    tmp = tempfile.mkdtemp(prefix="igray_purge_", dir=tmpbase)
    print(f"test dir: {tmp}")

    conn, cache_utils, _db, paths, avatar, keeper_avatar = build(tmp)

    test_plan(conn, cache_utils, paths)
    test_dry_run(conn, cache_utils, paths)
    test_execute(conn, cache_utils, paths, avatar, keeper_avatar)
    test_idempotent(conn, cache_utils)
    conn.close()
    test_keep_avatar(tmpbase)

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
