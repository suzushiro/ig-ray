#!/usr/bin/env python3
"""
ig-ray / dev/rename_db.py

DBファイルを旧名 insta_ray.db から新名 ig_ray.db へ移す。

    # 必ずコンテナを止めてから
    docker compose --profile cron --profile tools down
    python3 dev/rename_db.py ~/ig-ray/data
    docker compose --profile cron up -d

`config.db_path()` は「新名が無く旧名があれば旧名を使う」ので、
移行しなくても動く。これは単に名前を揃えたいときの掃除用。

依存なしでホストの python3 から直接叩ける（コンテナに入れる必要はない）。
"""

import os
import sqlite3
import sys

OLD = "insta_ray.db"
NEW = "ig_ray.db"
SUFFIXES = ("", "-wal", "-shm")


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "data"
    dry = "--dry-run" in sys.argv

    old = os.path.join(data_dir, OLD)
    new = os.path.join(data_dir, NEW)

    if not os.path.exists(old):
        if os.path.exists(new):
            print(f"すでに {NEW} になっています。何もしません。")
            return 0
        print(f"{old} が見つかりません。--data-dir を確認してください。", file=sys.stderr)
        return 1

    if os.path.exists(new):
        print(f"{new} が既にあります。取り違えると危険なので中止します。\n"
              f"どちらが本物か確認してから手で消してください。", file=sys.stderr)
        return 1

    # WAL が残っている = 直前まで開かれていた可能性。
    # チェックポイントしてから移さないと -wal を取り違えたとき破損する。
    print(f"移行元: {old}")
    try:
        conn = sqlite3.connect(old)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        row = conn.execute("SELECT COUNT(*) FROM posts").fetchone()
        ver = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        conn.close()
        print(f"  投稿 {row[0]} 件 / schema_version {ver[0] if ver else '?'}")
    except sqlite3.Error as e:
        print(f"DBを開けませんでした: {e}", file=sys.stderr)
        print("コンテナが動いたままだとロックされます。先に down してください。",
              file=sys.stderr)
        return 1

    moves = [(old + s, new + s) for s in SUFFIXES if os.path.exists(old + s)]
    for src, dst in moves:
        print(f"  {'(dry)' if dry else ''} {os.path.basename(src)} → "
              f"{os.path.basename(dst)}")
        if not dry:
            os.rename(src, dst)

    if dry:
        print("\n--dry-run のため移動していません。")
        return 0

    print(f"\n完了。次回から {NEW} が使われます。")
    print("起動後に `accounts list` で件数が合っているか確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
