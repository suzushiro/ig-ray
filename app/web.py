"""
insta-ray / web.py

X-Ray v18 の表示層を Instagram 構造へ移植したもの。

X-Ray との主な違い:
  - RT / 引用RT / 自己リプライ / スペースが無いので該当ロジックは全削除
  - カテゴリ機能は未実装（後で追加予定）。タブとR18除外は無し
  - メディアの持ち方が違う:
        X-Ray      media_json = URL配列 + local_media_json = ローカルパス配列
        insta-ray  media_json = dict配列 + media_index テーブル(local_path)
    → resolve_media() で吸収する
  - degraded 運用（web_profile_info が 429）だと accounts に username しか
    入らないので、display_name / profile_image_url は無い前提で組む
"""

import base64
import json
import os
import secrets
import time
from datetime import datetime, timezone, timedelta

from flask import (Flask, abort, g, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

import cache_utils
import config
import db
import tumblr_client

JST = timezone(timedelta(hours=9))

CACHE_DIR = config.env("CACHE", "/data/cache")
PER_PAGE = 60

# --- Tumblr 共有 --------------------------------------------------------
# 未設定なら機能まるごと無効（ボタンも出ない、APIも404）。
# Tumblr のシェアツールは **Tumblr のサーバー側から画像URLを取りに来る**ため、
# LAN内のURLでは成立しない。外部から到達できるベースURLが要る。
PUBLIC_SHARE_BASE_URL = config.env("PUBLIC_SHARE_BASE_URL", "").rstrip("/")
# 公開ホスト名。省略時は BASE_URL から自動抽出する。
PUBLIC_SHARE_HOST = (config.env("PUBLIC_SHARE_HOST", "") or "").split(":")[0].lower()
if not PUBLIC_SHARE_HOST and PUBLIC_SHARE_BASE_URL:
    try:
        from urllib.parse import urlparse
        PUBLIC_SHARE_HOST = (urlparse(PUBLIC_SHARE_BASE_URL).hostname or "").lower()
    except Exception:
        PUBLIC_SHARE_HOST = ""
SHARE_TOKEN_TTL_MIN = config.env_float("SHARE_TOKEN_TTL_MIN", 60)
SHARE_ENABLED = bool(PUBLIC_SHARE_BASE_URL)

# 公開ホスト経由で通してよいルート。ここに無いものは404にする。
SHARE_PUBLIC_PREFIXES = ("/share/", "/share-img/")

app = Flask(__name__)

# waitress 起動時（web:app）は __main__ を通らないので、ここでスキーマを整える。
# これが無いと worker を一度も動かしていない環境で、bookmarks の新しい列が
# 無いまま INSERT して 500 になる（☆が無反応になる原因だった）。
try:
    db.init_db()
except Exception as e:  # 起動自体は止めない
    print(f"[warn] init_db 失敗: {type(e).__name__}: {e}")


def conn():
    """リクエスト単位でコネクションを再利用し、teardown で必ず閉じる。"""
    if "conn" not in g:
        g.conn = db.connect()
    return g.conn


@app.teardown_appcontext
def close_db(exc):
    c = g.pop("conn", None)
    if c is not None:
        c.close()


def page_arg(name="page"):
    """?page=abc のような不正値で500にしない"""
    try:
        return max(1, int(request.args.get(name, 1)))
    except (TypeError, ValueError):
        return 1


@app.template_filter("b64encode")
def b64encode_filter(s):
    if not isinstance(s, str):
        s = json.dumps(s)
    return base64.b64encode(s.encode()).decode()


# --------------------------------------------------------------------------
# メディア解決
# --------------------------------------------------------------------------

def cache_url(local_path):
    """
    /data/cache/AB/XXX_0.jpg -> /cache/AB/XXX_0.jpg
    キャッシュ外のパスは配信しない（ディレクトリトラバーサル防止）。
    """
    if not local_path:
        return None
    try:
        rel = os.path.relpath(local_path, CACHE_DIR)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return "/cache/" + rel.replace(os.sep, "/")


def resolve_media(c, shortcode, media_json):
    """
    表示用メディアの配列を返す。

    ローカルにキャッシュがあればそれを、無ければリモートURLにフォールバック。
    media_index に行が無い（まだDLしていない）投稿でも表示は崩さない。
    """
    try:
        media = json.loads(media_json or "[]")
    except (TypeError, ValueError):
        media = []

    local_by_index = {}
    try:
        for r in c.execute(
            "SELECT media_index, local_path FROM media_index WHERE shortcode = ?",
            (shortcode,),
        ):
            if r["local_path"]:
                local_by_index[r["media_index"]] = r["local_path"]
    except Exception:
        pass

    out = []
    for m in media:
        if not isinstance(m, dict):
            continue
        idx = m.get("index", len(out))
        local = cache_url(local_by_index.get(idx))
        src = local or m.get("image_url")
        if not src:
            continue
        out.append({
            "index": idx,
            "src": src,
            "is_video": bool(m.get("is_video")),
            "video_url": m.get("video_url"),
            "is_local": bool(local),
        })
    return out


_avatar_cache = {"at": 0.0, "map": {}}
AVATAR_TTL = 60


def avatar_map(c):
    """username -> アイコンURL。毎カード引くと重いので60秒キャッシュ。"""
    now = time.time()
    if _avatar_cache["map"] and now - _avatar_cache["at"] < AVATAR_TTL:
        return _avatar_cache["map"]

    m = {}
    try:
        for r in c.execute(
            "SELECT username, profile_pic_local, profile_pic_url FROM accounts"
        ):
            # ローカルに落としてあればそれを使う。
            # リモートURLは署名付きで期限切れになるので当てにしない。
            src = cache_url(r["profile_pic_local"]) if r["profile_pic_local"] else None
            if src:
                m[r["username"]] = src
    except Exception:
        pass

    _avatar_cache.update({"at": now, "map": m})
    return m


_muted_cache = {"at": 0.0, "set": frozenset()}
MUTED_TTL = 30


def muted_set(c, fresh=False):
    """ミュート中のアカウント名。カードごとに引くと重いので短時間キャッシュ。"""
    now = time.time()
    if not fresh and now - _muted_cache["at"] < MUTED_TTL:
        return _muted_cache["set"]
    s = frozenset(db.muted_usernames(c))
    _muted_cache.update({"at": now, "set": s})
    return s


def format_post(c, d, bookmarked=None, media_from_bookmark=False):
    """DBの行dictを表示用に整形する。"""
    d = dict(d)

    if media_from_bookmark:
        # ブックマークは非正規化コピーなので、そちらのローカルパスを使う
        try:
            media = json.loads(d.get("media_json") or "[]")
            local = json.loads(d.get("local_media_json") or "[]")
        except (TypeError, ValueError):
            media, local = [], []
        imgs = []
        for i, m in enumerate(media):
            if not isinstance(m, dict):
                continue
            lp = cache_url(local[i]) if i < len(local) else None
            src = lp or m.get("image_url")
            if src:
                imgs.append({"index": m.get("index", i), "src": src,
                             "is_video": bool(m.get("is_video")),
                             "video_url": m.get("video_url"),
                             "is_local": bool(lp)})
        d["imgs"] = imgs
    else:
        d["imgs"] = resolve_media(c, d.get("shortcode"), d.get("media_json"))

    try:
        d["hashtags"] = json.loads(d.get("hashtags_json") or "[]")
    except (TypeError, ValueError):
        d["hashtags"] = []

    # 日時をJSTへ
    raw = d.get("date_utc") or ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        j = dt.astimezone(JST)
        d["date_jst"] = j.strftime("%Y-%m-%d %H:%M")
        d["date_compact"] = j.strftime("%Y%m%d%H%M")
    except Exception:
        d["date_jst"] = raw[:16].replace("T", " ")
        d["date_compact"] = raw[:16].replace("-", "").replace("T", "").replace(":", "")

    d["url"] = f"https://www.instagram.com/p/{d.get('shortcode')}/"
    d["avatar"] = avatar_map(c).get(d.get("owner_username"))
    d["owner_muted"] = d.get("owner_username") in muted_set(c)
    d["is_bookmarked"] = bool(bookmarked and d.get("shortcode") in bookmarked)
    # Tumblr 共有の可否。**ローカルに実体のある静止画が1枚以上**必要。
    # リモートURLは Instagram CDN の Referer 制限で Tumblr から取得できず、
    # 動画はサムネしか持っていないので対象外。
    d["has_local"] = any(i.get("is_local") and not i.get("is_video")
                         for i in d["imgs"])

    d["imgs_b64"] = base64.b64encode(
        json.dumps([i["src"] for i in d["imgs"]]).encode()
    ).decode() if d["imgs"] else ""

    # 各画像に対応する再生URL。動画でなければ None。
    # 動画本体は保存していないので、再生は Instagram 側へ飛ばす。
    d["vids_b64"] = base64.b64encode(
        json.dumps([d["url"] if i["is_video"] else None for i in d["imgs"]]).encode()
    ).decode() if d["imgs"] else ""
    return d


# --------------------------------------------------------------------------
# 画像配信
# --------------------------------------------------------------------------

@app.route("/cache/<path:filename>")
def serve_cache(filename):
    return send_from_directory(CACHE_DIR, filename)


# --------------------------------------------------------------------------
# フィード
# --------------------------------------------------------------------------

@app.before_request
def _restrict_public_host():
    """
    公開ホスト名で来たリクエストは共有系ルートだけ許可する。

    **これが最後の砦。** リバースプロキシ（Cloudflare Tunnel）側の path 指定を
    間違えても、公開ドメイン経由では `/share` 系以外に到達できない。
    スクレイパーの操作画面やバックアップ画面が外に出ると洒落にならないので、
    ingress の設定だけに頼らない。
    """
    if not PUBLIC_SHARE_HOST:
        return None
    host = (request.host or "").split(":")[0].lower()
    if host != PUBLIC_SHARE_HOST:
        return None
    if any(request.path.startswith(p) for p in SHARE_PUBLIC_PREFIXES):
        return None
    # /api/tumblr/* もここで404になる（公開ホストから投稿されては困る）
    abort(404)


@app.context_processor
def _inject_share_flags():
    """
    テンプレート全体から共有機能の有効/無効を見えるようにする。

    2方式ある:
      tumblr_enabled … OAuth API で直接投稿する（複数枚OK・外部公開不要）
      share_enabled  … シェアツール＋一時公開URL（複数枚が壊れている旧方式）
    どちらかが有効なら `t` ボタンを出す。両方有効なら API を優先する。

    **マクロは `with context` で import しないとこれが見えない。**
    """
    tumblr_ok = tumblr_client.is_configured()
    return {
        "share_enabled": SHARE_ENABLED or tumblr_ok,
        "tumblr_api_enabled": tumblr_ok,
        "share_link_enabled": SHARE_ENABLED,
    }


def _share_source_url(shortcode):
    """
    出典として Tumblr に載るURL。

    X-Ray には `tweets.url` 列があったが、ig-ray の posts には無いので組み立てる。
    """
    return f"https://www.instagram.com/p/{shortcode}/"


def _local_media_paths(c, shortcode):
    """
    共有できる（＝ローカルに実体がある）画像の絶対パスを順番に返す。

    リモートURLは対象外。Instagram の CDN は Referer 制限があり
    Tumblr のサーバーからは取得できないため、共有しても失敗する。
    動画も除外する（サムネしか持っておらず、mp4 は既定で落としていない）。
    """
    out = []
    for r in c.execute(
        "SELECT media_index, local_path, is_video FROM media_index "
        "WHERE shortcode = ? AND local_path IS NOT NULL ORDER BY media_index",
        (shortcode,),
    ):
        if r["is_video"]:
            continue
        # CACHE_DIR の外は配信しない（cache_url と同じ判定を通す）
        if cache_url(r["local_path"]):
            out.append(r["local_path"])
    return out


@app.route("/api/share/prepare", methods=["POST"])
def api_share_prepare():
    """
    共有用のトークンを発行し、Tumblr に渡す絶対URLを返す。

    payload に確定内容をスナップショットしておき、配信時はDBを引き直さない。
    """
    if not SHARE_ENABLED:
        return jsonify({"ok": False, "error": "共有機能が無効です"}), 404

    data = request.get_json(silent=True) or {}
    shortcode = (data.get("shortcode") or "").strip()
    if not shortcode:
        return jsonify({"ok": False, "error": "shortcode がありません"}), 400

    c = conn()
    paths = _local_media_paths(c, shortcode)
    if not paths:
        return jsonify({
            "ok": False,
            "error": "ローカルに画像がないため共有できません",
        }), 400

    token = secrets.token_urlsafe(16)
    source_url = _share_source_url(shortcode)
    payload = {
        "shortcode": shortcode,
        "paths": paths,
        "caption": (data.get("caption") or "").strip(),
        "tags": (data.get("tags") or "").strip(),
        "source_url": source_url,
    }

    try:
        db.purge_expired_share_tokens(c)
        db.create_share_token(c, token, shortcode, payload, SHARE_TOKEN_TTL_MIN)
    except Exception as e:
        return jsonify({"ok": False, "error": f"トークン発行に失敗: {e}"}), 500

    return jsonify({
        "ok": True,
        "share_url": f"{PUBLIC_SHARE_BASE_URL}/share/{token}",
        "image_urls": [f"{PUBLIC_SHARE_BASE_URL}/share-img/{token}/{i}"
                       for i in range(len(paths))],
        "source_url": source_url,
        "expires_in_min": int(SHARE_TOKEN_TTL_MIN),
    })


@app.route("/api/tumblr/accounts")
def api_tumblr_accounts():
    """投稿先の一覧。**トークンは絶対に返さない**（public_accounts を使う）。"""
    return jsonify({
        "ok": True,
        "enabled": tumblr_client.is_configured(),
        "accounts": tumblr_client.public_accounts(),
    })


@app.route("/api/tumblr/post", methods=["POST"])
def api_tumblr_post():
    """
    Tumblr へ直接投稿する。

    **画像はローカルファイルをそのままアップロードする**ので、
    公開URLもトンネルも要らない。複数枚も `data[0]`, `data[1]`, … で普通に通る
    （シェアツール方式が複数枚で壊れたのはTumblr側の劣化）。
    """
    if not tumblr_client.is_configured():
        return jsonify({"ok": False,
                        "error": "Tumblr投稿が未設定です（キーまたはアカウント）"}), 400

    data = request.get_json(silent=True) or request.form
    shortcode = (data.get("shortcode") or "").strip()
    if not shortcode:
        return jsonify({"ok": False, "error": "shortcode が必要です"}), 400

    account = tumblr_client.find_account((data.get("account") or "").strip())
    if not account:
        return jsonify({"ok": False, "error": "投稿先アカウントが見つかりません"}), 400

    state = (data.get("state") or "published").strip()
    if state not in tumblr_client.VALID_STATES:
        return jsonify({"ok": False, "error": f"state が不正です: {state}"}), 400

    # 添付する画像の添字（未指定なら全部）
    raw_idx = (data.get("indices") or "").strip()
    indices = None
    if raw_idx:
        try:
            indices = {int(x) for x in raw_idx.split(",") if x.strip() != ""}
        except ValueError:
            return jsonify({"ok": False, "error": "indices が不正です"}), 400

    c = conn()
    # **パスをそのまま使う。** ig-ray の local_path は
    # /data/cache/AB/XXX_0.jpg のようにサブディレクトリを含むので、
    # ファイル名だけ取り出すと開けなくなる。
    paths = _local_media_paths(c, shortcode)
    if indices is not None:
        paths = [p for i, p in enumerate(paths) if i in indices]
    if not paths:
        return jsonify({"ok": False,
                        "error": "投稿できるローカル画像がありません"}), 400

    source_url = _share_source_url(shortcode)
    caption = (data.get("caption") or "").strip()
    if caption and source_url:
        caption = f'<a href="{source_url}">{caption}</a>'
    tags = [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()]

    res = tumblr_client.create_photo_post(
        account, paths, caption=caption, tags=tags, state=state,
        source_url=source_url)
    if not res.get("ok"):
        return jsonify(res), 502
    res["label"] = account["label"]
    res["count"] = len(paths)
    return jsonify(res)


@app.route("/share/<token>")
def share_page(token):
    """
    OGP付きのプレビューページ。

    Tumblr への投稿自体は content で画像URLを直接渡すのでこれは必須ではないが、
    共有リンクを直接開いたときの見え方として機能する。
    """
    if not SHARE_ENABLED:
        abort(404)
    payload = db.get_share_payload(conn(), token)
    if not payload:
        abort(404)

    n = len(payload.get("paths") or [])
    return render_template(
        "share.html",
        token=token,
        count=n,
        caption=payload.get("caption") or "",
        tags=payload.get("tags") or "",
        source_url=payload.get("source_url") or "",
        image_urls=[f"{PUBLIC_SHARE_BASE_URL}/share-img/{token}/{i}"
                    for i in range(n)],
    )


@app.route("/share-img/<token>/<int:n>")
def share_image(token, n):
    """
    payload に記録したパスの画像を配信する。**Tumblr から叩かれる本命。**

    パスは発行時のスナップショットを使い、DBを引き直さない。
    CACHE_DIR 相対に直してから send_from_directory に渡す
    （ファイル名だけにするとサブディレクトリを失って配信できなくなる）。
    """
    if not SHARE_ENABLED:
        abort(404)
    payload = db.get_share_payload(conn(), token)
    if not payload:
        abort(404)

    paths = payload.get("paths") or []
    if n < 0 or n >= len(paths):
        abort(404)

    url = cache_url(paths[n])          # CACHE_DIR 外なら None が返る
    if not url:
        abort(404)
    rel = url[len("/cache/"):]
    if not os.path.exists(os.path.join(CACHE_DIR, rel)):
        abort(404)
    return send_from_directory(CACHE_DIR, rel)


@app.route("/")
def index():
    page = page_arg()
    offset = (page - 1) * PER_PAGE
    c = conn()
    marked = db.bookmarked_shortcodes(c)

    # ミュート中のアカウントの投稿はフィードに出さない（データは消さない）
    muted = muted_set(c)
    if muted:
        marks = ",".join("?" * len(muted))
        rows = c.execute(
            f"SELECT * FROM posts WHERE owner_username NOT IN ({marks}) "
            "ORDER BY date_utc DESC LIMIT ? OFFSET ?",
            list(muted) + [PER_PAGE + 1, offset],
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM posts ORDER BY date_utc DESC LIMIT ? OFFSET ?",
            (PER_PAGE + 1, offset),
        ).fetchall()
    has_next = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]
    posts = [format_post(c, r, marked) for r in rows]

    total = c.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"]

    last = c.execute(
        "SELECT ended_at FROM scrape_log ORDER BY id DESC LIMIT 1").fetchone()
    last_jst = None
    if last and last["ended_at"]:
        try:
            dt = datetime.fromisoformat(last["ended_at"].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            last_jst = dt.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
        except Exception:
            last_jst = last["ended_at"][:16].replace("T", " ") + " UTC"

    # 直近のエラー/レート制限を拾う。
    # 正常なステータスは db.OK_STATUSES に集約してある
    # （degraded も backfill の完了も正常。ここを直書きすると
    #  ステータスを増やしたとき成功が警告として出る）
    marks = ",".join("?" * len(db.OK_STATUSES))
    troubles = [dict(r) for r in c.execute(f"""
        SELECT username, status, COUNT(*) AS n
        FROM scrape_log
        WHERE status NOT IN ({marks})
          AND ended_at > datetime('now', '-6 hours')
        GROUP BY username, status
        ORDER BY n DESC LIMIT 5
    """, db.OK_STATUSES)]

    next_url = url_for("index", page=page + 1) if has_next else None
    if request.args.get("partial") == "1":
        return render_template("_feed.html", posts=posts, next_url=next_url)

    return render_template("index.html", posts=posts, next_url=next_url,
                           total=total, last_jst=last_jst, troubles=troubles,
                           page=page, has_next=has_next)


# --------------------------------------------------------------------------
# ユーザーページ
# --------------------------------------------------------------------------

@app.route("/user/<username>")
def user_profile(username):
    username = username.lower()
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    page = page_arg()
    offset = (page - 1) * PER_PAGE

    c = conn()
    marked = db.bookmarked_shortcodes(c)

    acc = c.execute(
        "SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()
    if acc is None:
        # accounts に無くても posts があれば表示する
        # （degraded 運用では取りこぼしが起きうるため）
        n = c.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE owner_username = ?",
            (username,)).fetchone()["n"]
        if not n:
            return "アカウントが見つかりません", 404
        acc = {"username": username}
    acc = dict(acc)
    acc["avatar"] = avatar_map(c).get(username)
    acc["is_muted"] = bool(acc.get("is_muted"))

    where = ["owner_username = ?"]
    params = [username]
    if date_from:
        where.append("date_utc >= ?")
        params.append(date_from)
    if date_to:
        where.append("date_utc <= ?")
        params.append(date_to + "T23:59:59")
    where_sql = " AND ".join(where)

    rows = c.execute(
        f"SELECT * FROM posts WHERE {where_sql} "
        "ORDER BY date_utc DESC LIMIT ? OFFSET ?",
        params + [PER_PAGE + 1, offset],
    ).fetchall()
    has_next = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]
    posts = [format_post(c, r, marked) for r in rows]

    stats = dict(c.execute("""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(likes), 0) AS total_likes,
               COALESCE(SUM(comments), 0) AS total_comments,
               COALESCE(SUM(is_carousel), 0) AS carousels,
               COALESCE(SUM(is_video), 0) AS videos,
               MIN(date_utc) AS oldest,
               MAX(date_utc) AS newest
        FROM posts WHERE owner_username = ?
    """, (username,)).fetchone() or {})

    stats["media_count"] = c.execute(
        "SELECT COUNT(*) AS n FROM media_index WHERE shortcode IN "
        "(SELECT shortcode FROM posts WHERE owner_username = ?)",
        (username,)).fetchone()["n"]

    stats["per_day"] = None
    try:
        if stats.get("oldest") and stats.get("newest") and stats.get("total"):
            o = datetime.fromisoformat(stats["oldest"].replace("Z", "+00:00"))
            n = datetime.fromisoformat(stats["newest"].replace("Z", "+00:00"))
            days = max(1, (n - o).days)
            stats["per_day"] = round(stats["total"] / days, 1)
            stats["oldest_jst"] = o.astimezone(JST).strftime("%Y-%m-%d")
            stats["newest_jst"] = n.astimezone(JST).strftime("%Y-%m-%d")
    except Exception:
        pass

    next_url = (url_for("user_profile", username=username, page=page + 1,
                        **{"from": date_from, "to": date_to})
                if has_next else None)
    if request.args.get("partial") == "1":
        return render_template("_feed.html", posts=posts, next_url=next_url)

    return render_template("user.html", account=acc, posts=posts, stats=stats,
                           date_from=date_from, date_to=date_to,
                           page=page, has_next=has_next, next_url=next_url)


# --------------------------------------------------------------------------
# ギャラリー
# --------------------------------------------------------------------------

@app.route("/gallery")
def gallery():
    page = page_arg()
    offset = (page - 1) * PER_PAGE
    username = request.args.get("user", "").strip().lower()

    c = conn()
    where = ["media_json IS NOT NULL", "media_json != '[]'"]
    params = []
    if username:
        where.append("owner_username = ?")
        params.append(username)
    else:
        # 個別に指定されたときは見せる。一覧では隠す。
        muted = db.muted_usernames(c)
        if muted:
            where.append(
                "owner_username NOT IN (%s)" % ",".join("?" * len(muted)))
            params.extend(muted)
    where_sql = " AND ".join(where)

    rows = c.execute(
        f"SELECT shortcode, owner_username, media_json, date_utc "
        f"FROM posts WHERE {where_sql} ORDER BY date_utc DESC LIMIT ? OFFSET ?",
        params + [PER_PAGE + 1, offset],
    ).fetchall()
    has_next = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]

    # 1画像=1グリッドアイテムに平坦化
    items = []
    for r in rows:
        d = dict(r)
        for m in resolve_media(c, d["shortcode"], d["media_json"]):
            items.append({
                "src": m["src"],
                "media_index": m["index"],
                "idx": m["index"] + 1,
                "is_video": m["is_video"],
                "owner_username": d["owner_username"],
                "shortcode": d["shortcode"],
                "url": f"https://www.instagram.com/p/{d['shortcode']}/",
                "date_utc": d["date_utc"] or "",
            })

    all_muted = db.muted_usernames(c)
    users = [r["owner_username"] for r in c.execute(
        "SELECT DISTINCT owner_username FROM posts ORDER BY owner_username")
        if r["owner_username"] not in all_muted]

    next_url = (url_for("gallery", page=page + 1,
                        **({"user": username} if username else {}))
                if has_next else None)
    if request.args.get("partial") == "1":
        return render_template("_gallery_items.html", items=items,
                               next_url=next_url)

    return render_template("gallery.html", items=items, next_url=next_url,
                           users=users, current_user=username,
                           page=page, has_next=has_next)


# --------------------------------------------------------------------------
# ブックマーク
# --------------------------------------------------------------------------

@app.route("/bookmarks")
def bookmarks():
    c = conn()
    rows = c.execute(
        "SELECT * FROM bookmarks ORDER BY created_at DESC").fetchall()
    posts = [format_post(c, r, media_from_bookmark=True) for r in rows]
    for p in posts:
        p["is_bookmarked"] = True
    return render_template("bookmarks.html", posts=posts)


# --------------------------------------------------------------------------
# ストレージ
# --------------------------------------------------------------------------

@app.route("/storage")
def storage():
    import shutil

    c = conn()

    dbsize = cache_utils.db_size()
    cache_size, cache_count = cache_utils.dir_stats()

    protected = cache_utils.protected_paths(c)
    protected_bytes = 0
    for p in protected:
        try:
            protected_bytes += os.path.getsize(p)
        except OSError:
            pass

    # 削除見込み（実際には消さない）
    stale = cache_utils.cleanup_cache(c, dry_run=True)

    try:
        du = shutil.disk_usage(os.path.dirname(cache_utils.DB_PATH) or "/")
        disk_total, disk_used, disk_free = du.total, du.used, du.free
    except OSError:
        disk_total = disk_used = disk_free = 0

    counts = {
        "posts": c.execute("SELECT COUNT(*) n FROM posts").fetchone()["n"],
        "accounts": c.execute("SELECT COUNT(*) n FROM accounts").fetchone()["n"],
        "bookmarks": c.execute("SELECT COUNT(*) n FROM bookmarks").fetchone()["n"],
        "media": c.execute("SELECT COUNT(*) n FROM media_index").fetchone()["n"],
    }
    counts["media_local"] = c.execute(
        "SELECT COUNT(*) n FROM media_index WHERE local_path IS NOT NULL"
    ).fetchone()["n"]
    counts["media_remote"] = counts["media"] - counts["media_local"]

    return render_template(
        "storage.html",
        db_size=cache_utils.fmt_size(dbsize),
        cache_size=cache_utils.fmt_size(cache_size),
        cache_count=cache_count,
        protected_count=len(protected),
        protected_size=cache_utils.fmt_size(protected_bytes),
        stale_count=stale["deleted"],
        stale_size=cache_utils.fmt_size(stale["freed"]),
        retention_days=cache_utils.RETENTION_DAYS,
        total_size=cache_utils.fmt_size(dbsize + cache_size),
        disk_total=cache_utils.fmt_size(disk_total),
        disk_used=cache_utils.fmt_size(disk_used),
        disk_free=cache_utils.fmt_size(disk_free),
        disk_pct=round(disk_used / disk_total * 100, 1) if disk_total else 0,
        counts=counts,
        per_account=cache_utils.per_account_usage(c),
        cache_dir=cache_utils.CACHE_DIR,
    )


@app.route("/api/cache/cleanup", methods=["POST"])
def api_cache_cleanup():
    """
    キャッシュの手動削除。
      days 未指定 → IG_RAY_CACHE_RETENTION_DAYS より古いもの
      days=0      → 全部（ブックマーク参照分は常に保護）
    """
    raw = request.form.get("days")
    if raw in (None, ""):
        days = None
    else:
        try:
            days = max(0, int(raw))
        except ValueError:
            return jsonify({"ok": False, "error": "days が不正です"}), 400

    c = conn()
    res = cache_utils.cleanup_cache(c, days=days)
    return jsonify({
        "ok": True,
        "deleted": res["deleted"],
        "kept": res["kept"],
        "freed": cache_utils.fmt_size(res["freed"]),
        "synced": res["synced"],
        "days": res["days"],
    })


@app.route("/backup")
def backup():
    """
    バックアップ状況。定期巡回（cron）と全件バックフィルの両方を見る。

    次回時刻は **実行側が meta に書いたものだけ** を出す。
    ここで「前回＋間隔」を計算すると、
      - 処理時間ぶんズレる（cron は「1周終わってから6時間」）
      - バックフィルの間隔はランダムなので当たらない
      - **ワーカーが止まっていても未来の時刻を出してしまう**
    ので、記録が無ければ素直に「予定なし」と出す。
    """
    c = conn()

    # --- 定期巡回 -------------------------------------------------------
    cron_next = db.get_next_run(c, "scrape")
    # バックフィルは巡回とは別枠なので集計から外す（失敗ではない）
    last_sweep = c.execute(
        "SELECT MAX(ended_at) AS t FROM scrape_log WHERE status != ?",
        (db.BACKFILL_DONE,)).fetchone()["t"]

    # 直近1周ぶんの内訳。ended_at の降順で accounts 件数までを見る
    n_accounts = c.execute(
        "SELECT COUNT(*) AS n FROM accounts "
        "WHERE is_enabled = 1 AND COALESCE(is_muted, 0) = 0").fetchone()["n"]
    recent = [dict(r) for r in c.execute(
        "SELECT username, status, fetched, inserted, message, ended_at "
        "FROM scrape_log WHERE status != ? "
        "ORDER BY ended_at DESC LIMIT ?", (db.BACKFILL_DONE, max(n_accounts, 1)))]
    cron_summary = {}
    for r in recent:
        cron_summary[r["status"]] = cron_summary.get(r["status"], 0) + 1

    # --- バックフィル ---------------------------------------------------
    try:
        jobs = db.backfill_list(c)
    except Exception:
        jobs = []      # backfill_jobs がまだ無い（worker 未更新）

    avatars = avatar_map(c)
    for j in jobs:
        j["avatar"] = avatars.get(j["username"])
        # 進捗率は出せない。**総投稿数が分からない**ため
        # （mediacount は web_profile_info 専用で、POSTS_ONLY 運用では取れない）。
        # 代わりに「どこまで遡ったか」を日付で見せる。
        j["oldest"] = (j["oldest_date"] or "")[:10]

    bf_next = db.get_next_run(c, "backfill")
    worker_age = db.seconds_since_activity(c, "backfill_worker")
    # run ループは最長でも IDLE_SLEEP ごとに打刻するので、
    # その倍以上音沙汰が無ければ止まっているとみなす
    worker_alive = worker_age is not None and worker_age < 900

    running = next((j for j in jobs if j["status"] == "running"), None)
    totals = {
        "queued": sum(1 for j in jobs if j["status"] == "queued"),
        "done": sum(1 for j in jobs if j["status"] == "done"),
        "posts": sum(j["posts_seen"] or 0 for j in jobs),
        "saved": sum(j["posts_saved"] or 0 for j in jobs),
        "media": sum(j["media_saved"] or 0 for j in jobs),
    }

    return render_template(
        "backup.html",
        cron_next=cron_next.isoformat() if cron_next else None,
        cron_last=last_sweep,
        cron_summary=cron_summary,
        cron_recent=recent,
        cron_interval_h=round(config.env_float("INTERVAL", 21600) / 3600, 1),
        cron_limit=config.env_int("CRON_LIMIT", 6),
        n_accounts=n_accounts,
        jobs=jobs,
        running=running,
        totals=totals,
        bf_next=bf_next.isoformat() if bf_next else None,
        worker_alive=worker_alive,
        worker_age=int(worker_age) if worker_age is not None else None,
    )


@app.route("/mutes")
def mutes():
    c = conn()
    rows = db.muted_accounts(c)
    for r in rows:
        r["avatar"] = cache_url(r.get("profile_pic_local"))

    # ミュート候補（投稿があるアカウント）
    muted_set = {r["username"] for r in rows}
    candidates = [
        {"username": x["owner_username"], "n_posts": x["n"]}
        for x in c.execute(
            "SELECT owner_username, COUNT(*) AS n FROM posts "
            "GROUP BY owner_username ORDER BY owner_username")
        if x["owner_username"] not in muted_set
    ]
    return render_template("mutes.html", muted=rows, candidates=candidates)


@app.route("/api/mute/toggle", methods=["POST"])
def api_mute_toggle():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    if not username:
        return jsonify({"ok": False, "error": "username が空です"}), 400

    c = conn()
    row = c.execute(
        "SELECT COALESCE(is_muted, 0) AS m FROM accounts WHERE username = ?",
        (username,)).fetchone()
    now_muted = bool(row["m"]) if row else False

    # muted を明示指定できる（冪等）。省略時のみトグル。
    # カードのダイアログから「ミュートする」を押したのに、既にミュート済みで
    # 解除されてしまう、という事故を防ぐため。
    want = data.get("muted")
    target = (not now_muted) if want is None else bool(want)

    db.set_mute(c, username, target, data.get("reason"))
    c.commit()
    _muted_cache.update({"at": 0.0, "set": frozenset()})
    return jsonify({"ok": True, "muted": target, "username": username,
                    "changed": target != now_muted})


@app.route("/api/bookmark/toggle", methods=["POST"])
def api_bookmark_toggle():
    data = request.get_json(silent=True) or {}
    shortcode = (data.get("shortcode") or "").strip()
    if not shortcode:
        return jsonify({"ok": False, "error": "shortcode が空です"}), 400

    c = conn()
    exists = c.execute(
        "SELECT 1 FROM bookmarks WHERE shortcode = ?", (shortcode,)).fetchone()
    if exists:
        db.remove_bookmark(c, shortcode)
        c.commit()
        return jsonify({"ok": True, "bookmarked": False})

    if not db.add_bookmark(c, shortcode):
        return jsonify({"ok": False, "error": "投稿が見つかりません"}), 404
    c.commit()
    return jsonify({"ok": True, "bookmarked": True})


if __name__ == "__main__":
    db.init_db()
    app.run(host="0.0.0.0", port=8079, debug=False)
