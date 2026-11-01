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
import db  # noqa: E402

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


# --------------------------------------------------------------------------
# スクレイパーから呼ぶ判定
#
# **「回線のIPが変わっただけ」と「プロキシを迂回して漏れている」を区別する。**
# 区別せずに不一致で止めると、住宅向け回線のIP変更で取得が黙って死ぬ
# （X-Ray で12日止まり、ログファイルにしか出ていなかったので誰も気づかなかった）。
#
# 切り分けの原理: プロキシ経由で測った出口IPと、プロキシを明示的に外して
# 測った出口IPを比べる。
#   違う → プロキシは効いている（回線側のIPが変わっただけ）→ 続行
#   同じ → プロキシを通っていない（分離が崩れた）→ 中止
# --------------------------------------------------------------------------

# スクレイプを続行してよい verdict。
# ここに無いものは中止（判定できないケースは安全側に倒す）。
CONTINUE_VERDICTS = ("match", "unchecked", "proxy_ip_changed")

# scrape_log に記録しない verdict（正常なので6時間ごとに行を増やす意味がない）
QUIET_VERDICTS = ("match", "unchecked")

# scrape_log に入れる擬似アカウント名。**定義は db.py に一本化してある**
# （web / scraper / ここの3箇所で別々に持つと、片方だけ直して
#   画面のフィルタが効かなくなる）。
EGRESS_LOG_NAME = db.EGRESS_LOG_NAME


def _fetch_global_ip(session=None, url=V4_URL, timeout=20):
    """
    出口のグローバルIPを1つ返す。取れなければ None。

    session を渡さなければ環境変数のプロキシを見る普通のセッションを使う。
    200 でもエラー本文が返ることがあるので、必ずIPの形か検証する。
    """
    import requests

    sess = session if session is not None else requests.Session()
    try:
        r = sess.get(url, timeout=timeout)
        body = (r.text or "").strip()
        ipaddress.ip_address(body)       # IPの形でなければ ValueError
        return body
    except Exception:
        return None


def _direct_session():
    """
    **プロキシを一切通さない**セッション。

    `trust_env = False` で環境変数（HTTPS_PROXY 等）を無視する。
    それだけでは足りず、`IG_RAY_PROXY` で明示的に渡している分も
    空の proxies で潰す必要がある（trust_env は環境変数しか見ない）。
    """
    import requests

    sess = requests.Session()
    sess.trust_env = False
    sess.proxies = {}
    return sess


def _proxy_configured():
    """プロキシが設定されているか。文字列をそのまま返す（表示に使う）。"""
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k)
        if v:
            return v
    return config.env("PROXY", "") or ""


def check_egress(expected=None):
    """
    スクレイプ前の出口IP照合。**戻り値は dict**。

      ok        … スクレイプを続行してよいか
      ip        … プロキシ経由で測った出口IP
      direct_ip … プロキシを外して測った出口IP（測る必要が無ければ None）
      expected  … 期待値
      proxy     … 検出したプロキシ設定（空文字なら未設定）
      verdict   … match / proxy_ip_changed / proxy_bypassed /
                  no_proxy_configured / undetermined / unchecked / unreachable
      reason    … 人間が読む1行。そのまま画面に出す

    verdict を文字列で持たせているのは、呼び出し側が ok だけでなく
    「なぜ ok なのか」で分岐できるようにするため（記録の有無に使う）。
    """
    expected = (expected if expected is not None
                else config.env("EXPECT_EGRESS_IP", "")) or ""
    expected = expected.strip()
    proxy = _proxy_configured()

    def out(verdict, reason, ip=None, direct_ip=None):
        return {"ok": verdict in CONTINUE_VERDICTS, "ip": ip,
                "direct_ip": direct_ip, "expected": expected,
                "proxy": proxy, "verdict": verdict, "reason": reason}

    # 期待値が無ければ照合しない。記録もしない。
    if not expected:
        return out("unchecked",
                   "期待する出口IPが未設定のため照合しません"
                   "（IG_RAY_EXPECT_EGRESS_IP）")

    ip = _fetch_global_ip()
    if ip is None:
        # 出口IPが取れない＝ネットワークが怪しい。安全側に倒して中止する。
        return out("unreachable",
                   "出口IPを取得できませんでした。"
                   "ネットワークかプロキシが不調の可能性があります")

    if ip == expected:
        return out("match", f"出口IPは期待どおり（{ip}）", ip=ip)

    # --- ここから切り分け ---
    if not proxy:
        return out("no_proxy_configured",
                   f"出口IPが期待値と違い（{ip} ≠ {expected}）、"
                   "プロキシも設定されていません。"
                   "分離されていないため中止します", ip=ip)

    direct_ip = _fetch_global_ip(session=_direct_session())
    if direct_ip is None:
        # プロキシ無しで出られない（＝NW的にプロキシ必須の構成）とも取れるが、
        # 迂回していないことを確認できていないので中止する。
        return out("undetermined",
                   f"出口IPが期待値と違います（{ip} ≠ {expected}）。"
                   "プロキシを外した出口IPが取得できず、"
                   "迂回していないか判定できないため中止します",
                   ip=ip, direct_ip=None)

    if direct_ip == ip:
        return out("proxy_bypassed",
                   f"プロキシを迂回しています（プロキシ経由 {ip} ＝ 直接 {direct_ip}）。"
                   "出口の分離が崩れているため中止します",
                   ip=ip, direct_ip=direct_ip)

    return out("proxy_ip_changed",
               f"プロキシは効いています（経由 {ip} ≠ 直接 {direct_ip}）が、"
               f"期待値 {expected} と違います。"
               f"回線側のIPが変わったものとして続行します。"
               f"IG_RAY_EXPECT_EGRESS_IP を {ip} に更新してください",
               ip=ip, direct_ip=direct_ip)


def log_egress(conn, result):
    """
    判定結果を scrape_log に記録する。書いたら True。

    **ログファイルだけでは気づけない。** X-Ray が12日止まったのは
    中止の事実が /var/log にしか出ておらず、web画面にも
    docker compose logs にも出ていなかったため。
    """
    if result.get("verdict") in QUIET_VERDICTS:
        return False
    status = "egress_warn" if result.get("ok") else "egress_error"
    db.log_scrape(conn, EGRESS_LOG_NAME, status, 0, 0, result.get("reason"))
    return True


def clear_egress_log(conn):
    """
    出口IPの記録を消す。`.env` を直したあとバナーを黙らせるために使う。
    戻り値は消した行数。
    """
    cur = conn.execute("DELETE FROM scrape_log WHERE username = ?",
                       (EGRESS_LOG_NAME,))
    conn.commit()
    return cur.rowcount


def main():
    ap = argparse.ArgumentParser(description="出口IPの確認")
    ap.add_argument("--expect", default=config.env("EXPECT_EGRESS_IP"),
                    help="期待する出口IP。指定すると一致しない場合に非ゼロ終了")
    ap.add_argument("--clear-log", action="store_true",
                    help="scrape_log から出口IPの記録を消す"
                         "（.env を直したあとweb画面のバナーを消すのに使う）")
    ap.add_argument("--verdict", action="store_true",
                    help="スクレイパーと同じ判定だけを実行して結果を出す")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    # --clear-log は測定をせずに終わる（.env 修正直後に叩くため、
    # まだ出口が直っていなくても使えるようにする）
    if args.clear_log:
        conn = db.connect(args.db)
        db.init_db(conn)
        try:
            n = clear_egress_log(conn)
        finally:
            conn.close()
        print(f"出口IPの記録を {n} 件削除しました。"
              if n else "削除する記録はありませんでした。")
        return 0

    if args.verdict:
        res = check_egress(args.expect)
        print(f"verdict: {res['verdict']}")
        print(f"ok     : {res['ok']}")
        print(f"reason : {res['reason']}")
        return 0 if res["ok"] else 1

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
