"""
ig-ray / dev/silence_check.py

**巡回が黙って止まることを検知できるか**の検証。

経緯:
  2026-10-05 から 10-29 までの24日間、401 でログイン検証に失敗し続けて
  巡回が1件も走っていなかった。ところが画面は
  「✓ 直近6時間の失敗なし」と出し続けていた。
  make_loader の失敗経路が scrape_log に何も書いていなかったため。

  その前に X-Ray でも同じことが12日起きている（出口IP判定での中止）。
  **原因を1つずつ塞いでも、想定していない死に方は必ず残る。**
  そこで原因に依存しない見張り（最後に取れた時刻だけを見る）を足した。

検証すること:
  1. ログイン失敗が scrape_log に残るか
  2. 失敗が続いたら再挑戦の間隔が延びるか（401は叩くほど制限が延びる）
  3. 復旧したら間隔が戻るか
  4. 止まっていることが**画面に出る**か（実レンダリング）
  5. 止まっているのに「失敗なし」と出さないか

    python3 dev/silence_check.py
"""

import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          f"{('  -- ' + detail) if detail and not cond else ''}")


def fresh(**env):
    """環境変数を入れ替えてモジュールを読み直す。"""
    for k in list(os.environ):
        if k.startswith(("IG_RAY_", "INSTA_RAY_")):
            del os.environ[k]
    os.environ.update({k: str(v) for k, v in env.items() if v is not None})
    for m in ("config", "db", "web", "check_egress", "ig_scraper"):
        sys.modules.pop(m, None)


def banner_classes(html):
    """
    描画された health バナーの class 属性だけを集める。

    **素の "health-error" を grep してはいけない。** _style.html の
    CSS 定義に一致して常に真になる（X-Ray で同じ罠を踏んでいる）。
    """
    return re.findall(r'<div class="(health[^"]*)"', html)


# --------------------------------------------------------------------------

def test_pseudo_accounts():
    print("\n[1] 擬似アカウント名の一本化")
    fresh()
    import db
    check("db に EGRESS_LOG_NAME がある", db.EGRESS_LOG_NAME == "__egress__",
          db.EGRESS_LOG_NAME)
    check("db に LOGIN_LOG_NAME がある", db.LOGIN_LOG_NAME == "__login__",
          db.LOGIN_LOG_NAME)
    check("PSEUDO_ACCOUNTS に両方入っている",
          set(db.PSEUDO_ACCOUNTS) == {"__egress__", "__login__"},
          str(db.PSEUDO_ACCOUNTS))
    # **これらは IG のユーザー名として文法上は有効**（`_` が使える）。
    # 「衝突しない」と決めつけず、登録を拒否して衝突を起こせなくする。
    import seed_accounts as sa
    for n in db.PSEUDO_ACCOUNTS:
        check(f"{n} は文法上は有効なユーザー名（だから予約が要る）",
              re.fullmatch(r"[A-Za-z0-9._]{1,30}", n) is not None, n)
        check(f"{n} は登録を拒否される", sa.normalize(n) is None, str(sa.normalize(n)))
        check(f"{n} は大文字でも拒否される",
              sa.normalize(n.upper()) is None, str(sa.normalize(n.upper())))
        check(f"{n} はURL形式でも拒否される",
              sa.normalize(f"https://www.instagram.com/{n}/") is None)
    check("普通の名前は通る", sa.normalize("minamina_flyover") == "minamina_flyover")
    check("前後に_が付くだけの名前は通る",
          sa.normalize("__someone__") == "__someone__")

    import check_egress as ce
    check("check_egress は db の定数を使う",
          ce.EGRESS_LOG_NAME is db.EGRESS_LOG_NAME or
          ce.EGRESS_LOG_NAME == db.EGRESS_LOG_NAME)
    import web
    check("web も db の定数を使う",
          web.EGRESS_LOG_NAME == db.EGRESS_LOG_NAME and
          web.LOGIN_LOG_NAME == db.LOGIN_LOG_NAME)
    check("login_error は OK_STATUSES ではない",
          "login_error" not in db.OK_STATUSES)


def test_backoff():
    print("\n[2] ログイン失敗が続いたら間隔を延ばす")
    fresh(IG_RAY_INTERVAL=21600, IG_RAY_LOGIN_BACKOFF_MAX=86400)
    import ig_scraper as igs

    check("1回目は通常間隔のまま",
          igs.login_retry_delay(1) == 21600, str(igs.login_retry_delay(1)))
    check("0回でも通常間隔", igs.login_retry_delay(0) == 21600)
    check("2回目は倍", igs.login_retry_delay(2) == 43200,
          str(igs.login_retry_delay(2)))
    check("3回目は4倍", igs.login_retry_delay(3) == 86400,
          str(igs.login_retry_delay(3)))
    check("上限で頭打ち", igs.login_retry_delay(10) == 86400,
          str(igs.login_retry_delay(10)))
    check("単調非減少",
          all(igs.login_retry_delay(i) <= igs.login_retry_delay(i + 1)
              for i in range(1, 12)))

    # 24日叩き続けた実績への回帰。既定なら96回ではなく大幅に減る
    fresh(IG_RAY_INTERVAL=21600)
    import ig_scraper as igs2
    total, tries = 0.0, 0
    while total < 24 * 86400:
        tries += 1
        total += igs2.login_retry_delay(tries)
    check("24日でも叩く回数は30回未満に収まる", tries < 30, f"{tries}回")


def test_consecutive_failures():
    print("\n[3] 連続失敗の数え方")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    fresh(IG_RAY_DB=path, IG_RAY_CACHE=tmp)
    import db
    c = db.connect(path)
    db.init_db(c)
    N = db.LOGIN_LOG_NAME

    check("記録が無ければ0", db.consecutive_failures(c, N, ("login_error",)) == 0)
    for _ in range(3):
        db.log_scrape(c, N, "login_error", 0, 0, "401")
    c.commit()
    check("3回続けば3", db.consecutive_failures(c, N, ("login_error",)) == 3,
          str(db.consecutive_failures(c, N, ("login_error",))))

    # 成功が1件入ったら連続は切れる
    db.log_scrape(c, N, "login_ok", 0, 0, "ok")
    c.commit()
    check("成功で連続が切れる",
          db.consecutive_failures(c, N, ("login_error",)) == 0,
          str(db.consecutive_failures(c, N, ("login_error",))))

    db.log_scrape(c, N, "login_error", 0, 0, "401")
    c.commit()
    check("切れたあとは1から数え直す",
          db.consecutive_failures(c, N, ("login_error",)) == 1)

    # 他のアカウントの行は数えない
    db.log_scrape(c, "someone", "error", 0, 0, "別件")
    c.commit()
    check("他のアカウントは混ざらない",
          db.consecutive_failures(c, N, ("login_error",)) == 1)
    c.close()


def test_last_success():
    print("\n[4] 最後に成功した時刻")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    fresh(IG_RAY_DB=path, IG_RAY_CACHE=tmp)
    import db
    c = db.connect(path)
    db.init_db(c)

    check("何も無ければ None", db.last_success_at(c) is None)

    db.log_scrape(c, "u1", "ok_degraded", 3, 1, None)
    c.commit()
    check("ok_degraded は成功として数える", db.last_success_at(c) is not None)

    # 擬似アカウントとバックフィルは成功に数えない
    c.execute("DELETE FROM scrape_log")
    db.log_scrape(c, db.LOGIN_LOG_NAME, "login_ok", 0, 0, None)
    db.log_scrape(c, db.EGRESS_LOG_NAME, "egress_warn", 0, 0, None)
    db.log_scrape(c, "u1", db.BACKFILL_DONE, 0, 0, None)
    c.commit()
    check("擬似アカウントとバックフィルは成功に数えない",
          db.last_success_at(c) is None, str(db.last_success_at(c)))

    # 失敗も数えない
    c.execute("DELETE FROM scrape_log")
    db.log_scrape(c, "u1", "ratelimited", 0, 0, "429")
    c.commit()
    check("失敗は成功に数えない", db.last_success_at(c) is None)
    c.close()


def _build(path, cache):
    import db
    c = db.connect(path)
    db.init_db(c)
    c.execute("INSERT INTO accounts (username, is_enabled, is_target) "
              "VALUES ('u1', 1, 1)")
    db.save_posts(c, [dict(
        shortcode="SC1", mediaid=None, owner_username="u1", caption="x",
        date_utc="2026-10-01T10:00:00+00:00", likes=1, comments=0,
        typename="GraphImage", is_video=0, is_carousel=0, location=None,
        hashtags_json="[]",
        media_json=json.dumps([{"index": 0, "is_video": False,
                                "image_url": "https://cdn.example/0.jpg",
                                "video_url": None}]),
        local_media_json=None)])
    c.commit()
    return c


def test_render():
    print("\n[5] 実レンダリング")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    cache = os.path.join(tmp, "cache")
    os.makedirs(cache, exist_ok=True)
    fresh(IG_RAY_DB=path, IG_RAY_CACHE=cache, IG_RAY_INTERVAL=21600)
    import db
    c = _build(path, cache)
    import web
    web.app.config["TESTING"] = True

    def get(p="/"):
        web._target_cache.update({"at": 0.0, "set": None})
        web._muted_cache.update({"at": 0.0, "set": frozenset()})
        with web.app.test_client() as cli:
            return cli.get(p).get_data(as_text=True)

    # --- 直近に成功している → 何も出ない ---
    db.log_scrape(c, "u1", "ok_degraded", 3, 1, None)
    c.commit()
    html = get()
    check("正常なら停止バナーは出ない", "巡回が止まっています" not in html)
    check("正常なら「失敗なし」が出る", "直近6時間の失敗なし" in html)
    check("正常なら赤バナーは無い",
          not any("health-error" in x for x in banner_classes(html)),
          str(banner_classes(html)))

    # --- 24日止まっている（実際に起きた状況の再現） ---
    c.execute("UPDATE scrape_log SET ended_at = datetime('now','-24 days')")
    c.commit()
    html = get()
    check("止まっていれば停止バナーが出る", "巡回が止まっています" in html)
    check("何日前かが出る", "24.0日前" in html or "24日前" in html,
          [l for l in html.splitlines() if "日前" in l][:1])
    check("赤バナーになる",
          any("health-error" in x for x in banner_classes(html)),
          str(banner_classes(html)))
    check("CSS定義ではなく要素に付いている",
          'class="health health-error"' in html)
    check("ログの見方を案内する", "docker compose logs" in html)
    check("**「失敗なし」と出さない**",
          "直近6時間の失敗なし" not in html,
          "失敗が無いのではなく、失敗が記録される手前で死んでいる")

    # --- 1周落としただけでは鳴らない ---
    c.execute("UPDATE scrape_log SET ended_at = datetime('now','-7 hours')")
    c.commit()
    check("1周ぶん（7時間）では鳴らない",
          "巡回が止まっています" not in get())
    c.execute("UPDATE scrape_log SET ended_at = datetime('now','-19 hours')")
    c.commit()
    check("3周ぶん（19時間）を超えたら鳴る",
          "巡回が止まっています" in get())

    # --- ログイン失敗バナー ---
    c.execute("DELETE FROM scrape_log")
    db.log_scrape(c, "u1", "ok_degraded", 3, 1, None)
    MSG = ("ログイン検証に失敗しました。セッションファイルは読めましたが、"
           "サーバがログインを認めていません")
    db.log_scrape(c, db.LOGIN_LOG_NAME, "login_error", 0, 0, MSG)
    c.commit()
    html = get()
    check("ログイン失敗のバナーが出る", "ログインできていません" in html)
    check("理由がそのまま出る", MSG in html)
    check("擬似アカウント名が生で出ない", "__login__" not in html)
    check("失敗一覧に混ざらない", "login_error" not in html.split("</header>")[0])
    check("ログイン失敗のときも「失敗なし」と出さない",
          "直近6時間の失敗なし" not in html)

    # 復旧したらバナーは消える
    db.log_scrape(c, db.LOGIN_LOG_NAME, "login_ok", 0, 0, "ログインできました")
    c.commit()
    html = get()
    check("復旧したらログインバナーは消える",
          "ログインできていません" not in html)

    # --- /backup にも擬似アカウントを出さない ---
    db.log_scrape(c, db.LOGIN_LOG_NAME, "login_error", 0, 0, MSG)
    c.commit()
    bk = get("/backup")
    check("backup に擬似アカウント名が出ない",
          "__login__" not in bk and "__egress__" not in bk)
    check("backup の集計に login_error が混ざらない", "login_error" not in bk)
    c.close()


def test_last_update_is_honest():
    print("\n[6] 「最終更新」が嘘をつかない")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    cache = os.path.join(tmp, "cache")
    os.makedirs(cache, exist_ok=True)
    fresh(IG_RAY_DB=path, IG_RAY_CACHE=cache, IG_RAY_INTERVAL=21600)
    import db
    c = _build(path, cache)
    import web
    web.app.config["TESTING"] = True

    def meta():
        web._target_cache.update({"at": 0.0, "set": None})
        web._muted_cache.update({"at": 0.0, "set": frozenset()})
        with web.app.test_client() as cli:
            h = cli.get("/").get_data(as_text=True)
        m = re.search(r"最終更新: ([^<\n]+)", h)
        return (m.group(1).strip() if m else None), h

    db.log_scrape(c, "u1", "ok_degraded", 6, 2, None)
    c.execute("UPDATE scrape_log SET ended_at = datetime('now','-24 days')")
    c.commit()
    before, _ = meta()
    check("成功した時刻が出る", before is not None, str(before))

    # **擬似アカウントの行で時刻が動いてはいけない。**
    # 動くと「1件も取れていないのに、たった今更新された」と表示され、
    # 24日の沈黙に気づく唯一の手掛かりが潰れる。
    db.log_scrape(c, db.LOGIN_LOG_NAME, "login_error", 0, 0, "401")
    db.log_scrape(c, db.EGRESS_LOG_NAME, "egress_warn", 0, 0, "IP変更")
    c.commit()
    after, h = meta()
    check("ログイン失敗の記録で時刻が動かない", after == before,
          f"{before} -> {after}")
    check("出口IPの記録でも動かない", after == before, f"{before} -> {after}")
    check("同時に止まっているバナーは出ている", "巡回が止まっています" in h)

    # 失敗の記録でも動かない
    db.log_scrape(c, "u1", "ratelimited", 0, 0, "429")
    c.commit()
    check("失敗の記録でも動かない", meta()[0] == before)

    # 成功したら動く
    db.log_scrape(c, "u1", "ok_degraded", 6, 2, None)
    c.commit()
    check("成功したら動く", meta()[0] != before, str(meta()[0]))
    c.close()


def test_backoff_actually_skips():
    print("\n[7] バックオフが実際に効く（表示だけ延ばさない）")
    # cron のシェルは sleep $INTERVAL 固定なので、次回予定を延ばすだけでは
    # 実際の間隔は延びない。起動直後に予定を見て抜ける必要がある。
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    fresh(IG_RAY_DB=path, IG_RAY_CACHE=os.path.join(tmp, "cache"),
          IG_RAY_INTERVAL=21600)
    import db
    import ig_scraper as igs
    c = db.connect(path)
    db.init_db(c)
    c.execute("INSERT INTO accounts (username, is_enabled, is_target) "
              "VALUES ('u1', 1, 1)")
    c.commit()
    c.close()

    hits = {"n": 0}

    def fail(*a, **k):
        hits["n"] += 1
        return (None, "ログイン検証に失敗しました\n401 Please wait a few minutes")
    igs.make_loader = fail

    def wake(argv=("s", "--cron", "--skip-egress-check")):
        old = sys.argv
        sys.argv = list(argv)
        try:
            return igs.main()
        finally:
            sys.argv = old

    rc1 = wake()
    check("1回目は実際に試す", hits["n"] == 1 and rc1 == 2, f"{hits['n']} rc={rc1}")
    rc2 = wake()
    check("2回目も試す（偶発的な失敗で止めない）",
          hits["n"] == 2 and rc2 == 2, f"{hits['n']} rc={rc2}")

    before = hits["n"]
    rcs = [wake() for _ in range(8)]
    check("以降は Instagram を叩かない", hits["n"] == before,
          f"{before} -> {hits['n']}")
    check("待機時は 0 で抜ける（cron のエラー表示を汚さない）",
          set(rcs) == {0}, str(set(rcs)))

    # 待機中でも記録は増やさない（同じ理由の行で埋めない）
    c = db.connect(path)
    n_rows = c.execute(
        "SELECT COUNT(*) n FROM scrape_log WHERE username = ?",
        (db.LOGIN_LOG_NAME,)).fetchone()["n"]
    check("待機中はログも増やさない", n_rows == 2, str(n_rows))

    # 予定時刻を過ぎたら再開する
    c.execute("DELETE FROM meta WHERE key = 'next_run:scrape'")
    c.commit()
    c.close()
    wake()
    check("予定を過ぎたら再開する", hits["n"] == before + 1,
          f"{before} -> {hits['n']}")

    # 手動実行（--cron なし）はバックオフを無視する
    before = hits["n"]
    wake(("s", "--skip-egress-check", "u1"))
    check("手動実行はバックオフを無視する", hits["n"] == before + 1,
          f"{before} -> {hits['n']}")


def test_scraper_wiring():
    print("\n[6] スクレイパーへの組み込み")
    src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "ig_scraper.py"), encoding="utf-8").read()
    i = src.find("if loader is None:")
    j = src.find("targets = args.usernames")
    block = src[i:j]
    check("ログインに失敗したら記録する",
          "_record_login_failure(conn" in block, block[:200])
    # 記録とバックオフは _record_login_failure にまとめた（巡回中の
    # セッション起因の失敗とバックフィルからも同じ処理を使うため）
    k = src.find("def _record_login_failure")
    rec = src[k:src.find("\ndef ", k + 10)]
    check("ログイン失敗を __login__ に記録する",
          "db.LOGIN_LOG_NAME" in rec and '"login_error"' in rec, rec[:200])
    check("連続回数を数える", "consecutive_failures" in rec)
    check("間隔を延ばす", "login_retry_delay" in rec)
    # 連続を切るのは「実際に1件取れたとき」。ログインできただけでは切らない
    # （v4.19 から検証は通信しないので、ログイン成功＝取得成功ではない）
    loop = src[j:]
    check("取得できたら連続を切る", '"login_ok"' in loop and
          'result["status"] in ("ok", "ok_degraded")' in loop)

    # 長文のエラーを画面にそのまま流さない
    fresh()
    import ig_scraper as igs
    long_err = "ログイン検証に失敗しました\n\n" + ("対処手順。" * 200)
    out = igs._login_error_summary(long_err)
    check("要約は300文字程度に収める", len(out) <= 301, str(len(out)))
    check("改行を潰す", "\n" not in out)
    check("先頭は残る", out.startswith("ログイン検証に失敗しました"))
    check("空でも落ちない", igs._login_error_summary(None))


def test_compose_passthrough():
    """
    **config.env() で読む名前が compose に無ければ、.env に書いても届かない。**

    compose は `${IG_RAY_*}` を明示的に参照した分しかコンテナに渡さない。
    追加した設定を compose に書き忘れると、既定値のまま黙って動き続ける
    （v4.3 まで IG_RAY_EXPECT_EGRESS_IP がこれで届いていなかった）。
    """
    print("\n[8] compose への受け渡し")
    root = os.path.join(os.path.dirname(__file__), "..")
    compose = open(os.path.join(root, "docker-compose.yml"),
                   encoding="utf-8").read()

    # どのファイルがどのサービスで動くか
    targets = {
        "ig_scraper.py": "worker", "backfill.py": "worker",
        "check_egress.py": "worker", "cache_utils.py": "both",
        "web.py": "web", "config.py": None, "seed_accounts.py": "worker",
        "tumblr_client.py": "web",
    }
    missing = []
    for fname, where in targets.items():
        if where is None:
            continue
        path = os.path.join(root, "app", fname)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        for name in re.findall(r"config\.env(?:_float|_int|_bool)?\(\s*[\"']"
                               r"([A-Z0-9_]+)[\"']", src):
            # 接頭辞つき・接頭辞なしのどちらかで渡っていればよい。
            # tumblr_client は os.environ.get("TUMBLR_...") を先に見るので
            # compose 側は接頭辞なしで渡している。
            if f"IG_RAY_{name}" not in compose and f"\n      {name}:" not in compose:
                missing.append(f"{fname}: IG_RAY_{name}")
    check("config.env で読む名前はすべて compose にある",
          not missing, str(sorted(set(missing))))

    # ツール系サービスは entrypoint 方式であること。
    # command: で書くと `docker compose run --rm <svc> --flag` が
    # コマンドごと置き換わり、tini が --flag を実行しようとして落ちる。
    import re as _re
    blocks = _re.split(r"\n  (?=[a-z][a-z0-9-]*:\n)", compose)
    no_ep = []
    for b in blocks:
        m = _re.match(r"\s*([a-z][a-z0-9-]*):", b)
        if not m or 'profiles: ["tools"]' not in b:
            continue
        if "entrypoint:" not in b:
            no_ep.append(m.group(1))
    check("tools のサービスは全部 entrypoint 方式", not no_ep, str(no_ep))

    # web が読む名前は web サービスの environment に入っているか
    i = compose.find("\n  web:")
    web_block = compose[i:] if i != -1 else ""
    for name in ("IG_RAY_INTERVAL", "IG_RAY_STALE_INTERVALS"):
        check(f"web サービスに {name} がある", name in web_block)
    # worker が読む名前は共通ブロックに入っているか
    j = compose.find("x-worker-base:")
    worker_block = compose[j:i] if j != -1 and i != -1 else ""
    check("worker に IG_RAY_LOGIN_BACKOFF_MAX がある",
          "IG_RAY_LOGIN_BACKOFF_MAX" in worker_block)

    # web と worker で巡回間隔がズレないこと（既定値が同じか）
    defaults = set(re.findall(r"IG_RAY_INTERVAL:-(\d+)", compose))
    check("INTERVAL の既定値が全サービスで揃っている",
          len(defaults) == 1, str(defaults))


def main():
    test_pseudo_accounts()
    test_backoff()
    test_consecutive_failures()
    test_last_success()
    test_render()
    test_last_update_is_honest()
    test_backoff_actually_skips()
    test_scraper_wiring()
    test_compose_passthrough()

    print("\n" + "=" * 50)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    for n in FAIL:
        print(f"  - {n}")
    if not FAIL:
        print("すべて通過")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
