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
        # is_enabled / is_target は列の既定に頼らず明示する。
        # 既定に頼っていたせいで、共同投稿の相手が作った行まで
        # 巡回対象になっていた（v4.16 で分離）。
        cur = conn.execute(
            "INSERT INTO accounts (username, note, is_enabled, is_target) "
            "VALUES (?, ?, 1, 1) ON CONFLICT(username) DO NOTHING",
            (u, args.note),
        )
        if cur.rowcount:
            added += 1
            print(f"  + {u}")
        else:
            # 既にある行が「自動でできた行」なら、監視対象へ昇格させる。
            # 共同投稿で先に流れてきたアカウントを後から追いたい場合に効く。
            if not db.is_target(conn, u):
                db.set_target(conn, u, True)
                added += 1
                print(f"  + {u}（自動登録済みの行を監視対象にしました）")
            else:
                skipped += 1
                print(f"  = {u}（既に登録済み）")
    conn.commit()
    if bad:
        print(f"\n不正なユーザー名として無視: {', '.join(bad)}", file=sys.stderr)
    print(f"\n追加 {added} / 既存 {skipped}" + (f" / 無視 {len(bad)}" if bad else ""))
    return 1 if bad and not added else 0


def cmd_list(conn, args):
    # 既定は監視対象だけ。--all で自動登録された行も含めて見る。
    where = "" if getattr(args, "all", False) else \
        "WHERE COALESCE(a.is_target, 0) = 1 "
    rows = conn.execute(
        "SELECT a.username, a.is_enabled, COALESCE(a.is_muted, 0) AS is_muted, "
        "  COALESCE(a.is_target, 0) AS is_target, "
        "  a.followers, a.note, a.updated_at, "
        "  (SELECT COUNT(*) FROM posts p WHERE p.owner_username = a.username) AS n_posts, "
        "  (SELECT MAX(date_utc) FROM posts p WHERE p.owner_username = a.username) AS latest "
        f"FROM accounts a {where}ORDER BY a.is_target DESC, a.is_enabled DESC, a.username"
    ).fetchall()

    if not rows:
        print("登録なし。`seed_accounts.py add ユーザー名` で追加してください。")
        return 0

    print(f"{'':2} {'username':22} {'posts':>6}  {'followers':>10}  latest")
    print("-" * 68)
    for r in rows:
        if r["is_muted"]:
            mark = "🔇"
        elif not r["is_target"]:
            mark = "-"
        elif not r["is_enabled"]:
            mark = "x"
        else:
            mark = " "
        latest = (r["latest"] or "")[:10]
        fol = r["followers"] if r["followers"] is not None else "-"
        print(f"{mark:2} {r['username']:22} {r['n_posts']:>6}  {str(fol):>10}  {latest}")
        if r["note"]:
            print(f"{'':25}note: {r['note']}")
    n_tgt = sum(1 for r in rows if r["is_target"])
    n_on = sum(1 for r in rows if r["is_target"] and r["is_enabled"])
    print(f"\n計 {len(rows)} 件（監視対象 {n_tgt} / うち有効 {n_on}）")
    print("x = 無効 / 🔇 = ミュート / - = 監視対象外（自動登録）。")
    if not getattr(args, "all", False):
        n_stray = conn.execute(
            "SELECT COUNT(*) c FROM accounts "
            "WHERE COALESCE(is_target, 0) = 0").fetchone()["c"]
        if n_stray:
            print(f"ほかに監視対象外が {n_stray} 件あります（`list --all` / `strays`）。")
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


def cmd_target(conn, args):
    """監視対象にする（既に accounts にある行の昇格にも使う）。"""
    n = 0
    for raw in args.usernames:
        u = normalize(raw)
        if not u:
            print(f"  不正な名前: {raw}", file=sys.stderr)
            continue
        db.set_target(conn, u, True)
        n += 1
        print(f"  ● {u}")
    conn.commit()
    if n:
        print(f"\n{n}件を監視対象にしました（巡回・表示の対象になります）。")
    return 0 if n else 1


def cmd_untarget(conn, args):
    """
    監視対象から外す。**行も投稿も消さない。**

    巡回されなくなり、フィード・ギャラリーにも出なくなる。
    ブックマーク済みの投稿は非正規化コピーなので残る。
    実体まで消したいときは purge を使う。
    """
    n = 0
    for raw in args.usernames:
        u = normalize(raw)
        if not u:
            continue
        row = conn.execute(
            "SELECT 1 FROM accounts WHERE username = ?", (u,)).fetchone()
        if row is None:
            print(f"  未登録: {u}", file=sys.stderr)
            continue
        db.set_target(conn, u, False)
        n += 1
        print(f"  ○ {u}")
    conn.commit()
    if n:
        print(f"\n{n}件を監視対象から外しました（データは残っています）。")
    return 0 if n else 1


def cmd_targets(conn, args):
    """
    監視対象を1行1ユーザー名で出す。**retarget に食わせる種を作るためのもの。**

        accounts targets --plain > data/targets.txt
        （エディタで知らない名前の行を消す）
        accounts retarget /data/targets.txt --apply

    マイグレーション直後は「混ざった行」も対象に入っているので、
    この往復で台帳の状態に一発で合わせられる（名前を打ち直さなくて済む）。
    """
    rows = db.target_accounts(conn)
    if args.plain:
        for r in rows:
            print(r["username"])
        return 0
    if not rows:
        print("監視対象がありません。`add ユーザー名` で追加してください。")
        return 0
    print(f"{'username':24} {'投稿数':>6}  状態")
    print("-" * 56)
    for r in rows:
        st = "ミュート" if r["is_muted"] else ("有効" if r["is_enabled"] else "無効")
        print(f"{r['username']:24} {r['n_posts']:>6}  {st}")
        if r["note"]:
            print(f"{'':26}note: {r['note']}")
    print(f"\n計 {len(rows)} 件")
    print("`targets --plain` で retarget 用のリストを書き出せます。")
    return 0


def cmd_strays(conn, args):
    """
    監視対象外なのに投稿が入っているアカウント。

    共同投稿の相手・関連投稿の出どころがここに出る。
    表示から消えるのはこの分。消す場合は purge。
    """
    rows = [r for r in db.stray_accounts(conn) if r["n_posts"]]
    if not rows:
        print("監視対象外のアカウントの投稿はありません。")
        return 0
    print(f"{'username':24} {'投稿数':>6}  表示名")
    print("-" * 60)
    total = 0
    for r in rows:
        total += r["n_posts"]
        mute = "🔇" if r["is_muted"] else ""
        print(f"{r['username']:24} {r['n_posts']:>6}  "
              f"{(r['full_name'] or '')[:20]} {mute}")
    print(f"\n計 {len(rows)} アカウント / {total} 投稿。これらは画面に出ません。")
    print("監視対象にするなら `target <username>`、")
    print("投稿ごと消すなら `purge <username>`（ブックマークは残ります）。")
    return 0


def cmd_retarget(conn, args):
    """
    監視対象リストを**ファイルの内容で置き換える**（1行1ユーザー名、# はコメント）。

    ファイルにある名前 → 監視対象 + 有効
    ファイルに無い既存の監視対象 → 監視対象から外す（データは残す）

    Notion の台帳と DB を一発で合わせるためのコマンド。
    破壊的なので、既定は差分の表示のみ。実行は --apply。
    """
    if not os.path.exists(args.path):
        print(f"ファイルがありません: {args.path}", file=sys.stderr)
        return 1

    wanted, bad = [], []
    with open(args.path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            u = normalize(line)
            (wanted if u else bad).append(u or line)
    if bad:
        print(f"不正なユーザー名として無視: {', '.join(bad)}", file=sys.stderr)
    if not wanted:
        print("ファイルに有効な行がありません。中止します。", file=sys.stderr)
        return 1

    wanted = set(wanted)
    current = {r["username"] for r in db.target_accounts(conn)}
    to_add = sorted(wanted - current)
    to_drop = sorted(current - wanted)

    print(f"ファイル {len(wanted)} 件 / 現在の監視対象 {len(current)} 件")
    for u in to_add:
        print(f"  + {u}")
    for u in to_drop:
        n = conn.execute("SELECT COUNT(*) c FROM posts WHERE owner_username = ?",
                         (u,)).fetchone()["c"]
        print(f"  - {u}（投稿 {n} 件は残ります）")
    if not to_add and not to_drop:
        print("\n差分なし。")
        return 0

    if not args.apply:
        print("\n表示のみです。実行するには --apply を付けてください。")
        return 0

    for u in to_add:
        db.set_target(conn, u, True)
    for u in to_drop:
        db.set_target(conn, u, False)
    conn.commit()
    print(f"\n反映しました（追加 {len(to_add)} / 除外 {len(to_drop)}）。"
          f" 監視対象は {len(wanted)} 件です。")
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

    p = sub.add_parser("list", help="一覧（既定は監視対象のみ）")
    p.add_argument("--all", action="store_true",
                   help="自動登録された監視対象外の行も含める")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("target", help="監視対象にする（巡回・表示の対象）")
    p.add_argument("usernames", nargs="+")
    p.set_defaults(func=cmd_target)

    p = sub.add_parser("untarget",
                       help="監視対象から外す（巡回も表示もしない。データは残る）")
    p.add_argument("usernames", nargs="+")
    p.set_defaults(func=cmd_untarget)

    p = sub.add_parser("targets", help="監視対象の一覧（--plain で retarget 用の種）")
    p.add_argument("--plain", action="store_true",
                   help="1行1ユーザー名だけを出す（リダイレクトして編集する用）")
    p.set_defaults(func=cmd_targets)

    p = sub.add_parser("strays", help="監視対象外なのに投稿があるアカウントの一覧")
    p.set_defaults(func=cmd_strays)

    p = sub.add_parser("retarget",
                       help="監視対象リストをファイルの内容で置き換える（--apply で実行）")
    p.add_argument("path")
    p.add_argument("--apply", action="store_true", help="実際に反映する")
    p.set_defaults(func=cmd_retarget)

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
