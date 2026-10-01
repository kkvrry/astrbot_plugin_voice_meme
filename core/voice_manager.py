# -*- coding: utf-8 -*-
"""voice_manager.py — 语音库扫描与匹配（角色索引、关键词/模糊匹配、随机选择）"""

import os
import re
import random
import hashlib
import difflib

from astrbot.api import logger

class VoiceManager:
    # 支持的音频格式（m4a 同样支持，发送前会经 ffmpeg 转码为 WAV）
    AUDIO_EXTS = ('.mp3', '.wav', '.m4a')

    def __init__(self, base_dir, default_lib="", min_keyword_len=2, extra_libs=None):
        """
        base_dir: 插件目录。
        default_lib: 默认语音库目录（兜底）。
        min_keyword_len: 关键词最短长度，过滤过短的词避免误触发（默认 2）。
        extra_libs: 额外语音库目录列表，每项可为一个库目录（其下为角色文件夹）
                   或含多个库的大目录（绝对路径或相对插件目录），在自动识别之外追加。

        语音库大目录固定为 插件目录/voice/，其下每个一级文件夹是一个语音库。
        """
        self.base_dir = base_dir
        self.default_lib = default_lib
        self.min_keyword_len = max(1, int(min_keyword_len or 2))
        self.extra_libs = [str(x).strip() for x in (extra_libs or []) if str(x).strip()]
        self.categories = {}  # 语音库名 -> {角色名: [音频路径]}（dict 保持插入序）
        self.category_order = []  # 语音库名有序列表
        self.role_map = {}  # 角色名 -> [音频路径]（跨库展平索引）
        self.role_category = {}  # 角色名 -> 语音库名
        self.file_to_role = {}  # 音频路径 -> (语音库名, 角色名)
        self.keyword_map = []  # List[(keyword, full_path)]
        self._bigram_index = {}  # bigram -> [keyword_map 下标]（模糊匹配粗筛）
        self.last_played = {}  # Hero -> last_file_path (for random selection)
        self._sorted_roles = []  # 按长度降序排列的角色名缓存
        self._all_files_cache = []  # 所有音频的扁平列表缓存
        self.lib_signature = ""  # 音频库内容签名（用于列表图片缓存失效判断）
        self.scan()

    @staticmethod
    def _is_audio(fname: str) -> bool:
        return fname.lower().endswith(VoiceManager.AUDIO_EXTS)

    @staticmethod
    def _sort_files(files) -> list:
        """按文件名数字前缀排序（无数字前缀的排后面）"""
        _digit_re = re.compile(r'^(\d+)')
        try:
            return sorted(
                files,
                key=lambda x: int(_digit_re.match(x).group(1)) if _digit_re.match(x) else 999
            )
        except Exception:
            return sorted(files)

    @staticmethod
    def _bigrams(s: str) -> set:
        """生成字符串的相邻二元组（bigram），用于模糊匹配粗筛"""
        s = s.lower()
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}

    def _add_role(self, category, role, role_dir, files):
        """登记一个角色及其音频，同时构建关键词映射"""
        full_paths = [os.path.join(role_dir, f) for f in files]
        self.categories.setdefault(category, {})[role] = full_paths
        if category not in self.category_order:
            self.category_order.append(category)
        for p in full_paths:
            self.file_to_role[p] = (category, role)
            name_without_ext = os.path.splitext(os.path.basename(p))[0]
            match = re.match(r'^(\d+)[_.\-\s]+(.+)$', name_without_ext)
            keyword = match.group(2) if match else name_without_ext
            # 过滤过短关键词，避免「杀」「死」等单字词误触发
            if keyword and len(keyword) >= self.min_keyword_len:
                self.keyword_map.append((keyword, p))

    def scan(self):
        """扫描音频目录，构建索引（支持多语音库）"""
        self.categories = {}
        self.category_order = []
        self.file_to_role = {}
        self.keyword_map = []
        self._bigram_index = {}
        self.role_map = {}
        self.role_category = {}

        roots = self._discover_audio_roots()
        if not roots:
            logger.error(f"[通用语音] 未发现任何语音库，请检查音频目录: {self.base_dir}")
            return

        for root in roots:
            self._scan_root(root)

        # 展平为兼容索引（重名时保留先扫描到的库，保证 sgs_voices 优先）
        for cat, roles in self.categories.items():
            for role, paths in roles.items():
                if role in self.role_map:
                    logger.warning(
                        f"[通用语音] 角色名「{role}」在多个语音库中重复，"
                        f"纯角色名匹配将使用库「{self.role_category[role]}」，"
                        f"可用「库名+角色名」精确点播（如「{cat}{role}」）"
                    )
                    continue
                self.role_map[role] = paths
                self.role_category[role] = cat

        self._sorted_roles = sorted(self.role_map.keys(), key=len, reverse=True)
        self._all_files_cache = [
            (f, role) for role, files in self.role_map.items() for f in files
        ]

        # 构建 bigram 索引（模糊匹配粗筛用）
        for idx, (kw, _) in enumerate(self.keyword_map):
            for bg in self._bigrams(kw):
                self._bigram_index.setdefault(bg, []).append(idx)

        # 计算音频库内容签名（列表图片缓存失效判断）
        sig_parts = []
        for paths in self.role_map.values():
            for p in paths:
                try:
                    st = os.stat(p)
                    sig_parts.append(f"{p}|{st.st_mtime_ns}|{st.st_size}")
                except OSError:
                    continue
        self.lib_signature = hashlib.md5('|'.join(sorted(sig_parts)).encode()).hexdigest()

        logger.info(
            f"[通用语音] 扫描完成: {len(self.category_order)} 个语音库"
            f"（{', '.join(self.category_order)}），{len(self.role_map)} 个角色，"
            f"{len(self.keyword_map)} 个关键词"
        )

    def _discover_audio_roots(self) -> list:
        """确定要扫描的语音库目录列表。

        组合规则（按扫描顺序，先扫描的库在角色重名时优先）：
        1. voice/ 下的一级文件夹（自动检测）；
        2. extra_libs 额外语音库目录列表，逐项追加（去重）；
        3. 全部为空时兜底 default_lib。
        """
        roots = []

        # 1. 默认大目录 voice/：其下每个含角色目录的一级文件夹视为语音库
        voice_root = os.path.join(self.base_dir, "voice")
        if os.path.isdir(voice_root):
            try:
                entries = sorted(os.listdir(voice_root))
            except OSError:
                entries = []
            for name in entries:
                if name.startswith('.'):
                    continue
                p = os.path.join(voice_root, name)
                if os.path.isdir(p) and self._has_role_dirs(p):
                    roots.append(p)

        # 2. 额外语音库目录列表：每项可为单个库目录，或含多个库的大目录
        seen = {os.path.normcase(os.path.abspath(p)) for p in roots}
        for item in self.extra_libs:
            p = item if os.path.isabs(item) else os.path.join(self.base_dir, item)
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                continue
            if os.path.isdir(p):
                roots.append(p)
                seen.add(key)
            else:
                logger.warning(f"[通用语音] 额外语音库目录不存在: {p}，已跳过")

        if roots:
            return roots

        # 3. 兜底：default_lib（默认 voice/三国杀）
        lib = (self.default_lib or "").strip()
        if lib:
            p = lib if os.path.isabs(lib) else os.path.join(self.base_dir, lib)
            if os.path.isdir(p) and self._has_role_dirs(p):
                return [p]

        return []

    def _has_role_dirs(self, dir_path: str) -> bool:
        """判断目录能否作为语音库：存在直接包含音频文件的子目录"""
        try:
            for name in os.listdir(dir_path):
                p = os.path.join(dir_path, name)
                if os.path.isdir(p) and any(self._is_audio(f) for f in os.listdir(p)):
                    return True
        except OSError:
            pass
        return False

    def _scan_root(self, root: str):
        """扫描一个音频根目录（大目录），支持两种布局：
        1. 平铺: 根/角色/音频        -> 角色归入默认库（根目录名）【根目录本身就是一个语音库时】
        2. 库式: 根/语音库/角色/音频 -> 语音库名 = 库文件夹名【voice/ 大目录或 extra_libs 指向含多库的大目录时】
        """
        root_name = os.path.basename(root)
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            return
        for name in entries:
            p = os.path.join(root, name)
            if not os.path.isdir(p):
                continue
            try:
                files = self._sort_files([f for f in os.listdir(p) if self._is_audio(f)])
            except OSError:
                continue
            if files:
                # 布局1: p 直接是角色目录
                self._add_role(root_name, name, p, files)
                continue
            # 布局2: p 是语音库，其子目录是角色
            try:
                subs = sorted(os.listdir(p))
            except OSError:
                continue
            for sub in subs:
                sp = os.path.join(p, sub)
                if not os.path.isdir(sp):
                    continue
                try:
                    sub_files = self._sort_files([f for f in os.listdir(sp) if self._is_audio(f)])
                except OSError:
                    continue
                if sub_files:
                    self._add_role(name, sub, sp, sub_files)

    def match_role(self, message: str):
        """
        匹配「库名+角色名+序号」或「角色名+序号」模式
        返回: (audio_path_or_list, is_random, is_all) 或 None
        """
        lowered = message.lower()

        # 1. 库名+角色名：多语音库角色重名时可精确定位（如「三国杀曹操3」）
        for category in self.category_order:
            if not lowered.startswith(category.lower()):
                continue
            rest = message[len(category):].strip()
            if not rest:
                continue
            roles = self.categories.get(category, {})
            for role in sorted(roles, key=len, reverse=True):
                suffix = self._exact_suffix(rest, role)
                if suffix is not None:
                    result = self._resolve_suffix(category, role, suffix)
                    if result:
                        return result

        # 2. 纯角色名匹配（重名时取先扫描到的语音库）
        for role in self._sorted_roles:
            suffix = self._exact_suffix(message, role)
            if suffix is None:
                continue
            category = self.role_category.get(role, "")
            result = self._resolve_suffix(category, role, suffix)
            if result:
                return result

        return None

    @staticmethod
    def _exact_suffix(text: str, name: str):
        """
        文件夹名完全匹配：text 必须以 name 开头，且剩余部分只能是空或纯数字序号。
        返回后缀（"" = 随机、数字 = 序号）或 None（不完全匹配）。
        用于避免「曹操攻略」「袁绍说」之类以角色名开头但不是点播的消息误触发。
        """
        if not text.lower().startswith(name.lower()):
            return None
        suffix = text[len(name):].strip()
        if not suffix or suffix.isdigit():
            return suffix
        return None

    def _resolve_suffix(self, category: str, role: str, suffix: str):
        """解析角色名后的后缀: 空=随机, 0=全部, 数字=指定序号"""
        files = (self.categories.get(category, {}) or {}).get(role, [])
        if not files:
            files = self.role_map.get(role, [])
        if not suffix:
            return self._get_random_audio(role, files), True, False
        if suffix == "0":
            return self._get_all_audio(files), False, True
        if suffix.isdigit():
            return self._get_indexed_audio(role, files, int(suffix)), False, False
        return None

    def role_display(self, role: str) -> str:
        """角色显示名：多语音库时带库名前缀，避免混淆"""
        if len(self.category_order) <= 1:
            return role
        cat = self.role_category.get(role, "")
        return f"{cat}·{role}" if cat else role

    def role_of(self, path: str) -> str:
        """根据音频路径反查角色名"""
        info = self.file_to_role.get(path)
        return info[1] if info else os.path.basename(os.path.dirname(path))

    def display_of(self, path: str) -> str:
        """根据音频路径反查角色显示名（多库时含库名前缀，重名角色显示实际命中的库）"""
        info = self.file_to_role.get(path)
        if info:
            cat, role = info
            return f"{cat}·{role}" if len(self.category_order) > 1 else role
        return self.role_display(self.role_of(path))

    def _get_random_audio(self, role: str, files: list):
        """获取随机音频，避免连续重复"""
        if not files:
            return None

        if len(files) == 1:
            return files[0]

        last = self.last_played.get(role)
        # 尝试随机选择一个与上次不同的
        candidates = [f for f in files if f != last]
        if not candidates: # 理论上不会发生，除非只有1个文件且上面已处理
            candidates = files

        selected = random.choice(candidates)
        self.last_played[role] = selected
        return selected

    def _get_indexed_audio(self, role: str, files: list, index: int):
        """获取指定序号的音频"""
        if not files:
            return None

        # 序号从1开始
        real_index = index - 1

        if real_index < 0: # 序号0或负数，默认第一个
            return files[0]

        if real_index >= len(files):
            return files[-1] # 超出范围，返回最后一个

        return files[real_index]

    def _get_all_audio(self, files: list):
        """获取角色的所有音频文件列表"""
        return files

    def get_random_voices(self, count: int):
        """从所有角色中随机选取指定数量的语音，返回 [(path, role_name), ...]"""
        if not self._all_files_cache:
            return []
        count = min(count, len(self._all_files_cache))
        return random.sample(self._all_files_cache, count)

    def match_keyword(self, message: str, fuzzy_threshold=0.6):
        """
        匹配关键词（支持模糊匹配）
        返回: (audio_path, keyword) 或 None

        双向包含（kw in message / message in kw）合并择优：
        按重合文本长度降序，重合相同时按 完全相等 > 消息包含关键词 > 关键词包含消息 优先，
        避免短关键词（如「哈基米」）抢先于更长更具体的命中（如「哈基米的约定」）。
        """
        message = message.lower()

        # 1+2. 双向包含，择优而非随机
        hits = []  # (overlap_len, priority, path, keyword)
        for kw, p in self.keyword_map:
            k = kw.lower()
            if k in message:
                # 关键词包含于消息：重合长度 = 关键词长度；完全相等优先级最高
                hits.append((len(k), 0 if k == message else 1, p, kw))
            elif message in k:
                # 消息包含于关键词：重合长度 = 消息长度
                hits.append((len(message), 2, p, kw))
        if hits:
            best_overlap = max(h[0] for h in hits)
            best_priority = min(h[1] for h in hits if h[0] == best_overlap)
            candidates = [
                (p, kw) for ov, pr, p, kw in hits
                if ov == best_overlap and pr == best_priority
            ]
            return random.choice(candidates)

        # 3. 模糊匹配：bigram 粗筛 + quick_ratio 预过滤 + SequenceMatcher 精算
        #    仅当消息长度适中时尝试，避免对极短或极长消息进行昂贵计算
        if 2 <= len(message) <= 20:
            candidates = self._fuzzy_candidates(message, fuzzy_threshold)
            best_ratio = 0
            best_match = None
            for kw, path in candidates:
                ratio = difflib.SequenceMatcher(None, message, kw.lower()).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = (path, kw)
            if best_ratio >= fuzzy_threshold and best_match:
                return best_match

        return None

    def _fuzzy_candidates(self, message: str, fuzzy_threshold: float) -> list:
        """
        模糊匹配候选筛选：
        1) bigram 索引取与消息共享相邻字对的候选（通常只剩几十条）
        2) 无 bigram 命中时退化为全量，但用 quick_ratio 快速预过滤（比 ratio 快数倍）
        """
        idxs = set()
        for bg in self._bigrams(message):
            idxs.update(self._bigram_index.get(bg, ()))
        pool = [self.keyword_map[i] for i in idxs] if idxs else self.keyword_map

        # quick_ratio 粗筛：只保留可能达到阈值的候选，再交给上层精算
        return [
            (kw, path) for kw, path in pool
            if difflib.SequenceMatcher(None, message, kw.lower()).quick_ratio()
            >= fuzzy_threshold - 0.15
        ]
