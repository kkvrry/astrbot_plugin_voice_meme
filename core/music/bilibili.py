"""B 站点歌兜底：本地曲库没有时，搜索 B 站取前几条候选，由 LLM 挑选后下载音频。

链路：WBI 签名搜索（取前 N 条）→ LLM 选出最适合点歌的一条（无 LLM 时取首条）
→ 解析 DASH 音轨 → 下载 m4a → ffmpeg 转 mp3（192k）→ 交给副歌裁剪/文件发送。

按 bvid 落盘缓存：同一首歌重复点播不重复下载（LLM 也只跑一次），且稳定的文件
路径可复用 clip.py 基于「路径+mtime+size」的副歌分析缓存。

Cookie：从 data_dir/bilibili_cookie.json 读取（JSON: {"cookie": "..."}）。
未配置 Cookie 时仍会尝试（部分视频无需登录即可解析），失败时给出明确提示。
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import time
from urllib.parse import urlencode

BILIBILI_API = "https://api.bilibili.com"
BILIBILI_WEB = "https://www.bilibili.com"
_NAV_URL = f"{BILIBILI_API}/x/web-interface/nav"
_SEARCH_URL = f"{BILIBILI_API}/x/web-interface/wbi/search/type"
_VIEW_URL = f"{BILIBILI_API}/x/web-interface/wbi/view"
_PLAY_URL = f"{BILIBILI_API}/x/player/wbi/playurl"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# WBI 密钥重排表（B 站公开约定，与官方前端一致）
_MIXIN_INDICES = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27,
    43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48,
    7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54,
    21, 56, 62, 6, 63, 57, 20, 34, 52, 59, 11, 36, 44,
)
_WBI_FILTER_RE = re.compile(r"[!'()*]")
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")

#: 下载体积硬上限（防异常大文件写满磁盘）
_MAX_AUDIO_BYTES = 200 * 1024 * 1024


# ---------------------------------------------------------------- 工具

def _strip_html(value) -> str:
    return _WHITESPACE_RE.sub(
        " ", _HTML_TAG_RE.sub("", html.unescape(str(value or "")))
    ).strip()


def _load_cookie(cookie_file: str | None) -> str:
    """读取 cookie 文件（JSON: {"cookie": "..."}），缺失/损坏返回空串。"""
    if not cookie_file or not os.path.isfile(cookie_file):
        return ""
    try:
        with open(cookie_file, encoding="utf-8") as fp:
            data = json.load(fp)
        return str(data.get("cookie") or "").strip()
    except Exception:
        return ""


def _session(cookie: str):
    import requests
    s = requests.Session()
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"{BILIBILI_WEB}/",
    }
    if cookie:
        headers["Cookie"] = cookie
    s.headers.update(headers)
    return s


def _wbi_key(session) -> str:
    """从 nav 接口取 WBI 签名密钥（img_key + sub_key 重排前 32 位）。"""
    r = session.get(_NAV_URL, timeout=10)
    r.raise_for_status()
    wbi = (r.json().get("data") or {}).get("wbi_img") or {}
    img = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".", 1)[0]
    sub = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".", 1)[0]
    raw = img + sub
    mixed = "".join(raw[i] for i in _MIXIN_INDICES if i < len(raw))
    if len(mixed) < 32:
        raise RuntimeError("B站未返回有效 WBI 密钥")
    return mixed[:32]


def _signed_params(session, params: dict) -> dict:
    """对请求参数做 WBI 签名（wts + w_rid）。"""
    key = _wbi_key(session)
    clean = {
        str(k): _WBI_FILTER_RE.sub("", str(v))
        for k, v in params.items() if v is not None
    }
    clean["wts"] = str(int(time.time()))
    ordered = dict(sorted(clean.items()))
    query = urlencode(tuple(ordered.items()))
    ordered["w_rid"] = hashlib.md5(f"{query}{key}".encode("utf-8")).hexdigest()
    return ordered


def _api_data(session, url: str, params: dict) -> dict:
    try:
        r = session.get(url, params=_signed_params(session, params), timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:
        raise RuntimeError(f"B站请求失败: {exc}") from exc
    if payload.get("code") != 0:
        raise RuntimeError(
            str(payload.get("message") or payload.get("msg") or "B站接口错误"))
    return payload.get("data") or {}


def _search_top(session, query: str, limit: int = 5) -> list[dict]:
    """搜索视频并返回前 limit 条结果 [{bvid, title, uploader}]。"""
    data = _api_data(session, _SEARCH_URL, {
        "search_type": "video",
        "keyword": str(query or "").strip(),
        "order": "totalrank",
        "duration": 0,
        "tids": 0,
        "page": 1,
        "page_size": min(max(1, limit), 20),
    })
    raw_items = data.get("result")
    if not isinstance(raw_items, list):
        return []
    results = []
    for raw in raw_items:
        if not isinstance(raw, dict) or raw.get("type") not in (None, "video"):
            continue
        bvid = str(raw.get("bvid") or "").strip()
        title = _strip_html(raw.get("title"))
        if not (bvid and title):
            continue
        results.append({
            "bvid": bvid,
            "title": title,
            "uploader": _strip_html(raw.get("author")) or "未知UP",
        })
        if len(results) >= limit:
            break
    return results


def search_candidates(query: str, cookie_file: str | None,
                      limit: int = 5, session=None) -> list[dict]:
    """搜索并返回前 limit 条候选（供 LLM 挑选）；无结果抛 RuntimeError。

    session 供测试注入替身；生产留空时按 cookie_file 自建会话。
    """
    if not str(query or "").strip():
        raise RuntimeError("搜索关键词为空")
    if session is None:
        session = _session(_load_cookie(cookie_file))
    results = _search_top(session, query, limit)
    if not results:
        raise RuntimeError("B站没有搜到相关视频")
    return results


def parse_pick(text: str, n: int) -> int | None:
    """从 LLM 回复中解析候选编号（1..n），无有效编号返回 None。"""
    m = re.search(r"\d+", str(text or ""))
    if not m:
        return None
    idx = int(m.group())
    return idx if 1 <= idx <= n else None


def _audio_track(session, bvid: str) -> tuple[str, str]:
    """解析视频详情，返回 (音频流 URL, 视频标题)。"""
    data = _api_data(session, _VIEW_URL, {"bvid": bvid})
    title = _strip_html(data.get("title")) or bvid
    pages = data.get("pages")
    if isinstance(pages, list) and pages:
        cid = int(pages[0].get("cid") or 0)
    else:
        cid = int(data.get("cid") or 0)
    if cid <= 0:
        raise RuntimeError("该视频没有可用的音频")

    play = _api_data(session, _PLAY_URL, {
        "bvid": bvid,
        "cid": cid,
        "qn": 80,
        "fnval": 16,   # DASH 格式
        "fnver": 0,
        "fourk": 0,
        "platform": "pc",
    })
    dash = play.get("dash")
    tracks = (dash or {}).get("audio") if isinstance(dash, dict) else None
    if not (isinstance(tracks, list) and tracks):
        raise RuntimeError("B站未返回可下载的音频流（可能需要登录 Cookie）")
    # 优先 ≤192kbps 中最高码率；没有则取最低码率保证能下
    usable = [t for t in tracks if int(t.get("bandwidth") or 0) <= 192000]
    track = (max(usable, key=lambda t: int(t.get("bandwidth") or 0)) if usable
             else min(tracks, key=lambda t: int(t.get("bandwidth") or 0)))
    url = str(track.get("baseUrl") or track.get("base_url") or "").strip()
    if not url:
        raise RuntimeError("音频流 URL 为空")
    return url, title


def safe_filename(title: str, bvid: str) -> str:
    """把 B 站标题转成本地安全的 mp3 文件名。"""
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", str(title or ""))
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return f"{cleaned[:80] or 'bilibili'}-{bvid}.mp3"


def _download_audio(session, url: str, bvid: str, dst: str) -> None:
    """下载音频流并转 mp3（m4a 中转后删除）。"""
    import requests
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Referer": f"{BILIBILI_WEB}/video/{bvid}/",
    }
    if session.headers.get("Cookie"):
        headers["Cookie"] = session.headers["Cookie"]
    m4a = dst[:-4] + ".m4a"
    try:
        with requests.get(url, headers=headers, stream=True, timeout=30) as r:
            r.raise_for_status()
            total = 0
            with open(m4a, "wb") as fh:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _MAX_AUDIO_BYTES:
                        raise RuntimeError("音频超过 200MB 上限，已中止")
                    fh.write(chunk)
        subprocess.run(
            ["ffmpeg", "-y", "-i", m4a, "-vn",
             "-acodec", "libmp3lame", "-b:a", "192k", dst],
            check=True, capture_output=True, timeout=180,
        )
    finally:
        try:
            os.unlink(m4a)
        except OSError:
            pass


def resolve_cached(query: str, save_dir: str) -> dict | None:
    """查询关键词已有下载缓存（query → bvid 索引命中）时直接返回元数据。"""
    query = str(query or "").strip()
    if not query:
        return None
    try:
        with open(os.path.join(save_dir, "_index.json"), encoding="utf-8") as fp:
            index = json.load(fp)
        bvid = str(index.get(query) or "").strip()
        if bvid:
            return _load_cached(save_dir, bvid)
    except Exception:
        pass
    return None


def download_pick(query: str, pick: dict, cookie_file: str | None,
                  save_dir: str) -> dict:
    """下载选定候选的音频（选自 search_candidates / LLM 挑选）。

    返回 {"path": mp3路径, "title": 标题, "uploader": UP主, "bvid": bvid}。
    按 bvid 落盘缓存（<save_dir>/<bvid>.mp3 + .json 元数据），重复点播直接复用。
    抛 RuntimeError 携带面向用户的失败原因。
    """
    query = str(query or "").strip()
    bvid = str((pick or {}).get("bvid") or "").strip()
    if not (query and bvid):
        raise RuntimeError("点歌候选无效")

    os.makedirs(save_dir, exist_ok=True)
    meta_index = os.path.join(save_dir, "_index.json")

    # 音频文件已存在（别的关键词点过同一首）→ 直接复用
    cached = _load_cached(save_dir, bvid)
    if cached:
        _save_index(meta_index, query, bvid)
        return cached

    title = str(pick.get("title") or bvid)
    session = _session(_load_cookie(cookie_file))
    url, page_title = _audio_track(session, bvid)
    if page_title:
        title = page_title  # 详情页标题比搜索结果更干净（无高亮标签）
    dst = os.path.join(save_dir, safe_filename(title, bvid))
    try:
        _download_audio(session, url, bvid, dst)
    except RuntimeError:
        raise
    except Exception as exc:
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise RuntimeError(f"音频下载失败: {exc}") from exc
    if not os.path.isfile(dst) or os.path.getsize(dst) <= 0:
        raise RuntimeError("音频转码失败")

    meta = {"path": dst, "title": title, "uploader": pick.get("uploader") or "未知UP",
            "bvid": bvid}
    try:
        with open(os.path.join(save_dir, f"{bvid}.json"), "w", encoding="utf-8") as fp:
            json.dump(meta, fp, ensure_ascii=False)
    except Exception:
        pass
    _save_index(meta_index, query, bvid)
    return meta


def _load_cached(save_dir: str, bvid: str) -> dict | None:
    """按 bvid 读取已下载的缓存（mp3 + 元数据都要在才算命中）。"""
    meta_file = os.path.join(save_dir, f"{bvid}.json")
    if not os.path.isfile(meta_file):
        return None
    try:
        with open(meta_file, encoding="utf-8") as fp:
            meta = json.load(fp)
        path = str(meta.get("path") or "")
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            return meta
    except Exception:
        pass
    return None


def _save_index(meta_index: str, query: str, bvid: str) -> None:
    """把 query → bvid 写入关键词索引（供缓存命中加速，失败不影响主流程）。"""
    try:
        index: dict = {}
        if os.path.isfile(meta_index):
            with open(meta_index, encoding="utf-8") as fp:
                index = json.load(fp)
        index[query] = bvid
        with open(meta_index, "w", encoding="utf-8") as fp:
            json.dump(index, fp, ensure_ascii=False)
    except Exception:
        pass
