# -*- coding: utf-8 -*-
"""core.music.request — 自然语言点歌解析

统一识别「[给XX] <动词> [YY]」句式，动词族 = 来一首 / 来首 / 奏乐：
  给fk来一首晴天   → ("fk", "晴天")
  来一首晴天       → ("",  "晴天")
  来首晴天         → ("",  "晴天")
  给大家奏乐       → ("大家", "")   # 无宾语 = 曲库随机
  奏乐             → ("",  "")      # 随机
  奏乐 日语        → ("",  "日语")

词汇占用刻意收窄：裸「来一首/来首」无宾语不触发（None），
「点歌」「播放」等易与其他音乐插件冲突的词不收。
纯函数无副作用，可脱离 astrbot 单测（tests/test_song_request.py）。
"""

import re

# 动词族（与日常口语对齐，刻意不收「点歌/播放」以防插件间词汇冲突）
_VERB = r"(?:来一首|来首|奏乐)"

# 给<人> + 动词 + 宾语：人名限长 1~12，防止误伤含该句式的普通长句
_RE_GIVE = re.compile(rf"^给(.{{1,12}}?){_VERB}\s*(.*)$")
# 裸动词 + 宾语
_RE_BARE = re.compile(rf"^({_VERB})\s*(.*)$")

# 歌名/目录名首尾的语气/结束标点（解析时剔除）；仅剥首尾，不动内部字符
_TRIM_PUNCT = " \u3000。．，,、！!？?～~·'\"“”‘’（）()【】[]《》<>"


def _clean_target(raw: str):
    """清洗宾语：去首尾空白与首尾标点，剔除后为空返回 None。"""
    t = raw.strip().strip(_TRIM_PUNCT).strip()
    return t or None


# 泛指宾语（无具体目标）→ 曲库随机，如「来一首歌」「来首音乐」
_GENERIC_TARGETS = {"歌", "歌曲", "音乐", "曲子"}


def parse(message: str):
    """解析自然语言点歌句式。

    返回 (recipient, target)：
      target 非空 → 按歌名/子目录处理；
      target 为空字符串 → 随机（奏乐 / 给XX奏乐 / 来一首歌 等泛指）；
      不匹配 → None。
    """
    msg = (message or "").strip()

    m = _RE_GIVE.match(msg)
    if m:
        recipient = m.group(1).strip()
        target = _clean_target(m.group(2))
        if target is None or target in _GENERIC_TARGETS:
            # 无宾语或泛指（给XX来一首歌）→ 为 XX 随机
            return recipient, ""
        return recipient, target

    m = _RE_BARE.match(msg)
    if m:
        target = _clean_target(m.group(2))
        if target is not None:
            if target in _GENERIC_TARGETS:
                return "", ""
            return "", target
        # 裸动词无宾语：仅「奏乐」作为随机触发词占用，「来一首/来首」不占用
        if m.group(1) == "奏乐":
            return "", ""
    return None
