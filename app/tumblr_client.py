"""
Tumblr API v2 クライアント（OAuth1）。

OAuth2 ではなく OAuth1 を使う理由:
  OAuth2 のアクセストークンは expires_in が約42分で、リフレッシュ処理が必須になる。
  OAuth1 のトークンは失効しないので、一度認可すれば放置できる。個人ツール向き。

複数アカウント対応:
  data/tumblr_accounts.json に (label, blog, token, secret) のリストを持つ。
  別々のTumblrアカウントでもトークンを分けて持てば同じ形で扱える。
"""
import json
import os
from datetime import datetime, timezone

import config

API_BASE = "https://api.tumblr.com/v2"
# User-Agent は一貫した値であることが求められている（変動させるとアプリ停止の恐れ）。
# 環境ごとに変えたり、バージョンを自動で埋め込んだりしないこと。
USER_AGENT = "ig-ray-archiver/1.0"

# トークンの実体が入るファイル。**.gitignore 済み。**
# config.env() を使うので IG_RAY_ 接頭辞つきでも読める
# （TUMBLR_* は接頭辞なしで書けるよう素の environ も見る）。
ACCOUNTS_PATH = (os.environ.get("TUMBLR_ACCOUNTS_FILE", "").strip()
                 or config.env("TUMBLR_ACCOUNTS_FILE", "")
                 or "/data/tumblr_accounts.json")

CONSUMER_KEY = (os.environ.get("TUMBLR_CONSUMER_KEY", "").strip()
                or config.env("TUMBLR_CONSUMER_KEY", "")).strip()
CONSUMER_SECRET = (os.environ.get("TUMBLR_CONSUMER_SECRET", "").strip()
                   or config.env("TUMBLR_CONSUMER_SECRET", "")).strip()

VALID_STATES = ("published", "draft", "private", "queue")


def is_configured():
    """投稿機能が使える状態か（キーとアカウントが揃っているか）"""
    return bool(CONSUMER_KEY and CONSUMER_SECRET and load_accounts())


def load_accounts():
    """
    保存済みアカウントを読む。
    戻り値: [{label, blog, token, secret}, ...] （トークンは呼び出し側で秘匿すること）
    """
    try:
        with open(ACCOUNTS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    accounts = data.get("accounts") if isinstance(data, dict) else data
    if not isinstance(accounts, list):
        return []
    out = []
    for a in accounts:
        if not isinstance(a, dict):
            continue
        if a.get("blog") and a.get("token") and a.get("secret"):
            out.append({
                "label": a.get("label") or a["blog"],
                "blog": a["blog"],
                "token": a["token"],
                "secret": a["secret"],
            })
    return out


def public_accounts():
    """UIに渡す用。トークンを含めない。"""
    return [{"label": a["label"], "blog": a["blog"]} for a in load_accounts()]


def find_account(label):
    """label（無ければblog名）でアカウントを引く"""
    accounts = load_accounts()
    if not accounts:
        return None
    if not label:
        return accounts[0]
    for a in accounts:
        if a["label"] == label or a["blog"] == label:
            return a
    return None


def save_accounts(accounts):
    """アカウント一覧を保存する。原子的に書き、パーミッションを絞る。"""
    path = ACCOUNTS_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    payload = {"accounts": accounts, "updated_at": datetime.now(timezone.utc).isoformat()}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _session(account):
    """OAuth1 署名付きセッションを作る"""
    from requests_oauthlib import OAuth1Session
    return OAuth1Session(
        CONSUMER_KEY,
        client_secret=CONSUMER_SECRET,
        resource_owner_key=account["token"],
        resource_owner_secret=account["secret"],
    )


def verify(account):
    """トークンが生きているか確認し、ブログ一覧を返す"""
    try:
        s = _session(account)
        r = s.get(f"{API_BASE}/user/info", headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}", "body": r.text[:200]}
        blogs = r.json().get("response", {}).get("user", {}).get("blogs", [])
        return {"ok": True, "blogs": [b.get("name") for b in blogs]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def create_photo_post(account, image_paths, caption="", tags=None, state="published",
                      source_url="", timeout=120):
    """
    写真投稿を作る。画像はローカルファイルを直接アップロードする
    （公開URLもリバースプロキシも不要）。

    image_paths: ローカルの画像ファイルパスのリスト（複数枚可）
    state: published / draft / private / queue
    source_url: 出典。Tumblr上で元投稿へのリンクになる

    戻り値: {ok, id?, post_url?, error?}
    """
    if state not in VALID_STATES:
        return {"ok": False, "error": f"stateが不正です: {state}"}
    if not image_paths:
        return {"ok": False, "error": "画像がありません"}

    missing = [p for p in image_paths if not os.path.exists(p)]
    if missing:
        return {"ok": False, "error": f"画像が見つかりません: {missing[0]}"}

    data = {
        "type": "photo",
        "state": state,
        "caption": caption or "",
    }
    if source_url:
        data["source_url"] = source_url
    tags = tags or []
    if tags:
        # Tumblr はカンマ区切りの文字列で受ける
        data["tags"] = ",".join(tags)

    files = []
    opened = []
    try:
        for i, p in enumerate(image_paths):
            fh = open(p, "rb")
            opened.append(fh)
            # 複数枚は data[0], data[1], ... として送る
            files.append((f"data[{i}]", (os.path.basename(p), fh, "application/octet-stream")))

        s = _session(account)
        r = s.post(f"{API_BASE}/blog/{account['blog']}/post",
                   data=data, files=files,
                   headers={"User-Agent": USER_AGENT}, timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        for fh in opened:
            try:
                fh.close()
            except OSError:
                pass

    try:
        body = r.json()
    except ValueError:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}

    if r.status_code not in (200, 201):
        msg = ""
        resp = body.get("response")
        if isinstance(resp, dict):
            errs = resp.get("errors")
            if errs:
                msg = str(errs)[:200]
        if not msg:
            msg = body.get("meta", {}).get("msg", "")
        return {"ok": False, "error": f"HTTP {r.status_code} {msg}".strip()}

    post_id = (body.get("response") or {}).get("id_string") or \
              (body.get("response") or {}).get("id")
    post_url = f"https://www.tumblr.com/{account['blog']}/{post_id}" if post_id else ""
    return {"ok": True, "id": str(post_id) if post_id else "", "post_url": post_url,
            "state": state, "blog": account["blog"]}
