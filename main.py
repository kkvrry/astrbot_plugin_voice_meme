# -*- coding: utf-8 -*-
"""
语音罐头插件 — 入口
事件路由、/v 管理指令与发送编排放这里；能力实现拆在 core/ 包内：
  constants.py      常量（LLM 唤醒词）
  voice_manager.py  语音库扫描与匹配
  audio_tools.py    WAV 转换缓存 / 多段合并（ffmpeg）
  role_image.py     角色列表图片绘制
  llm_gate.py       LLM 回复判定
  music/library.py  音乐曲库（扫描/匹配/随机）
  music/clip.py     副歌定位与裁剪（分析+转码一体）
  music/request.py  自然语言点歌解析（「[给XX]来一首/奏乐」句式）
  music/bilibili.py B站点歌兜底（曲库未命中时搜索下载首个结果）
"""

import os
import re
import sys
import time
import asyncio

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import AstrBotConfig, logger
import astrbot.api.message_components as Comp

# 保证插件根目录在 sys.path 中，core 包可被稳定导入（不依赖加载器的行为）
_PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

# astrbot 重载插件时不会清除本插件自行导入的 core.* 子模块缓存，
# 旧模块会以旧签名残留在 sys.modules 中与新版 main.py 混用
# （如 VoiceManager 参数错位导致初始化 TypeError）。这里强制清除，
# 保证每次重载 core 都按当前磁盘代码重新导入。
for _m in [k for k in sys.modules if k == "core" or k.startswith("core.")]:
    del sys.modules[_m]

from core import audio_tools, llm_gate, role_image
from core.voice_manager import VoiceManager
from core.constants import DEFAULT_LLM_PATTERNS

# 音乐点播依赖：numpy 缺失时仅禁用音乐功能，不影响语音主体
try:
    from core.music import clip as song_clip
    from core.music import request as song_request
    from core.music.library import MusicLibrary
    _MUSIC_OK = getattr(song_clip, "_HAS_NUMPY", False)
except Exception:
    song_clip = None
    song_request = None
    MusicLibrary = None
    _MUSIC_OK = False

# B 站点歌兜底：曲库未命中时联网搜索（requests 缺失时静默禁用兜底）
try:
    from core.music import bilibili as song_bili
    _BILI_OK = _MUSIC_OK  # 兜底产物要走同一套副歌裁剪，跟随音乐可用性
except Exception:
    song_bili = None
    _BILI_OK = False


@register("astrbot_plugin_voice_meme", "kkvrry", "语音罐头 - 语音玩梗与音乐点播：角色名/台词关键词触发语音（多语音库），支持自然语言点歌与副歌裁剪", "1.12.0", "https://github.com/kkvrry/astrbot_plugin_voice_meme")
class SgsVoiceMeme(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.base_dir = os.path.dirname(__file__)
        # 语音库大目录固定为 voice/（其下每个一级文件夹是一个语音库），
        # 插件目录外的库通过 extra_lib_dirs 追加（每项可为单个库目录或含多库的大目录）
        self.default_lib = self.config.get("default_lib", "voice/三国杀")
        self.extra_libs = [
            str(x).strip() for x in (self.config.get("extra_lib_dirs", []) or [])
            if str(x).strip()
        ]

        # 加载配置（min_keyword_len 等需在 VoiceManager 前读取）
        self._load_config()

        # 初始化语音管理器
        self.voice_manager = VoiceManager(
            self.base_dir, self.default_lib,
            self.min_keyword_len, self.extra_libs
        )

        # 统计信息
        self.trigger_count = 0

        # 加载持久化数据目录
        self.data_dir = StarTools.get_data_dir("voice_meme")

        # 角色列表图片缓存路径 + 内容签名
        self._role_list_img_path = os.path.join(self.data_dir, "role_list_cache.png")
        self._role_list_signature = ""  # 缓存时的音频库签名

        # 音乐曲库（惰性创建，reload 时置空重建）
        self._music_lib = None
        self._music_clip_dir = os.path.join(self.data_dir, "cache_clip")
        # B 站兜底：cookie 与下载缓存目录
        self._bili_cookie_file = os.path.join(self.data_dir, "bilibili_cookie.json")
        self._bili_cache_dir = os.path.join(self.data_dir, "cache_online")

        # 清理过期音频缓存
        self._cleanup_cache()
        self._cleanup_music_cache()

        logger.info(f"[语音罐头] 插件初始化完成！")
        logger.info(f"[语音罐头] 语音库: {self.voice_manager.category_order}")
        logger.info(f"[语音罐头] 数据目录: {self.data_dir}")

    # ---- 音频工具（薄封装，实现在 core/audio_tools.py）----

    def _wav_cache_path(self, audio_path: str) -> str:
        return audio_tools.wav_cache_path(self.data_dir, self.base_dir, audio_path)

    def _get_wav_path(self, audio_path: str) -> str:
        return audio_tools.get_wav_path(self.data_dir, self.base_dir, audio_path)

    def _extract_voice_text(self, audio_path: str) -> str:
        return audio_tools.extract_voice_text(audio_path)

    def _merge_audio_files(self, audio_paths: list) -> str:
        return audio_tools.merge_audio_files(self.data_dir, audio_paths)
    # ================================================================
    # 音乐点播（副歌裁剪通道）
    # ================================================================

    def _music_ready(self) -> bool:
        return (_MUSIC_OK and song_clip is not None
                and self.music_dir and os.path.isdir(self.music_dir))

    def _get_music_lib(self):
        if MusicLibrary is None or not self.music_dir:
            return None
        if self._music_lib is None or self._music_lib.root != os.path.abspath(self.music_dir):
            self._music_lib = MusicLibrary(self.music_dir,
                                           exclude_dirs=self.music_exclude_dirs)
        return self._music_lib

    @staticmethod
    def _norm_text(s: str) -> str:
        return re.sub(r"[\s　]+", "", s or "").lower()

    @staticmethod
    def _resolve_music_key(lib, key: str):
        """把「随机音乐 <X>」/「来一首<宾语>」的 X 解析为 (kind, value)。

        委派给 song_request.resolve_target：子目录 → 歌手 → 歌名，命中即止，
        宾语口语后缀（的歌/歌曲/音乐）由 target_candidates 逐层剥离。
        曲库不可用时降级为 (song, 原文)，交由点播通道报错提示。
        """
        if lib is None or song_request is None:
            return "song", (key or "").strip()
        return song_request.resolve_target(lib, key)

    async def _handle_music(self, event: AstrMessageEvent, query: str | None,
                            full: bool = False, subdir: str | None = None,
                            artist: str | None = None, recipient: str = ""):
        """「音乐 <歌名>」/「随机音乐 [目录]」：定位副歌起点，裁剪至最大时长后发送。

        full=True（「完整音乐 <歌名>」）时跳过分析，直接发送完整歌曲文件。
        subdir 限定子目录，artist 限定歌手（该歌手歌曲内随机）。
        recipient 非空（「给XX来一首」）时提示语改为「为XX献上一首」。
        """
        self.trigger_count += 1
        if not _MUSIC_OK:
            yield event.plain_result("❌ 音乐功能依赖 numpy/librosa，当前运行环境缺失，请联系管理员安装。")
            return
        if not self.music_dir or not os.path.isdir(self.music_dir):
            yield event.plain_result("❌ 音乐功能未启用：未配置有效的音乐目录（music_dir）。")
            return

        lib = self._get_music_lib()
        if lib is None or len(lib) == 0:
            yield event.plain_result("❌ 音乐目录里没有找到音频文件。")
            return

        # 选曲
        if query:
            matches = lib.match(query)
            if subdir or artist:  # 限定子目录/歌手范围内匹配
                keep = set(lib.songs(subdir, artist))
                matches = [(p, s) for p, s in matches if p in keep]
            if not matches:
                scope = f"（{subdir or artist}）" if (subdir or artist) else ""
                if scope:
                    # 限定范围（子目录/歌手）内未命中不联网兜底，保持语义精确
                    yield event.plain_result(
                        f"❌ 曲库里没找到「{query}」{scope}，换个更完整的关键词试试。")
                    return
                async for r in self._handle_bilibili_fallback(
                        event, query, full=full, recipient=recipient):
                    yield r
                return
            # 多首匹配时直接取得分最高的第一首，不再要求手动精确选择
            song_path = matches[0][0]
        else:
            song_path = lib.random(subdir, artist)
            if not song_path:
                scope = f"（{subdir or artist}）" if (subdir or artist) else ""
                yield event.plain_result(f"❌ 该范围{scope}里没有找到音频文件。")
                return

        stem = os.path.splitext(os.path.basename(song_path))[0]
        title = lib.display_name(song_path)
        src = ("随机" if not query else ("完整" if full else "点播"))
        if subdir:
            src += f"·{subdir}"
        elif artist:
            src += f"·{artist}"
        # 播放提示：命令带人名时改为「为XX献上一首」
        who = f"为{recipient}" if recipient else ""
        tip = (f"📀 {who}献上完整歌曲《{title}》" if who
               else f"📀 正在发送完整歌曲《{title}》")

        # 完整文件模式：跳过副歌分析，直接以文件形式发送原曲
        if full:
            logger.info(f"[语音罐头] 完整音乐: {stem}")
            async for r in self._send_full_song(event, song_path, tip):
                yield r
            return

        logger.info(f"[语音罐头] 音乐播放: {stem} (来源: {src})")

        try:
            result = await asyncio.to_thread(
                song_clip.find_chorus_clip, song_path,
                float(self.music_clip_max_sec), "mp3", self._music_clip_dir,
            )
        except Exception as e:
            logger.error(f"[语音罐头] 副歌提取异常: {e}")
            result = None
        if not result or not os.path.isfile(result["clip_path"]):
            yield event.plain_result(f"❌「{stem}」副歌提取失败，稍后再试试。")
            return

        clip_path = result["clip_path"]
        async for r in self._send_music_clip(event, clip_path, title, recipient):
            yield r

    async def _send_music_clip(self, event: AstrMessageEvent, clip_path: str,
                               title: str, recipient: str = "",
                               source: str = ""):
        """发送副歌语音：提示语（裁剪模式用「献上」措辞）→ Record 直发 → WAV 回落。"""
        tag = f"（{source}）" if source else ""
        yield event.plain_result(
            f"🎵 为{recipient}献上一首《{title}》{tag}" if recipient
            else f"🎵 正在播放《{title}》片段{tag}")
        try:
            yield event.chain_result([Comp.Record(file=clip_path, url=clip_path)])
        except Exception as e:
            # 个别平台只认 WAV：回落到既有 _get_wav_path 转换通道重发一次
            logger.warning(f"[语音罐头] mp3 直发失败，转 WAV 重试: {e}")
            try:
                wav_path = self._get_wav_path(clip_path)
                yield event.chain_result([Comp.Record(file=wav_path, url=wav_path)])
            except Exception as e2:
                logger.error(f"[语音罐头] 音乐发送失败: {e2}")
                yield event.plain_result("❌ 音乐片段发送失败。")

    async def _send_full_song(self, event: AstrMessageEvent, song_path: str,
                              tip: str):
        """以文件形式发送完整歌曲。"""
        yield event.plain_result(tip)
        try:
            yield event.chain_result([
                Comp.File(file=song_path, name=os.path.basename(song_path))
            ])
        except Exception as e:
            logger.error(f"[语音罐头] 完整歌曲发送失败: {e}")
            yield event.plain_result("❌ 完整歌曲发送失败。")

    async def _handle_bilibili_fallback(self, event: AstrMessageEvent,
                                        query: str, full: bool = False,
                                        recipient: str = ""):
        """曲库未命中时的 B 站兜底：搜索第一个结果下载音频，走同一套发送链路。

        full=True 以文件发送完整歌曲，否则裁剪副歌后以语音发送。
        下载按 bvid 落盘缓存（cache_online），重复点播不重复下载。
        """
        if not _BILI_OK or song_bili is None:
            yield event.plain_result(
                f"❌ 曲库里没找到「{query}」，换个更完整的关键词试试。")
            return
        yield event.plain_result(f"🔍 曲库里没有「{query}」，正在从B站搜索…")
        try:
            meta = await asyncio.to_thread(
                song_bili.fetch_first_audio,
                query, self._bili_cookie_file, self._bili_cache_dir,
            )
        except Exception as e:
            logger.warning(f"[语音罐头] B站兜底失败: {e}")
            yield event.plain_result(
                f"❌ 曲库里没找到「{query}」，B站搜索也不顺利：{e}")
            return

        song_path = meta["path"]
        title = meta["title"]
        who = f"为{recipient}" if recipient else ""
        tag = "（B站）"
        if full:
            logger.info(f"[语音罐头] 完整音乐(B站): {meta['bvid']} {title}")
            tip = (f"📀 {who}献上完整歌曲《{title}》{tag}" if who
                   else f"📀 正在发送完整歌曲《{title}》{tag}")
            async for r in self._send_full_song(event, song_path, tip):
                yield r
            return

        logger.info(f"[语音罐头] 音乐播放(B站): {meta['bvid']} {title}")
        try:
            result = await asyncio.to_thread(
                song_clip.find_chorus_clip, song_path,
                float(self.music_clip_max_sec), "mp3", self._music_clip_dir,
            )
        except Exception as e:
            logger.error(f"[语音罐头] 副歌提取异常: {e}")
            result = None
        if not result or not os.path.isfile(result["clip_path"]):
            yield event.plain_result(f"❌「{title}」副歌提取失败，稍后再试试。")
            return
        clip_path = result["clip_path"]
        async for r in self._send_music_clip(event, clip_path, title,
                                             recipient, source="B站"):
            yield r

    def _cleanup_music_cache(self):
        """清理副歌片段/分析缓存：按 cache_max_days 过期删除（0 为关闭清理）。"""
        if self.cache_max_days <= 0 or not os.path.isdir(self._music_clip_dir):
            return
        cutoff = time.time() - self.cache_max_days * 86400
        for f in os.listdir(self._music_clip_dir):
            p = os.path.join(self._music_clip_dir, f)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass

    def _load_config(self):
        """从配置对象加载设置"""
        self.need_llm_patterns = self.config.get("llm_wake_patterns", DEFAULT_LLM_PATTERNS)
        self.wake_word_prefix = self.config.get("wake_word_prefix", "/")
        self.require_prefix = self.config.get("require_prefix", False)
        self.enable_group_only = self.config.get("enable_group_only", True)
        self.private_chat_llm_mode = self.config.get("private_chat_llm_mode", "smart")
        self.fuzzy_threshold = self.config.get("fuzzy_threshold", 0.6)
        self.min_keyword_len = max(1, int(self.config.get("min_keyword_len", 2) or 2))
        self.cache_max_days = max(0, int(self.config.get("cache_max_days", 30) or 30))
        # 音乐点播配置
        self.music_dir = str(self.config.get("music_dir", "") or "").strip()
        self.music_clip_max_sec = max(10, min(300, int(
            self.config.get("music_clip_max_sec", 60) or 60)))
        excl = self.config.get("music_exclude_dirs", []) or []
        self.music_exclude_dirs = [str(d) for d in excl if str(d).strip()]

    def _cleanup_cache(self):
        """清理缓存：删除源文件已不存在的 WAV 缓存（孤儿缓存），以及超期的合并缓存"""
        try:
            # 1. cache_wav: 以当前扫描索引重建有效缓存集合，不在集合内的视为孤儿缓存删除。
            #    统一走 _wav_cache_path 计算，插件目录内/外的音频均可正确反查
            cache_wav = os.path.join(self.data_dir, "cache_wav")
            if os.path.isdir(cache_wav):
                valid_paths = {
                    self._wav_cache_path(p) for p in self.voice_manager.file_to_role
                }
                removed = 0
                for root, dirs, files in os.walk(cache_wav):
                    for f in files:
                        p = os.path.join(root, f)
                        if p in valid_paths:
                            continue
                        try:
                            os.remove(p)
                            removed += 1
                        except OSError:
                            continue
                if removed:
                    logger.info(f"[语音罐头] 缓存清理: 删除 {removed} 个无效 WAV 缓存")

            # 2. cache_merged: 按保留天数清理
            if self.cache_max_days > 0:
                cache_merged = os.path.join(self.data_dir, "cache_merged")
                if os.path.isdir(cache_merged):
                    cutoff = time.time() - self.cache_max_days * 86400
                    removed = 0
                    for f in os.listdir(cache_merged):
                        p = os.path.join(cache_merged, f)
                        try:
                            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                                os.remove(p)
                                removed += 1
                        except OSError:
                            continue
                    if removed:
                        logger.info(f"[语音罐头] 缓存清理: 删除 {removed} 个超期合并缓存")
        except Exception as e:
            logger.error(f"[语音罐头] 缓存清理失败: {e}")

    def _needs_llm_response(self, message: str, event: AstrMessageEvent) -> bool:
        """判断消息是否需要 LLM 回复（实现在 core/llm_gate.py）"""
        return llm_gate.needs_llm_response(
            message, event, self.need_llm_patterns,
            self.wake_word_prefix, self.private_chat_llm_mode)

    def _generate_role_list_image(self, category=None):
        """生成角色列表图片（缓存签名判断在此，绘制在 core/role_image.py）"""
        if category:
            role_names = list(self.voice_manager.categories.get(category, {}).keys())
        else:
            role_names = list(self.voice_manager.role_map.keys())
        cache_key = f"{self.voice_manager.lib_signature}|cat:{category or '*'}"
        if (os.path.exists(self._role_list_img_path)
                and self._role_list_signature == cache_key
                and len(role_names) > 0):
            return self._role_list_img_path
        img_path = role_image.build(self.voice_manager, self._role_list_img_path, category)
        if img_path:
            self._role_list_signature = cache_key
        return img_path

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息"""
        message = event.message_str.strip()
        if not message:
            return

        is_private = not event.message_obj.group_id
        if self.enable_group_only and is_private:
            return

        # 前缀触发逻辑
        if self.require_prefix:
            if not message.startswith(self.wake_word_prefix):
                return
            message = message[len(self.wake_word_prefix):].strip()
            if not message:
                return

        voice_infos = []  # [(role_name, voice_text, path), ...]
        trigger_keyword = ""

        # ---- 功能: 音乐（点播/随机/完整/自然语言，走副歌裁剪通道，优先于语音匹配）----
        #   随机音乐 [目录] / 音乐 / 音乐 <歌名> / 完整音乐 <歌名> / 给XX来一首YY
        music_random_bare = message in ("随机音乐", "音乐")
        rand_match = re.match(r"^随机音乐\s+(.+)$", message)
        full_match = re.match(r"^完整音乐\s+(.+)$", message)
        music_match = re.match(r"^音乐\s+(.+)$", message)
        # 自然语言点歌「给XX来一首YY」：仅在音乐功能就绪时拦截，
        # 否则回落语音匹配/LLM，避免吞掉含该句式的普通聊天
        song_req = song_request.parse(message) if self._music_ready() else None
        if music_random_bare or rand_match or music_match or full_match or song_req:
            lib = self._get_music_lib()
            # 三条入口统一归一为「(子目录, 歌手, 歌名查询)」三元组后再分派，
            # 判定逻辑全部收敛在 song_request.resolve_target 里，路由只负责分派
            if rand_match and not full_match:
                # 随机音乐 <目录|歌手>：子目录优先，未命中当歌手解析
                kind, value = self._resolve_music_key(lib, rand_match.group(1))
                if kind not in ("subdir", "artist"):
                    tips = "、".join(lib.subdirs()) if lib else ""
                    yield event.plain_result(
                        f"❌ 没有这个音乐目录或歌手。可用目录：{tips or '（曲库无子目录）'}\n"
                        f"用法：随机音乐 <目录名或歌手名>，如「随机音乐 古风」")
                    event.stop_event()
                    return
                async for r in self._handle_music(event, None, subdir=value):
                    yield r
                event.stop_event()
                return
            if song_req and not full_match and not music_match:
                # [给XX]来一首/来首/奏乐 [YY]，解析顺序：
                # 空 → 曲库随机；子目录（容忍口头后缀）→ 目录内随机；
                # 歌手名 → 该歌手歌曲内随机；否则按歌名点播（模糊匹配）
                # 「整首/完整」修饰对三种分支都生效（整首日语 = 整首文件直发）
                recipient, target = song_req
                full = song_request.wants_full(message)
                kind, value = self._resolve_music_key(lib, target)
                if kind == "random":
                    async for r in self._handle_music(event, None,
                                                      recipient=recipient,
                                                      full=full):
                        yield r
                elif kind == "subdir":
                    async for r in self._handle_music(event, None, subdir=value,
                                                      recipient=recipient,
                                                      full=full):
                        yield r
                elif kind == "artist":
                    async for r in self._handle_music(event, None, artist=value,
                                                      recipient=recipient,
                                                      full=full):
                        yield r
                else:
                    async for r in self._handle_music(event, value,
                                                      recipient=recipient,
                                                      full=full):
                        yield r
                event.stop_event()
                return
            m = full_match or music_match
            query = m.group(1).strip() if m else None
            async for r in self._handle_music(event, query, full=bool(full_match)):
                yield r
            event.stop_event()
            return

        # ---- 功能: 随机台词+数字 ----
        random_match = re.match(r'^随机台词\s*(\d+)$', message)
        if random_match:
            count = min(int(random_match.group(1)), 5)
            results = self.voice_manager.get_random_voices(count)
            if results:
                for path, role in results:
                    text = self._extract_voice_text(path)
                    display_name = self.voice_manager.role_display(role)
                    voice_infos.append((display_name, text, path))
                trigger_keyword = f"随机台词 {count}条"

        # ---- 功能: 角色列表（可指定语音库，如「角色列表 鬼畜语录」）----
        # 兼容旧触发词「英雄列表」
        elif (message in ("角色列表", "英雄列表")
                or message.startswith("角色列表 ") or message.startswith("英雄列表 ")):
            head, _, tail = message.partition(" ")
            lib = tail.strip() if tail else ""
            if lib and lib not in self.voice_manager.category_order:
                yield event.plain_result(f"❌ 未找到语音库「{lib}」，可用「/v list」查看全部语音库")
                event.stop_event()
                return
            img_path = self._generate_role_list_image(lib or None)
            if img_path:
                logger.info(f"[语音罐头] 发送角色列表图片 (库: {lib or '全部'})")
                self.trigger_count += 1
                try:
                    yield event.chain_result([
                        Comp.Image(file=img_path)
                    ])
                except Exception as e:
                    logger.error(f"[语音罐头] 发送角色列表图片失败: {e}")
                    yield event.plain_result(f"角色列表图片发送失败，请检查日志。")
                event.stop_event()
                return

        # ---- 原有: 角色名+序号 ----
        else:
            role_match = self.voice_manager.match_role(message)
            if role_match:
                result, is_random, is_all = role_match
                if is_all:
                    # 角色名+0：全部语音
                    # 从路径中获取实际角色名（匹配后的标准名）
                    role_name = self.voice_manager.role_of(result[0]) if result else message.rstrip('0').strip()
                    display_name = self.voice_manager.role_display(role_name)
                    for path in result:
                        text = self._extract_voice_text(path)
                        voice_infos.append((display_name, text, path))
                    trigger_keyword = f"{display_name} 全部语音（共{len(result)}条）"
                else:
                    # 角色名+序号：单条语音
                    path = result
                    display_name = self.voice_manager.display_of(path)
                    text = self._extract_voice_text(path)
                    voice_infos.append((display_name, text, path))
                    trigger_keyword = f"{display_name}: {text}"
            else:
                # ---- 原有: 关键词模糊匹配 ----
                keyword_match = self.voice_manager.match_keyword(message, self.fuzzy_threshold)
                if keyword_match:
                    path, kw = keyword_match
                    display_name = self.voice_manager.display_of(path)
                    voice_infos.append((display_name, kw, path))
                    trigger_keyword = kw

        if not voice_infos:
            return

        # 过滤无效路径
        voice_infos = [(h, t, p) for h, t, p in voice_infos if p and os.path.exists(p)]
        if not voice_infos:
            return

        n = len(voice_infos)
        logger.info(f"[语音罐头] 触发: '{trigger_keyword}', 发送 {n} 条语音")
        self.trigger_count += 1

        # ---- 多条语音：提示所有台词 + 5条限制 + 音频合并 ----
        if n > 1:
            need_merge = n > 4
            individual = voice_infos[:3] if need_merge else voice_infos
            merged = voice_infos[3:] if need_merge else []

            # 构建提示文本：列出所有语音的角色名+台词
            lines = [f"🎭 {trigger_keyword}"]
            for i, (role, text, _) in enumerate(voice_infos, 1):
                lines.append(f"{i}. 【{role}】{text}")
            if need_merge:
                lines.append(f"（第4~{n}条已合并为一条音频）")

            try:
                yield event.plain_result('\n'.join(lines))
                await asyncio.sleep(0.3)
            except Exception:
                pass

            # 逐条发送单独语音（前3条）
            for role, text, path in individual:
                final_audio_path = self._get_wav_path(path)
                try:
                    yield event.chain_result([
                        Comp.Record(file=final_audio_path, url=final_audio_path)
                    ])
                    await asyncio.sleep(0.6)
                except Exception as e:
                    logger.error(f"[语音罐头] 发送语音失败: {e}")

            # 多余语音合并为一条音频发送
            if merged:
                merge_paths = [p for _, _, p in merged]
                merged_audio = self._merge_audio_files(merge_paths)
                if merged_audio and os.path.exists(merged_audio):
                    try:
                        yield event.chain_result([
                            Comp.Record(file=merged_audio, url=merged_audio)
                        ])
                    except Exception as e:
                        logger.error(f"[语音罐头] 发送合并语音失败: {e}")
                else:
                    # 合并失败，逐条发送（可能超出5条限制）
                    for role, text, path in merged:
                        final_audio_path = self._get_wav_path(path)
                        try:
                            yield event.chain_result([
                                Comp.Record(file=final_audio_path, url=final_audio_path)
                            ])
                            await asyncio.sleep(0.6)
                        except Exception as e:
                            logger.error(f"[语音罐头] 发送语音失败: {e}")
        else:
            # 单条语音直接发送
            _, _, path = voice_infos[0]
            final_audio_path = self._get_wav_path(path)
            try:
                yield event.chain_result([
                    Comp.Record(file=final_audio_path, url=final_audio_path)
                ])
            except Exception as e:
                logger.error(f"[语音罐头] 发送语音失败: {e}")

        # 智能判断是否需要 LLM 回复
        if self._needs_llm_response(message, event):
            yield event.request_llm(prompt=message)
        else:
            event.stop_event()

    @filter.command_group("v")
    def v_group(self):
        pass

    @v_group.command("help")
    async def v_help(self, event: AstrMessageEvent):
        prefix_mode = f"前缀触发（{self.wake_word_prefix}）" if self.require_prefix else "自由触发"
        help_text = f"""🎭 语音罐头 v1.11.0

📌 功能：
1. 「角色名+序号」点播语音（如：SP关羽3）
2. 「角色名+0」发送该角色全部语音（如：曹操0）
3. 角色名不带序号随机播放（不重复）
4. 「随机台词N」随机发送N条语音（上限5条，如：随机台词3）
5. 群聊发送「角色列表」查看全部角色图片（「角色列表 库名」只看某个库；兼容旧触发词「英雄列表」）
6. 关键词匹配（含模糊匹配，阈值: {self.fuzzy_threshold*100:.0f}%）
7. 多语音库：voice/ 下自动识别 + 配置额外库目录（extra_lib_dirs），
   角色重名时可用「库名+角色名」精确点播（如：三国杀曹操3）
8. 「随机音乐 [目录/歌手]」随机播放曲库副歌片段，可指定子目录或歌手（如：随机音乐 古风 / 随机音乐 F4）
9. 「音乐 <歌名>」点播歌曲，按副歌段裁剪发送（music_clip_max_sec 为上限，多首匹配默认第一首）；
   歌名支持模糊匹配：忽略空格/大小写/全角半角、假名与常见中文音译（白金disco = 白金ディスコ = 白金迪斯科）、
   容忍错别字，≥2 字片段即可命中（白金 → 白金ディスコ），也可用「歌手的歌」（如：邓紫棋的泡沫）
10. 「完整音乐 <歌名>」以文件形式发送完整歌曲（不经裁剪）
11. 「来一首YY」「来首YY」「奏乐 [YY]」「给XX奏乐」等自然语言点歌；
    YY 为空 → 曲库随机；YY 为曲库子目录或歌手（可带「的歌/歌曲/音乐」后缀）→ 目录或歌手内随机；
    否则按歌名点播；加「整首/完整」则直发完整文件
    （如：奏乐 / 给我来一首晴天 / 来首日语歌 / 给fk来一首F4的歌 / 整首流星雨）
12. 播放时会先发一条提示：命令带人名时为「为XX献上一首《歌名》」

当前状态：
• 触发模式: {prefix_mode}
• 语音库: {len(self.voice_manager.category_order)} 个（{', '.join(self.voice_manager.category_order)}）
• 角色数量: {len(self.voice_manager.role_map)}
• 关键词数: {len(self.voice_manager.keyword_map)}

可用指令：
• /v help - 帮助
• /v list - 列出所有语音库
• /v list <库名> - 指定语音库的角色列表图片
• /v list <库名> <角色名> - 列出该角色文件夹下的音频文件（也可只输角色名全库查找）
• /v stats - 统计
• /v reload - 重载配置和音频
"""
        yield event.plain_result(help_text)

    @v_group.command("list")
    async def v_list(self, event: AstrMessageEvent, lib: str = "", role: str = ""):
        """
        三级查询：
        /v list                  - 列出所有语音库
        /v list <库名>            - 该语音库的角色列表图片
        /v list <库名> <角色名>   - 列出该角色文件夹下的音频文件（台词）
        /v list <角色名>          - 角色名不在库名位置时，全库查找该角色并列出音频文件
        """
        lib = (lib or "").strip()
        role = (role or "").strip()

        # 1. 无参数：语音库列表
        if not lib:
            lines = ["🎭 语音库列表"]
            for cat in self.voice_manager.category_order:
                role_count = len(self.voice_manager.categories.get(cat, {}))
                lines.append(f"• {cat}（{role_count} 位角色）")
            lines.append("\n发送「角色列表」查看全部角色；「/v list <库名>」看库角色；「/v list <库名> <角色名>」看角色音频文件")
            yield event.plain_result('\n'.join(lines))
            return

        # 2. 第一个参数是库名
        if lib in self.voice_manager.category_order:
            if role:
                # 2a. 库名+角色名：列出该角色文件夹下的音频文件
                files = (self.voice_manager.categories.get(lib, {}) or {}).get(role)
                if not files:
                    yield event.plain_result(f"❌ 语音库「{lib}」中没有角色「{role}」，可用「/v list {lib}」查看库内角色")
                    return
                lines = [f"🎭 {lib}·{role}（{len(files)} 条音频）"]
                for i, p in enumerate(files, 1):
                    lines.append(f"{i}. {self._extract_voice_text(p)}")
                yield event.plain_result('\n'.join(lines))
                return
            # 2b. 仅库名：角色列表图片（现状）
            img_path = self._generate_role_list_image(lib)
            if img_path:
                try:
                    yield event.chain_result([
                        Comp.Image(file=img_path)
                    ])
                except Exception as e:
                    yield event.plain_result(f"❌ 图片发送失败: {e}")
            else:
                yield event.plain_result("❌ 角色列表图片生成失败，请检查日志或安装 Pillow 库。")
            return

        # 3. 第一个参数不是库名：当作角色名，全库查找
        files = self.voice_manager.role_map.get(lib)
        if files:
            cat = self.voice_manager.role_category.get(lib, "")
            prefix = f"{cat}·{lib}" if cat else lib
            lines = [f"🎭 {prefix}（{len(files)} 条音频）"]
            for i, p in enumerate(files, 1):
                lines.append(f"{i}. {self._extract_voice_text(p)}")
            yield event.plain_result('\n'.join(lines))
            return

        # 4. 都没有命中
        yield event.plain_result(f"❌ 未找到语音库「{lib}」或角色「{lib}」，可用「/v list」查看全部语音库")

    @v_group.command("stats")
    async def v_stats(self, event: AstrMessageEvent):
        role_count = len(self.voice_manager.role_map)
        keyword_count = len(self.voice_manager.keyword_map)

        stats_text = f"""📊 统计信息
本次触发: {self.trigger_count} 次
语音库: {len(self.voice_manager.category_order)} 个
收录角色: {role_count} 位
收录关键词: {keyword_count} 条
曲库歌曲: {len(self._get_music_lib()) if self._music_ready() else '未启用'} 首
"""
        yield event.plain_result(stats_text)

    @v_group.command("reload")
    async def v_reload(self, event: AstrMessageEvent):
        """重新加载配置和音频文件"""
        # 1. 重新加载配置
        self._load_config()
        # 2. 按新配置重建语音管理器（min_keyword_len、extra_lib_dirs 等可能变化）
        self.extra_libs = [
            str(x).strip() for x in (self.config.get("extra_lib_dirs", []) or [])
            if str(x).strip()
        ]
        self.default_lib = self.config.get("default_lib", "voice/三国杀")
        self.voice_manager = VoiceManager(
            self.base_dir, self.default_lib,
            self.min_keyword_len, self.extra_libs
        )
        # 3. 清理过期缓存
        self._cleanup_cache()
        self._cleanup_music_cache()
        # 4. 清除角色列表图片缓存
        self._role_list_signature = ""
        # 5. 音乐曲库按新配置重建
        self._music_lib = None

        yield event.plain_result(
            f"✅ 已重载配置并扫描音频目录！\n"
            f"语音库: {len(self.voice_manager.category_order)} 个\n"
            f"角色: {len(self.voice_manager.role_map)}\n"
            f"关键词: {len(self.voice_manager.keyword_map)}\n"
            f"触发模式: {'前缀触发' if self.require_prefix else '自由触发'}\n"
            f"模糊匹配阈值: {self.fuzzy_threshold*100:.0f}%\n"
            f"关键词最短长度: {self.min_keyword_len}"
        )
