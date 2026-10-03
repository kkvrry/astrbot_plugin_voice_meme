# -*- coding: utf-8 -*-
"""test_song_request.py — 自然语言点歌解析单测。运行：venv python tests/test_song_request.py"""

import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.music.request import (parse, target_candidates, wants_full,  # noqa: E402
                               resolve_target)
from core.music.library import MusicLibrary  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), name, detail)


def make_lib():
    """临时曲库：子目录「日语」+ 三位歌手，覆盖目录/歌手/歌名三条解析路径。"""
    tmp = tempfile.mkdtemp()
    sub = os.path.join(tmp, "日语")
    os.makedirs(sub)
    files = [
        os.path.join(sub, "米津玄師 - Lemon.mp3"),
        os.path.join(tmp, "m-flo - 白金ディスコ.mp3"),
        os.path.join(tmp, "G.E.M.邓紫棋 - 泡沫.mp3"),
        os.path.join(tmp, "F4 - 流星雨.mp3"),
        os.path.join(tmp, "偏偏喜欢你 - 喜欢你.mp3"),
        os.path.join(tmp, "Beyond乐队 - 海阔天空.mp3"),
    ]
    for f in files:
        open(f, "wb").close()
    return tmp, MusicLibrary(tmp)


def main():
    r = parse("给我来一首晴天")
    check("给+来一首+歌名", r == ("我", "晴天"), f"{r}")

    r = parse("来一首晴天")
    check("来一首+歌名", r == ("", "晴天"), f"{r}")

    r = parse("来首晴天")
    check("来首+歌名", r == ("", "晴天"), f"{r}")

    r = parse("给fk来首F4的歌")
    check("给XX来首XX的歌 → (XX, XX的歌)",
          r == ("fk", "F4的歌"), f"{r}")

    r = parse("奏乐")
    check("裸奏乐=随机", r == ("", ""), f"{r}")

    r = parse("给大家奏乐")
    check("给XX奏乐=随机", r == ("大家", ""), f"{r}")

    r = parse("奏乐 日语")
    check("奏乐+目录", r == ("", "日语"), f"{r}")

    r = parse("来一首歌")
    check("泛指「歌」=随机", r == ("", ""), f"{r}")

    r = parse("来首音乐")
    check("泛指「音乐」=随机", r == ("", ""), f"{r}")

    r = parse("给fk来一首歌")
    check("给XX+泛指=为XX随机", r == ("fk", ""), f"{r}")

    r = parse("给fk奏乐 日语歌")
    check("给XX奏乐+目录", r == ("fk", "日语歌"), f"{r}")

    r = parse("来一首 喜欢你！")
    check("歌名尾部标点剔除", r == ("", "喜欢你"), f"{r}")

    r = parse("  给我来一首 晴天  ")
    check("首尾空白容忍", r == ("我", "晴天"), f"{r}")

    r = parse("给某人来一首")
    check("给XX+动词 无宾语=随机（动词族统一规则）", r == ("某人", ""), f"{r}")

    r = parse("来一首")
    check("裸来一首不占用", r is None, f"{r}")

    r = parse("来首")
    check("裸来首不占用", r is None, f"{r}")

    r = parse("点歌")
    check("「点歌」不收，留给其他插件", r is None, f"{r}")

    r = parse("播放晴天")
    check("「播放」不收", r is None, f"{r}")

    r = parse("给你的约定一直记在心里")
    check("含「给」的长句不误伤", r is None, f"{r}")

    r = parse("给一个名字特别特别特别特别特别长的人来一首晴天")
    check("人名超长不匹配", r is None, f"{r}")

    r = parse("音乐 晴天")
    check("指令格式不受影响", r is None, f"{r}")

    r = parse("随机音乐 古风")
    check("随机指令不受影响", r is None, f"{r}")

    # ---- 宾语候选：口语后缀逐层剥离 ----
    check("「F4的歌」候选含 F4", "F4" in target_candidates("F4的歌"),
          f"{target_candidates('F4的歌')}")
    check("「日语歌曲」剥到 日语",
          target_candidates("日语歌曲")[-1] == "日语",
          f"{target_candidates('日语歌曲')}")
    check("「经典音乐」剥到 经典",
          target_candidates("经典音乐")[-1] == "经典",
          f"{target_candidates('经典音乐')}")
    check("纯泛指「歌」不产出空候选", target_candidates("歌") == ["歌"],
          f"{target_candidates('歌')}")

    # ---- 完整音乐修饰 ----
    check("「整首晴天」要完整", wants_full("整首晴天") is True)
    check("「来一首完整的泡沫」要完整", wants_full("来一首完整的泡沫") is True)
    check("普通点歌不要完整", wants_full("来一首晴天") is False)

    # ---- resolve_target：目录 → 歌手 → 歌名 ----
    tmp, lib = make_lib()
    try:
        check("目录命中", resolve_target(lib, "日语歌") == ("subdir", "日语"),
              f"{resolve_target(lib, '日语歌')}")
        check("歌手命中", resolve_target(lib, "F4") == ("artist", "F4"),
              f"{resolve_target(lib, 'F4')}")
        check("歌手+口语后缀", resolve_target(lib, "F4的歌") == ("artist", "F4"),
              f"{resolve_target(lib, 'F4的歌')}")
        check("歌手错别字", resolve_target(lib, "邓紫琪") == ("artist", "G.E.M.邓紫棋"),
              f"{resolve_target(lib, '邓紫琪')}")
        check("泛指=随机", resolve_target(lib, "歌") == ("random", None),
              f"{resolve_target(lib, '歌')}")
        check("空宾语=随机", resolve_target(lib, "") == ("random", None))
        kind, val = resolve_target(lib, "白金")
        check("「白金」落到歌名（子串兜底在 match 里）", kind == "song", f"{kind}")
        m = lib.match(val) if kind == "song" else []
        check("「白金」能返回白金ディスコ",
              m and "ディスコ" in m[0][0], f"{[os.path.basename(p) for p, _ in m]}")
        m = lib.match("白金 disco")
        check("「白金 disco」分段输入也命中",
              m and "ディスコ" in m[0][0], f"{[os.path.basename(p) for p, _ in m]}")

        # ---- 回归防护：短查询不得被长歌手名反向包含吃掉 ----
        got = resolve_target(lib, "喜欢你")
        check("「喜欢你」应判歌名而非歌手「偏偏喜欢你」",
              got == ("song", "喜欢你"), got)
        m = lib.match("喜欢你")
        check("「喜欢你」精确命中同名歌",
              m and m[0][0].endswith("- 喜欢你.mp3"), f"{[os.path.basename(p) for p, _ in m]}")
        check("「Beyond乐队」仍能解析为歌手",
              resolve_target(lib, "Beyond乐队") == ("artist", "Beyond乐队"),
              f"{resolve_target(lib, 'Beyond乐队')}")
        # 查无此歌时兜底档不能硬塞：完全无关的长串仍应返回空
        check("无关长串不硬塞候选", lib.match("超级不存在的一首长歌名xyz") == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
