"""
ig-ray / dev/css_check.py

テンプレートのCSSの健全性を検査する。ネットワーク不要。

    python3 dev/css_check.py

**未定義のCSS変数を使うと、その宣言ごと無効になる。**
`background: var(--bg-card)` の `--bg-card` が未定義だと背景が塗られず、
モーダルの後ろが透けて読めなくなる（2026-09 に実際に起きた。
`_style.html` は `--bg` と `--surface` しか定義していないのに、
ダイアログ・カード・ボタンの9箇所が `--bg-card` を参照していた）。

ブラウザは未定義変数を黙って無視するので、目視でも気づきにくい。
ここで機械的に潰す。
"""

import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
TPL = os.path.join(ROOT, "app", "templates")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  {mark}  {name}{('  -- ' + detail) if detail and not cond else ''}")


def templates():
    return sorted(f for f in os.listdir(TPL) if f.endswith(".html"))


def read(fn):
    return open(os.path.join(TPL, fn), encoding="utf-8").read()


def defined_vars():
    """`:root` や `[data-theme=...]` で定義されている変数名。"""
    text = read("_style.html")
    return {m.group(1) for m in re.finditer(r"^\s*(--[a-z0-9-]+)\s*:", text, re.M)}


def used_vars():
    """`var(--x)` で参照されている変数名 → 使っているファイル。"""
    out = {}
    for fn in templates():
        for m in re.finditer(r"var\(\s*(--[a-z0-9-]+)", read(fn)):
            out.setdefault(m.group(1), set()).add(fn)
    return out


def test_no_undefined():
    print("\n[1] 未定義のCSS変数を使っていないか")
    defined = defined_vars()
    used = used_vars()
    check("変数が定義されている", len(defined) > 0, str(len(defined)))

    # var(--x, フォールバック) は未定義でも動くので許す
    fallback_ok = set()
    for fn in templates():
        for m in re.finditer(r"var\(\s*(--[a-z0-9-]+)\s*,", read(fn)):
            fallback_ok.add(m.group(1))

    missing = {v: fs for v, fs in used.items()
               if v not in defined and v not in fallback_ok}
    check("未定義の変数を参照していない", not missing,
          "; ".join(f"{v} ({', '.join(sorted(fs))})"
                    for v, fs in sorted(missing.items())))
    if missing:
        print("\n  検出:")
        for v, fs in sorted(missing.items()):
            print(f"    {v}  ← {', '.join(sorted(fs))}")


def test_theme_parity():
    print("\n[2] ライト／ダークで変数が揃っているか")
    text = read("_style.html")

    def block(pattern):
        m = re.search(pattern + r"\s*\{(.*?)\}", text, re.S)
        if not m:
            return set()
        return {x.group(1) for x in re.finditer(r"(--[a-z0-9-]+)\s*:", m.group(1))}

    light = block(r":root")
    dark = block(r'\[data-theme="dark"\]')

    check("ライトの定義がある", bool(light), str(len(light)))
    check("ダークの定義がある", bool(dark), str(len(dark)))
    # 片方だけにあると、そのテーマでだけ背景が消える
    check("ダークに漏れがない", not (light - dark), str(sorted(light - dark)))
    check("ライトに漏れがない", not (dark - light), str(sorted(dark - light)))


def test_opaque_surfaces():
    print("\n[3] 重ねて表示する面が不透明か")
    text = read("_style.html")

    # モーダルやカードの地の色が半透明だと後ろが透ける。
    # 暗幕（.dlg-wrap）とオーバーレイは半透明でよい。
    for name in ("--bg-card", "--bg", "--surface"):
        vals = re.findall(rf"{name}\s*:\s*([^;]+);", text)
        check(f"{name} が定義されている", bool(vals), "未定義")
        bad = [v.strip() for v in vals
               if "rgba" in v or "transparent" in v or "hsla" in v]
        check(f"{name} が不透明", not bad, str(bad))

    check(".dlg に背景指定がある",
          re.search(r"\.dlg\s*\{[^}]*background", text, re.S) is not None)
    check(".dlg-wrap に暗幕がある",
          re.search(r"\.dlg-wrap\s*\{[^}]*background:\s*rgba", text, re.S) is not None)


def test_modal_stacking():
    print("\n[4] モーダルの重ね順")
    text = read("_style.html")
    m = re.search(r"\.dlg-wrap\s*\{(.*?)\}", text, re.S)
    check(".dlg-wrap がある", m is not None)
    if not m:
        return
    body = m.group(1)
    check("position: fixed", "position: fixed" in body)
    z = re.search(r"z-index:\s*(\d+)", body)
    check("z-index がある", z is not None)

    # ライトボックスより手前でないと、モーダルが裏に隠れる
    lb = re.search(r"#lb\s*\{(.*?)\}", text, re.S)
    if lb and z:
        lz = re.search(r"z-index:\s*(\d+)", lb.group(1))
        if lz:
            check("ライトボックスより手前", int(z.group(1)) > int(lz.group(1)),
                  f"dlg={z.group(1)} lb={lz.group(1)}")


def main():
    print(f"検査対象: {len(templates())} テンプレート")
    test_no_undefined()
    test_theme_parity()
    test_opaque_surfaces()
    test_modal_stacking()

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
