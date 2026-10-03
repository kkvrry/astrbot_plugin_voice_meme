# -*- coding: utf-8 -*-
"""test_music_library.py — 曲库扫描/匹配/随机不重复 单测。运行：venv python tests/test_music_library.py"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.music.library import (MusicLibrary, fold, fold_match,  # noqa: E402
                                split_name,
                                subdir_candidates)

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
        os.path.join(TMP, "m-flo - 白金ディスコ.mp3"),   # 片假名歌名
        os.path.join(sub, "周杰伦 - 晴天.mp3"),   # 子目录也应被扫到
        os.path.join(TMP, "封面.jpg"),             # 非音频要被过滤
    ]
    for f in files:
        open(f, "wb").close()

    lib = MusicLibrary(TMP)
    check("递归扫描且过滤非音频", len(lib) == 5, f"count={len(lib)}")

    m = lib.match("泡沫")
    check("曲名精确匹配", len(m) == 1 and m[0][1] == 5.5 and m[0][0].endswith("泡沫.mp3"),
          f"{[(os.path.basename(p), s) for p, s in m]}")

    m = lib.match("G.E.M.邓紫棋-泡沫")
    check("整名（歌手+歌名）归一化精确匹配", m and m[0][1] == 5.0,
          f"score={m[0][1] if m else None}")

    m = lib.match("晴天")
    check("子目录歌曲可匹配", len(m) == 1 and "晴天" in m[0][0])

    m = lib.match("邓紫棋泡沫")   # 与"泡沫"近似（歌手+歌名连写）
    check("模糊匹配兜底", len(m) == 1 and "泡沫" in m[0][0],
          f"{[os.path.basename(p) for p, _ in m]}")

    check("无匹配返回空", lib.match("不存在的歌") == [])

    picks = {lib.random() for _ in range(4)}
    check("随机选曲且近期不重复", len(picks) == 4, f"unique={len(picks)}")

    single = MusicLibrary(os.path.join(TMP, "empty_dir"))
    os.makedirs(os.path.join(TMP, "empty_dir"), exist_ok=True)
    check("空目录 random 返回 None", single.random() is None)
    check("空目录 match 返回空", single.match("晴天") == [])

    # ---- 归一化 / 歌手拆分 / 歌手维度 ----
    check("fold 全角与空格归一", fold("Ｄｉｓ-Ｃｏ") == "disco", f"{fold('Ｄｉｓ-Ｃｏ')}")
    check("fold 假名罗马化", fold("ディスコ").startswith("dis"), f"{fold('ディスコ')}")
    check("拆分歌手-歌名", split_name("G.E.M.邓紫棋 - 泡沫") == ("G.E.M.邓紫棋", "泡沫"),
          f"{split_name('G.E.M.邓紫棋 - 泡沫')}")
    check("无分隔符时歌手为空", split_name("告白气球") == ("", "告白气球"),
          f"{split_name('告白气球')}")

    m = lib.match("白金disco")     # 文件名是片假名「白金ディスコ」
    check("口语 disco 命中片假名歌名", m and "ディスコ" in m[0][0],
          f"{[os.path.basename(p) for p, _ in m]}")
    m = lib.match("白金迪斯科")     # 中文音译同样命中
    check("中文音译命中片假名歌名", m and "ディスコ" in m[0][0],
          f"{[os.path.basename(p) for p, _ in m]}")
    check("音译归一：迪斯科→disco", fold_match("迪斯科") == "disco", f"{fold_match('迪斯科')}")
    m = lib.match("邓紫琪")          # 错别字
    check("错别字模糊命中", m and "泡沫" in m[0][0],
          f"{[os.path.basename(p) for p, _ in m]}")
    m = lib.match("邓紫棋")
    check("按歌手名可匹配", m and "泡沫" in m[0][0],
          f"{[os.path.basename(p) for p, _ in m]}")
    m = lib.match("邓紫棋的泡沫")
    check("「歌手的歌」式匹配", m and m[0][0].endswith("泡沫.mp3") and m[0][1] >= 7,
          f"{[(os.path.basename(p), s) for p, s in m]}")
    m = lib.match("F4 流星雨")
    check("「歌手 歌名」空格式匹配", m and "流星雨" in m[0][0],
          f"{[os.path.basename(p) for p, _ in m]}")

    check("artists 列出歌手", "G.E.M.邓紫棋" in lib.artists(), f"{lib.artists()}")
    check("resolve_artist 模糊命中", lib.resolve_artist("邓紫棋") == "G.E.M.邓紫棋",
          f"{lib.resolve_artist('邓紫棋')}")
    check("resolve_artist 未命中返回 None", lib.resolve_artist("林俊杰") is None)
    check("resolve_artist 乐队后缀可识别", lib.resolve_artist("Beyond乐队") == "Beyond",
          f"{lib.resolve_artist('Beyond乐队')}")
    check("resolve_artist 支持带连字符的歌手", lib.resolve_artist("m-flo") == "m-flo",
          f"{lib.resolve_artist('m-flo')}")
    pick = lib.random(artist="F4")
    check("限定歌手随机", pick and "F4" in pick, f"{pick}")
    check("display_name 去掉歌手前缀", lib.display_name(lib.match("泡沫")[0][0]) == "泡沫",
          f"{lib.display_name(lib.match('泡沫')[0][0])}")

    # ---- 排除目录 / 子目录随机 ----
    excl_dir = os.path.join(TMP, "Playlists")
    light_dir = os.path.join(TMP, "轻音乐")
    os.makedirs(excl_dir)
    os.makedirs(light_dir)
    open(os.path.join(excl_dir, "歌单缓存 - A.mp3"), "wb").close()
    open(os.path.join(light_dir, "班得瑞 - 安静.mp3"), "wb").close()

    lib2 = MusicLibrary(TMP, exclude_dirs=["Playlists"])
    check("排除目录不被扫描", len(lib2) == 6, f"count={len(lib2)}")
    check("排除目录歌曲不可匹配", lib2.match("歌单缓存") == [])

    lib3 = MusicLibrary(TMP)
    check("未配置排除时包含该目录", len(lib3) == 7, f"count={len(lib3)}")

    check("subdirs 列出顶层目录", set(lib2.subdirs()) >= {"华语", "轻音乐"},
          f"{lib2.subdirs()}")
    check("resolve_subdir 归一化命中", lib2.resolve_subdir("轻 音乐") == "轻音乐")
    check("resolve_subdir 未知名返回 None", lib2.resolve_subdir("不存在") is None)

    pick = lib2.random("轻音乐")
    check("限定子目录随机", pick is not None and "轻音乐" in pick,
          f"{pick}")
    pick = lib2.random("Playlists")
    check("限定排除目录随机为空池兜底", pick is None or "Playlists" not in pick,
          f"{pick}")

    # ---- 自然语言点歌的子目录候选名 ----
    check("候选名：原名优先", subdir_candidates("晴天") == ["晴天"],
          f"{subdir_candidates('晴天')}")
    check("候选名：剥「歌」", subdir_candidates("日语歌") == ["日语歌", "日语"],
          f"{subdir_candidates('日语歌')}")
    check("候选名：剥「歌曲」", subdir_candidates("经典歌曲") == ["经典歌曲", "经典"],
          f"{subdir_candidates('经典歌曲')}")
    check("候选名：剥「音乐」", subdir_candidates("经典音乐") == ["经典音乐", "经典"],
          f"{subdir_candidates('经典音乐')}")
    check("候选名：连剥不剥到空", subdir_candidates("歌") == ["歌"],
          f"{subdir_candidates('歌')}")

    shutil.rmtree(TMP, ignore_errors=True)
    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"\n{len(RESULTS) - n_fail}/{len(RESULTS)} passed")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
