# -*- coding: utf-8 -*-
"""core.music.request — 自然语言点歌解析（纯函数，可脱离 astrbot 单测）

句式统一为「[给XX] <动词> [宾语]」，动词族 = 来一首 / 来首 / 奏乐：
  给fk来一首晴天     → ("fk", "晴天")
  来一首晴天         → ("",  "晴天")
  来首F4的歌         → ("",  "F4")      # 宾语尾缀「的歌」被剥离
  给大家奏乐         → ("大家", "")      # 无宾语 = 曲库随机
  奏乐               → ("",  "")         # 随机
  奏乐 日语歌        → ("",  "日语")     # 尾缀「歌」被剥离
  整首/完整点的xx    → wants_full() = True，走完整音乐通道

三个层次各管一件事：
  parse()        句式 → (recipient, target)，只做「是不是点歌」与「给谁」
  target_candidates()  宾语口语后缀逐层剥离，产出候选（目录/歌手/歌名通用）
  resolve_target()     拿真实曲库把候选解析成 (kind, value)，kind ∈
                   random / subdir / artist / song，路由层只做分派

词汇占用刻意收窄：裸「来一首/来首」无宾语不触发（None），
「点歌」「播放」等易与其他音乐插件冲突的词不收。
"""

import re

# ---------------------------------------------------------------- 句式

# 动词族（与日常口语对齐，刻意不收「点歌/播放」以防插件间词汇冲突）
_VERB = r"(?:来一首|来首|奏乐)"

# 给<人> + 动词 + 宾语：人名限长 1~12，防止误伤含该句式的普通长句
_RE_GIVE = re.compile(rf"^给(.{{1,12}}?){_VERB}\s*(.*)$")
# 裸动词 + 宾语
_RE_BARE = re.compile(rf"^({_VERB})\s*(.*)$")

# 「整首 / 完整 / 全曲」修饰 → 走完整音乐通道（不经副歌裁剪）
_RE_FULL = re.compile(r"(整首|完整版|完整|全曲|全篇)")

# 歌名/目录名首尾的语气/结束标点（解析时剔除）；仅剥首尾，不动内部字符
_TRIM_PUNCT = " \u3000。．，,、！!？?～~·'\"“”‘’（）()【】[]《》<>"

# 宾语口语后缀，逐层剥离：长的在前，避免「歌曲」被「歌」抢先剥掉
# 覆盖「X的歌」「X歌曲」「X音乐」「X曲子」「X歌」「X曲」六种说法
TARGET_SUFFIXES = ("的一首", "的一曲", "的歌", "歌曲", "音乐", "曲子", "歌", "曲")

# 泛指宾语（无具体目标）→ 曲库随机，如「来一首歌」
_GENERIC_TARGETS = {"歌", "歌曲", "音乐", "曲子"}


def _clean(raw: str):
    """去首尾空白与首尾标点；剔除后为空返回 None。"""
    t = (raw or "").strip().strip(_TRIM_PUNCT).strip()
    return t or None


# ---------------------------------------------------------------- 宾语候选

def target_candidates(target: str) -> list:
    """宾语候选列表：原名 + 逐层剥离口语后缀的变体，剥到不再变化为止。

    例：「F4的歌」→ [F4的歌, F4]；「日语歌曲」→ [日语歌曲, 日语]；
    「经典音乐」→ [经典音乐, 经典]；「来首歌」→ [来首歌]（剥空则不再产出）。
    原名始终排第一，逐层变体在后——匹配时按顺序取第一个命中的。
    """
    name = _clean(target)
    if not name:
        return []
    cands = [name]
    cur = name
    while True:
        for suf in TARGET_SUFFIXES:
            if cur.endswith(suf) and len(cur) > len(suf):
                cur = cur[:-len(suf)].strip()
                if cur:
                    cands.append(cur)
                break
        else:
            return cands


def wants_full(message: str) -> bool:
    """消息是否要求完整歌曲（「整首XX」「来首完整的XX」）。"""
    return bool(_RE_FULL.search(message or ""))


def parse(message: str):
    """解析自然语言点歌句式。

    返回 (recipient, target)：
      target 非空 → 具体目标（子目录 / 歌手 / 歌名，由 resolve_target 判定）；
      target 为空字符串 → 随机（奏乐 / 给XX奏乐 / 来一首歌 等泛指）；
      不匹配   → None（不是点歌句式，交给语音匹配 / LLM / 其他插件）。
    """
    msg = (message or "").strip()

    m = _RE_GIVE.match(msg)
    if m:
        recipient = m.group(1).strip()
        target = _clean(m.group(2))
        if target is None or target in _GENERIC_TARGETS:
            # 无宾语或泛指（给XX来一首歌）→ 为 XX 随机
            return recipient, ""
        return recipient, target

    m = _RE_BARE.match(msg)
    if m:
        target = _clean(m.group(2))
        if target is not None:
            if target in _GENERIC_TARGETS:
                return "", ""
            return "", target
        # 裸动词无宾语：仅「奏乐」作为随机触发词占用，「来一首/来首」不占用
        if m.group(1) == "奏乐":
            return "", ""
    return None


# ---------------------------------------------------------------- 目标解析

def resolve_target(lib, target: str):
    """把自然语言宾语解析成 (kind, value)。

    kind:
      random  泛指/无宾语 → 曲库随机，value=None
      subdir  命中曲库子目录 → 该目录内随机，value=实际目录名
      artist  命中歌手名 → 该歌手歌曲内随机，value=实际歌手名
      song    兜底按歌名点播（模糊匹配），value=剥离后最干净的宾语

    解析顺序 子目录 → 歌手 → 歌名，命中即止。
    目录与歌手都用「原名 + 逐层剥离后缀」的候选依次尝试，
    所以「随机音乐 奏乐曲」「来首F4的歌」这类口语后缀都能落到正确目标。
    """
    cands = target_candidates(target)
    if not cands or cands[-1] in _GENERIC_TARGETS:
        return "random", None
    for c in cands:
        sub = lib.resolve_subdir(c)
        if sub:
            return "subdir", sub
    # 歌手 vs 歌名的判定顺序：先问曲名，再问歌手。
    # 「喜欢你」既是歌手「偏偏喜欢你」的一部分、也是歌名，两者在字符串层面同形
    # （修饰 + 查询），无法靠相似度区分；靠的是"命中的到底是曲名还是歌手名"——
    # match() 的 by_title 标记就是为此存在。
    song = cands[-1]
    hits, top_is_title = lib.match(song, limit=1, with_source=True)
    if hits and top_is_title:
        return "song", song
    for c in cands:
        art = lib.resolve_artist(c)
        if art:
            return "artist", art
    # 曲名与歌手都没命中，但仍有近似候选（兜底档）→ 照样当歌名点播，
    # 保证「总能返回一首歌」，而不是轻易报"没找到"
    return "song", song
