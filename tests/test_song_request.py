# -*- coding: utf-8 -*-
"""test_song_request.py — 自然语言点歌解析单测。运行：venv python tests/test_song_request.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.music.request import parse  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), name, detail)


def main():
    r = parse("给我来一首晴天")
    check("给+来一首+歌名", r == ("我", "晴天"), f"{r}")

    r = parse("来一首晴天")
    check("来一首+歌名", r == ("", "晴天"), f"{r}")

    r = parse("来首晴天")
    check("来首+歌名", r == ("", "晴天"), f"{r}")

    r = parse("奏乐")
    check("裸奏乐=随机", r == ("", ""), f"{r}")

    r = parse("给大家奏乐")
    check("给XX奏乐=随机", r == ("大家", ""), f"{r}")

    r = parse("奏乐 日语")
    check("奏乐+目录", r == ("", "日语"), f"{r}")

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

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
