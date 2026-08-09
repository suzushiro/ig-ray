"""
insta-ray / dev/ratelimit_check.py

v1 のバグ検証: 429 を食ったとき instaloader が内部で長時間 sleep して
自力リトライしてしまい、こちらの中止ロジックが発火しなかった。

ここでは instaloader の **本物の RateController / get_json のリトライループ** を
使って、FailFastRateController の例外が握り潰されずに上まで抜けるかを確かめる。
ネットワークは使わない（sleep と HTTP を差し替える）。

    python3 dev/ratelimit_check.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import ig_scraper as igs   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  -- ' + detail) if detail and not cond else ''}")


class FakeContext:
    """RateController が触る最低限。"""
    def __init__(self):
        self.logged = []
        self.is_logged_in = True

    def log(self, *a, **kw):
        self.logged.append(" ".join(str(x) for x in a))

    def error(self, *a, **kw):
        self.logged.append(" ".join(str(x) for x in a))


def test_exception_class():
    print("\n[1] RateLimitAbort のクラス設計")
    from instaloader.exceptions import (
        ConnectionException, TooManyRequestsException,
        AbortDownloadException, InstaloaderException,
    )

    e = igs.RateLimitAbort(666, "other")
    check("Exception のサブクラス", isinstance(e, Exception))
    check("ConnectionException では **ない**", not isinstance(e, ConnectionException),
          "get_json のリトライループに捕まってしまう")
    check("TooManyRequestsException では **ない**", not isinstance(e, TooManyRequestsException))
    check("AbortDownloadException では **ない**", not isinstance(e, AbortDownloadException),
          "test_login で握り潰される")
    check("InstaloaderException では **ない**", not isinstance(e, InstaloaderException))
    check("waittime を保持", e.waittime == 666)
    check("query_type を保持", e.query_type == "other")
    check("メッセージに秒数が出る", "666" in str(e), str(e))


def test_sleep_threshold():
    print("\n[2] sleep() の閾値判定")
    factory = igs.make_rate_controller(max_sleep=60)
    rc = factory(FakeContext())

    slept = []
    orig = time.sleep
    time.sleep = lambda s: slept.append(s)
    try:
        rc.sleep(0)
        rc.sleep(5)
        rc.sleep(60)          # 境界: 60 は許容（> 判定なので）
        check("閾値以下は通常sleep", slept == [0, 5, 60], f"got {slept}")

        raised = None
        try:
            rc.sleep(61)
        except igs.RateLimitAbort as e:
            raised = e
        check("閾値超過で RateLimitAbort", raised is not None)
        check("超過分は sleep しない", slept == [0, 5, 60], f"got {slept}")
        check("waittime が伝わる", raised and raised.waittime == 61)

        # 実ログの 666 秒
        try:
            rc.sleep(666)
            check("666秒で中止", False, "例外が上がらなかった")
        except igs.RateLimitAbort as e:
            check("666秒で中止", True)
            check("666 が記録される", e.waittime == 666)
    finally:
        time.sleep = orig


def test_handle_429_path():
    print("\n[3] handle_429 経由（本物の RateController ロジック）")
    factory = igs.make_rate_controller(max_sleep=60)
    ctx = FakeContext()
    rc = factory(ctx)

    orig = time.sleep
    time.sleep = lambda s: None
    try:
        # 実際の流れ: wait_before_query でクエリが記録され、その後 429 を食う。
        # handle_429 は記録済みタイムスタンプが無いと計算できないので順序が要る。
        raised = None
        for i in range(40):
            try:
                rc.wait_before_query("other")
                rc.handle_429("other")
            except igs.RateLimitAbort as e:
                raised = e
                break
        check("handle_429 連発で最終的に中止する", raised is not None,
              "40回叩いても閾値を超えなかった")
        if raised:
            check("query_type が 'other' として記録される",
                  raised.query_type == "other", f"got {raised.query_type}")
            check("待機要求が閾値超え", raised.waittime > 60, f"got {raised.waittime}")
    finally:
        time.sleep = orig


def test_escapes_get_json_retry_loop():
    """
    v1 の本丸。get_json() の
        except (ConnectionException, json.JSONDecodeError, RequestException)
    に捕まらずに抜けられるかを、本物の get_json で確かめる。
    """
    print("\n[4] get_json のリトライループを貫通するか（本物のループ使用）")

    import instaloader
    from instaloader.instaloadercontext import InstaloaderContext

    class Resp429:
        status_code = 429
        headers = {}
        text = "rate limited"
        url = "https://www.instagram.com/api/v1/users/web_profile_info/"
        reason = "Too Many Requests"  # _response_error() が参照
        is_redirect = False           # get_json が while resp.is_redirect で見る
        history = []

        def json(self):
            return {"message": "rate limited"}

    class FakeSession:
        def __init__(self):
            self.calls = 0
            self.headers = {}
            self.cookies = {}

        def get(self, *a, **kw):
            self.calls += 1
            return Resp429()

    L = instaloader.Instaloader(
        sleep=True, quiet=True,
        download_pictures=False, download_videos=False,
        download_video_thumbnails=False, save_metadata=False,
        max_connection_attempts=3,
        rate_controller=igs.make_rate_controller(max_sleep=60),
    )
    ctx: InstaloaderContext = L.context
    fake = FakeSession()
    ctx._session = fake

    orig = time.sleep
    time.sleep = lambda s: None
    try:
        outcome = None
        try:
            ctx.get_json("api/v1/users/web_profile_info/", params={"username": "dummy"})
            outcome = "returned"
        except igs.RateLimitAbort as e:
            outcome = "RateLimitAbort"
        except Exception as e:
            outcome = f"{type(e).__name__}"

        check("RateLimitAbort が呼び出し側まで抜ける", outcome == "RateLimitAbort",
              f"got {outcome} — リトライループに握り潰されている")
        check("無限リトライしない（試行回数が有限）", fake.calls <= 5,
              f"HTTP呼び出し {fake.calls} 回")
        print(f"       （HTTP試行回数: {fake.calls}）")
    finally:
        time.sleep = orig


def test_wait_before_query_path():
    """
    対照実験。

    handle_429 経由なら TooManyRequestsException でも実は抜けられる
    （handle_429 は `except KeyboardInterrupt:` の中で呼ばれるため）。
    差が出るのは **wait_before_query 経由** ——
    レートコントローラが先回りして「次のクエリまで N 秒待て」と判断する経路。
    こちらは get_json() の try: の内側なので、ConnectionException 系を上げると
    リトライループに捕まって ConnectionException に化ける。

    実運用では連続取得中にこの経路に入るので、ここを外すと
    scrape_log が "error" になって "ratelimited" と区別できなくなる。
    """
    print("\n[5] 対照実験: wait_before_query 経由（差が出るのはここ）")

    import instaloader
    from instaloader import RateController
    from instaloader.exceptions import TooManyRequestsException

    class NaiveController(RateController):
        """v1 で想定していた（が効かなかった）やり方。"""
        def sleep(self, secs):
            if secs > 60:
                raise TooManyRequestsException(f"待機 {secs:.0f}秒 — 中止します")
            super().sleep(secs)

    class Resp200:
        """429 は返さない。純粋に「先回りの長時間待機」だけを起こす。"""
        status_code = 200
        headers = {}
        text = "{}"
        reason = "OK"
        url = "https://www.instagram.com/api/v1/users/web_profile_info/"
        is_redirect = False
        history = []

        def json(self):
            return {"status": "ok", "data": {}}

    class FakeSession:
        def __init__(self):
            self.calls = 0
            self.headers = {}
            self.cookies = {}

        def get(self, *a, **kw):
            self.calls += 1
            return Resp200()

    def run(controller_factory):
        L = instaloader.Instaloader(
            sleep=True, quiet=True,
            download_pictures=False, download_videos=False,
            download_video_thumbnails=False, save_metadata=False,
            max_connection_attempts=3,
            rate_controller=controller_factory,
        )
        ctx = L.context
        fake = FakeSession()
        ctx._session = fake
        # 直近に大量クエリを投げた状態を作る → 先回りの長時間待機が発生する
        now = time.monotonic()
        ctx._rate_controller._query_timestamps['other'] = [now - i * 0.1 for i in range(200)]
        try:
            ctx.get_json("api/v1/users/web_profile_info/", params={"username": "dummy"})
            return "returned", fake.calls
        except igs.RateLimitAbort:
            return "RateLimitAbort", fake.calls
        except Exception as e:
            return type(e).__name__, fake.calls

    from instaloader.exceptions import ConnectionException

    class ConnErrController(RateController):
        """ConnectionException を上げた場合（= リトライループの捕捉対象）。"""
        def sleep(self, secs):
            if secs > 60:
                raise ConnectionException(f"待機 {secs:.0f}秒 — 中止します")
            super().sleep(secs)

    orig = time.sleep
    time.sleep = lambda s: None
    try:
        tmr, tmr_calls = run(lambda ctx: NaiveController(ctx))
        conn, conn_calls = run(lambda ctx: ConnErrController(ctx))
        fast, fast_calls = run(igs.make_rate_controller(max_sleep=60))

        print(f"       TooManyRequests版   -> {tmr}（HTTP {tmr_calls} 回)")
        print(f"       ConnectionException版 -> {conn}（HTTP {conn_calls} 回)")
        print(f"       FailFast(独自例外)版  -> {fast}（HTTP {fast_calls} 回)")

        # 実測に基づく事実のみを検証する
        check("FailFast版は RateLimitAbort でそのまま抜ける",
              fast == "RateLimitAbort", f"got {fast}")

        check("ConnectionException版は捕捉され ConnectionException に化ける",
              conn == "ConnectionException", f"got {conn}")

        # TMR版は「捕捉されるが handle_429 の sleep で再度上がる」ため結果的に抜ける。
        # 動くには動くが、instaloader 内部のハンドラ構造に依存している。
        check("TooManyRequests版も結果的には抜ける（ただし内部実装依存）",
              tmr == "TooManyRequestsException", f"got {tmr}")

        check("FailFast版はどの経路でも instaloader のハンドラに触れない",
              fast == "RateLimitAbort" and fast_calls == 0,
              f"got {fast} / HTTP {fast_calls}")
    finally:
        time.sleep = orig


def main():
    test_exception_class()
    test_sleep_threshold()
    test_handle_429_path()
    test_escapes_get_json_retry_loop()
    test_wait_before_query_path()

    print(f"\n{'='*50}")
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("すべて通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
