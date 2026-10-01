# -*- coding: utf-8 -*-
"""core.music.request — 自然语言点歌解析：「给XX来一首YY」

纯函数无副作用，便于脱离 astrbot 环境单测（tests/test_song_request.py）。
"""

import re

# 给<人>来一首<歌名>：人名限长 1~12，防止误伤含该句式的普通长句
_SONG_REQUEST_RE = re.compile(r"^给(.{1,12}?)来一首(.+)$")

# 歌名首尾的语气/结束标点（解析时剔除，如「给我来一首晴天！」）；
# 仅剥离首尾，不动歌名内部字符
_TRIM_PUNCT = " \u3000。．，,、！!？?～~·'\"“”‘’（）()【】[]《》<>"


def parse(message: str):
    """解析「给XX来一首YY」句式。

    返回 (recipient, song)；不匹配时返回 None。
    song 去除首尾空白与首尾标点，剔除后为空则视为不匹配。
    """
    m = _SONG_REQUEST_RE.match((message or "").strip())
    if not m:
        return None
    song = m.group(2).strip().strip(_TRIM_PUNCT).strip()
    if not song:
        return None
    return m.group(1).strip(), song
