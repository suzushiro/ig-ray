"""
ig-ray / config.py

環境変数の読み取りを1箇所にまとめる。

改名（insta-ray → ig-ray）にあたり、**`INSTA_RAY_*` も引き続き読む**。
既存の `.env` をそのまま使い続けられるようにするため。
新旧が両方あるときは新しい `IG_RAY_*` を優先する。
"""

import os

PREFIXES = ("IG_RAY_", "INSTA_RAY_")


def env(name, default=None):
    """
    env("CACHE") → IG_RAY_CACHE → INSTA_RAY_CACHE → default の順に探す。
    name は接頭辞を除いた部分だけを渡す。
    """
    for p in PREFIXES:
        v = os.environ.get(p + name)
        if v is not None and v != "":
            return v
    return default


def env_bool(name, default=False):
    v = env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(env(name, default))
    except (TypeError, ValueError):
        return default


def env_float(name, default):
    try:
        return float(env(name, default))
    except (TypeError, ValueError):
        return default


def db_path():
    """
    DBファイルのパス。

    既定は ig_ray.db だが、**旧名 insta_ray.db が既にあればそちらを使う**。
    改名でDBを見失うのが一番まずいので、勝手に新規作成しない。
    移行したい場合は手で mv すればよい（そのとき -wal / -shm も一緒に）。
    """
    explicit = env("DB")
    if explicit:
        return explicit

    data_dir = env("DATA_DIR", "/data")
    new = os.path.join(data_dir, "ig_ray.db")
    old = os.path.join(data_dir, "insta_ray.db")
    if not os.path.exists(new) and os.path.exists(old):
        return old
    return new
