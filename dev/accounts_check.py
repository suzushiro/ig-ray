"""
insta-ray / dev/accounts_check.py

seed_accounts.py の検証。ネットワーク不要。

    python3 dev/accounts_check.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import db                    # noqa: E402
import seed_accounts as sa   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  -- ' + detail) if detail and not cond else ''}")


class Args:
    def __init__(self, **kw):
        self.note = None
        self.__dict__.update(kw)


def test_normalize():
    print("\n[1] ユーザー名の正規化")
    cases = [
        ("minamina_flyover", "minamina_flyover"),
        ("@foo", "foo"),
        ("  bar  ", "bar"),
        ("BazQux", "bazqux"),
        ("https://www.instagram.com/someone/", "someone"),
        ("https://instagram.com/someone", "someone"),
        ("instagram.com/someone/?hl=ja", "someone"),
        ("with.dots", "with.dots"),
        ("with_under", "with_under"),
    ]
    for raw, want in cases:
        got = sa.normalize(raw)
        check(f"{raw!r} -> {want!r}", got == want, f"got {got!r}")

    bad = ["", "   ", "bad name", "has/slash", "a" * 31, "日本語", "has!bang"]
    for raw in bad:
        check(f"不正として弾く: {raw[:20]!r}", sa.normalize(raw) is None,
              f"got {sa.normalize(raw)!r}")


def test_crud(dbfile):
    print("\n[2] 追加 / 一覧 / 有効無効 / 削除")
    conn = db.connect(dbfile)
    db.init_db(conn)

    rc = sa.cmd_add(conn, Args(usernames=["alpha", "@Beta",
                                          "https://www.instagram.com/gamma/",
                                          "bad name"], note="テスト"))
    names = {r["username"] for r in conn.execute("SELECT username FROM accounts")}
    check("3件登録された", names == {"alpha", "beta", "gamma"}, str(names))
    check("不正名は登録されない", "bad name" not in names)

    note = conn.execute("SELECT note FROM accounts WHERE username='alpha'").fetchone()["note"]
    check("note が入る", note == "テスト", str(note))

    # 冪等
    sa.cmd_add(conn, Args(usernames=["alpha"]))
    n = conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
    check("重複追加で増えない", n == 3, f"got {n}")

    # note を上書きしない
    sa.cmd_add(conn, Args(usernames=["alpha"], note="別のノート"))
    note2 = conn.execute("SELECT note FROM accounts WHERE username='alpha'").fetchone()["note"]
    check("既存の note を壊さない", note2 == "テスト", str(note2))

    # 有効無効
    check("初期は全部有効", set(db.enabled_accounts(conn)) == {"alpha", "beta", "gamma"})
    sa.cmd_disable(conn, Args(usernames=["beta"]))
    check("disable が効く", set(db.enabled_accounts(conn)) == {"alpha", "gamma"},
          str(db.enabled_accounts(conn)))
    check("無効でも行は残る",
          conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 3)
    sa.cmd_enable(conn, Args(usernames=["beta"]))
    check("enable で戻る", "beta" in db.enabled_accounts(conn))

    # 大文字/URL でも既存にマッチする
    sa.cmd_disable(conn, Args(usernames=["https://www.instagram.com/BETA/"]))
    check("URL/大文字でも既存にマッチ", "beta" not in db.enabled_accounts(conn),
          str(db.enabled_accounts(conn)))

    # 未登録への操作
    rc = sa.cmd_disable(conn, Args(usernames=["nonexistent"]))
    check("未登録の disable は非ゼロ終了", rc != 0, f"rc={rc}")

    conn.commit()
    conn.close()


def test_remove_keeps_posts(dbfile):
    print("\n[3] remove は投稿データを消さない")
    conn = db.connect(dbfile)

    conn.execute(
        "INSERT INTO posts (shortcode, owner_username, date_utc) VALUES (?,?,?)",
        ("XYZ1", "gamma", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()

    sa.cmd_remove(conn, Args(usernames=["gamma"]))
    gone = conn.execute(
        "SELECT COUNT(*) c FROM accounts WHERE username='gamma'").fetchone()["c"]
    kept = conn.execute(
        "SELECT COUNT(*) c FROM posts WHERE owner_username='gamma'").fetchone()["c"]
    check("accounts からは消える", gone == 0)
    check("posts は残る（キャッシュ側は巻き込まない）", kept == 1, f"got {kept}")

    conn.commit()
    conn.close()


def test_import(dbfile):
    print("\n[4] テキストからの一括取り込み")
    conn = db.connect(dbfile)

    tmp = tempfile.mktemp(suffix=".txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# コメント行\n")
        f.write("delta\n")
        f.write("epsilon   # 行末コメント\n")
        f.write("\n")
        f.write("   \n")
        f.write("@Zeta\n")

    sa.cmd_import(conn, Args(path=tmp))
    names = {r["username"] for r in conn.execute("SELECT username FROM accounts")}
    check("delta が入る", "delta" in names)
    check("行末コメントを除去", "epsilon" in names, str(names))
    check("@と大文字を正規化", "zeta" in names, str(names))
    check("コメント行は無視", not any("コメント" in n for n in names))

    rc = sa.cmd_import(conn, Args(path="/nonexistent/file.txt"))
    check("存在しないファイルは非ゼロ終了", rc == 1, f"rc={rc}")

    conn.commit()
    conn.close()


def test_list_empty():
    print("\n[5] 空のときの list")
    tmp = tempfile.mktemp(suffix=".db")
    conn = db.connect(tmp)
    db.init_db(conn)
    rc = sa.cmd_list(conn, Args())
    check("空でも落ちない", rc == 0)
    conn.close()


def main():
    dbfile = os.path.join(tempfile.mkdtemp(prefix="instaray_acc_"), "t.db")
    print(f"test db: {dbfile}")

    test_normalize()
    test_crud(dbfile)
    test_remove_keeps_posts(dbfile)
    test_import(dbfile)
    test_list_empty()

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
