# -*- coding: utf-8 -*-
"""test_song_request.py — 自然语言点歌「给XX来一首YY」解析单测。运行：venv python tests/test_song_request.py"""

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
    check("基础句式", r == ("我", "晴天"), f"{r}")

    r = parse("给大家来一首泡沫")
    check("人名为大家", r == ("大家", "泡沫"), f"{r}")

    r = parse("给fk来一首G.E.M.邓紫棋-泡沫")
    check("人名与含符号歌名", r == ("fk", "G.E.M.邓紫棋-泡沫"), f"{r}")

    r = parse("给我来一首晴天！")
    check("歌名尾部标点剔除", r == ("我", "晴天"), f"{r}")

    r = parse("  给我来一首 晴天  ")
    check("首尾空白容忍", r == ("我", "晴天"), f"{r}")

    r = parse("给某人来一首")
    check("缺歌名不匹配", r is None, f"{r}")

    r = parse("来一首晴天")
    check("无「给」不匹配", r is None, f"{r}")

    r = parse("给你的约定一直记在心里")
    check("含「给」的长句不误伤", r is None, f"{r}")

    r = parse("给一个名字特别特别特别特别特别长的人来一首晴天")
    check("人名超长不匹配", r is None, f"{r}")

    r = parse("音乐 晴天")
    check("指令格式不受影响", r is None, f"{r}")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
