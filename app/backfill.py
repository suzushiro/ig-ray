#!/usr/bin/env python3
"""
ig-ray / app/backfill.py

指定アカウントの投稿を**最古まで遡って丸ごと保管する**（全件バックフィル）。
1アカウントに数日〜1週間かけてよい前提で、とにかく叩きすぎないことを優先している。

    python backfill.py add 対象アカウント名
    python backfill.py list
    python backfill.py run              # 常駐。キューを順に消化する
    python backfill.py run --once       # 1アカウント終わったら抜ける

## なぜイテレータを生かしたまま常駐させるのか

instaloader の `NodeIterator` は `freeze()` / `thaw()` でページ送りの途中状態を
保存・復元できる（`FrozenNodeIterator` は素の JSON、実測 500バイト程度）。
ただし **`thaw()` するには新しいイテレータを作る必要があり、
`NodeIterator.__init__` はコンストラクタの中で1ページ目を取りに行く**
（`Profile.get_posts()` はログイン時 `first_data=None` のため）。
`thaw()` はその結果を捨てるので、**再開1回につき1リクエストが丸損**になる。

なので「1ページ取ってプロセス終了、次回再開」はリクエストが倍増する悪手。
プロセスは生かしたままページ間でスリープし、
`freeze()` の保存は**クラッシュ保険のチェックポイント**として使う
（freeze 自体はローカル処理でリクエストを消費しない）。

## 既知の投稿に当たったときの打ち切り

2周目以降は、**完全に保管済みの投稿が KNOWN_STREAK 件連続したら打ち切る**。
「保管済み」は posts に行があるだけでなく media_index の実体まで揃っていること
（画像バックアップが目的なので、レコードだけある投稿を既知と数えると取りこぼす）。

**初回は必ず最後まで走る。** cron が上位N件を既に取っているので、
初回から打ち切り判定を効かせると先頭で止まってしまう。
`completed_runs > 0` のときだけ有効にしている。
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone

import config
import db
import ig_scraper as igs

# ページ間の休憩（秒）。1ページ12件なので、既定15分前後だと
# 1日あたり およそ1000件強のペースになる。
PAGE_SLEEP_MIN = config.env_float("BACKFILL_PAGE_SLEEP_MIN", 600)
PAGE_SLEEP_MAX = config.env_float("BACKFILL_PAGE_SLEEP_MAX", 1200)

# 保管済みの投稿が何件連続したら打ち切るか（2周目以降のみ）
KNOWN_STREAK = config.env_int("BACKFILL_KNOWN_STREAK", 30)

# cron の巡回が直近この秒数以内に動いていたら待つ
QUIET_SEC = config.env_float("BACKFILL_QUIET_SEC", 180)

# 429 を食ったときのバックオフ（秒）
RATELIMIT_BACKOFF = config.env_float("BACKFILL_RATELIMIT_BACKOFF", 3600)

# ジョブが1件も無いときのポーリング間隔
IDLE_SLEEP = config.env_float("BACKFILL_IDLE_SLEEP", 300)

WHO = "backfill"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# 再開情報（FrozenNodeIterator）の出し入れ
# --------------------------------------------------------------------------

def freeze_to_json(iterator):
    """NodeIterator -> JSON文字列。freeze できない状態なら None。"""
    try:
        frozen = iterator.freeze()
    except Exception as e:
        log(f"  freeze できませんでした: {type(e).__name__}: {e}")
        return None
    try:
        return json.dumps(frozen._asdict(), ensure_ascii=False)
    except (TypeError, ValueError) as e:
        log(f"  freeze のJSON化に失敗: {e}")
        return None


def thaw_from_json(iterator, blob):
    """
    保存済みの再開情報をイテレータへ流し込む。成功したら True。

    thaw() は query_variables / query_referer / doc_id / **ログインアカウント名**
    の一致を要求する。ズレていれば InvalidArgumentException で弾かれるので、
    その場合は素直に最初からやり直す（黙って別の位置から再開するより安全）。
    """
    from instaloader.nodeiterator import FrozenNodeIterator

    try:
        frozen = FrozenNodeIterator(**json.loads(blob))
    except (TypeError, ValueError) as e:
        log(f"  再開情報を読めません（最初から取り直します）: {e}")
        return False

    if frozen.best_before and frozen.best_before < time.time():
        log("  再開情報の有効期限切れ（最初から取り直します）")
        return False

    try:
        iterator.thaw(frozen)
    except Exception as e:
        log(f"  再開情報が一致しません（最初から取り直します）: {type(e).__name__}: {e}")
        return False
    return True


# --------------------------------------------------------------------------
# 待ち
# --------------------------------------------------------------------------

def wait_for_quiet(conn):
    """cron の巡回が走っている間は手を出さない。"""
    waited = 0
    while True:
        since = db.seconds_since_activity(conn, "scrape")
        if since is None or since >= QUIET_SEC:
            return waited
        nap = min(60, QUIET_SEC - since + 5)
        if waited == 0:
            log(f"  巡回が動いているので待機します（あと {int(QUIET_SEC - since)}秒目安）")
        time.sleep(nap)
        waited += nap
        if waited > QUIET_SEC * 4:      # 何かおかしいときに無限に待たない
            log("  待機が長すぎるので続行します")
            return waited


def page_nap(conn=None):
    """
    ページ間の休憩。**寝る直前に次回時刻を記録する。**

    間隔はランダムなので、表示側で「前回＋間隔」を計算しても当たらない。
    実際に何秒寝るかを知っているのはここだけ。
    """
    secs = random.uniform(PAGE_SLEEP_MIN, PAGE_SLEEP_MAX)
    if conn is not None:
        try:
            db.set_next_run(conn, WHO, secs)
        except Exception:
            pass
    log(f"  次のページまで {int(secs//60)}分{int(secs%60)}秒 休みます")
    time.sleep(secs)


# --------------------------------------------------------------------------
# 本体
# --------------------------------------------------------------------------

def run_job(conn, loader, job, page_limit=None):
    """
    1アカウントぶんのバックフィルを進める。

    戻り値: 'done' / 'stopped'（既知に当たって打ち切り）/ 'ratelimited' / 'error'
    """
    username = job["username"]
    login_user = getattr(loader.context, "username", None)
    use_streak = (job["completed_runs"] or 0) > 0

    log(f"=== バックフィル: {username} "
        f"（{'2周目以降・打ち切りあり' if use_streak else '初回・最後まで走ります'}）")

    profile = igs.minimal_profile(loader.context, username)

    # ここで1リクエスト飛ぶ（NodeIterator のコンストラクタが1ページ目を取る）
    wait_for_quiet(conn)
    db.touch_activity(conn, WHO)
    try:
        iterator = profile.get_posts()
    except igs.RateLimitAbort as e:
        db.backfill_update(conn, username, status="ratelimited", message=str(e))
        return "ratelimited"

    resumed = False
    if job["resume_json"]:
        if job["login_user"] and job["login_user"] != login_user:
            log(f"  ログインアカウントが変わっています "
                f"（{job['login_user']} → {login_user}）。最初から取り直します")
        else:
            resumed = thaw_from_json(iterator, job["resume_json"])
    if resumed:
        log(f"  再開しました（{job['posts_seen']} 件処理済み）")

    db.backfill_update(conn, username, status="running", login_user=login_user,
                       started_at=job["started_at"] or
                       datetime.now(timezone.utc).isoformat(), message=None)

    seen = job["posts_seen"] or 0
    saved = job["posts_saved"] or 0
    media = job["media_saved"] or 0
    pages = job["pages"] or 0
    streak = job["known_streak"] or 0
    oldest = job["oldest_date"]

    media_sess = igs.media_session(loader)
    in_page = 0
    result = "done"

    # チェックポイントの間隔は instaloader のページ長に合わせる（既定12）。
    # 即値で書くと向こうが変えたときに境界がずれる。
    try:
        page_len = iterator.page_length()
    except Exception:
        page_len = 12

    try:
        while True:
            try:
                post = next(iterator)
            except StopIteration:
                break

            seen += 1
            in_page += 1

            try:
                shortcode = post.shortcode
            except Exception:
                shortcode = None

            archived = bool(shortcode) and db.post_is_archived(conn, shortcode)
            if archived:
                streak += 1
            else:
                streak = 0

            if use_streak and streak >= KNOWN_STREAK:
                log(f"  保管済みが {streak} 件連続したので打ち切ります")
                result = "stopped"
                break

            if not archived:
                try:
                    record = igs.post_to_record(post)
                    _, ins = db.save_posts(conn, [record])
                    saved += ins
                    owner = igs.owner_from_node(post)
                    if owner:
                        owner["profile_pic_local"] = igs.save_avatar(
                            owner["username"], owner.get("profile_pic_url"),
                            session_obj=media_sess)
                        db.merge_account(conn, owner)
                    media += igs.download_media(
                        conn, record["shortcode"],
                        json.loads(record["media_json"]), session_obj=media_sess)
                    # 投稿ごとにコミットする（v3.8 の教訓：まとめてコミットすると
                    # 休憩のあいだ書き込みロックを握りっぱなしになる）
                    conn.commit()
                    oldest = record["date_utc"]
                except igs.RateLimitAbort:
                    raise
                except Exception as e:
                    log(f"  投稿 {shortcode} で失敗（続行）: {type(e).__name__}: {e}")

                igs._nap()

            # ページ境界でチェックポイント＋休憩
            if in_page >= page_len:
                pages += 1
                in_page = 0
                db.backfill_update(
                    conn, username, resume_json=freeze_to_json(iterator),
                    posts_seen=seen, posts_saved=saved, media_saved=media,
                    pages=pages, known_streak=streak, oldest_date=oldest)
                log(f"  {seen} 件処理（新規 {saved} / メディア {media}）"
                    f"{'  ※保管済み連続 %d' % streak if streak else ''}")
                if page_limit and pages >= page_limit:
                    log("  --pages の上限に達しました")
                    result = "stopped"
                    break
                page_nap(conn)
                wait_for_quiet(conn)
                db.touch_activity(conn, WHO)

    except igs.RateLimitAbort as e:
        db.backfill_update(
            conn, username, status="ratelimited",
            resume_json=freeze_to_json(iterator), posts_seen=seen,
            posts_saved=saved, media_saved=media, pages=pages,
            known_streak=streak, oldest_date=oldest, message=str(e))
        log(f"  レート制限：{e}")
        return "ratelimited"
    except KeyboardInterrupt:
        db.backfill_update(
            conn, username, status="queued",
            resume_json=freeze_to_json(iterator), posts_seen=seen,
            posts_saved=saved, media_saved=media, pages=pages,
            known_streak=streak, oldest_date=oldest, message="中断")
        log("  中断しました（進捗は保存済み）")
        raise
    except Exception as e:
        db.backfill_update(
            conn, username, status="error",
            resume_json=freeze_to_json(iterator), posts_seen=seen,
            posts_saved=saved, media_saved=media, pages=pages,
            known_streak=streak, oldest_date=oldest,
            message=f"{type(e).__name__}: {e}")
        log(f"  失敗: {type(e).__name__}: {e}")
        return "error"

    if result == "stopped" and page_limit:
        # --pages で止めただけなので再開できるようにしておく
        db.backfill_update(
            conn, username, status="queued",
            resume_json=freeze_to_json(iterator), posts_seen=seen,
            posts_saved=saved, media_saved=media, pages=pages,
            known_streak=streak, oldest_date=oldest, message="ページ上限で中断")
        return "stopped"

    # 走り切った or 既知に当たって打ち切り = 1周完了
    db.backfill_update(
        conn, username, status="done", resume_json=None,
        posts_seen=seen, posts_saved=saved, media_saved=media, pages=pages,
        known_streak=0, oldest_date=oldest,
        completed_runs=(job["completed_runs"] or 0) + 1,
        completed_at=datetime.now(timezone.utc).isoformat(),
        message="打ち切り（保管済み）" if result == "stopped" else None)
    db.clear_next_run(conn, WHO)
    log(f"  完了: {seen} 件確認 / 新規 {saved} / メディア {media}")
    db.log_scrape(conn, username, db.BACKFILL_DONE, fetched=seen, inserted=saved)
    conn.commit()
    return result


# --------------------------------------------------------------------------
# サブコマンド
# --------------------------------------------------------------------------

def cmd_add(conn, args):
    from seed_accounts import normalize
    n = 0
    for raw in args.usernames:
        u = normalize(raw)
        if not u:
            print(f"  不正な名前: {raw}", file=sys.stderr)
            continue
        if db.backfill_add(conn, u):
            n += 1
            print(f"  + {u}")
        else:
            print(f"  = {u}（既にキューにあります）")
    print(f"\n追加 {n} 件")
    return 0


def cmd_add_all(conn, args):
    """監視対象の有効アカウントをまとめてキューに積む。"""
    users = db.enabled_accounts(conn)
    n = sum(1 for u in users if db.backfill_add(conn, u))
    print(f"監視対象 {len(users)} 件のうち {n} 件を新規に追加しました。")
    return 0


def cmd_list(conn, args):
    rows = db.backfill_list(conn)
    if not rows:
        print("バックフィルのジョブはありません。`backfill add ユーザー名` で追加します。")
        return 0
    print(f"{'status':12} {'username':22} {'確認':>7} {'新規':>7} "
          f"{'メディア':>8} {'頁':>5}  最古")
    print("-" * 78)
    for r in rows:
        print(f"{r['status']:12} {r['username']:22} {r['posts_seen']:>7} "
              f"{r['posts_saved']:>7} {r['media_saved']:>8} {r['pages']:>5}  "
              f"{(r['oldest_date'] or '')[:10]}")
        if r["message"]:
            print(f"{'':13}└ {r['message']}")
    print(f"\n計 {len(rows)} 件")
    return 0


def _set_status(conn, usernames, status):
    n = 0
    for u in usernames:
        if db.backfill_get(conn, u) is None:
            print(f"  未登録: {u}", file=sys.stderr)
            continue
        db.backfill_update(conn, u, status=status)
        n += 1
        print(f"  {u} → {status}")
    return 0 if n else 1


def cmd_pause(conn, args):
    return _set_status(conn, args.usernames, "paused")


def cmd_resume(conn, args):
    return _set_status(conn, args.usernames, "queued")


def cmd_reset(conn, args):
    """再開情報を捨てて最初からやり直す。取得済みの投稿は消さない。"""
    n = 0
    for u in args.usernames:
        if db.backfill_get(conn, u) is None:
            print(f"  未登録: {u}", file=sys.stderr)
            continue
        db.backfill_update(conn, u, status="queued", resume_json=None,
                           posts_seen=0, posts_saved=0, media_saved=0,
                           pages=0, known_streak=0, message=None)
        n += 1
        print(f"  {u} をリセットしました（投稿データは残っています）")
    return 0 if n else 1


def cmd_remove(conn, args):
    n = 0
    for u in args.usernames:
        cur = conn.execute("DELETE FROM backfill_jobs WHERE username = ?", (u,))
        if cur.rowcount:
            n += 1
            print(f"  キューから削除: {u}")
    conn.commit()
    return 0 if n else 1


def cmd_run(conn, args):
    loader, err = igs.make_loader(args.login_user)
    if loader is None:
        print(err, file=sys.stderr)
        return 2

    log(f"バックフィル開始（ページ間 {int(PAGE_SLEEP_MIN)}〜{int(PAGE_SLEEP_MAX)}秒 / "
        f"打ち切り {KNOWN_STREAK}件連続）")

    while True:
        db.touch_activity(conn, "backfill_worker")   # ワーカーの生存確認用
        job = db.backfill_next(conn)
        if job is None:
            if args.once:
                log("処理するジョブがありません。")
                return 0
            db.clear_next_run(conn, WHO)
            time.sleep(IDLE_SLEEP)
            continue

        if job["status"] == "ratelimited":
            log(f"{job['username']} は直前にレート制限。"
                f"{int(RATELIMIT_BACKOFF//60)}分あけます")
            db.set_next_run(conn, WHO, RATELIMIT_BACKOFF)
            time.sleep(RATELIMIT_BACKOFF)

        result = run_job(conn, loader, job, page_limit=args.pages)

        if result == "ratelimited":
            db.set_next_run(conn, WHO, RATELIMIT_BACKOFF)
            time.sleep(RATELIMIT_BACKOFF)
        elif result == "error":
            # 同じジョブを掴み続けて空回りしないよう status=error のまま置く
            time.sleep(60)

        if args.once:
            return 0 if result in ("done", "stopped") else 1


def main():
    ap = argparse.ArgumentParser(description="ig-ray 全件バックフィル")
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="キューに積む")
    p.add_argument("usernames", nargs="+")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("add-all", help="監視対象の有効アカウントを全部積む")
    p.set_defaults(func=cmd_add_all)

    p = sub.add_parser("list", help="進捗一覧")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("pause", help="一時停止")
    p.add_argument("usernames", nargs="+")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume", help="再開")
    p.add_argument("usernames", nargs="+")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("reset", help="進捗を捨てて最初から（投稿データは残る）")
    p.add_argument("usernames", nargs="+")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("remove", help="キューから外す")
    p.add_argument("usernames", nargs="+")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("run", help="キューを消化する（常駐）")
    p.add_argument("--once", action="store_true", help="1ジョブで抜ける")
    p.add_argument("--pages", type=int, default=None,
                   help="このページ数で中断する（試運転用）")
    p.add_argument("--login-user", default=None)
    p.set_defaults(func=cmd_run)

    args = ap.parse_args()

    conn = db.connect(args.db)
    db.init_db(conn)
    try:
        return args.func(conn, args)
    except KeyboardInterrupt:
        print("\n中断しました。")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
