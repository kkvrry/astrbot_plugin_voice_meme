# -*- coding: utf-8 -*-
"""audio_tools.py — 音频工具：WAV 转换缓存 / 多段合并 / 台词提取（ffmpeg 封装）"""

import os
import re
import hashlib
import subprocess

def wav_cache_path(data_dir: str, base_dir: str, audio_path: str) -> str:
    """计算音频在 cache_wav 下的缓存路径。

    插件目录内的音频镜像源相对目录结构；目录外的音频（extra_libs 等）
    用源目录哈希隔离，避免 ".." 相对路径逃逸缓存目录或不同库间重名冲突。
    """
    cache_dir = os.path.join(data_dir, "cache_wav")
    try:
        rel_path = os.path.relpath(audio_path, base_dir)
    except ValueError:
        rel_path = None  # 跨盘符等情况无法取相对路径
    if not rel_path or rel_path == ".." or rel_path.startswith(".." + os.sep):
        digest = hashlib.md5(
            os.path.dirname(audio_path).encode("utf-8", "ignore")
        ).hexdigest()[:12]
        rel_path = os.path.join("_external", digest, os.path.basename(audio_path))
    return os.path.join(cache_dir, os.path.splitext(rel_path)[0] + ".wav")

def get_wav_path(data_dir: str, base_dir: str, audio_path: str) -> str:
    """
    获取音频的 WAV 版本路径。如果是 MP3，则转换为 WAV。
    用于兼容某些只支持 WAV 的平台（如 qq_official）。
    """
    if audio_path.lower().endswith(".wav"):
        return audio_path

    wav_path = wav_cache_path(data_dir, base_dir, audio_path)

    # 如果缓存已存在，直接返回
    if os.path.exists(wav_path):
        return wav_path

    # 否则进行转换
    os.makedirs(os.path.dirname(wav_path), exist_ok=True)
    try:
        # 使用 ffmpeg 转换为 WAV (pcm_s16le, 16000Hz, mono 这种格式最通用)
        # 如果不确定格式，直接简单转换也可
        cmd = [
            "ffmpeg", "-y", "-i", audio_path,
            "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000",
            wav_path, "-loglevel", "quiet"
        ]
        subprocess.run(cmd, check=True)
        return wav_path
    except Exception as e:
        logger.error(f"[语音罐头] 音频转换失败 (MP3 -> WAV): {e}")
        # 如果转换失败，返回原路径，尝试让适配器自行处理
        return audio_path

def extract_voice_text(audio_path: str) -> str:
    """从文件名提取台语文本: 01_台词.mp3 -> 台词"""
    filename = os.path.basename(audio_path)
    name_without_ext = os.path.splitext(filename)[0]
    match = re.match(r'^\d+[_.\-\s]+(.+)$', name_without_ext)
    return match.group(1) if match else name_without_ext


def merge_audio_files(data_dir: str, audio_paths: list) -> str:
    """将多个音频文件合并为一个 MP3，片段间插入 0.6s 静音间隔"""
    cache_dir = os.path.join(data_dir, "cache_merged")
    os.makedirs(cache_dir, exist_ok=True)

    # 用路径列表的哈希值做缓存文件名
    key = hashlib.md5('|'.join(sorted(audio_paths)).encode()).hexdigest()
    merged_path = os.path.join(cache_dir, f"{key}.mp3")

    if os.path.exists(merged_path):
        return merged_path

    # 构建 ffmpeg concat filter：[0:a][silence][1:a][silence]...concat=n:out_type=a
    # 所有输入先统一为 44100Hz / s16 / mono，避免采样率或声道不一致导致 concat 报错
    inputs = []
    filter_parts = []
    concat_inputs = []

    for i, path in enumerate(audio_paths):
        inputs.extend(["-i", path])
        if i > 0:
            # 在前一个片段后插入 0.6s 静音（参数与统一后的输入一致）
            silence_label = f"[s{i}]"
            filter_parts.append(
                f"anullsrc=r=44100:cl=mono:d=0.6,"
                f"aformat=sample_fmts=s16:channel_layouts=mono{silence_label}"
            )
            concat_inputs.append(silence_label)
        audio_label = f"[a{i}]"
        filter_parts.append(
            f"[{i}:a]aresample=44100,aformat=sample_fmts=s16:channel_layouts=mono{audio_label}"
        )
        concat_inputs.append(audio_label)

    concat_n = len(concat_inputs)
    filter_parts.append(
        f"{''.join(concat_inputs)}concat=n={concat_n}:v=0:a=1[out]"
    )

    filter_str = ';'.join(filter_parts)

    try:
        cmd = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", filter_str,
            "-map", "[out]",
            "-acodec", "libmp3lame", "-ab", "128k",
            "-t", "300",  # 最长 5 分钟
            merged_path,
            "-loglevel", "quiet"
        ]
        subprocess.run(cmd, check=True, timeout=120)
        return merged_path
    except Exception as e:
        logger.error(f"[语音罐头] 音频合并失败: {e}")
        return None
