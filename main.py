# -*- coding: utf-8 -*-
"""
通用语音玩梗插件 — 入口
事件路由、/v 管理指令与发送编排放这里；能力实现拆在 core/ 包内：
  constants.py      常量（LLM 唤醒词）
  voice_manager.py  语音库扫描与匹配
  audio_tools.py    WAV 转换缓存 / 多段合并（ffmpeg）
  role_image.py     角色列表图片绘制
  llm_gate.py       LLM 回复判定
  music/library.py  音乐曲库（扫描/匹配/随机）
  music/clip.py     副歌定位与裁剪（分析+转码一体）
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

from core import audio_tools, llm_gate, role_image
from core.voice_manager import VoiceManager
from core.constants import DEFAULT_LLM_PATTERNS

# 音乐点播依赖：numpy 缺失时仅禁用音乐功能，不影响语音主体
try:
    from core.music import clip as song_clip
    from core.music.library import MusicLibrary
    _MUSIC_OK = getattr(song_clip, "_HAS_NUMPY", False)
except Exception:
    song_clip = None
    MusicLibrary = None
    _MUSIC_OK = False


@register("astrbot_plugin_voice_meme", "落日七号、复读机长", "通用语音玩梗插件 - 按语音库/角色名/台词关键词自动发送对应语音，支持多语音库与外部库目录（mp3/wav/m4a）", "1.7.0", "https://github.com/kkvrry/astrbot_plugin_voice_meme")
class SgsVoiceMeme(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.base_dir = os.path.dirname(__file__)
        # 语音库大目录：默认 voice/（其下每个一级文件夹是一个语音库，sgs_voices 为默认库），
        # 可通过配置 audio_root 指定其它大目录；extra_lib_dirs 为额外语音库目录列表（任意位置，
        # 每项可为单个库目录或含多库的大目录），在自动识别之外追加
        self.audio_root = self.config.get("audio_root", "voice")
        self.default_lib = self.config.get("default_lib", "voice/sgs_voices")
        self.extra_libs = [
            str(x).strip() for x in (self.config.get("extra_lib_dirs", []) or [])
            if str(x).strip()
        ]

        # 加载配置（min_keyword_len 等需在 VoiceManager 前读取）
        self._load_config()

        # 初始化语音管理器
        self.voice_manager = VoiceManager(
            self.base_dir, self.audio_root, self.default_lib,
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

        # 清理过期音频缓存
        self._cleanup_cache()
        self._cleanup_music_cache()

        logger.info(f"[通用语音] 插件初始化完成！")
        logger.info(f"[通用语音] 语音库: {self.voice_manager.category_order}")
        logger.info(f"[通用语音] 数据目录: {self.data_dir}")

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
            self._music_lib = MusicLibrary(self.music_dir)
        return self._music_lib

    async def _handle_music(self, event: AstrMessageEvent, query: str | None,
                            full: bool = False):
        """「点歌 <歌名>」/「随机音乐」：定位副歌起点，裁剪至最大时长后发送。

        full=True（「点歌完整 <歌名>」）时跳过分析，直接发送完整歌曲文件。
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
            if not matches:
                yield event.plain_result(f"❌ 曲库里没找到「{query}」，试试更完整的歌名。")
                return
            # 仅一个候选，或首名得分明显领先时直接播；并列歧义则列出让用户选
            if len(matches) == 1 or matches[0][1] > matches[1][1]:
                song_path = matches[0][0]
            else:
                names = [f"{i}. {os.path.splitext(os.path.basename(p))[0]}"
                         for i, (p, _) in enumerate(matches, 1)]
                yield event.plain_result("🎵 找到多首匹配，请更精确地点歌：\n" + "\n".join(names))
                return
        else:
            song_path = lib.random()
            if not song_path:
                yield event.plain_result("❌ 音乐目录里没有找到音频文件。")
                return

        stem = os.path.splitext(os.path.basename(song_path))[0]

        # 完整文件模式：跳过副歌分析，直接以文件形式发送原曲
        if full:
            logger.info(f"[通用语音] 完整点歌: {stem}")
            yield event.plain_result(f"📀 正在发送完整歌曲：{stem}")
            try:
                yield event.chain_result([
                    Comp.File(file=song_path, name=os.path.basename(song_path))
                ])
            except Exception as e:
                logger.error(f"[通用语音] 完整歌曲发送失败: {e}")
                yield event.plain_result("❌ 完整歌曲发送失败。")
            return

        logger.info(f"[通用语音] 音乐点播: {stem} (来源: {'点歌' if query else '随机'})")
        yield event.plain_result(f"🎵 副歌提取中：{stem}（首次解析需要几秒~几十秒）")

        try:
            result = await asyncio.to_thread(
                song_clip.find_chorus_clip, song_path,
                float(self.music_clip_max_sec), "mp3", self._music_clip_dir,
            )
        except Exception as e:
            logger.error(f"[通用语音] 副歌提取异常: {e}")
            result = None
        if not result or not os.path.isfile(result["clip_path"]):
            yield event.plain_result(f"❌「{stem}」副歌提取失败，稍后再试试。")
            return

        clip_path = result["clip_path"]
        try:
            yield event.chain_result([Comp.Record(file=clip_path, url=clip_path)])
        except Exception as e:
            # 个别平台只认 WAV：回落到既有 _get_wav_path 转换通道重发一次
            logger.warning(f"[通用语音] mp3 直发失败，转 WAV 重试: {e}")
            try:
                wav_path = self._get_wav_path(clip_path)
                yield event.chain_result([Comp.Record(file=wav_path, url=wav_path)])
            except Exception as e2:
                logger.error(f"[通用语音] 音乐发送失败: {e2}")
                yield event.plain_result("❌ 音乐片段发送失败。")

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
                    logger.info(f"[通用语音] 缓存清理: 删除 {removed} 个无效 WAV 缓存")

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
                        logger.info(f"[通用语音] 缓存清理: 删除 {removed} 个超期合并缓存")
        except Exception as e:
            logger.error(f"[通用语音] 缓存清理失败: {e}")

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

        # ---- 功能: 音乐点播 / 随机音乐（走副歌裁剪通道，优先于语音匹配）----
        # 音乐点播 / 随机音乐入口
        music_random = message in ("随机音乐", "随机歌曲", "来首歌", "点歌")
        full_match = re.match(r"^点歌完整\s+(.+)$", message)
        music_match = re.match(r"^点歌\s+(.+)$", message)
        if music_random or music_match or full_match:
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
                logger.info(f"[通用语音] 发送角色列表图片 (库: {lib or '全部'})")
                self.trigger_count += 1
                try:
                    yield event.chain_result([
                        Comp.Image(file=img_path)
                    ])
                except Exception as e:
                    logger.error(f"[通用语音] 发送角色列表图片失败: {e}")
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
        logger.info(f"[通用语音] 触发: '{trigger_keyword}', 发送 {n} 条语音")
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
                    logger.error(f"[通用语音] 发送语音失败: {e}")

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
                        logger.error(f"[通用语音] 发送合并语音失败: {e}")
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
                            logger.error(f"[通用语音] 发送语音失败: {e}")
        else:
            # 单条语音直接发送
            _, _, path = voice_infos[0]
            final_audio_path = self._get_wav_path(path)
            try:
                yield event.chain_result([
                    Comp.Record(file=final_audio_path, url=final_audio_path)
                ])
            except Exception as e:
                logger.error(f"[通用语音] 发送语音失败: {e}")

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
        help_text = f"""🎭 通用语音插件 v1.7.0

📌 功能：
1. 「角色名+序号」点播语音（如：SP关羽3）
2. 「角色名+0」发送该角色全部语音（如：曹操0）
3. 角色名不带序号随机播放（不重复）
4. 「随机台词N」随机发送N条语音（上限5条，如：随机台词3）
5. 群聊发送「角色列表」查看全部角色图片（「角色列表 库名」只看某个库；兼容旧触发词「英雄列表」）
6. 关键词匹配（含模糊匹配，阈值: {self.fuzzy_threshold*100:.0f}%）
7. 多语音库：voice/ 下自动识别 + 配置额外库目录（extra_lib_dirs），
   角色重名时可用「库名+角色名」精确点播（如：三国杀曹操3）
8. 「随机音乐」随机播放曲库歌曲的副歌片段（music_dir）
9. 「点歌 <歌名>」点播歌曲，从副歌开始裁剪（上限 music_clip_max_sec 秒）
10. 「点歌完整 <歌名>」以文件形式发送完整歌曲（不经裁剪）

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
        self.audio_root = self.config.get("audio_root", "voice")
        self.default_lib = self.config.get("default_lib", "voice/sgs_voices")
        self.voice_manager = VoiceManager(
            self.base_dir, self.audio_root, self.default_lib,
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
