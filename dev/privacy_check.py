"""
ig-ray / dev/privacy_check.py

**公開するファイルに環境固有の情報が混ざっていないか**を機械的に検査する。

    python3 dev/privacy_check.py

README や .env.example にうっかり実IP・ホスト名を書いてしまうのを防ぐのが目的。
そういう情報は `NOTES.md`（`.gitignore` 済み）に置く。

git があれば追跡対象ファイルだけを見る（= 実際に公開されるもの）。
無ければ `.gitignore` を自前で解釈して同じ集合を作る。
"""

import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  {mark}  {name}{('  -- ' + detail) if detail and not cond else ''}")


# 公開ファイルに出てはいけないパターン。
#
# **ここには汎用パターンしか書かない。**
# ホスト名・回線事業者名・鍵の名前のようなサイト固有の語をここに直接書くと、
# 「検査ツール自身が要注意語を公開する」という本末転倒になる（実際に一度やった）。
# サイト固有の語は `dev/privacy_words.txt`（.gitignore 済み）に置く。
#
# 正規表現のエスケープのおかげで、これらのパターンは**自分自身にはマッチしない**
# （ソース上は `192\.168\.` のように間にバックスラッシュが入るため）。
FORBIDDEN = [
    # プライベートIP（実際のLAN構成が読み取れてしまう）
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "プライベートIP(192.168.x.x)"),
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "プライベートIP(10.x.x.x)"),
    (r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "プライベートIP(172.16-31.x.x)"),
    # 鍵ファイル名（汎用形。ed25519 や rsa の既定名を拾う）
    (r"\bid_[a-z0-9]{2,}\b", "SSH鍵ファイル名"),
    # セッションの実値
    (r"sessionid\s*=\s*[A-Za-z0-9%]{10,}", "sessionid の実値"),
    (r"csrftoken\s*=\s*[A-Za-z0-9]{10,}", "csrftoken の実値"),
]

# ドキュメント用レンジ以外のグローバルIPは、書かれていたらまず環境固有の情報。
GLOBAL_IP = re.compile(r"\b(?!0)(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")

DOC_OR_LOCAL = (
    ("203", "0", "113"), ("198", "51", "100"), ("192", "0", "2"),
)


def _is_public_ip(octets):
    """グローバルIPか（プライベート・ループバック・ドキュメント用は除く）。"""
    try:
        a, b, c, d = (int(x) for x in octets)
    except ValueError:
        return False
    if not all(0 <= x <= 255 for x in (a, b, c, d)):
        return False
    if (a, b, c) in [tuple(int(y) for y in t) for t in DOC_OR_LOCAL]:
        return False
    if a in (0, 10, 127) or (a == 192 and b == 168) or \
       (a == 172 and 16 <= b <= 31) or (a == 169 and b == 254) or a >= 224:
        return False
    return True


def load_extra_words():
    """
    サイト固有の要注意語を読む（`dev/privacy_words.txt`・任意・git管理外）。

    1行1パターン（正規表現）。`#` から始まる行はコメント。
    ここにホスト名や回線事業者名を書いておくと、README に書き戻したときに気づける。
    ファイルが無ければ汎用パターンだけで検査する。
    """
    path = os.path.join(ROOT, "dev", "privacy_words.txt")
    if not os.path.exists(path):
        return [], False
    words = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            words.append((line, "要注意語（privacy_words.txt）"))
    return words, True

# ドキュメント用に予約されたIPレンジ（RFC 5737）は例外扱い。
# テストや例示ではこちらを使う。
EXEMPT_LINE = [
    # User-Agent のバージョン文字列は "124.0.0.0" のようにIPと同じ形になる
    re.compile(r"(?i)(user[-_ ]?agent|mozilla|chrome|safari|version)"),
    re.compile(r"203\.0\.113\."),
    re.compile(r"198\.51\.100\."),
    re.compile(r"192\.0\.2\."),
    # 127.0.0.1 / localhost は構成情報にならない
    re.compile(r"127\.0\.0\.1"),
    # プレースホルダとして書いてある行
    re.compile(r"<[^>]*IP[^>]*>"),
]

# バイナリ等、検査対象外
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".db"}


def tracked_files():
    """公開される（= git 追跡対象の）ファイル一覧。"""
    try:
        out = subprocess.run(
            ["git", "-C", ROOT, "ls-files"],
            capture_output=True, text=True, timeout=20)
        if out.returncode == 0 and out.stdout.strip():
            return [os.path.join(ROOT, p) for p in out.stdout.splitlines()]
    except (OSError, subprocess.SubprocessError):
        pass

    # git が無い場合のフォールバック（.gitignore を素朴に解釈）
    ignored = set()
    gi = os.path.join(ROOT, ".gitignore")
    if os.path.exists(gi):
        for line in open(gi, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                ignored.add(line.rstrip("/"))

    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in ignored and d != ".git"
                       and d != "node_modules"]
        for fn in filenames:
            if fn in ignored or fn.endswith(".pyc") or fn.endswith(".zip"):
                continue
            files.append(os.path.join(dirpath, fn))
    return files


def test_no_leaks(files):
    print("\n[1] 公開ファイルに環境固有情報が無いか")
    extra, has_wordlist = load_extra_words()
    patterns = FORBIDDEN + extra
    if has_wordlist:
        print(f"  （dev/privacy_words.txt から {len(extra)} 語を追加）")
    else:
        print("  （dev/privacy_words.txt は無し。汎用パターンのみで検査）")

    hits = []
    for path in files:
        if os.path.splitext(path)[1].lower() in SKIP_EXT:
            continue
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        rel = os.path.relpath(path, ROOT)
        for i, line in enumerate(text.splitlines(), 1):
            if any(x.search(line) for x in EXEMPT_LINE):
                continue
            for pat, label in patterns:
                if re.search(pat, line):
                    hits.append((rel, i, label, line.strip()[:70]))
            for m in GLOBAL_IP.finditer(line):
                if _is_public_ip(m.groups()):
                    hits.append((rel, i, "グローバルIP", line.strip()[:70]))

    check("要注意パターンの混入なし", not hits,
          "; ".join(f"{f}:{n} {lab}" for f, n, lab, _ in hits[:5]))
    if hits:
        print("\n  検出:")
        for f, n, lab, snippet in hits:
            print(f"    {f}:{n}  [{lab}]  {snippet}")


def test_checker_is_clean(files):
    print("\n[2] 検査ツール自身が要注意語を持っていないか")
    # サイト固有の語を FORBIDDEN に直書きすると、
    # 検査ツールがそれを公開してしまう（実際に一度やった）。
    me = os.path.join(ROOT, "dev", "privacy_check.py")
    text = open(me, encoding="utf-8").read()
    extra, _ = load_extra_words()
    bad = [lab for pat, lab in extra if re.search(pat, text)]
    check("privacy_words.txt の語が検査ツールに書かれていない", not bad, str(bad[:3]))
    check("privacy_words.txt が .gitignore 済み",
          re.search(r"^dev/privacy_words\.txt\s*$",
                    open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read(),
                    re.M) is not None)


def test_notes_ignored():
    print("\n[3] 作業メモが公開されない設定になっているか")
    gi_path = os.path.join(ROOT, ".gitignore")
    check(".gitignore がある", os.path.exists(gi_path))
    if not os.path.exists(gi_path):
        return
    gi = open(gi_path, encoding="utf-8").read()
    check("NOTES.md が .gitignore に入っている",
          re.search(r"^NOTES\.md\s*$", gi, re.M) is not None)
    check(".env が .gitignore に入っている",
          re.search(r"^\.env\s*$", gi, re.M) is not None)
    check("data/ が .gitignore に入っている",
          re.search(r"^data/\s*$", gi, re.M) is not None)

    check("NOTES.example.md（雛形）がある",
          os.path.exists(os.path.join(ROOT, "NOTES.example.md")))

    try:
        out = subprocess.run(["git", "-C", ROOT, "ls-files", "NOTES.md"],
                             capture_output=True, text=True, timeout=20)
        if out.returncode == 0:
            check("NOTES.md が git 追跡対象になっていない",
                  not out.stdout.strip(), out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass


def test_readme_points_to_notes():
    print("\n[4] README から作業メモへの導線")
    readme = os.path.join(ROOT, "README.md")
    check("README.md がある", os.path.exists(readme))
    if not os.path.exists(readme):
        return
    text = open(readme, encoding="utf-8").read()
    check("NOTES.md に触れている", "NOTES.md" in text)
    check("方針が書いてある", "git管理外" in text or ".gitignore" in text)


def test_env_example_has_no_real_values():
    print("\n[5] .env.example に実値が入っていないか")
    p = os.path.join(ROOT, ".env.example")
    check(".env.example がある", os.path.exists(p))
    if not os.path.exists(p):
        return
    text = open(p, encoding="utf-8").read()
    # 有効行（コメントでない行）に生IPが無いこと
    bad = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.search(r"=\s*https?://\d{1,3}(\.\d{1,3}){3}", stripped) or \
           re.search(r"=\s*\d{1,3}(\.\d{1,3}){3}\s*$", stripped):
            if "127.0.0.1" not in stripped:
                bad.append((i, stripped))
    check("有効な設定行に生IPが無い", not bad, str(bad[:3]))
    check("プロキシ設定はコメントアウトされている",
          not re.search(r"^HTTPS?_PROXY=\S", text, re.M))


def main():
    files = tracked_files()
    print(f"検査対象: {len(files)} ファイル")

    test_no_leaks(files)
    test_checker_is_clean(files)
    test_notes_ignored()
    test_readme_points_to_notes()
    test_env_example_has_no_real_values()

    print(f"\n{'=' * 50}")
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        print("\n※ 環境固有の情報は NOTES.md（git管理外）へ移してください。")
        return 1
    print("すべて通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
