# -*- coding: utf-8 -*-
"""
music_library.py — 音乐曲库（无第三方依赖，可独立测试）

职责：递归扫描音乐目录、歌名排名匹配、随机选曲（近期不重复）。
不负责分析/裁剪（那是 song_clip 的事），也不依赖 ffmpeg。
"""

import os
import re
import random
import time
from collections import deque


class MusicLibrary:
    AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg",
                  ".ape", ".wma", ".opus"}
    SCAN_TTL = 30  # 秒。网络盘（SMB）目录扫描有开销，带 TTL 缓存

    def __init__(self, root: str, recent_size: int = 32):
        self.root = os.path.abspath(root)
        self.recent = deque(maxlen=recent_size)  # 最近播放，随机时避开
        self._files = []          # 缓存的文件列表
        self._scanned_at = 0.0

    # ------------------------------------------------------------ 扫描

    def _rescan(self):
        files = []
        for dirpath, _dirs, names in os.walk(self.root):
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
        """归一化：去空格/标点、转小写，只留字母数字与 CJK。"""
        return re.sub(r"[^0-9a-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", "",
                      s.lower())

    def match(self, query: str, limit: int = 5) -> list:
        """
        歌名匹配，返回 [(path, score), ...] 按分数降序。
        计分：文件名归一化后 精确等于(6) > 前缀(4) > 包含(3) > difflib 相似(1~2)。
        """
        q = self._norm(query)
        if not q:
            return []
        scored = []
        for path in self.files:
            stem = self._norm(os.path.splitext(os.path.basename(path))[0])
            if not stem:
                continue
            if stem == q:
                score = 6
            elif stem.startswith(q):
                score = 4
            elif q in stem:
                score = 3
            else:
                ratio = _quick_ratio(q, stem)
                score = ratio if ratio >= 0.6 else 0
            if score > 0:
                scored.append((path, score))
        scored.sort(key=lambda x: (-x[1], len(os.path.basename(x[0])),
                                   os.path.basename(x[0])))
        return scored[:limit]

    # ------------------------------------------------------------ 随机

    def random(self) -> str:
        """随机选一首，避开最近播放过的；曲库为空返回 None。"""
        pool = [p for p in self.files if p not in self.recent]
        if not pool:
            pool = self.files
        if not pool:
            return None
        path = random.choice(pool)
        self.recent.append(path)
        return path


def _quick_ratio(q: str, stem: str) -> float:
    """轻量相似度：difflib SequenceMatcher.quick_ratio，够用且便宜。"""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, q, stem).quick_ratio()
