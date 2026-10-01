# -*- coding: utf-8 -*-
"""
song_clip.py — 歌曲"副歌片段"提取模块（自包含，无 AstrBot 依赖）

链路：结构分析（副歌定位）→ 切点吸附（气口）→ ffmpeg 一次完成裁剪 + 转码 + 淡入淡出

分析策略（两级）：
  1. structure：librosa 可用时，自相似矩阵 + Foote novelty 找段边界，
     段间用 mean-chroma 余弦相似度分组，"重复次数多 + 能量高"的段 = 副歌。
     全程在整曲 mix 上完成，不需要人声分离。
  2. energy：librosa 不可用时，纯 numpy 能量包络找最响窗口（近似副歌）。

切点不依赖分离：人声乐句间必有短暂能量低谷（气口），把裁剪点吸附到最近的低谷，
配合 60ms 淡入淡出，听感不会切断唱词。

缓存：分析结果与成品片段均按 md5(路径|mtime|size|参数) 落盘，同文件只分析一次。
"""

import hashlib
import json
import os
import subprocess

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None
    _HAS_NUMPY = False

SR = 22050          # 分析用采样率
HOP = 512           # 分析帧移
FPS_RESAMPLE = 1    # 自相似矩阵降到 1 秒/帧，4 分钟歌矩阵只有 ~240x240
MIN_SEG_SEC = 12    # 副歌段最短时长
NOVELTY_KERNEL = 16 # Foote checkerboard 半窗（秒）
SEG_SIM_THRESHOLD = 0.92   # 段落判为"同一段"的 mean-chroma 余弦阈值
MIN_CLIP_SEC = 25          # 成品最短时长：在此之后寻找第一个自然气口收尾
GAP_SEARCH_SEC = 3.0       # 切点吸附搜索半径
LEAD_IN_SEC = 0.5          # 起点前预留量：吸附到气口后再往前退一点，避免从唱词中间起头
FADE_SEC = 0.1             # 淡入淡出时长

# 输出格式预设：与插件既有 _get_wav_path 约定对齐
OUT_FORMATS = {
    "wav": ["-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000"],
    "mp3": ["-acodec", "libmp3lame", "-ab", "192k", "-ar", "44100", "-ac", "2"],
}

try:
    import librosa
    _HAS_LIBROSA = True
except ImportError:
    librosa = None
    _HAS_LIBROSA = False


# ---------------------------------------------------------------- 缓存辅助

def _file_key(path: str, *extra) -> str:
    st = os.stat(path)
    raw = f"{os.path.abspath(path)}|{st.st_mtime}|{st.st_size}|{'|'.join(map(str, extra))}"
    return hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()


def _load_json_cache(cache_dir: str, key: str):
    p = os.path.join(cache_dir, key + ".json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _save_json_cache(cache_dir: str, key: str, data):
    os.makedirs(cache_dir, exist_ok=True)
    p = os.path.join(cache_dir, key + ".json")
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


# ---------------------------------------------------------------- ffmpeg 辅助

def _run_ffmpeg(args, timeout=180):
    subprocess.run(
        ["ffmpeg", "-y", *args, "-loglevel", "quiet"],
        check=True, timeout=timeout,
    )


def _decode_raw(path: str) -> tuple:
    """ffmpeg 解码为 16k 单声道 float32 raw，返回 (samples, sr)。"""
    cmd = [
        "ffmpeg", "-y", "-i", path,
        "-acodec", "pcm_f32le", "-ac", "1", "-ar", str(SR),
        "-f", "f32le", "-loglevel", "quiet", "-",
    ]
    out = subprocess.run(cmd, capture_output=True, timeout=180, check=True).stdout
    return np.frombuffer(out, dtype=np.float32), SR


# ---------------------------------------------------------------- 结构分析（librosa 路线）

def _aggregate_1s(features: np.ndarray) -> np.ndarray:
    """把帧级特征按整秒聚合，返回 (n_dim, T_sec)。"""
    n, t = features.shape
    t_sec = t * HOP // SR
    usable = min(t, t_sec * SR // HOP)
    feats = features[:, :usable]
    per_sec = SR // HOP
    usable = (usable // per_sec) * per_sec
    return feats[:, :usable].reshape(n, -1, per_sec).mean(axis=2)


def _foote_boundaries(S: np.ndarray, half: int) -> list:
    """Foote checkerboard novelty 沿对角线找段边界（单位：秒帧）。"""
    T = S.shape[0]
    cb = np.ones((2 * half, 2 * half))
    cb[:half, half:] = -1
    cb[half:, :half] = -1
    novelty = np.zeros(T)
    for t in range(half, T - half):
        novelty[t] = (S[t - half:t + half, t - half:t + half] * cb).sum()
    # 取显著峰，最小间距 8 秒
    peaks = []
    thr = novelty.mean() + novelty.std() * 0.3
    for t in range(half, T - half):
        if novelty[t] < thr:
            continue
        if novelty[t] < novelty[max(0, t - 4):t + 5].max():
            continue
        if peaks and t - peaks[-1] < 8:
            if novelty[t] > novelty[peaks[-1]]:
                peaks[-1] = t
            continue
        peaks.append(t)
    return [0] + peaks + [T]


def _analyze_structure(y: np.ndarray) -> dict:
    """自相似 + 段落分组，返回 {"start": float, "end": float, "segments": [...]}。"""
    chroma = librosa.feature.chroma_cqt(y=y, sr=SR, hop_length=HOP)
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    C = _aggregate_1s(chroma)                     # (12, T)
    E = _aggregate_1s(rms.reshape(1, -1))[0]      # (T,)
    # 列归一化后余弦自相似
    Cn = C / (np.linalg.norm(C, axis=0, keepdims=True) + 1e-9)
    S = Cn.T @ Cn

    half = max(4, NOVELTY_KERNEL // 2)
    bounds = _foote_boundaries(S, half)

    # 段特征与能量
    segs = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        if b - a < 4:   # 太碎的段不参与
            continue
        feat = Cn[:, a:b].mean(axis=1)
        feat /= (np.linalg.norm(feat) + 1e-9)
        segs.append({"a": a, "b": b, "feat": feat,
                     "energy": float(E[a:b].mean())})
    if len(segs) < 2:
        return {}

    # 分组：重复次数多 + 能量高者优先
    best, best_score = None, -1.0
    for s in segs:
        reps = sum(
            1 for o in segs
            if o is not s and float(o["feat"] @ s["feat"]) > SEG_SIM_THRESHOLD
        )
        dur = s["b"] - s["a"]
        if dur < MIN_SEG_SEC or reps == 0:
            continue
        score = reps * dur * (0.5 + s["energy"])
        if score > best_score:
            best_score, best = score, s
    if best is None:
        return {}

    # 副歌起点：取"能量达标组"里能量最高的一次出现（高潮感最强），
    # 而非首次出现——前奏常铺副歌和弦（mean-chroma 无法区分）且能量可能不低，
    # 取最高能量 + 跳过开头 8% 的前奏区，可避免起点落在前奏/主歌（真实曲库冒烟测得的坑）
    members = [s for s in segs
               if float(s["feat"] @ best["feat"]) > SEG_SIM_THRESHOLD]
    loud = [s for s in members if s["energy"] >= best["energy"] * 0.75]
    ref = loud if loud else members
    total = segs[-1]["b"] if segs else 0
    pool = [s for s in ref if s["a"] >= total * 0.08] or ref
    start = max(pool, key=lambda s: s["energy"])["a"]
    end = max(s["b"] for s in ref)
    # 副歌首次出现段的自然结束（仅作参考信息，收尾长度由气口检测决定）
    first = min(ref, key=lambda s: s["a"])
    return {"start": float(start), "end": float(end),
            "first_end": float(first["b"])}


# ---------------------------------------------------------------- 能量兜底（无 librosa）

def _analyze_energy(y: np.ndarray, duration: float = 30.0) -> dict:
    """最响窗口作为副歌近似（无 librosa 时的兜底）。

    分析窗固定 30 秒（定位用，与裁剪时长解耦——收尾长度由气口检测决定）；
    起点从窗口起点向前回退到"持续响度起点"，避免落在乐句中间。
    """
    per_sec = SR
    n = len(y) // per_sec
    if n < 40:
        return {}
    env = np.abs(y[: n * per_sec]).reshape(n, per_sec).mean(axis=1)
    win = 30
    energy = np.convolve(env, np.ones(win) / win, mode="valid")
    peak = int(np.argmax(energy))
    # 起点细化：向前回退到能量跌破区域均值 60% 的位置，其下一秒即持续响度起点
    region = float(env[peak:peak + win].mean()) + 1e-9
    s = peak
    while s > 0 and env[s] >= region * 0.6:
        s -= 1
    s = min(s + 1, peak)
    return {"start": float(s), "end": float(s + win)}


# ---------------------------------------------------------------- 气口吸附

def _find_valley_end(y: np.ndarray, start: float, min_len: float,
                     max_len: float):
    """在 [start+min_len, start+max_len] 内找第一个持续的能量低谷作为收尾切点。

    低谷 = 20ms 粗包络（300ms 平滑）持续 ≥0.4s 低于区域参考值的谷；
    深度阈值从严格到宽松逐级放宽（25%/35%/45%），取最早的满足项。
    返回谷底时间（秒），找不到返回 None。
    """
    frame = SR // 50
    i0 = int((start + min_len) * SR)
    i1 = min(len(y), int((start + max_len) * SR))
    if i1 - i0 < SR:
        return None
    seg = np.abs(y[i0:i1])
    n = (len(seg) // frame) * frame
    env = seg[:n].reshape(-1, frame).mean(axis=1)
    k = max(3, int(0.3 * 50))     # 300ms 平滑
    env = np.convolve(env, np.ones(k) / k, mode="same")
    ref = np.percentile(env, 90) + 1e-9
    for frac in (0.25, 0.35, 0.45):
        idx = np.where(env < ref * frac)[0]
        if not len(idx):
            continue
        groups = np.split(idx, np.where(np.diff(idx) > 1)[0] + 1)
        runs = [g for g in groups if len(g) >= 20]   # 持续 ≥0.4s
        if runs:
            g = runs[0]              # 最早出现的满足项
            return (i0 + int(g[int(np.argmin(env[g]))]) * frame) / SR
    return None


def _snap_to_gap(y: np.ndarray, t_sec: float, before_only: bool = False) -> float:
    """在 t_sec 附近找能量包络最低点（换气/乐句间隙）作为切点。

    before_only=True 时只向前（时间负方向）搜索——用于"最大时长"约束下的
    结尾切点：宁可提前收在气口，也不超过设定时长。
    """
    i = int(t_sec * SR)
    w = int(GAP_SEARCH_SEC * SR)
    lo, hi = max(0, i - w), min(len(y), i + 1 if before_only else i + w)
    if hi - lo < SR // 2:
        return float(t_sec)
    seg = np.abs(y[lo:hi])
    frame = SR // 50  # 20ms 帧取均值 → 粗包络
    n = (len(seg) // frame) * frame
    env = seg[:n].reshape(-1, frame).mean(axis=1)
    k = max(3, int(0.15 * 50))  # 150ms 平滑
    env = np.convolve(env, np.ones(k) / k, mode="same")
    return float(lo + int(np.argmin(env)) * frame) / SR


# ---------------------------------------------------------------- 对外接口

def find_chorus_clip(path: str, duration: float = 30.0,
                     out_format: str = "mp3", cache_dir: str = None) -> dict:
    """
    主入口：定位副歌 → 气口吸附 → ffmpeg 一次完成裁剪+转码+淡入淡出。

    返回 {"clip_path", "start", "end", "method"}；失败返回 None。
    """
    if not os.path.isfile(path):
        return None
    if out_format not in OUT_FORMATS:
        out_format = "mp3"
    if not _HAS_NUMPY:
        return None
    if cache_dir is None:
        # 默认落在插件根目录下（clip.py 位于 core/music/，向上三级）
        cache_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "cache_clip")
    os.makedirs(cache_dir, exist_ok=True)

    # 1. 分析缓存（v5：起点取能量最高副歌 + 气口收尾）
    akey = _file_key(path, "v5")
    info = _load_json_cache(cache_dir, akey)
    method = info.get("method") if info else None

    if info is None:
        y, _ = _decode_raw(path)   # 解码一次，分析与吸附共用
        if len(y) < SR * 15:       # 过短音频没有"副歌"可言
            return None
        if _HAS_LIBROSA:
            try:
                info = _analyze_structure(y)
                method = "structure"
            except Exception:
                info = {}
        if not info:
            info = _analyze_energy(y, duration)
            method = "energy"
        if not info:
            return None
        info["method"] = method
        _save_json_cache(cache_dir, akey, info)

    # 2. 切点吸附（气口）
    y = None
    if info.get("snapped"):
        start, end = info["start"], info["end"]
    else:
        y, _ = _decode_raw(path)
        # 起点只向前吸附：落在片段开始前的气口/间奏上，
        # 再往前预留 LEAD_IN_SEC，让副歌人声自然进入，而不是从唱词中间开始
        start = _snap_to_gap(y, max(0.0, info["start"]), before_only=True)
        start = max(0.0, start - LEAD_IN_SEC)
        # 收尾长度由真实段落气口决定：在 [start+MIN_CLIP_SEC, start+duration]
        # 内找第一个持续低谷（乐句/段落间隙），长度随歌自然变化；
        # 找不到才退化为"起点+上限"并向前吸附气口。music_clip_max_sec 为硬上限
        lo = min(MIN_CLIP_SEC, duration)
        valley = _find_valley_end(y, start, lo, duration)
        if valley:
            end = min(valley, start + duration)
        else:
            end = _snap_to_gap(y, start + duration, before_only=True)
        end = min(end, len(y) / SR)
        if end - start < 5.0:      # 音频本身比设定时长还短，能切多少切多少
            end = len(y) / SR
        info.update(start=round(start, 3), end=round(end, 3), snapped=True)
        _save_json_cache(cache_dir, akey, info)

    # 3. 成品缓存
    ckey = _file_key(path, "clip_v3", round(start, 2), round(end, 2), out_format)
    clip_path = os.path.join(cache_dir, ckey + "." + out_format)
    if not os.path.exists(clip_path):
        eff_dur = max(1.0, end - start)
        fade_out_st = max(0.0, eff_dur - FADE_SEC)
        af = (f"afade=t=in:st=0:d={FADE_SEC},"
              f"afade=t=out:st={fade_out_st:.3f}:d={FADE_SEC}")
        _run_ffmpeg([
            "-ss", f"{start:.3f}", "-i", path, "-t", f"{eff_dur:.3f}",
            "-af", af, *OUT_FORMATS[out_format], clip_path,
        ])

    return {"clip_path": clip_path, "start": round(start, 2),
            "end": round(end, 2), "method": info.get("method", method)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python song_clip.py <音频文件> [时长秒] [wav|mp3]")
        sys.exit(1)
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    fmt = sys.argv[3] if len(sys.argv) > 3 else "mp3"
    r = find_chorus_clip(sys.argv[1], dur, fmt)
    print(r if r else "提取失败")
