"""
insta-ray / dev/probe_posts.py

投稿の生データを覗いて、どのフィールドで落ちるかを特定する。
DB には一切書かない。

    docker compose run --rm probe 対象アカウント名 --limit 12
    docker compose run --rm probe --shortcode ABCdef123      # 投稿1件だけ狙い撃ち

post_to_record が KeyError で落ちるときに、
どの投稿の・どのプロパティが原因かを切り分けるためのもの。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, "/app")

import ig_scraper as igs   # noqa: E402

# 個別に試すプロパティ。location は追加リクエストを飛ばすので既定では触らない。
# **video_url も既定では触らない。** instaloader 4.15.3 の実装では、動画の解像度
# 候補が複数あると Content-Length を比べるために CDN へ HEAD を投げる
# （= 動画1件で2〜4リクエスト）。--video-url-probe を付けたときだけ試す。
PROPS = [
    "shortcode", "mediaid", "typename", "is_video",
    "owner_username", "caption", "date_utc", "likes", "comments",
    "url", "caption_hashtags",
]


def probe_post(post, index, check_location=False, dump_node=False,
               video_url_probe=False):
    print(f"\n--- [{index}] ---")

    sc = None
    try:
        sc = post.shortcode
        print(f"shortcode: {sc}")
    except Exception as e:
        print(f"shortcode: !! {type(e).__name__}: {e}")

    failures = []
    for name in PROPS:
        if name == "shortcode":
            continue
        try:
            v = getattr(post, name)
            if name == "caption" and v:
                v = v.replace("\n", " ")[:40] + "..."
            elif name in ("url", "video_url") and v:
                v = str(v)[:50] + "..."
            print(f"  {name:18} = {v}")
        except Exception as e:
            failures.append(name)
            print(f"  {name:18} !! {type(e).__name__}: {e}")

    # カルーセル
    try:
        tn = post.typename
    except Exception:
        tn = None
    if tn == "GraphSidecar":
        try:
            nodes = list(post.get_sidecar_nodes())
            print(f"  sidecar_nodes      = {len(nodes)} 件")
            for j, n in enumerate(nodes[:3]):
                print(f"    [{j}] is_video={n.is_video} "
                      f"display={str(n.display_url)[:40]}...")
        except Exception as e:
            failures.append("get_sidecar_nodes")
            print(f"  sidecar_nodes      !! {type(e).__name__}: {e}")

    # 動画URLは既定で「生ノードから読む」経路を確認する（追加HTTPなし）
    if _is_video(post):
        try:
            v = igs.video_url_of(post, probe=False)
            print(f"  {'video_url(node)':18} = {str(v)[:50] + '...' if v else None}")
        except Exception as e:
            failures.append("video_url_of")
            print(f"  {'video_url(node)':18} !! {type(e).__name__}: {e}")
        if video_url_probe:
            try:
                v = post.video_url
                print(f"  {'video_url(prop)':18} = {str(v)[:50]}...  "
                      f"※CDNへHEADが飛んでいる")
            except Exception as e:
                failures.append("video_url(prop)")
                print(f"  {'video_url(prop)':18} !! {type(e).__name__}: {e}")

    if check_location:
        try:
            loc = post.location
            print(f"  location           = {loc.name if loc else None}")
        except Exception as e:
            failures.append("location")
            print(f"  location           !! {type(e).__name__}: {e}")

    # 実際に post_to_record を通してみる
    try:
        rec = igs.post_to_record(post)
        media = json.loads(rec["media_json"])
        print(f"  => post_to_record OK（media {len(media)}件, "
              f"is_video={rec['is_video']}, is_carousel={rec['is_carousel']}）")
    except Exception as e:
        print(f"  => post_to_record !! {type(e).__name__}: {e}")
        failures.append("post_to_record")

    if dump_node:
        node = getattr(post, "_node", None)
        if isinstance(node, dict):
            print(f"  _node のキー: {sorted(node.keys())}")
            iph = node.get("iphone_struct")
            if isinstance(iph, dict):
                print(f"  iphone_struct のキー: {sorted(iph.keys())}")
                loc = iph.get("location")
                if loc:
                    print(f"  iphone_struct.location のキー: {sorted(loc.keys())}")

    return sc, failures


def _is_video(post):
    try:
        return bool(post.is_video) or post.typename == "GraphVideo"
    except Exception:
        return False


def _summary(results):
    print(f"\n{'='*56}")
    print(f"調べた投稿: {len(results)} 件")
    bad = [(sc, f) for sc, f in results if f]
    if bad:
        print("問題のあった投稿:")
        for sc, f in bad:
            print(f"  {sc}: {', '.join(f)}")
    else:
        print("すべてのプロパティが取得できました。")


def main():
    ap = argparse.ArgumentParser(description="投稿の生データを覗く（DBに書かない）")
    ap.add_argument("username", nargs="?", default=None,
                    help="対象アカウント名（--shortcode を使うときは省略可）")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--login-user", default=None)
    ap.add_argument("--location", action="store_true",
                    help="location も試す（投稿ごとに追加リクエストが飛ぶ）")
    ap.add_argument("--dump-node", action="store_true",
                    help="生ノードのキー一覧も出す")
    ap.add_argument("--shortcode", default=None,
                    help="投稿1件を shortcode で直接引く（アカウント登録不要）。"
                         "GraphVideo の実データ検証はこれが一番軽い")
    ap.add_argument("--video-url-probe", action="store_true",
                    help="Post.video_url プロパティも試す（CDNへHEADが飛ぶ）")
    args = ap.parse_args()

    if not args.username and not args.shortcode:
        ap.error("username か --shortcode のどちらかが必要です")

    loader, err = igs.make_loader(args.login_user)
    if loader is None:
        print(err)
        return 2

    results = []

    # --- shortcode 直指定 ---------------------------------------------------
    # 監視対象に無いアカウントの投稿でも1件だけ確認できる。
    # get_posts() のページ送りを回さないのでリクエストは1回で済む。
    if args.shortcode:
        import instaloader
        try:
            post = instaloader.Post.from_shortcode(loader.context, args.shortcode)
            results.append(probe_post(post, 0, args.location, args.dump_node,
                                      args.video_url_probe))
        except igs.RateLimitAbort as e:
            print(f"\nレート制限で中止: {e}")
        except Exception as e:
            print(f"\n取得失敗: {type(e).__name__}: {e}")
            print("※ from_shortcode は get_posts() とは別エンドポイントです。"
                  "429 を食った場合は時間を空けてください。")
        _summary(results)
        return 0

    profile = igs.minimal_profile(loader.context, args.username)

    try:
        for i, post in enumerate(profile.get_posts()):
            if i >= args.limit:
                break
            results.append(probe_post(post, i, args.location, args.dump_node,
                                      args.video_url_probe))
    except igs.RateLimitAbort as e:
        print(f"\nレート制限で中止: {e}")
    except Exception as e:
        print(f"\n取得中断: {type(e).__name__}: {e}")

    _summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
