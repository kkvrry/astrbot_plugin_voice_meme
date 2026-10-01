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

    def random(self, subdir: str | None = None) -> str:
        """随机选一首，避开最近播放过的；可限定子目录。曲库为空返回 None。"""
        pool = self.files
        if subdir:
            prefix = os.path.join(self.root, subdir) + os.sep
            pool = [p for p in pool if p.startswith(prefix)]
        pool = [p for p in pool if p not in self.recent]
        if not pool:
            full = [p for p in self.files
                    if not subdir or p.startswith(
                        os.path.join(self.root, subdir) + os.sep)]
            pool = full
        if not pool:
            return None
        path = random.choice(pool)
        self.recent.append(path)
        return path


def _quick_ratio(q: str, stem: str) -> float:
    """轻量相似度：difflib SequenceMatcher.quick_ratio，够用且便宜。"""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, q, stem).quick_ratio()
