# -*- coding: utf-8 -*-
"""
music_library.py — 音乐曲库（无第三方依赖，可独立测试）

职责：递归扫描音乐目录、模糊匹配歌名/歌手、随机选曲（近期不重复）。
不负责分析/裁剪（那是 clip.py 的事），也不依赖 ffmpeg。

匹配三要素：
  1. 归一化 fold()：NFKC → 假名转罗马音（ディスコ ≈ disco）→ 去空格标点，
     容忍全角/半角、大小写、空格、错别字；
  2. 文件名按「歌手 - 歌名」拆分（曲库普遍命名），匹配时以歌名为主、歌手为辅，
     支持「邓紫棋的泡沫」「G.E.M.邓紫棋 - 泡沫」两种写法；
  3. 分层打分：精确 > 前缀 > 包含 > 相似度（difflib），多首时取最高分。
"""

import os
import re
import random
import time
import unicodedata
from collections import deque
from difflib import SequenceMatcher

# ---------------------------------------------------------------- 假名罗马音
# 曲库常见片假名/平假名歌名（如「ディスコ」），与用户口语「disco」按相似度即可命中。
# 只覆盖日常音乐词用得到的音节，长音「ー」直接丢弃（相似度容忍）。
# 「歌手 - 歌名」分隔符
_KANA = {
    "ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o",
    "カ": "ka", "キ": "ki", "ク": "ku", "ケ": "ke", "コ": "ko",
    "ガ": "ga", "ギ": "gi", "グ": "gu", "ゲ": "ge", "ゴ": "go",
    "サ": "sa", "シ": "shi", "ス": "su", "セ": "se", "ソ": "so",
    "ザ": "za", "ジ": "ji", "ズ": "zu", "ゼ": "ze", "ゾ": "zo",
    "タ": "ta", "チ": "chi", "ツ": "tsu", "テ": "te", "ト": "to",
    "ダ": "da", "ヂ": "ji", "ヅ": "zu", "デ": "de", "ド": "do",
    "ナ": "na", "ニ": "ni", "ヌ": "nu", "ネ": "ne", "ノ": "no",
    "ハ": "ha", "ヒ": "hi", "フ": "fu", "ヘ": "he", "ホ": "ho",
    "バ": "ba", "ビ": "bi", "ブ": "bu", "ベ": "be", "ボ": "bo",
    "パ": "pa", "ピ": "pi", "プ": "pu", "ペ": "pe", "ポ": "po",
    "マ": "ma", "ミ": "mi", "ム": "mu", "メ": "me", "モ": "mo",
    "ヤ": "ya", "ユ": "yu", "ヨ": "yo",
    "ラ": "ra", "リ": "ri", "ル": "ru", "レ": "re", "ロ": "ro",
    "ワ": "wa", "ヰ": "i", "ヱ": "e", "ヲ": "o", "ン": "n",
    "ァ": "a", "ィ": "i", "ゥ": "u", "ェ": "e", "ォ": "o",
    "ャ": "ya", "ュ": "yu", "ョ": "yo", "ヴ": "vu",
}
_KANA_MAP = {}
for _k, _v in _KANA.items():
    _KANA_MAP[_k] = _v
    _KANA_MAP[chr(ord(_k) - 0x60)] = _v      # 平假名（片假名 codepoint - 0x60）

# 两字组合读音特殊（Unicode 拆分会读错）：ディ→di 而非 dei、ウィ→wi 而非 ui
_KANA_DIGRAPH = {
    "ディ": "di", "デュ": "dyu", "ティ": "ti", "トゥ": "tu", "ドゥ": "du",
    "ウィ": "wi", "ウェ": "we", "ウォ": "wo", "ヴァ": "va", "ツァ": "tsa",
}

# 常见外来词的中文音译 → 拉丁写法：让「白金迪斯科」与「白金ディスコ」互相靠拢
_SYNONYMS = {
    "迪斯科": "disco", "迪思科": "disco", "迪扣": "disco", "的士高": "disco",
    "狄斯科": "disco", "摇滚": "rock", "洛客": "rock", "爵士": "jazz",
    "钢琴": "piano", "混音": "remix", "现场": "live", "演唱会": "live",
    "纯音乐": "instrumental", "原声": "acoustic", "动漫": "anime",
    "轻音乐": "instrumental", "抒情": "ballad",
}

# 「歌手 - 歌名」分隔符（半角/全角/破折号/下划线），长音「ー」不作分隔
_NAME_SEP = re.compile(r"\s*[-–—－]\s*|\s*_\s*")
# 查询内歌手与歌名的分隔（的/空格/顿号/斜杠/&/中点/冒号）
_TOKEN_SEP = re.compile(r"[的\s、,，/／&＆·・:：]+")


def fold(s: str) -> str:
    """基础归一化：NFKC → 假名转罗马音 → 只保留字母数字与 CJK。

    「Ｄｉｓｃｏ」「ディスコ」「dis co」「Dis-Co」都归一为同一串，可直接比相似度。
    只用于目录名/排除目录等结构比较——不含同义词，避免污染这些语义。
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).lower()
    for k, v in _KANA_DIGRAPH.items():        # 组合读音优先于逐字拆分
        if k in s:
            s = s.replace(k, "\x00" + v + "\x00")
    out = []
    for ch in s:
        if ch == "\x00":
            continue
        v = _KANA_MAP.get(ch)
        if v is not None:
            out.append(v)
        elif ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            out.append(ch)
        # 其余（空格/标点/长音/符号）丢弃
    return "".join(out)


def fold_match(s: str) -> str:
    """匹配用归一化：fold 基础上再把中文音译统一成拉丁写法。

    「白金迪斯科」与「白金ディスコ」都归一为「白金disco…」，互相可匹配；
    只用于歌名/歌手匹配，不用于目录比较。
    """
    t = fold(s)
    for k, v in _SYNONYMS.items():
        if k in t:
            t = t.replace(k, v)
    return t


def split_name(stem: str):
    """把「歌手 - 歌名」拆成 (歌手, 歌名)；无分隔符时歌手为空。

    按**最后一个**分隔符拆：歌手名本身可能带连字符（m-flo、F-4），
    用第一个分隔符会把「m-flo - 白金ディスコ」拆成歌手 m、歌名 flo - …
    例：「G.E.M.邓紫棋 - 泡沫」→「G.E.M.邓紫棋", "泡沫"。
    """
    stem = (stem or "").strip()
    last = None
    for m in _NAME_SEP.finditer(stem):
        last = m
    if last is not None and stem[last.end():].strip():
        return stem[:last.start()].strip(), stem[last.end():].strip()
    return "", stem


def _ratio(a: str, b: str) -> float:
    """相似度：difflib SequenceMatcher.ratio（比 quick_ratio 更准，字符串很短够快）。"""
    return SequenceMatcher(None, a, b).ratio()


def _coverage(a: str, b: str) -> float:
    """查询侧覆盖率：a 的字符有多少能按序出现在 b 中。

    用于「查询短、目标长」的场景——歌手名比歌名长得多时 ratio 会被长度差拉低，
    而错别字只错一两个字符，覆盖率仍接近 1（「邓紫琪」vs「G.E.M.邓紫棋」≈0.67）。
    """
    if not a:
        return 0.0
    matched = sum(bl.size for bl in SequenceMatcher(None, a, b).get_matching_blocks())
    return matched / len(a)


class MusicLibrary:
    AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg",
                  ".ape", ".wma", ".opus"}
    SCAN_TTL = 30  # 秒。网络盘（SMB）目录扫描有开销，带 TTL 缓存

    def __init__(self, root: str, recent_size: int = 32,
                 exclude_dirs: list | None = None):
        self.root = os.path.abspath(root)
        self.recent = deque(maxlen=recent_size)  # 最近播放，随机时避开
        # 排除的子目录名（按目录名匹配，任意层级）
        self.exclude_dirs = {self._norm(d) for d in (exclude_dirs or []) if d}
        self._files = []          # 缓存的文件列表
        self._scanned_at = 0.0

    # ------------------------------------------------------------ 扫描

    def _rescan(self):
        files = []
        for dirpath, dirs, names in os.walk(self.root):
            # 剪枝：跳过被排除的子目录（不进入）
            dirs[:] = [d for d in dirs
                       if self._norm(d) not in self.exclude_dirs]
            for name in names:
                if os.path.splitext(name)[1].lower() in self.AUDIO_EXTS:
                    files.append(os.path.join(dirpath, name))
        self._files = files
        self._scanned_at = time.time()

    @property
    def files(self) -> list:
        """曲库全部音频文件（带 TTL 缓存的递归扫描）。"""
        if not self._files or time.time() - self._scanned_at > self.SCAN_TTL:
            try:
                self._rescan()
            except OSError:
                return []
        return self._files

    def __len__(self):
        return len(self.files)

    # ------------------------------------------------------------ 匹配

    @staticmethod
    def _norm(s: str) -> str:
        """归一化（见模块级 fold）：去空格/标点/大小写/全角、假名转罗马音。"""
        return fold(s)

    def _score(self, q: str, fq: list, ft: str, fa: str, fstem: str):
        """严格档打分。返回 (score, by_title)；未命中返回 (0.0, False)。

        by_title 标记分数是否来自**曲名**——resolve_target 需要它区分
        「用歌手名搜索」（来一首F4 → by_title=False）与「真的在点歌名」。
        """
        # 查询带歌手限定时，歌手对不上的直接淘汰（避免「邓紫棋的泡沫」命中别人歌）
        if fq and not all(self._soft_hit(a, fa) or self._soft_hit(a, fstem)
                          for a in fq):
            return 0.0, False
        if ft == q:
            score, by_title = 5.5, True
        elif ft.startswith(q):
            score, by_title = 4.5, True
        elif q in ft:
            score, by_title = 4.0, True
        elif fa and fa == q:
            score, by_title = 3.2, False   # 直接以歌手名搜索（「来一首周杰伦」）
        elif fstem == q:
            score, by_title = 5.0, True    # 「歌手 - 歌名」整名精确
        elif q in fstem:
            score, by_title = 2.6, True
        elif fa and len(q) >= 3 and _coverage(q, fa) >= 0.62:
            score, by_title = 3.0, False   # 歌手名错别字（「邓紫琪」→G.E.M.邓紫棋）
        else:
            return 0.0, False
        if score and fq:
            score += 1.5          # 歌手限定命中，加分压过同名歧义
        return score, by_title

    def _score_loose(self, q: str, fq: list, ft: str, fa: str, fstem: str) -> float:
        """兜底档打分：子串与相似度近似，命中即返回正分（保证「总能返回一首歌」）。

        优先级：歌手限定下的曲名子串 > 整名子串 > 曲名相似度 > 歌手名相似度。
        分数统一压在严格档之下（最高 1.9），排序时永远不会抢走精确命中。
        """
        # 有歌手限定时不参与兜底——限定不符就不是它，宁可让严格档报"没找到"
        if fq:
            return 0.0
        # 查询 ≥2 字且是曲名/整名的子串：「白金」→ 白金ディスコ，「喜欢你」→ …喜欢你
        if len(q) >= 2:
            if q in ft:
                return 1.8
            if q in fstem:
                return 1.5
        # 存在子串关系时 ratio 天然偏高（已被上面的子串档覆盖，这里只做兜底），
        # 歌手名近似同理排除——否则「喜欢你」会被歌手「偏偏喜欢你」劫持
        r = max(_ratio(q, ft), _ratio(q, fstem))
        if fa and (q in fa or fa in q):
            r = max(r, 0.0)
        # 短查询（2~3 字）阈值放宽到 0.45：错一字/两字仍应给候选
        thr = 0.45 if len(q) <= 3 else 0.55
        if r >= thr:
            return 0.5 + r
        # 曲名里含查询的多数字符（顺序可乱）：「流星雨歌」→「流星雨」
        if len(q) >= 3 and _coverage(q, ft) >= 0.8:
            return 0.9
        # 歌手名近似：「邓紫琪」这类错字，且曲名与查询有一定重合。
        # 查询是歌手名的子串时不算（那是歌名检索，见 resolve_artist 同处注释）
        if (fa and len(q) >= 3 and q not in fa and fa not in q
                and _coverage(q, fa) >= 0.62):
            return 0.8
        return 0.0

    @staticmethod
    def _soft_hit(a: str, b: str) -> bool:
        """歌手限定判定：归一化后相等/互含/高相似即算命中。"""
        if not a or not b:
            return False
        return a == b or a in b or b in a or _ratio(a, b) >= 0.6

    def match(self, query: str, limit: int = 5, with_source: bool = False) -> list:
        """歌名/歌手模糊匹配，返回 [(path, score), ...] 按分数降序。

        with_source=True 时额外返回 (results, top_is_title)：最高分是否来自曲名
        而非歌手名，供 request.resolve_target 区分「点歌名」与「按歌手随机」。

        支持「泡沫」「邓紫棋的泡沫」「邓紫棋 泡沫」「白金disco（→白金ディスコ）」
        「白金」（子串兜底）「白金 disco」（多段合并）「F4」（歌手名）等写法；
        一次遍历三档打分：严格档 → 多段合并 → 低阈值兜底，
        只要曲库里有近似歌曲就返回结果，不轻易让用户得到"没找到"。
        """
        raw = (query or "").strip()
        if not raw:
            return []
        toks = [t for t in _TOKEN_SEP.split(raw) if t]
        title_q = toks[-1]
        fq = [fold_match(t) for t in toks[:-1]]
        fq = [a for a in fq if a]
        q = fold_match(title_q)
        if not q:
            return []
        # 多段查询合并形态：「白金 disco」→ 白金disco。用户分两段打，
        # 但曲名里没有空格，此时前段不是歌手限定（严格档会全灭），
        # 用合并形态再打一次档，避免"分段输入就查不到"。
        merged = fold_match(_TOKEN_SEP.sub("", raw)) if len(toks) > 1 else ""
        fq_merged = []          # 合并形态不再带歌手限定

        scored = []
        for path in self.files:
            stem = os.path.splitext(os.path.basename(path))[0]
            artist, title = split_name(stem)
            ft, fa = fold_match(title), fold_match(artist)
            if not ft and not fa:
                continue
            fstem = fold_match(stem)
            score, by_title = self._score(q, fq, ft, fa, fstem)
            # 合并形态不携带歌手限定，否则「白金 disco」会把唯一的候选判死
            if score <= 0 and merged:
                score, by_title = self._score(merged, fq_merged, ft, fa, fstem)
            if score <= 0:
                alt = merged if (merged and fq) else q
                score = self._score_loose(alt, fq_merged if fq else fq,
                                          ft, fa, fstem)
                by_title = score > 0     # 兜底档一律按歌名处理
            if score > 0:
                scored.append((path, score, by_title))
        scored.sort(key=lambda x: (-x[1], len(os.path.basename(x[0])),
                                   os.path.basename(x[0])))
        top = scored[:limit]
        if with_source:
            return [(p, s) for p, s, _ in top],                    bool(top and top[0][2])
        return [(p, s) for p, s, _ in top]

    def display_name(self, path: str) -> str:
        """展示用歌名：去掉「歌手 - 」前缀，只留曲名。"""
        _, title = split_name(os.path.splitext(os.path.basename(path))[0])
        return title or os.path.splitext(os.path.basename(path))[0]

    # ------------------------------------------------------------ 歌手

    def artists(self) -> list:
        """曲库内全部歌手名（来自「歌手 - 歌名」命名，去重排序）。"""
        seen = []
        for p in self.files:
            a, _ = split_name(os.path.splitext(os.path.basename(p))[0])
            if a and a not in seen:
                seen.append(a)
        return sorted(seen)

    def resolve_artist(self, name: str) -> str | None:
        """把输入解析为曲库内实际歌手名。

        三级回落：归一化精确 → 歌手名的超集（输入带修饰后缀）→ 近似
        （相似度 ≥0.7，或覆盖率 ≥0.62 容忍错别字，如「邓紫琪」）。

        互含只处理「输入是歌手名的扩展」这一个方向（「Beyond乐队」「F4天团」）；
        反方向不做——输入更短、只是歌手名的子串时（「喜欢你」⊂「偏偏喜欢你」），
        那几乎总是歌名检索需求，应交给 match()，否则歌名永远被长歌手名劫持。
        """
        target = fold_match(name)
        if not target:
            return None
        arts = self.artists()
        for a in arts:
            if fold_match(a) == target:
                return a
        for a in arts:
            fa = fold_match(a)
            if fa in target and len(target) <= len(fa) * 1.5:
                return a
        best, best_r, best_cov_name, best_cov = None, 0.0, None, 0.0
        for a in arts:
            fa = fold_match(a)
            r = _ratio(target, fa)
            # 存在子串关系时 ratio 天然偏高（「喜欢你」vs「偏偏喜欢你」=0.75，
            # 会被误判成同名歌手），这类查询一律不算歌手近似，交给 match()
            # 按歌名检索。真正的错别字（邓紫琪 vs G.E.M.邓紫棋）没有子串关系，
            # 仍按覆盖率命中。
            cov = _coverage(target, fa)
            if r > best_r:
                best, best_r = a, r
            if cov > best_cov:
                best_cov_name, best_cov = a, cov
        # 阈值口径与 match() 的歌手错别字档一致：≥3 字按覆盖率 0.62 容忍错字
        # （返回覆盖率最高的那位歌手），短词仍要求 0.7 相似度，
        # 否则「王」会命中所有含「王」的歌手
        if len(target) >= 3 and best_cov >= 0.62:
            return best_cov_name
        return best if best_r >= 0.7 else None    # 否则要 0.7 以上才算同名

    # ------------------------------------------------------------ 随机

    def subdirs(self) -> list:
        """曲库根目录下含音频文件的子目录名（去重排序，供「随机音乐 <目录>」提示）。"""
        seen = []
        for p in self.files:
            rel = os.path.relpath(os.path.dirname(p), self.root)
            if rel not in (".", ".."):
                top = rel.split(os.sep)[0]
                if top not in seen:
                    seen.append(top)
        return sorted(seen)

    def resolve_subdir(self, name: str) -> str | None:
        """把用户输入的目录名解析为曲库内实际子目录（归一化精确匹配，含嵌套路径）。"""
        if not name:
            return None
        target = self._norm(name)
        for top in self.subdirs():
            if self._norm(top) == target:
                return top
        return None

    def songs(self, subdir: str | None = None, artist: str | None = None) -> list:
        """按子目录/歌手筛选歌曲（歌手按归一化后的曲名前缀精确匹配）。"""
        out = []
        fa_target = fold_match(artist) if artist else None
        prefix = os.path.join(self.root, subdir) + os.sep if subdir else None
        for p in self.files:
            if prefix and not p.startswith(prefix):
                continue
            if fa_target:
                a, _ = split_name(os.path.splitext(os.path.basename(p))[0])
                if fold_match(a) != fa_target:
                    continue
            out.append(p)
        return out

    def random(self, subdir: str | None = None, artist: str | None = None) -> str | None:
        """随机选一首，避开最近播放过的；可限定子目录与歌手。曲库为空返回 None。"""
        pool = [p for p in self.songs(subdir, artist) if p not in self.recent]
        if not pool:
            pool = self.songs(subdir, artist)
        if not pool:
            return None
        path = random.choice(pool)
        self.recent.append(path)
        return path
