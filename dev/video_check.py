"""
ig-ray / dev/video_check.py

GraphVideo（動画投稿）の取り扱い検証。ネットワーク不要。

    python3 dev/video_check.py

背景:
  監視対象28件に動画投稿が無く実データで引けていない、というのが長らくの宿題だった。
  instaloader 4.15.3 の実装を読んだ結果、**`Post.video_url` プロパティは
  動画1件につきCDNへ HEAD を2〜4回投げる**ことが判明した
  （`iphone_struct['video_versions']` の各解像度を Content-Length で比較するため）。
  location と同じ「プロパティを触ると勝手に外へ出る」型の罠。

  ここでは instaloader が実際に組み立てる fake_node の形を再現して、
    - typename / is_video の判定が正しいか
    - 追加リクエスト（HEAD）が発生しないか
    - DOWNLOAD_VIDEOS の ON/OFF で保存されるものが変わるか
  を確認する。
"""

import json
import os
import sys
import tempfile
from collections import namedtuple
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import db                      # noqa: E402
import ig_scraper as igs       # noqa: E402

SidecarNode = namedtuple("PostSidecarNode", ["is_video", "display_url", "video_url"])

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  {mark}  {name}{('  -- ' + detail) if detail and not cond else ''}")


class HeadTripwire(Exception):
    """プロパティ経由で HEAD が飛んだことを検出するための例外。"""


def iphone_video_media(pk="123", code="VID001", n_versions=3):
    """
    Instagram の iphone_struct（Polaris media）の動画版を模す。
    media_type: 1=画像 / 2=動画 / 8=カルーセル
    """
    return {
        "pk": pk,
        "code": code,
        "media_type": 2,
        "taken_at": 1753000000,
        "like_count": 42,
        "comment_count": 7,
        "user": {"pk": 999, "username": "video_user", "full_name": "Video User",
                 "profile_pic_url": "https://scontent.example/pp.jpg"},
        "image_versions2": {"candidates": [
            {"url": "https://scontent.example/VID001_thumb.jpg", "width": 1080}]},
        "video_versions": [
            {"url": f"https://scontent.example/VID001_v{i}.mp4",
             "width": 1080 - i * 200} for i in range(n_versions)],
        "video_duration": 12.5,
        "view_count": 3000,
    }


class NodePost:
    """
    _node を持つ Post のモック。
    instaloader の fake_node と同じキーを持たせる。

    video_url プロパティに触れたら HeadTripwire を投げる。
    実物ではここで CDN への HEAD が走るので、
    「触っていないこと」をテストで固定したい。
    """

    def __init__(self, media, include_video_url=True):
        node = {
            "id": int(media["pk"]),
            "shortcode": media["code"],
            "__typename": "GraphVideo",
            "is_video": True,
            "taken_at_timestamp": media["taken_at"],
            "display_url": media["image_versions2"]["candidates"][0]["url"],
            "iphone_struct": media,
        }
        if include_video_url:
            # instaloader は video_versions[-1] を入れる
            node["video_url"] = media["video_versions"][-1]["url"]
        self._node = node
        self.shortcode = media["code"]
        self.mediaid = int(media["pk"])
        self.owner_username = media["user"]["username"]
        self.caption = "動画のテスト #video"
        self.date_utc = datetime(2026, 7, 20, 12, 0, 0)
        self.likes = media["like_count"]
        self.comments = media["comment_count"]
        self.typename = "GraphVideo"
        self.is_video = True
        self.url = node["display_url"]
        self.head_calls = 0

    @property
    def video_url(self):
        self.head_calls += 1
        raise HeadTripwire("Post.video_url に触れた（実物ではCDNへHEADが飛ぶ）")

    @property
    def caption_hashtags(self):
        return ["video"]

    def get_sidecar_nodes(self):
        return iter([])


def test_video_url_of():
    print("\n[1] video_url_of（追加リクエストなしで動画URLを取る）")

    media = iphone_video_media()
    p = NodePost(media)
    url = igs.video_url_of(p, probe=False)
    check("生ノードの video_url を読む",
          url == "https://scontent.example/VID001_v2.mp4", f"got {url}")
    check("プロパティに触っていない（HEADが飛ばない）", p.head_calls == 0,
          f"head_calls={p.head_calls}")

    # fake_node に video_url が入らなかった場合（video_versions 欠損など）
    p2 = NodePost(media, include_video_url=False)
    url2 = igs.video_url_of(p2, probe=False)
    check("video_url 欠損時は iphone_struct から拾う",
          url2 == "https://scontent.example/VID001_v2.mp4", f"got {url2}")
    check("その場合もプロパティに触らない", p2.head_calls == 0)

    # iphone_struct も無い場合は諦める（プロパティに落ちない）
    p3 = NodePost(media, include_video_url=False)
    del p3._node["iphone_struct"]
    url3 = igs.video_url_of(p3, probe=False)
    check("どちらも無ければ None を返す（追加HTTPに落ちない）", url3 is None,
          f"got {url3}")
    check("諦めるときもプロパティに触らない", p3.head_calls == 0,
          f"head_calls={p3.head_calls}")

    # probe=True は明示的にプロパティを使う
    p4 = NodePost(media, include_video_url=False)
    del p4._node["iphone_struct"]
    try:
        igs.video_url_of(p4, probe=True)
    except HeadTripwire:
        pass    # tripwire は _safe の捕捉対象外なので抜けてくるのが正しい
    check("probe=True のときだけプロパティを使う", p4.head_calls == 1,
          f"head_calls={p4.head_calls}")


def test_extract_media_video():
    print("\n[2] extract_media（単体動画）")

    p = NodePost(iphone_video_media())
    m = igs.extract_media(p)
    check("1件になる", len(m) == 1, f"got {len(m)}")
    check("is_video=True", m[0]["is_video"] is True)
    check("image_url はサムネ",
          m[0]["image_url"] == "https://scontent.example/VID001_thumb.jpg")
    check("video_url が入る",
          m[0]["video_url"] == "https://scontent.example/VID001_v2.mp4")
    check("HEADが飛んでいない", p.head_calls == 0, f"head_calls={p.head_calls}")


def test_post_to_record_video():
    print("\n[3] post_to_record（単体動画）")

    p = NodePost(iphone_video_media())
    r = igs.post_to_record(p)
    check("typename=GraphVideo", r["typename"] == "GraphVideo", r["typename"])
    check("is_video=1", r["is_video"] == 1, str(r["is_video"]))
    check("is_carousel=0", r["is_carousel"] == 0, str(r["is_carousel"]))
    check("shortcode", r["shortcode"] == "VID001")
    check("owner_username", r["owner_username"] == "video_user")
    media = json.loads(r["media_json"])
    check("media_json に1件", len(media) == 1, f"got {len(media)}")
    check("HEADが飛んでいない", p.head_calls == 0, f"head_calls={p.head_calls}")

    # location は動画でも生ノード優先（追加HTTPなし）
    check("location は既定で None", r["location"] is None, str(r["location"]))


def test_owner_from_node_video():
    print("\n[4] owner_from_node（動画投稿でもアイコンが取れる）")
    p = NodePost(iphone_video_media())
    row = igs.owner_from_node(p)
    check("username", row and row.get("username") == "video_user", str(row))
    check("full_name", row and row.get("full_name") == "Video User", str(row))
    check("profile_pic_url あり", bool(row and row.get("profile_pic_url")))


def test_download_ext(dbfile):
    print("\n[5] download_media の拡張子選択")

    class FakeResp:
        status_code = 200
        content = b"binarydata"

        def raise_for_status(self):
            pass

    class FakeSess:
        def __init__(self):
            self.urls = []

        def get(self, url, timeout=None):
            self.urls.append(url)
            return FakeResp()

    conn = db.connect(dbfile)
    db.init_db(conn)
    conn.execute(
        "INSERT INTO posts (shortcode, owner_username, date_utc) VALUES (?,?,?)",
        ("VID001", "video_user", "2026-07-20T12:00:00+00:00"))
    conn.commit()

    media = igs.extract_media(NodePost(iphone_video_media()))

    orig = igs.DOWNLOAD_VIDEOS
    try:
        # 既定（動画本体は落とさない）
        igs.DOWNLOAD_VIDEOS = False
        s = FakeSess()
        igs.download_media(conn, "VID001", media, session_obj=s)
        conn.commit()
        check("DOWNLOAD_VIDEOS=0 ではサムネURLを取りに行く",
              s.urls and s.urls[0].endswith("_thumb.jpg"), str(s.urls))
        row = conn.execute(
            "SELECT local_path, is_video FROM media_index WHERE shortcode='VID001'"
        ).fetchone()
        check("拡張子は .jpg", row["local_path"].endswith(".jpg"), row["local_path"])
        check("is_video フラグは 1 のまま", row["is_video"] == 1)

        # 動画も落とす設定
        conn.execute("DELETE FROM media_index WHERE shortcode='VID001'")
        conn.commit()
        igs.DOWNLOAD_VIDEOS = True
        s2 = FakeSess()
        igs.download_media(conn, "VID001", media, session_obj=s2)
        conn.commit()
        check("DOWNLOAD_VIDEOS=1 では mp4 を取りに行く",
              s2.urls and s2.urls[0].endswith(".mp4"), str(s2.urls))
        row = conn.execute(
            "SELECT local_path FROM media_index WHERE shortcode='VID001'").fetchone()
        check("拡張子は .mp4", row["local_path"].endswith(".mp4"), row["local_path"])
    finally:
        igs.DOWNLOAD_VIDEOS = orig
        conn.close()


def test_sidecar_with_video():
    print("\n[6] カルーセル内の動画（sidecar は追加リクエストなし）")

    nodes = [
        SidecarNode(False, "https://scontent.example/c0.jpg", None),
        SidecarNode(True, "https://scontent.example/c1_thumb.jpg",
                    "https://scontent.example/c1.mp4"),
    ]

    class SidecarPost(NodePost):
        def __init__(self):
            media = iphone_video_media(code="MIX001")
            media["media_type"] = 8
            super().__init__(media)
            self.typename = "GraphSidecar"
            self.is_video = False
            self._node["__typename"] = "GraphSidecar"
            self._nodes = nodes

        def get_sidecar_nodes(self):
            return iter(self._nodes)

    p = SidecarPost()
    m = igs.extract_media(p)
    check("2件になる", len(m) == 2, f"got {len(m)}")
    check("動画ノードの video_url",
          m[1]["video_url"] == "https://scontent.example/c1.mp4", str(m[1]))
    check("画像ノードの video_url は None", m[0]["video_url"] is None)
    check("HEADが飛んでいない", p.head_calls == 0, f"head_calls={p.head_calls}")

    r = igs.post_to_record(p)
    check("is_carousel=1", r["is_carousel"] == 1)
    check("is_video=0（親は動画ではない）", r["is_video"] == 0)


def main():
    tmp = tempfile.mkdtemp(prefix="igray_video_")
    dbfile = os.path.join(tmp, "test.db")
    print(f"test db: {dbfile}")

    test_video_url_of()
    test_extract_media_video()
    test_post_to_record_video()
    test_owner_from_node_video()
    test_download_ext(dbfile)
    test_sidecar_with_video()

    print(f"\n{'=' * 50}")
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("すべて通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
