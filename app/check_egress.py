#!/usr/bin/env python3
"""
ig-ray / check_egress.py

**スクレイピングの出口IPが期待どおりか**を確認する。

背景:
  Instagram のスクレイピングは BAN / IP制限のリスクがあるため、
  普段使いの回線と切り離し、別回線側に立てたプロキシから出している。
  環境変数を入れ忘れたコンテナは IPv6 で本来の回線から出てしまい、
  分離が黙って崩れる。設定漏れに気づけるようにするのがこのツール。

確認するのは3経路。実際に使っている出口をそれぞれ試す:
  1. 素の requests            … 環境変数（HTTPS_PROXY）が効いているか
  2. instaloader の匿名セッション … メディアDLの出口
  3. instaloader のログイン済みセッション … 投稿取得の出口

    docker compose run --rm ipcheck
    docker compose run --rm ipcheck --expect 203.0.113.10
"""

import argparse
import ipaddress
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402

# IPv4 / IPv6 を区別して見たいので複数使う
V4_URL = "https://api.ipify.org"
V6_URL = "https://api64.ipify.org"


def show_env():
    print("=== 環境変数 ===")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
              "http_proxy", "https_proxy", "no_proxy"):
        v = os.environ.get(k)
        if v:
            print(f"  {k}={v}")
    p = config.env("PROXY", "")
    if p:
        print(f"  IG_RAY_PROXY={p}")
    if not any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy")) and not p:
        print("  ⚠ プロキシ設定が見当たりません（直接接続になります）")


def fetch(sess, url, label, results):
    try:
        r = sess.get(url, timeout=20)
        body = (r.text or "").strip()
        # 200 でもエラー本文が返ることがあるので、IPの形かを必ず検証する
        try:
            ipaddress.ip_address(body)
        except ValueError:
            # \r があるとカーソルが行頭に戻って表示が壊れるので潰す
            snippet = " ".join(body.split())[:70]
            print(f"  {label:34} !! IPが返らなかった (HTTP {r.status_code}): {snippet}")
            results.append((label, None))
            return
        print(f"  {label:34} {body}")
        results.append((label, body))
    except Exception as e:
        print(f"  {label:34} !! {type(e).__name__}: {e}")
        results.append((label, None))


def main():
    ap = argparse.ArgumentParser(description="出口IPの確認")
    ap.add_argument("--expect", default=config.env("EXPECT_EGRESS_IP"),
                    help="期待する出口IP。指定すると一致しない場合に非ゼロ終了")
    args = ap.parse_args()

    show_env()

    import requests
    results = []

    print("\n=== 出口IP ===")

    # 1) 素の requests（環境変数のプロキシが効く）
    fetch(requests.Session(), V4_URL, "requests (IPv4)", results)
    fetch(requests.Session(), V6_URL, "requests (IPv6優先)", results)

    # 2) instaloader の匿名セッション = メディアDLの出口
    try:
        import ig_scraper as igs
        fetch(igs.media_session(), V4_URL, "メディアDL用セッション", results)
    except Exception as e:
        print(f"  メディアDL用セッション            !! {type(e).__name__}: {e}")

    # 3) ログイン済みセッション = 投稿取得の出口
    #
    # このセッションには Instagram 用のヘッダ（Host: www.instagram.com など）が
    # 載っているため、そのまま ipify に投げると 403 で弾かれる。
    # メディアDLが全件404になったのと同じ構図。
    # 見たいのは「どのIPから出るか」だけなので、測定時はヘッダを外す。
    try:
        import ig_scraper as igs
        loader, err = igs.make_loader(verify=False)
        if loader is None:
            print("  投稿取得用セッション               (スキップ: セッション未設定)")
        else:
            sess = loader.context._session
            saved = dict(sess.headers)
            try:
                for k in ("Host", "Origin", "Referer", "X-Requested-With",
                          "X-Instagram-AJAX", "X-CSRFToken", "X-IG-App-ID"):
                    sess.headers.pop(k, None)
                fetch(sess, V4_URL, "投稿取得用セッション", results)
            finally:
                # 測定のために壊したままにしない
                sess.headers.clear()
                sess.headers.update(saved)
    except Exception as e:
        print(f"  投稿取得用セッション               !! {type(e).__name__}: {e}")

    # --- 判定 ---
    print()
    ips = {ip for _, ip in results if ip}
    if not ips:
        print("NG: 出口IPを1つも取得できませんでした。")
        return 2

    v6 = [ip for ip in ips if ":" in ip]
    if v6:
        print(f"⚠ IPv6 で出ている経路があります: {', '.join(v6)}")
        print("  プロキシは IPv4 のみなので、IPv6 経路は分離を迂回します。")

    if len(ips) > 1:
        print(f"⚠ 経路によって出口IPが違います: {', '.join(sorted(ips))}")
        print("  一部だけプロキシを通っていない可能性があります。")

    if args.expect:
        bad = [(label, ip) for label, ip in results if ip and ip != args.expect]
        if bad:
            print(f"\nNG: 期待した出口 {args.expect} と違う経路があります:")
            for label, ip in bad:
                print(f"  {label}: {ip}")
            return 1
        print(f"\nOK: すべて {args.expect} から出ています。")
        return 0

    print(f"\n出口IP: {', '.join(sorted(ips))}")
    print("（--expect か IG_RAY_EXPECT_EGRESS_IP を指定すると自動判定します）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
