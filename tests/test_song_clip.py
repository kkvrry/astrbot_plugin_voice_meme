# -*- coding: utf-8 -*-
"""
test_song_clip.py — song_clip 模块自测

合成一段结构明确的"假歌"（intro / verse / chorus 重复 ×3 / bridge / outro），
验证：副歌定位落点、气口吸附范围、成片时长与格式、缓存命中。
运行：venv python tests/test_song_clip.py
"""

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import song_clip  # noqa: E402

SR = 22050
TMP = tempfile.mkdtemp(prefix="song_clip_test_")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), name, detail)


def synth_segment(sec, kind, rng):
    """生成一段具有区分度和弦的音频（返回 float32 波形）。"""
    t = np.arange(int(sec * SR)) / SR
    if kind == "chorus":
        # 明亮大三和弦 + 强节奏幅度
        x = (np.sin(2 * np.pi * 523.25 * t) * 0.4
             + np.sin(2 * np.pi * 659.25 * t) * 0.3
             + np.sin(2 * np.pi * 783.99 * t) * 0.3)
        x *= 0.95 * (0.7 + 0.3 * np.abs(np.sin(2 * np.pi * 2.0 * t)))  # 节奏起伏
    elif kind == "verse":
        x = (np.sin(2 * np.pi * 440.0 * t) * 0.5
             + np.sin(2 * np.pi * 329.63 * t) * 0.3)
        x *= 0.45
    elif kind == "bridge":
        x = np.sin(2 * np.pi * 261.63 * t) * 0.35
    else:  # intro / outro
        x = rng.normal(0, 0.03, len(t))
    return (x + rng.normal(0, 0.01, len(t))).astype(np.float32)


def main():
    rng = np.random.default_rng(42)
    # 结构：intro 10s / verse 20s / chorus 25s / verse 20s / chorus 25s /
    #       bridge 15s / chorus 25s / outro 10s   → 第一段副歌起点 = 30s
    plan = [("intro", 10), ("verse", 20), ("chorus", 25),
            ("verse", 20), ("chorus", 25), ("bridge", 15),
            ("chorus", 25), ("outro", 10)]
    parts = [synth_segment(s, k, rng) for k, s in plan]
    song = np.concatenate(parts)
    chorus_start = sum(d for k, d in plan[:2])  # = 30

    wav_path = os.path.join(TMP, "fake_song.wav")
    pcm = (np.clip(song, -1, 1) * 32767).astype("<i2").tobytes()
    import wave
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)

    # 也转一份 mp3，验证 mp3 输入链路
    mp3_path = os.path.join(TMP, "fake_song.mp3")
    subprocess.run(["ffmpeg", "-y", "-i", wav_path, "-acodec", "libmp3lame",
                    "-ab", "128k", mp3_path, "-loglevel", "quiet"], check=True)

    cache_dir = os.path.join(TMP, "cache")
    r = song_clip.find_chorus_clip(mp3_path, duration=30, out_format="mp3",
                                   cache_dir=cache_dir)
    print("result:", json.dumps(r, ensure_ascii=False))
    check("提取成功", r is not None)
    if not r:
        return
    check("走结构分析路线", r.get("method") == "structure", f"method={r.get('method')}")
    off = abs(r["start"] - chorus_start)
    check("副歌起点接近第一段副歌(30s, ±4s)", off <= 4.0, f"start={r['start']} 偏差={off:.2f}s")
    clip_len = r["end"] - r["start"]
    check("结尾吸附不超过最大时长(30s)", clip_len <= 30.2 and clip_len >= 20.0,
          f"clip时长={clip_len:.2f}s")

    clip = r["clip_path"]
    check("成片文件存在", os.path.isfile(clip), os.path.basename(clip))
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "json", clip], capture_output=True, text=True)
    dur = float(json.loads(probe.stdout)["format"]["duration"])
    check("成片时长 ≤ 30s(吸附后)", 20.0 <= dur <= 30.5, f"duration={dur:.2f}s")
    enc = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries",
                          "stream=codec_name", "-of", "json", clip],
                         capture_output=True, text=True)
    codec = json.loads(enc.stdout)["streams"][0]["codec_name"]
    check("编码格式正确(mp3)", codec == "mp3", codec)

    # 缓存命中：第二次调用应直接返回同一文件
    r2 = song_clip.find_chorus_clip(mp3_path, duration=30, out_format="mp3",
                                    cache_dir=cache_dir)
    check("二次调用命中缓存", r2 == r or r2["clip_path"] == r["clip_path"])

    # wav 输出格式预设
    r3 = song_clip.find_chorus_clip(mp3_path, duration=15, out_format="wav",
                                    cache_dir=cache_dir)
    check("wav 输出可用", r3 is not None and r3["clip_path"].endswith(".wav"))

    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"\n{len(RESULTS) - n_fail}/{len(RESULTS)} passed")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
