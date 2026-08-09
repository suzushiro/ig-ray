"""
insta-ray / dev/mock_check.py

instaloader の Post / Profile をモックして、
extract_media・post_to_record・save_posts のDB書き込みを検証する。
ネットワーク不要・ログイン不要。

    python3 dev/mock_check.py

dev/ はコンテナには同梱しない（X-Ray と同じ方針）。
"""

import json
import os
import sys
import tempfile
from collections import namedtuple
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import db                      # noqa: E402
import ig_scraper as igs       # noqa: E402

# instaloader.structures.PostSidecarNode と同じ形
SidecarNode = namedtuple("PostSidecarNode", ["is_video", "display_url", "video_url"])

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  -- ' + detail) if detail and not cond else ''}")


class FakeLocation:
    def __init__(self, name):
        self.name = name


class FakePost:
    """instaloader.Post の必要な面だけ模したモック。"""

    def __init__(self, shortcode, typename="GraphImage", caption="test caption #foo #bar",
                 likes=10, comments=2, is_video=False, sidecar=None,
                 location=None, date_utc=None, mediaid=None, owner="target_user"):
        self.shortcode = shortcode
        self.mediaid = mediaid if mediaid is not None else abs(hash(shortcode)) % 10**17
        self.owner_username = owner
        self.caption = caption
        self.date_utc = date_utc or datetime(2026, 7, 20, 12, 30, 0)  # naive: IGはUTC naive
        self.likes = likes
        self.comments = comments
        self.typename = typename
        self.is_video = is_video
        self.url = f"https://scontent.example/{shortcode}_display.jpg"
        self.video_url = f"https://scontent.example/{shortcode}.mp4" if is_video else None
        self.location = FakeLocation(location) if location else None
        self._sidecar = sidecar or []

    @property
    def caption_hashtags(self):
        if not self.caption:
            return []
        return [w[1:] for w in self.caption.split() if w.startswith("#")]

    def get_sidecar_nodes(self):
        return iter(self._sidecar)


class FakeProfile:
    def __init__(self, username="target_user", posts=None):
        self.username = username
        self.userid = 123456789
        self.full_name = "Target User"
        self.biography = "bio here"
        self.profile_pic_url = "https://scontent.example/pp.jpg"
        self.followers = 4321
        self.mediacount = 99
        self._posts = posts or []

    def get_posts(self):
        return iter(self._posts)


# --------------------------------------------------------------------------

def test_extract_media():
    print("\n[1] extract_media")

    # 単体画像
    p = FakePost("AAA111")
    m = igs.extract_media(p)
    check("単体画像は1件", len(m) == 1, f"got {len(m)}")
    check("単体画像 is_video=False", m[0]["is_video"] is False)
    check("単体画像 video_url は None", m[0]["video_url"] is None)
    check("単体画像 image_url は post.url", m[0]["image_url"] == p.url)

    # 単体動画
    p = FakePost("BBB222", typename="GraphVideo", is_video=True)
    m = igs.extract_media(p)
    check("単体動画 is_video=True", m[0]["is_video"] is True)
    check("単体動画 video_url あり", m[0]["video_url"] == p.video_url)
    check("単体動画 image_url はサムネ", m[0]["image_url"] == p.url)

    # カルーセル（画像2 + 動画1）
    nodes = [
        SidecarNode(False, "https://scontent.example/c0.jpg", None),
        SidecarNode(True, "https://scontent.example/c1_thumb.jpg", "https://scontent.example/c1.mp4"),
        SidecarNode(False, "https://scontent.example/c2.jpg", None),
    ]
    p = FakePost("CCC333", typename="GraphSidecar", sidecar=nodes)
    m = igs.extract_media(p)
    check("カルーセルは3件", len(m) == 3, f"got {len(m)}")
    check("カルーセル index が 0,1,2", [x["index"] for x in m] == [0, 1, 2])
    check("カルーセル動画ノードの video_url", m[1]["video_url"] == "https://scontent.example/c1.mp4")
    check("カルーセル画像ノードの video_url は None", m[0]["video_url"] is None and m[2]["video_url"] is None)

    # 20枚上限のカルーセル
    big = [SidecarNode(False, f"https://scontent.example/b{i}.jpg", None) for i in range(20)]
    p = FakePost("DDD444", typename="GraphSidecar", sidecar=big)
    check("20枚カルーセルが全件出る", len(igs.extract_media(p)) == 20)


def test_post_to_record():
    print("\n[2] post_to_record")

    nodes = [SidecarNode(False, "https://scontent.example/x0.jpg", None),
             SidecarNode(False, "https://scontent.example/x1.jpg", None)]
    p = FakePost("EEE555", typename="GraphSidecar", sidecar=nodes,
                 location="Tokyo", likes=555, comments=44)
    r = igs.post_to_record(p)

    check("shortcode", r["shortcode"] == "EEE555")
    check("owner_username", r["owner_username"] == "target_user")
    check("is_carousel=1", r["is_carousel"] == 1)
    check("is_video=0", r["is_video"] == 0)
    # location は既定OFF（参照すると追加HTTPリクエストが飛ぶため）
    check("既定では location を取らない", r["location"] is None, str(r["location"]))
    r_loc = igs.post_to_record(p, with_location=True)
    check("明示的に有効化すれば取れる", r_loc["location"] == "Tokyo", str(r_loc["location"]))
    check("likes/comments", r["likes"] == 555 and r["comments"] == 44)

    tags = json.loads(r["hashtags_json"])
    check("hashtags がソート済みユニーク", tags == ["bar", "foo"], f"got {tags}")

    media = json.loads(r["media_json"])
    check("media_json が2件", len(media) == 2)

    # date_utc: naive -> UTC ISO
    check("date_utc が UTC ISO", r["date_utc"].startswith("2026-07-20T12:30:00+00:00"),
          f"got {r['date_utc']}")

    # aware datetime も壊れない
    p2 = FakePost("FFF666", date_utc=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    check("aware datetime も通る", igs.post_to_record(p2)["date_utc"].startswith("2026-01-02T03:04:05"))

    # caption が None（IGでは普通にある）
    p3 = FakePost("GGG777", caption=None)
    r3 = igs.post_to_record(p3)
    check("caption None で落ちない", r3["caption"] is None)
    check("caption None でも hashtags は []", json.loads(r3["hashtags_json"]) == [])

    # location なし
    check("location なしは None",
          igs.post_to_record(FakePost("HHH888"), with_location=True)["location"] is None)

    # 単体動画
    rv = igs.post_to_record(FakePost("III999", typename="GraphVideo", is_video=True))
    check("動画 is_video=1 / is_carousel=0", rv["is_video"] == 1 and rv["is_carousel"] == 0)


def test_save_posts(dbfile):
    print("\n[3] save_posts (DB書き込み)")

    conn = db.connect(dbfile)
    db.init_db(conn)

    posts = [
        FakePost("P001", likes=10),
        FakePost("P002", typename="GraphSidecar",
                 sidecar=[SidecarNode(False, "https://s/1.jpg", None),
                          SidecarNode(True, "https://s/2t.jpg", "https://s/2.mp4")],
                 likes=20),
        FakePost("P003", typename="GraphVideo", is_video=True, likes=30),
    ]
    records = [igs.post_to_record(p) for p in posts]

    fetched, inserted = db.save_posts(conn, records)
    check("初回 fetched=3", fetched == 3, f"got {fetched}")
    check("初回 inserted=3", inserted == 3, f"got {inserted}")

    n = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
    check("posts に3行", n == 3, f"got {n}")

    row = db.get_post(conn, "P002")
    check("P002 is_carousel=1", row["is_carousel"] == 1)
    check("P002 media_json 2件", len(json.loads(row["media_json"])) == 2)
    check("P002 fetched_at が入る", bool(row["fetched_at"]))

    # --- 冪等性 ---
    fetched2, inserted2 = db.save_posts(conn, records)
    check("2回目 inserted=0（冪等）", inserted2 == 0, f"got {inserted2}")
    check("行数は3のまま",
          conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"] == 3)

    # --- いいね数の上書き更新 ---
    posts[0].likes = 999
    posts[0].caption = "updated #new"
    db.save_posts(conn, [igs.post_to_record(posts[0])])
    upd = db.get_post(conn, "P001")
    check("likes が更新される", upd["likes"] == 999, f"got {upd['likes']}")
    check("caption が更新される", upd["caption"] == "updated #new")
    check("hashtags も更新される", json.loads(upd["hashtags_json"]) == ["new"])

    # --- 空リスト ---
    check("空リストは (0,0)", db.save_posts(conn, []) == (0, 0))

    # --- 並び順 ---
    older = FakePost("P000", date_utc=datetime(2020, 1, 1))
    db.save_posts(conn, [igs.post_to_record(older)])
    recent = db.recent_posts(conn, username="target_user", limit=10)
    check("date_utc 降順で返る", recent[-1]["shortcode"] == "P000",
          f"last={recent[-1]['shortcode']}")

    conn.commit()
    conn.close()


def test_media_index(dbfile):
    print("\n[4] media_index / 重複排除")

    conn = db.connect(dbfile)

    db.record_media(conn, "P002", 0, False, "https://s/1.jpg",
                    local_path="/data/cache/P0/P002_0.jpg",
                    sha256="deadbeef", size_bytes=1234)
    db.record_media(conn, "P002", 1, True, "https://s/2.mp4",
                    local_path="/data/cache/P0/P002_1.jpg",
                    sha256="cafebabe", size_bytes=5678)

    rows = db.media_for(conn, "P002")
    check("P002 のメディア2件", len(rows) == 2, f"got {len(rows)}")
    check("index 昇順", [r["media_index"] for r in rows] == [0, 1])
    check("is_video フラグ", rows[0]["is_video"] == 0 and rows[1]["is_video"] == 1)

    # 同じ (shortcode, media_index) は増えない
    db.record_media(conn, "P002", 0, False, "https://s/1.jpg",
                    local_path="/data/cache/P0/P002_0.jpg",
                    sha256="deadbeef", size_bytes=1234)
    check("UNIQUE 制約で重複しない", len(db.media_for(conn, "P002")) == 2)

    # sha 検索
    found = db.find_media_by_sha(conn, "deadbeef")
    check("sha256 でローカルパスが引ける", found == "/data/cache/P0/P002_0.jpg", f"got {found}")
    check("未知の sha は None", db.find_media_by_sha(conn, "0000") is None)

    # local_path なしで記録 → 後から COALESCE で埋まる
    db.record_media(conn, "P003", 0, True, "https://s/3.mp4")
    check("local_path なしでも記録できる", db.media_for(conn, "P003")[0]["local_path"] is None)
    db.record_media(conn, "P003", 0, True, "https://s/3.mp4",
                    local_path="/data/cache/P0/P003_0.jpg", sha256="feed", size_bytes=9)
    check("後追いで local_path が埋まる",
          db.media_for(conn, "P003")[0]["local_path"] == "/data/cache/P0/P003_0.jpg")

    conn.commit()
    conn.close()


def test_accounts_and_log(dbfile):
    print("\n[5] accounts / scrape_log")

    conn = db.connect(dbfile)

    prof = FakeProfile()
    db.upsert_account(conn, igs.profile_to_row(prof))
    row = conn.execute("SELECT * FROM accounts WHERE username='target_user'").fetchone()
    check("accounts に挿入", row is not None)
    check("followers が入る", row["followers"] == 4321)
    check("is_enabled 既定1", row["is_enabled"] == 1)

    prof.followers = 5000
    db.upsert_account(conn, igs.profile_to_row(prof))
    row = conn.execute("SELECT * FROM accounts WHERE username='target_user'").fetchone()
    check("upsert で followers 更新", row["followers"] == 5000)
    check("行は増えない",
          conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 1)

    check("enabled_accounts が返す", db.enabled_accounts(conn) == ["target_user"])

    db.log_scrape(conn, "target_user", "ok", 12, 3)
    db.log_scrape(conn, "target_user", "ratelimited", 5, 0, "429")
    logs = conn.execute(
        "SELECT * FROM scrape_log WHERE username='target_user' ORDER BY id"
    ).fetchall()
    check("ログ2件", len(logs) == 2)
    check("status 記録", logs[0]["status"] == "ok" and logs[1]["status"] == "ratelimited")
    check("started_at が入る", bool(logs[0]["started_at"]))

    conn.commit()
    conn.close()


def test_init_idempotent(dbfile):
    print("\n[6] init_db 冪等性")
    conn = db.connect(dbfile)
    db.init_db(conn)
    db.init_db(conn)  # 2回でも壊れない
    n = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
    check("再 init でデータが消えない", n == 4, f"got {n}")
    v = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    check("schema_version 記録", v["value"] == str(db.SCHEMA_VERSION))
    conn.close()


def main():
    tmp = tempfile.mkdtemp(prefix="instaray_")
    dbfile = os.path.join(tmp, "test.db")
    print(f"test db: {dbfile}")

    test_extract_media()
    test_post_to_record()
    test_save_posts(dbfile)
    test_media_index(dbfile)
    test_accounts_and_log(dbfile)
    test_init_idempotent(dbfile)

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
