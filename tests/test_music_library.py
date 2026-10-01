# -*- coding: utf-8 -*-
"""test_music_library.py — 曲库扫描/匹配/随机不重复 单测。运行：venv python tests/test_music_library.py"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from xgs_voice.music.library import MusicLibrary  # noqa: E402

TMP = tempfile.mkdtemp(prefix="music_lib_test_")
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), name, detail)


def main():
    sub = os.path.join(TMP, "华语")
    os.makedirs(sub)
    files = [
        os.path.join(TMP, "G.E.M.邓紫棋 - 泡沫.mp3"),
        os.path.join(TMP, "Beyond - 喜欢你.flac"),
        os.path.join(TMP, "F4 - 流星雨.mp3"),
        os.path.join(sub, "周杰伦 - 晴天.mp3"),   # 子目录也应被扫到
        os.path.join(TMP, "封面.jpg"),             # 非音频要被过滤
    ]
    for f in files:
        open(f, "wb").close()

    lib = MusicLibrary(TMP)
    check("递归扫描且过滤非音频", len(lib) == 4, f"count={len(lib)}")

    m = lib.match("泡沫")
    check("包含匹配命中", len(m) == 1 and m[0][1] == 3 and m[0][0].endswith("泡沫.mp3"),
          f"{[(os.path.basename(p), s) for p, s in m]}")

    m = lib.match("G.E.M.邓紫棋-泡沫")
    check("归一化后精确匹配优先", m and m[0][1] == 6, f"score={m[0][1] if m else None}")

    m = lib.match("晴天")
    check("子目录歌曲可匹配", len(m) == 1 and "晴天" in m[0][0])

    m = lib.match("邓紫棋泡沫")   # 与"泡沫"近似（歌手+歌名，quick_ratio≈0.77）
    check("模糊匹配兜底", len(m) == 1 and "泡沫" in m[0][0],
          f"{[os.path.basename(p) for p, _ in m]}")

    check("无匹配返回空", lib.match("不存在的歌") == [])

    picks = {lib.random() for _ in range(4)}
    check("随机选曲且近期不重复", len(picks) == 4, f"unique={len(picks)}")

    single = MusicLibrary(os.path.join(TMP, "empty_dir"))
    os.makedirs(os.path.join(TMP, "empty_dir"), exist_ok=True)
    check("空目录 random 返回 None", single.random() is None)
    check("空目录 match 返回空", single.match("晴天") == [])

    shutil.rmtree(TMP, ignore_errors=True)
    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"\n{len(RESULTS) - n_fail}/{len(RESULTS)} passed")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
