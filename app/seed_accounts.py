#!/usr/bin/env python3
"""
insta-ray / app/seed_accounts.py

監視対象アカウントの登録・一覧・有効無効の切り替え。
移植マップで 🔴新規 としていたもの。

accounts は「永続側」なので、ここで手で育てる。
プロフィール情報（followers 等）はスクレイパが取得時に埋めるので、
ここでは username だけ入っていればよい。

    python seed_accounts.py add user1 user2 user3
    python seed_accounts.py add user1 --note "資料用"
    python seed_accounts.py list
    python seed_accounts.py disable user1
    python seed_accounts.py enable user1
    python seed_accounts.py remove user1
    python seed_accounts.py import accounts.txt
"""

import argparse
import os
import re
import sys

import db

# IGのユーザー名: 英数字・アンダースコア・ピリオド、30文字以内
VALID = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def normalize(name):
    """URL や @ 付きで渡されても拾えるようにする。"""
    name = name.strip()
    if not name:
        return None
    # https://www.instagram.com/foo/ → foo
    m = re.search(r"instagram\.com/([^/?#]+)", name)
    if m:
        name = m.group(1)
    name = name.lstrip("@").strip("/")
    name = name.lower()
    return name if VALID.match(name) else None


def cmd_add(conn, args):
    added, skipped, bad = 0, 0, []
    for raw in args.usernames:
        u = normalize(raw)
        if not u:
            bad.append(raw)
            continue
        cur = conn.execute(
            "INSERT INTO accounts (username, note) VALUES (?, ?) "
            "ON CONFLICT(username) DO NOTHING",
            (u, args.note),
        )
        if cur.rowcount:
            added += 1
            print(f"  + {u}")
        else:
            skipped += 1
            print(f"  = {u}（既に登録済み）")
    conn.commit()
    if bad:
        print(f"\n不正なユーザー名として無視: {', '.join(bad)}", file=sys.stderr)
    print(f"\n追加 {added} / 既存 {skipped}" + (f" / 無視 {len(bad)}" if bad else ""))
    return 1 if bad and not added else 0


def cmd_list(conn, args):
    rows = conn.execute(
        "SELECT a.username, a.is_enabled, COALESCE(a.is_muted, 0) AS is_muted, "
        "  a.followers, a.note, a.updated_at, "
        "  (SELECT COUNT(*) FROM posts p WHERE p.owner_username = a.username) AS n_posts, "
        "  (SELECT MAX(date_utc) FROM posts p WHERE p.owner_username = a.username) AS latest "
        "FROM accounts a ORDER BY a.is_enabled DESC, a.username"
    ).fetchall()

    if not rows:
        print("登録なし。`seed_accounts.py add ユーザー名` で追加してください。")
        return 0

    print(f"{'':2} {'username':22} {'posts':>6}  {'followers':>10}  latest")
    print("-" * 68)
    for r in rows:
        mark = "🔇" if r["is_muted"] else (" " if r["is_enabled"] else "x")
        latest = (r["latest"] or "")[:10]
        fol = r["followers"] if r["followers"] is not None else "-"
        print(f"{mark:2} {r['username']:22} {r['n_posts']:>6}  {str(fol):>10}  {latest}")
        if r["note"]:
            print(f"{'':25}note: {r['note']}")
    n_on = sum(1 for r in rows if r["is_enabled"])
    print(f"\n計 {len(rows)} 件（有効 {n_on} / 無効 {len(rows)-n_on}）")
    print("x = 無効 / 🔇 = ミュート。どちらも巡回時にスキップされます。")
    return 0


def _set_enabled(conn, usernames, value):
    n = 0
    for raw in usernames:
        u = normalize(raw)
        if not u:
            continue
        cur = conn.execute(
            "UPDATE accounts SET is_enabled = ? WHERE username = ?", (value, u)
        )
        if cur.rowcount:
            n += 1
            print(f"  {'有効' if value else '無効'}: {u}")
        else:
            print(f"  未登録: {u}", file=sys.stderr)
    conn.commit()
    return 0 if n else 1


def cmd_enable(conn, args):
    return _set_enabled(conn, args.usernames, 1)


def cmd_disable(conn, args):
    return _set_enabled(conn, args.usernames, 0)


def cmd_mute(conn, args):
    """ミュート。取得も表示もされなくなる（データは消さない）。"""
    n = 0
    for raw in args.usernames:
        u = normalize(raw)
        if not u:
            print(f"  不正な名前: {raw}", file=sys.stderr)
            continue
        if db.set_mute(conn, u, True, getattr(args, "reason", None)):
            n += 1
            print(f"  🔇 {u}")
    conn.commit()
    if n:
        print(f"\n{n}件をミュートしました。取得対象から外れます。")
    return 0 if n else 1


def cmd_unmute(conn, args):
    n = 0
    for raw in args.usernames:
        u = normalize(raw)
        if not u:
            continue
        row = conn.execute(
            "SELECT is_muted FROM accounts WHERE username = ?", (u,)).fetchone()
        if row is None:
            print(f"  未登録: {u}", file=sys.stderr)
            continue
        db.set_mute(conn, u, False)
        n += 1
        print(f"  🔊 {u}")
    conn.commit()
    return 0 if n else 1


def cmd_muted(conn, args):
    rows = db.muted_accounts(conn)
    if not rows:
        print("ミュート中のアカウントはありません。")
        return 0
    print(f"{'username':24} {'投稿数':>6}  ミュート日時")
    print("-" * 60)
    for r in rows:
        when = (r["muted_at"] or "")[:16].replace("T", " ")
        print(f"{r['username']:24} {r['n_posts']:>6}  {when}")
        if r["mute_reason"]:
            print(f"{'':26}理由: {r['mute_reason']}")
    print(f"\n計 {len(rows)} 件")
    return 0


def cmd_remove(conn, args):
    """
    accounts からのみ削除する。posts / media_index は消さない。
    キャッシュ側は別途消せるので、ここで巻き込むと事故る。
    """
    n = 0
    for raw in args.usernames:
        u = normalize(raw)
        if not u:
            continue
        n_posts = conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE owner_username = ?", (u,)
        ).fetchone()["c"]
        cur = conn.execute("DELETE FROM accounts WHERE username = ?", (u,))
        if cur.rowcount:
            n += 1
            print(f"  削除: {u}")
            if n_posts:
                print(f"    ※ 取得済みの {n_posts} 件の投稿は残しています")
                print(f"       まとめて消すなら: purge {u}")
        else:
            print(f"  未登録: {u}", file=sys.stderr)
    conn.commit()
    return 0 if n else 1


def cmd_purge(conn, args):
    """
    アカウントを完全に削除する（破壊的）。

    remove との違い:
      remove … accounts から消すだけ。posts / media_index / 実体は残る
      purge  … 投稿・メディア実体・アイコン・accounts 行まで消す

    **ブックマークは消さない。** 非正規化コピーなので bookmarks 行はそのまま残り、
    それが参照しているメディア実体も保護される（本人方針）。
    ただし accounts 行が消えるため、ブックマーク表示のアイコンと表示名は
    失われる。残したい場合は --keep-avatar。
    """
    import cache_utils

    u = normalize(args.username)
    if not u:
        print(f"不正なユーザー名: {args.username}", file=sys.stderr)
        return 1

    plan = cache_utils.purge_plan(conn, u)

    known = conn.execute(
        "SELECT 1 FROM accounts WHERE username = ?", (u,)).fetchone()
    if not known and not plan["posts"] and not plan["bookmarks"]:
        print(f"{u} に関するデータはありません。")
        return 1

    print(f"\n=== {u} の完全削除 ===")
    print(f"  投稿             : {plan['posts']} 件")
    print(f"  media_index 行   : {plan['media_rows']} 行")
    print(f"  削除するファイル : {len(plan['files'])} 件"
          f"（{cache_utils.fmt_size(plan['bytes'])}）")
    if plan["protected_bookmark"]:
        print(f"  保護（ブックマーク参照）: {plan['protected_bookmark']} 件")
    if plan["protected_shared"]:
        print(f"  保護（他アカウントも参照）: {plan['protected_shared']} 件")
    print(f"  アイコン         : "
          f"{'残す' if args.keep_avatar else ('削除' if plan['avatar'] else 'なし')}")
    print(f"  accounts 行      : {'削除' if known else 'なし'}")

    if plan["bookmarks"]:
        print(f"\n  ※ ブックマーク {plan['bookmarks']} 件はそのまま残ります。")
        if not args.keep_avatar:
            print("     accounts 行が消えるので、表示上のアイコンと表示名は失われます。")
            print("     残したい場合は --keep-avatar を付けてください。")

    if args.dry_run:
        print("\n--dry-run のため何も削除していません。")
        return 0

    if not args.yes:
        print(f"\nこの操作は取り消せません。続けるならユーザー名を入力してください。")
        try:
            typed = input(f"  {u} > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n中止しました。")
            return 1
        if typed != u:
            print("一致しないので中止しました。")
            return 1

    result = cache_utils.purge_account(conn, u, keep_avatar=args.keep_avatar)
    print(f"\n削除しました: 投稿 {result['posts']} 件 / "
          f"ファイル {result['deleted_files']} 件 "
          f"({cache_utils.fmt_size(result['freed'])} 解放)")
    if result["bookmarks"]:
        print(f"ブックマーク {result['bookmarks']} 件は保持しています。")
    return 0


def cmd_import(conn, args):
    """1行1ユーザー名のテキストから取り込む。# 以降はコメント。"""
    if not os.path.exists(args.path):
        print(f"ファイルがありません: {args.path}", file=sys.stderr)
        return 1
    names = []
    with open(args.path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                names.append(line)
    if not names:
        print("取り込む行がありませんでした。")
        return 0
    args.usernames = names
    if not hasattr(args, "note"):
        args.note = None
    return cmd_add(conn, args)


def main():
    ap = argparse.ArgumentParser(description="insta-ray 監視アカウント管理")
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="追加")
    p.add_argument("usernames", nargs="+")
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="一覧")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("enable", help="有効化")
    p.add_argument("usernames", nargs="+")
    p.set_defaults(func=cmd_enable)

    p = sub.add_parser("disable", help="無効化（巡回対象から外す）")
    p.add_argument("usernames", nargs="+")
    p.set_defaults(func=cmd_disable)

    p = sub.add_parser("mute", help="ミュート（取得も表示もしない）")
    p.add_argument("usernames", nargs="+")
    p.add_argument("--reason", default=None)
    p.set_defaults(func=cmd_mute)

    p = sub.add_parser("unmute", help="ミュート解除")
    p.add_argument("usernames", nargs="+")
    p.set_defaults(func=cmd_unmute)

    p = sub.add_parser("muted", help="ミュート一覧")
    p.set_defaults(func=cmd_muted)

    p = sub.add_parser("remove", help="監視対象から外す（投稿データは残る）")
    p.add_argument("usernames", nargs="+")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("purge", help="完全削除（投稿・メディア実体まで消す。ブックマークは残る）")
    p.add_argument("username")
    p.add_argument("--dry-run", action="store_true",
                   help="何が消えるかだけ表示する")
    p.add_argument("--yes", action="store_true",
                   help="確認プロンプトを飛ばす")
    p.add_argument("--keep-avatar", action="store_true",
                   help="アイコンを残す（ブックマーク表示を維持したいとき）")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("import", help="テキストから一括取り込み")
    p.add_argument("path")
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_import)

    args = ap.parse_args()

    conn = db.connect(args.db)
    db.init_db(conn)
    try:
        return args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
