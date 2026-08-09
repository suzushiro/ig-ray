"""
insta-ray / ig_scraper.py

instaloader をセッションファイル方式で使い、指定ユーザーの直近N投稿を
posts テーブルへ保存し、メディアを /data/cache に落とす。

方針:
  - パスワードはコードにも env にも置かない。
    事前に手元で `instaloader -l <username>` を実行してセッションを作り、
    生成物を /data/sessions/session-<username> に置く。
  - セッションが無ければ手順を案内して安全終了（クラッシュさせない）。
  - レート制限に素直に従う（sleep=True + バッチ間の明示 sleep）。

構造:
  extract_media(post)   -> 純粋関数。Post からメディア記述の list[dict]
  post_to_record(post)  -> 純粋関数。Post から posts テーブル1行の dict
  この2つはネットワーク不要 = モックでテストできる。
"""

import argparse
import hashlib
import json
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone

import config
import db

# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------

SESSION_DIR = config.env("SESSIONS", "/data/sessions")
CACHE_DIR = config.env("CACHE", "/data/cache")
LOGIN_USER = config.env("LOGIN_USER", "")

# 動画本体は容量を食うので既定OFF（サムネは常に取る）
DOWNLOAD_VIDEOS = config.env_bool("DOWNLOAD_VIDEOS", False)

# バッチ間の休憩（秒）。凍結対策の主役はこれ。
SLEEP_MIN = config.env_float("SLEEP_MIN", 3)
SLEEP_MAX = config.env_float("SLEEP_MAX", 8)

# これを超える待機を要求されたら中止する（秒）
MAX_SLEEP = config.env_float("MAX_SLEEP", 60)

# 1 にすると Profile.from_username() を最初から呼ばない。
# web_profile_info が恒常的に 429 で弾かれる環境向け（実機で遭遇）。
# プロフィール詳細は取れなくなるが、投稿取得は graphql 経路なので通る。
POSTS_ONLY = config.env_bool("POSTS_ONLY", False)

# ジオタグを取るか。ログイン時の投稿データに location は含まれないため、
# 参照すると instaloader が投稿ごとに追加の HTTP リクエストを飛ばす。
# レート制限に直結するので既定 OFF。
FETCH_LOCATION = config.env_bool("FETCH_LOCATION", False)

# 動画URLを instaloader の Post.video_url プロパティ経由で取るか。
# **既定 OFF。** プロパティは iphone_struct.video_versions の各解像度に対して
# CDN へ HEAD を投げ、Content-Length が最大のものを選ぶ実装になっている
# （instaloader 4.15.3 structures.py）。つまり**動画投稿1件につき追加で
# 2〜4リクエスト**発生する。location と同じ「プロパティが勝手に外へ出る」型。
# 生ノードの video_url（fake_node に既に入っている）を読めば追加コストゼロ。
# 最高画質を厳密に選びたい場合だけ 1 にする（DOWNLOAD_VIDEOS=1 のときのみ意味がある）。
VIDEO_URL_PROBE = config.env_bool("VIDEO_URL_PROBE", False)

# IPが弾かれている場合は別経路のプロキシを噛ませる
# 例: IG_RAY_PROXY=http://user:pass@host:port
PROXY = config.env("PROXY", "")

# 定期巡回の間隔（秒）。compose の cron サービスが同じ値で sleep する。
# --cron 付きで呼ばれたとき、次回予定時刻の算出に使う。
CRON_INTERVAL = config.env_float("INTERVAL", 21600)


def _nap():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


# --------------------------------------------------------------------------
# レート制限 fail-fast
# --------------------------------------------------------------------------

class RateLimitAbort(Exception):
    """
    長時間待機を要求されたので中止する、という合図。

    なぜ instaloader の例外を使わないか:
      - TooManyRequestsException は ConnectionException のサブクラスで、
        get_json() の `except (ConnectionException, ...)` に捕まってリトライされる。
        wait_before_query() はその try: の内側から呼ばれるので握り潰される。
      - AbortDownloadException は test_login() の
        `except (AbortDownloadException, ConnectionException)` で握り潰される。
      - instaloader には broad な `except Exception` が存在しないので、
        Exception 直下の独自クラスなら確実に呼び出し側まで抜ける。
    """

    def __init__(self, waittime, query_type=None):
        self.waittime = waittime
        self.query_type = query_type
        super().__init__(
            f"レート制限: {waittime:.0f}秒の待機を要求されたため中止しました"
            + (f"（query_type={query_type}）" if query_type else "")
        )


def make_rate_controller(max_sleep=None):
    """
    Instaloader(rate_controller=...) に渡すファクトリを返す。

    instaloader は 429 を食うと handle_429() → sleep(666) のように
    長時間眠って自力リトライする。放置すると戻ってこないうえ、
    凍結リスクも上がるので、閾値を超える待機要求で例外を上げて即中止する。

    wait_before_query() / handle_429() のどちらも最終的に sleep() を呼ぶので、
    sleep() だけを差し替えれば両方の経路を押さえられる。
    """
    from instaloader import RateController

    limit = MAX_SLEEP if max_sleep is None else float(max_sleep)

    class FailFastRateController(RateController):
        def __init__(self, context):
            super().__init__(context)
            self._last_query_type = None

        def handle_429(self, query_type):
            # sleep() 側にどのクエリで詰まったか伝えるためだけに記録
            self._last_query_type = query_type
            super().handle_429(query_type)

        def sleep(self, secs):
            if secs > limit:
                raise RateLimitAbort(secs, self._last_query_type)
            super().sleep(secs)

    return lambda ctx: FailFastRateController(ctx)


# --------------------------------------------------------------------------
# 純粋関数（テスト対象）
# --------------------------------------------------------------------------

def extract_media(post):
    """
    Post -> [{index, is_video, image_url, video_url}, ...]

    - カルーセル（GraphSidecar）: get_sidecar_nodes() を展開（最大20枚）
    - 単体画像/動画: post.url / post.video_url
    sidecar ノードは namedtuple (is_video, display_url, video_url)。
    """
    out = []
    typename = _safe(lambda: post.typename)

    if typename == "GraphSidecar":
        nodes = _safe(lambda: list(post.get_sidecar_nodes()), []) or []
        for i, node in enumerate(nodes):
            is_video = bool(_safe(lambda: node.is_video, False))
            out.append({
                "index": i,
                "is_video": is_video,
                "image_url": _safe(lambda: node.display_url),
                # sidecar ノードは namedtuple なので追加リクエストは起きない
                "video_url": _safe(lambda: node.video_url) if is_video else None,
            })
        if out:
            return out
        # カルーセルなのに子ノードが取れなかった場合は単体として扱う

    is_video = bool(_safe(lambda: post.is_video, False))
    out.append({
        "index": 0,
        "is_video": is_video,
        "image_url": _safe(lambda: post.url),
        "video_url": video_url_of(post) if is_video else None,
    })
    return out


def video_url_of(post, probe=None):
    """
    単体動画（GraphVideo）の動画URLを **追加リクエストなしで** 取る。

    instaloader の `Post.video_url` を既定で使わない理由:
      候補が2つ以上あると Content-Length を比べるために
      `context.head()` を候補ごとに投げる（= 動画1件で2〜4リクエスト）。
      候補は `_field('video_url')` と `iphone_struct['video_versions']` の
      全解像度で、実データではまず複数になる。
      location と同じ「プロパティを触ると外へ出る」型の罠。

    生ノードの `video_url` は `Post.from_iphone_struct` が
    `video_versions[-1]['url']` から既に埋めているので、そこを読めば足りる。

    probe=True のときだけ本来のプロパティを使う（最高画質を厳密に選ぶ）。
    DOWNLOAD_VIDEOS=1 で画質にこだわる場合以外は不要。
    """
    use_probe = VIDEO_URL_PROBE if probe is None else probe

    node = _safe(lambda: post._node)
    if isinstance(node, dict):
        url = node.get("video_url")
        if url:
            return url
        versions = _safe(lambda: node["iphone_struct"]["video_versions"])
        if isinstance(versions, list) and versions:
            url = _safe(lambda: versions[-1]["url"])
            if url:
                return url

    # 生ノードが辞書として読めない = iphone_struct 由来ではない（旧graphql形式や
    # テスト用モック）。この場合プロパティを触っても候補は1つで HEAD は飛ばないので、
    # 素直にプロパティへ委ねる。
    if node is None or use_probe:
        return _safe(lambda: post.video_url)

    # 生ノードはあるが video_url が無いケース。ここでプロパティを触ると
    # _full_metadata の取得（追加HTTP）に落ちるので諦める。
    # DOWNLOAD_VIDEOS=0 の既定では動画URLは使わないので実害は無い。
    return None


def _safe(fn, default=None):
    """
    instaloader のプロパティは内部で辞書を引くので、
    フィールドが欠けていると AttributeError ではなく KeyError / TypeError が飛ぶ。
    getattr(obj, name, default) では拾えないので、こちらを使う。
    """
    try:
        return fn()
    except (KeyError, TypeError, IndexError, AttributeError):
        return default


def location_name(post, fetch_property=False):
    """
    ジオタグの地名を生ノードから直接読む。

    instaloader の `Post.location` を使わない理由:
      - `int(loc['id'])` を要求するが、iphone_struct の location は
        `pk` を持ち `id` を持たない → KeyError: 'id'
        （実データで確認: キーは lat/lng/name/pk/profile_pic_url）
      - `_field("location")` は fake_node に無いと `_full_metadata` を
        取りに行くので、投稿ごとに追加の HTTP リクエストが飛ぶ

    生ノードの iphone_struct.location.name を読めば両方回避できる。
    追加リクエストなし・例外なし。
    """
    node = _safe(lambda: post._node)
    if isinstance(node, dict):
        for loc in (_safe(lambda: node["iphone_struct"]["location"]),
                    _safe(lambda: node["location"])):
            if isinstance(loc, dict):
                name = loc.get("name")
                if name:
                    return name

    # 生ノードに無ければ諦める。プロパティ経由の取得は
    # 追加リクエストを伴うので、明示的に有効化されたときだけ。
    if fetch_property:
        loc = _safe(lambda: post.location)
        return _safe(lambda: loc.name) if loc is not None else None
    return None


def post_to_record(post, with_location=None):
    """
    Post -> posts テーブル1行分の dict。

    with_location:
      生ノードから読める地名は常に拾う（追加コストなし）。
      このフラグは「生ノードに無いときにプロパティ経由で取りに行くか」の制御で、
      有効にすると投稿ごとに追加の HTTP リクエストが発生する。
    """
    fetch_property = FETCH_LOCATION if with_location is None else with_location

    typename = _safe(lambda: post.typename)
    media = extract_media(post)
    loc_name = location_name(post, fetch_property=fetch_property)
    hashtags = _safe(lambda: sorted(set(post.caption_hashtags or [])), []) or []

    date_utc = _safe(lambda: post.date_utc)
    if isinstance(date_utc, datetime):
        if date_utc.tzinfo is None:
            date_utc = date_utc.replace(tzinfo=timezone.utc)
        date_str = date_utc.astimezone(timezone.utc).isoformat()
    else:
        date_str = str(date_utc)

    return {
        "shortcode": post.shortcode,
        "mediaid": _safe(lambda: post.mediaid),
        "owner_username": _safe(lambda: post.owner_username),
        "caption": _safe(lambda: post.caption),
        "date_utc": date_str,
        "likes": _safe(lambda: post.likes),
        "comments": _safe(lambda: post.comments),
        "typename": typename,
        "is_video": 1 if _safe(lambda: post.is_video, False) else 0,
        "is_carousel": 1 if typename == "GraphSidecar" else 0,
        "location": loc_name,
        "hashtags_json": json.dumps(hashtags, ensure_ascii=False),
        "media_json": json.dumps(media, ensure_ascii=False),
    }


def profile_to_row(profile):
    return {
        "username": profile.username,
        "userid": getattr(profile, "userid", None),
        "full_name": getattr(profile, "full_name", None),
        "biography": getattr(profile, "biography", None),
        "profile_pic_url": getattr(profile, "profile_pic_url", None),
        "followers": getattr(profile, "followers", None),
        "mediacount": getattr(profile, "mediacount", None),
    }


def clear_rate_penalty(context, query_type=None):
    """
    429 で入ったグローバルな待機ペナルティを解除する。

    なぜ必要か:
      RateController.query_waittime() の untracked_next_request_time() は
      query_type に関係なく `_earliest_next_request_time` を返す。
      つまり `other`（web_profile_info）で 429 を食うと、その 666 秒が
      graphql を含む**全クエリ種別**に波及する。

      web_profile_info だけが恒常的に弾かれている環境では、
      叩けないエンドポイントのペナルティを、通るエンドポイントが
      背負い続けることになるので、切り替え時に一度だけ解除する。

    注意:
      レート制限保護を意図的に外す操作なので、フォールバック時に一度だけ呼ぶ。
      切り替え先の graphql 経路は自前でタイムスタンプを記録するため、
      そちらの保護は生きたまま。
      そもそも 429 を食わないようにするのが本筋なので、恒常的に弾かれる環境では
      IG_RAY_POSTS_ONLY=1 にしてこの経路自体を通らないようにするのが望ましい。
    """
    rc = getattr(context, "_rate_controller", None)
    if rc is None:
        return
    rc._earliest_next_request_time = 0.0
    rc._iphone_earliest_next_request_time = 0.0
    if query_type is not None:
        rc._query_timestamps.pop(query_type, None)
    else:
        rc._query_timestamps.clear()


def owner_from_node(post):
    """
    投稿の生ノードからオーナー情報を取り出す。**追加リクエストなし。**

    ログイン時の投稿には iphone_struct.user が丸ごと入っており、
    instaloader 自身も Profile.from_iphone_struct() でここから
    username / full_name / profile_pic_url を組み立てている。
    つまり web_profile_info が 429 でもアイコンと表示名は取れる。

    Profile.profile_pic_url プロパティを使わないのは、ログイン時に
    `_iphone_struct['hd_profile_pic_url_info']` を先に見に行き、
    無いと context.error() でノイズを出すため。辞書を直接読む。
    """
    node = _safe(lambda: post._node)
    if not isinstance(node, dict):
        return None
    iph = node.get("iphone_struct")
    if not isinstance(iph, dict):
        return None
    user = iph.get("user")
    if not isinstance(user, dict):
        return None

    username = user.get("username")
    if not username:
        return None

    # HD版があればそちらを優先
    pic = _safe(lambda: user["hd_profile_pic_url_info"]["url"]) or \
          user.get("profile_pic_url")

    return {
        "username": str(username).lower(),
        "userid": user.get("pk"),
        "full_name": user.get("full_name") or None,
        "profile_pic_url": pic or None,
    }


AVATAR_DIR_NAME = "_avatars"


def avatar_path(username):
    d = os.path.join(CACHE_DIR, AVATAR_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{username}.jpg")


def save_avatar(username, url, session_obj=None, max_age_days=7):
    """
    アイコンをキャッシュに保存してローカルパスを返す。

    プロフィール画像のURLは署名付きで期限切れになるため、
    DBにURLだけ持っていても後で表示できない。実体を落としておく。
    アイコンは変わるので max_age_days を過ぎたら取り直す。
    """
    if not url:
        return None
    path = avatar_path(username)

    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < max_age_days * 86400:
            return path

    sess = session_obj if session_obj is not None else media_session()
    try:
        r = sess.get(url, timeout=30)
        if r.status_code != 200:
            return path if os.path.exists(path) else None
        content = r.content
    except Exception as e:
        print(f"  [avatar] 取得失敗 {username}: {e}", file=sys.stderr)
        return path if os.path.exists(path) else None

    try:
        with open(path, "wb") as f:
            f.write(content)
    except OSError as e:
        print(f"  [avatar] 保存失敗 {username}: {e}", file=sys.stderr)
        return None
    return path


def minimal_profile(context, username):
    """
    プロフィール詳細を取らずに投稿だけ取るための最小 Profile。

    背景:
      `Profile.from_username()` は `api/v1/users/web_profile_info/` 一択で、
      このエンドポイントだけが 429 で弾かれることがある（実機で遭遇）。
      一方 `get_posts()` はログイン時に doc_id ベースの graphql POST を使い、
      必要な変数は username だけ。userid も詳細メタデータも要らない。

      そこで `_has_full_metadata = True` を立てて `_obtain_metadata()` を
      短絡させ、投稿取得だけ通す。

    制約:
      followers / full_name / biography などのプロフィール項目は取れない
      （アクセスすると KeyError）。accounts には username だけ入れること。
    """
    import instaloader

    # __init__ が id の存在を要求するのでダミーを入れる。
    # _has_full_metadata=True により再取得は走らないので、この 0 は
    # そのまま残る。だから userid を DB に書いてはいけない。
    p = instaloader.Profile(context, {"username": username.lower(), "id": 0})
    p._has_full_metadata = True
    return p


# --------------------------------------------------------------------------
# メディア保存
# --------------------------------------------------------------------------

def _cache_path(shortcode, index, ext):
    # shortcode の頭2文字でシャーディング（1ディレクトリ肥大化回避）
    sub = shortcode[:2]
    d = os.path.join(CACHE_DIR, sub)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{shortcode}_{index}{ext}")


def media_session(loader=None):
    """
    CDN からメディアを落とすためのセッションを返す。

    **ログイン済みセッションを使い回してはいけない。**
    instaloader のセッションには `Host: www.instagram.com` が固定で入っており、
    それを scontent-*.cdninstagram.com に送ると host 不一致で **404** になる
    （実機で全件失敗して判明）。`Origin` / `X-Instagram-AJAX` /
    `X-Requested-With` も CDN には不要。

    instaloader 自身も get_raw() で匿名セッションを使っている。
    同じものが取れるならそれを使い、無ければ素の Session で代替する。
    """
    import requests

    ctx = getattr(loader, "context", None)
    if ctx is not None:
        sess = _safe(lambda: ctx.get_anonymous_session())
        if sess is not None:
            return sess

    sess = requests.Session()
    sess.headers.update({
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.8",
        "User-Agent": _safe(lambda: ctx.user_agent) or
                      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
    })
    return sess


def download_media(conn, shortcode, media_list, session_obj=None):
    """
    media_list の各要素を CACHE_DIR に保存し、media_index に記録する。
    session_obj は requests.Session 互換。**匿名セッションを渡すこと**
    （media_session() を使う）。
    """
    import requests

    sess = session_obj if session_obj is not None else media_session()
    saved = 0

    for m in media_list:
        url = m["video_url"] if (m["is_video"] and DOWNLOAD_VIDEOS) else m["image_url"]
        if not url:
            continue

        ext = ".mp4" if (m["is_video"] and DOWNLOAD_VIDEOS) else ".jpg"
        path = _cache_path(shortcode, m["index"], ext)

        if os.path.exists(path):
            db.record_media(conn, shortcode, m["index"], m["is_video"], url,
                            local_path=path, size_bytes=os.path.getsize(path))
            continue

        try:
            r = sess.get(url, timeout=60)
            # メディアCDNは instaloader のレートコントローラを通らないので自前で見る
            if r.status_code == 429:
                raise RateLimitAbort(0, "media")
            r.raise_for_status()
            content = r.content
        except RateLimitAbort:
            raise
        except Exception as e:
            print(f"  [media] 取得失敗 {shortcode}#{m['index']}: {e}", file=sys.stderr)
            db.record_media(conn, shortcode, m["index"], m["is_video"], url)
            continue

        sha = hashlib.sha256(content).hexdigest()

        # 既に同一実体があれば書かずに使い回す
        existing = db.find_media_by_sha(conn, sha)
        if existing and os.path.exists(existing):
            db.record_media(conn, shortcode, m["index"], m["is_video"], url,
                            local_path=existing, sha256=sha,
                            size_bytes=os.path.getsize(existing))
            continue

        with open(path, "wb") as f:
            f.write(content)
        db.record_media(conn, shortcode, m["index"], m["is_video"], url,
                        local_path=path, sha256=sha, size_bytes=len(content))
        saved += 1

    return saved


# --------------------------------------------------------------------------
# ログイン
# --------------------------------------------------------------------------

SESSION_HELP = """
セッションファイルが見つかりません。

  1) 捨てアカウントで、使い捨てコンテナから対話ログイン:

        mkdir -p ~/insta-ray/data/sessions
        docker run --rm -it \\
          -v ~/insta-ray/data/sessions:/sessions \\
          python:3.12-slim \\
          bash -c 'pip install -q instaloader==4.15.3 && \\
                   instaloader -l "$0" && \\
                   cp /root/.config/instaloader/session-* /sessions/' \\
          IGアカウント名

     （母艦のPythonに直接入れると PEP 668 で弾かれる。
       山カッコは使わず、IGアカウント名は実際の値に置き換える）

  2) 所有権を直す:
        sudo chown $USER:$USER {session_dir}/session-*
        chmod 600 {session_dir}/session-*

  3) 環境変数:
        IG_RAY_LOGIN_USER=IGアカウント名

セッションは無期限ではありません。落ちたら作り直してください。
"""

SOFTBLOCK_HELP = """
セッションファイルは読めましたが、サーバがログインを認めていません。

  典型的な症状:
    - 401 "Please wait a few minutes before you try again."
    - 直近1リクエストしか投げていないのに 429

  これはレート超過ではなく、**アカウントが制限されている**サインです。
  作りたてのアカウントがログイン直後にAPIを叩くと、ほぼこうなります。

  対処:
    1) ブラウザで同じアカウントにログインし、
       本人確認・電話番号などのチャレンジが出たら消化する
    2) 数日、普通に使う（フォロー・スクロール・プロフィール設定）
       「few minutes」と書かれていますが実際は数時間〜数日です
    3) その間 instaloader は回さない。叩くほど延びます

  それでも直らない場合:
    - セッションを作り直す（失効していた可能性）
    - IPが弾かれている疑いがあれば IG_RAY_PROXY で別経路を試す

  切り分けには dev/diag_session.py を使ってください。
"""


def make_loader(login_user=None, verify=True):
    """
    ログイン済み Instaloader を返す。失敗時は (None, 理由) 。
    """
    import instaloader
    from instaloader.exceptions import ConnectionException, LoginException

    user = login_user or LOGIN_USER
    if not user:
        return None, "IG_RAY_LOGIN_USER が未設定です。\n" + \
                     SESSION_HELP.format(session_dir=SESSION_DIR)

    session_file = os.path.join(SESSION_DIR, f"session-{user}")
    if not os.path.exists(session_file):
        return None, f"{session_file} がありません。\n" + \
                     SESSION_HELP.format(session_dir=SESSION_DIR)

    L = instaloader.Instaloader(
        sleep=True,                      # instaloader 側のレート制御に従う
        quiet=True,
        download_pictures=False,         # DLは自前でやるので全部OFF
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=3,
        request_timeout=60.0,
        rate_controller=make_rate_controller(),
    )

    if PROXY:
        L.context._session.proxies = {"http": PROXY, "https": PROXY}

    try:
        L.load_session_from_file(user, session_file)
    except (ConnectionException, LoginException) as e:
        return None, f"セッション読み込み失敗: {e}\n作り直してください。"
    except Exception as e:
        return None, f"セッション読み込み失敗（想定外）: {e}"

    # load_session_from_file() はファイルを読むだけで、そのセッションが
    # まだ有効かは検証しない。死んだセッションのまま走ると匿名アクセス扱いになり、
    # 意味不明な 429 で死ぬので、ここで必ず確かめる。
    if verify:
        try:
            who = L.test_login()
        except Exception as e:
            return None, f"ログイン検証中にエラー: {type(e).__name__}: {e}\n" + SOFTBLOCK_HELP

        if not who:
            return None, ("ログイン検証に失敗しました"
                          f"（セッションは {user} のものとして読めています）\n"
                          + SOFTBLOCK_HELP)

        if who.lower() != user.lower():
            print(f"  [警告] セッションは '{who}' のものです"
                  f"（IG_RAY_LOGIN_USER は '{user}'）", file=sys.stderr)

    return L, None


# --------------------------------------------------------------------------
# メイン処理
# --------------------------------------------------------------------------

def scrape_user(conn, loader, username, limit=12, with_media=True,
                fallback=True, posts_only=None):
    """
    指定ユーザーの直近 limit 件を取得して保存。
    戻り値: dict(status, fetched, inserted, message)
    """
    import instaloader
    from instaloader.exceptions import (
        ProfileNotExistsException,
        PrivateProfileNotFollowedException,
        LoginRequiredException,
        TooManyRequestsException,
        QueryReturnedBadRequestException,
        ConnectionException,
    )

    started = datetime.now(timezone.utc).isoformat()
    degraded = False

    # ミュート中は取得自体を行わない。
    # 引数で直接指定された場合も同じ（意図せず取りに行くのを防ぐ）。
    if username.lower() in db.muted_usernames(conn):
        return {"status": "muted", "fetched": 0, "inserted": 0,
                "message": "ミュート中のためスキップしました", "started_at": started}

    if posts_only:
        # 弾かれると分かっているエンドポイントは最初から叩かない。
        # 一度 429 を食うとペナルティが全クエリ種別に波及するため、
        # 「試してから諦める」より「試さない」ほうが確実に速い。
        profile = minimal_profile(loader.context, username)
        degraded = True
        db.ensure_account(conn, username.lower())
        return _scrape_posts(conn, loader, profile, username, limit,
                             with_media, degraded, started)

    try:
        profile = instaloader.Profile.from_username(loader.context, username)
    except ProfileNotExistsException:
        return {"status": "error", "fetched": 0, "inserted": 0,
                "message": "アカウントが存在しません", "started_at": started}
    except (LoginRequiredException, PrivateProfileNotFollowedException) as e:
        return {"status": "error", "fetched": 0, "inserted": 0,
                "message": f"閲覧権限なし: {e}", "started_at": started}
    except (RateLimitAbort, TooManyRequestsException,
            ConnectionException, QueryReturnedBadRequestException) as e:
        # web_profile_info だけが弾かれているケースがある。
        # 投稿取得は別エンドポイント（doc_id graphql）なので、そちらを試す。
        if not fallback:
            status = "ratelimited" if isinstance(
                e, (RateLimitAbort, TooManyRequestsException)) else "error"
            return {"status": status, "fetched": 0, "inserted": 0,
                    "message": str(e), "started_at": started}
        print(f"  [情報] プロフィール取得に失敗（{type(e).__name__}）。"
              f"投稿のみの取得に切り替えます。")
        # 429 のペナルティは全クエリ種別に波及するので、
        # 切り替え先の graphql 経路が巻き添えにならないよう解除する。
        clear_rate_penalty(loader.context)
        profile = minimal_profile(loader.context, username)
        degraded = True
        print("  [情報] 恒常的に弾かれる場合は IG_RAY_POSTS_ONLY=1 で"
              "この往復を省けます。")

    if degraded:
        # userid 等は取れていないので username だけ確保する。
        # upsert_account を使うと既存の followers 等を None で潰してしまう。
        db.ensure_account(conn, username.lower())
    else:
        db.upsert_account(conn, profile_to_row(profile))

    return _scrape_posts(conn, loader, profile, username, limit,
                         with_media, degraded, started)


def _scrape_posts(conn, loader, profile, username, limit,
                  with_media, degraded, started):
    """投稿取得〜保存の共通部分。posts_only と通常経路で共有する。"""
    from instaloader.exceptions import TooManyRequestsException

    records = []
    media_map = {}
    skipped = []
    owners = {}
    muted = db.muted_usernames(conn)

    # 「いま巡回が動いている」を記録する。バックフィルはこれを見て待つ。
    # 同時に叩くとレート予算を取り合い、429 は全クエリ種別に波及するため。
    try:
        db.touch_activity(conn, "scrape")
    except Exception:
        pass    # 記録できなくても巡回自体は止めない

    try:
        for i, post in enumerate(profile.get_posts()):
            if i >= limit:
                break
            # 1件の失敗でバッチ全体を落とさない。
            # instaloader の Post は構造の差異で KeyError を投げることがある。
            sc = _safe(lambda: post.shortcode) or f"<index {i}>"
            try:
                rec = post_to_record(post)
            except Exception as e:
                skipped.append((sc, f"{type(e).__name__}: {e}"))
                print(f"  [警告] {sc} をスキップ: {type(e).__name__}: {e}",
                      file=sys.stderr)
                continue
            records.append(rec)
            media_map[rec["shortcode"]] = json.loads(rec["media_json"])

            # 投稿データにオーナーのアイコン/表示名が入っている。
            # 共同投稿では相手側の情報も拾えるので username をキーに集める。
            owner = owner_from_node(post)
            if owner and owner["username"] not in muted:
                owners[owner["username"]] = owner

            if (i + 1) % 5 == 0:
                try:
                    db.touch_activity(conn, "scrape")
                except Exception:
                    pass
                _nap()
    except (RateLimitAbort, TooManyRequestsException) as e:
        # 途中まででも保存する
        inserted = 0
        if records:
            _, inserted = db.save_posts(conn, records)
        return {"status": "ratelimited", "fetched": len(records),
                "inserted": inserted, "message": str(e), "started_at": started}
    except Exception as e:
        # 保存はするのに inserted を 0 と報告していたことがある（v2.6以前）。
        # 実際の件数を返さないと、データが失われたと誤解する。
        inserted = 0
        if records:
            _, inserted = db.save_posts(conn, records)
        return {"status": "error", "fetched": len(records), "inserted": inserted,
                "message": f"取得中断: {type(e).__name__}: {e}", "started_at": started}

    fetched, inserted = db.save_posts(conn, records)
    conn.commit()   # メディアDLに入る前に投稿だけ確定させる

    # アイコン・表示名を accounts に反映する。
    # merge_account は None で既存値を潰さないので、degraded でも安全。
    if owners:
        sess = media_session(loader)
        for name, row in owners.items():
            row = dict(row)
            row["profile_pic_local"] = save_avatar(
                name, row.get("profile_pic_url"), session_obj=sess)
            db.merge_account(conn, row)
        conn.commit()

    if with_media:
        # ログイン済みセッションは Host ヘッダが www.instagram.com 固定なので
        # CDN に投げると 404 になる。必ず匿名セッションを使う。
        sess = media_session(loader)
        try:
            for sc, mlist in media_map.items():
                download_media(conn, sc, mlist, session_obj=sess)
                # 1投稿ぶんごとにコミットする。
                # まとめてコミットすると、投稿ごとの休憩（3〜8秒）のあいだ
                # ずっと書き込みロックを握ることになり、
                # web や次の巡回が "database is locked" で落ちる。
                conn.commit()
                _nap()
        except RateLimitAbort as e:
            # posts は保存済みなので、メディアだけ次回に持ち越す
            return {"status": "ratelimited", "fetched": fetched, "inserted": inserted,
                    "message": f"メディア取得中に中止: {e}", "started_at": started}

    msgs = []
    if degraded:
        msgs.append("プロフィール詳細は取得できていません（投稿のみ）")
    if skipped:
        msgs.append(f"{len(skipped)}件スキップ: " +
                    ", ".join(f"{sc}({err})" for sc, err in skipped[:3]))

    return {"status": "ok_degraded" if (degraded or skipped) else "ok",
            "fetched": fetched, "inserted": inserted,
            "message": " / ".join(msgs) if msgs else None,
            "started_at": started}


def main():
    ap = argparse.ArgumentParser(description="insta-ray scraper (PoC)")
    ap.add_argument("usernames", nargs="*", help="取得対象。省略時は accounts テーブル")
    ap.add_argument("--limit", type=int, default=12, help="1アカウントあたり件数")
    ap.add_argument("--login-user", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--no-media", action="store_true", help="メディアDLをスキップ")
    ap.add_argument("--skip-verify", action="store_true",
                    help="起動時のログイン検証を省略（非推奨）")
    ap.add_argument("--no-fallback", action="store_true",
                    help="プロフィール取得に失敗したとき投稿のみ取得に切り替えない")
    ap.add_argument("--posts-only", action="store_true", default=None,
                    help="プロフィール取得を最初から試みない"
                         "（web_profile_info が恒常的に弾かれる環境向け）")
    ap.add_argument("--cron", action="store_true",
                    help="定期巡回から呼ばれていることを示す。"
                         "終了時に次回予定時刻を記録する（web画面の表示用）。"
                         "手動実行で付けると嘘の予定時刻が出るので付けないこと")
    args = ap.parse_args()

    conn = db.connect(args.db)
    db.init_db(conn)

    # 巡回が始まったので「次回予定」は一旦消す（走っている最中は予定ではない）
    if args.cron:
        try:
            db.clear_next_run(conn, "scrape")
        except Exception:
            pass

    loader, err = make_loader(args.login_user, verify=not args.skip_verify)
    if loader is None:
        print(err)
        if args.cron:
            # ログインできなくても cron は INTERVAL 後に再挑戦する
            try:
                db.set_next_run(conn, "scrape", CRON_INTERVAL)
            except Exception:
                pass
        conn.close()
        return 2

    targets = args.usernames or db.enabled_accounts(conn)
    if not targets:
        print("対象がありません。引数で指定するか accounts テーブルに入れてください。")
        conn.close()
        return 1

    rc = 0
    for u in targets:
        print(f"=== {u} ===")
        started = datetime.now(timezone.utc).isoformat()
        try:
            result = scrape_user(conn, loader, u, limit=args.limit,
                                 with_media=not args.no_media,
                                 fallback=not args.no_fallback,
                                 posts_only=(POSTS_ONLY if args.posts_only is None
                                             else args.posts_only))
        except sqlite3.OperationalError as e:
            # "database is locked" 等。1アカウントの失敗で巡回全体を
            # 落とさない（cron では次の周回まで何も取れなくなるため）。
            try:
                conn.rollback()
            except Exception:
                pass
            result = {"status": "error", "fetched": 0, "inserted": 0,
                      "message": f"DBエラー: {e}", "started_at": started}
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            result = {"status": "error", "fetched": 0, "inserted": 0,
                      "message": f"{type(e).__name__}: {e}", "started_at": started}

        try:
            db.log_scrape(conn, u, result["status"], result["fetched"],
                          result["inserted"], result["message"],
                          result["started_at"])
            conn.commit()
        except sqlite3.OperationalError as e:
            print(f"  [警告] ログ記録に失敗: {e}", file=sys.stderr)
        print(f"  {result['status']}: fetched={result['fetched']} "
              f"inserted={result['inserted']} {result['message'] or ''}")
        if result["status"] not in ("ok", "ok_degraded", "muted"):
            rc = 1
        if result["status"] == "ratelimited":
            print("  レート制限を検知。以降を中止します。")
            break
        _nap()

    # 次の周回がいつ始まるかを記録する。cron のシェルはこの直後に
    # sleep INTERVAL に入るので、ここが実際の予定時刻になる。
    if args.cron:
        try:
            when = db.set_next_run(conn, "scrape", CRON_INTERVAL)
            print(f"次回の巡回予定: {when.astimezone().strftime('%Y-%m-%d %H:%M')}")
        except Exception as e:
            print(f"  [警告] 次回予定の記録に失敗: {e}", file=sys.stderr)

    conn.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
