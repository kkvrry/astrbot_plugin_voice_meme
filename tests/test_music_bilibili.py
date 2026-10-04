# -*- coding: utf-8 -*-
"""test_music_bilibili.py — B站兜底纯逻辑单测（不联网）。运行：python tests/test_music_bilibili.py"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.music import bilibili as bili  # noqa: E402

TMP = tempfile.mkdtemp(prefix="bili_test_")
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), name, detail)


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """最小化 session 替身：按 URL 返回预置 payload。"""

    def __init__(self, payloads):
        self._payloads = payloads  # {url片段: payload}
        self.headers = {}

    def get(self, url, **kwargs):
        for frag, payload in self._payloads.items():
            if frag in url:
                return FakeResp(payload)
        raise AssertionError(f"unexpected url: {url}")


def main():
    # 1) cookie 读取：正常 / 缺失 / 损坏
    cf = os.path.join(TMP, "cookie.json")
    with open(cf, "w", encoding="utf-8") as fp:
        json.dump({"cookie": "SESSDATA=abc; buvid=xyz"}, fp)
    check("cookie_正常读取", bili._load_cookie(cf) == "SESSDATA=abc; buvid=xyz")
    check("cookie_文件缺失", bili._load_cookie(os.path.join(TMP, "nope.json")) == "")
    bad = os.path.join(TMP, "bad.json")
    with open(bad, "w", encoding="utf-8") as fp:
        fp.write("{broken")
    check("cookie_损坏容错", bili._load_cookie(bad) == "")
    check("cookie_路径为空", bili._load_cookie(None) == "")

    # 2) safe_filename：非法字符与长度截断
    fn = bili.safe_filename('白金/ディスコ:*?"<>| 副歌版', "BV1xx411c7mD")
    check("文件名净化", "/" not in fn and ":" not in fn and fn.endswith("BV1xx411c7mD.mp3"), fn)
    long_fn = bili.safe_filename("超" * 100, "BV1xx411c7mD")
    check("文件名截断80", len(long_fn) - len("-BV1xx411c7mD.mp3") <= 80 * 3)  # utf-8 中文按字符截断
    check("空标题兜底", bili.safe_filename("", "BV1") == "bilibili-BV1.mp3")

    # 3) _search_first：取第一个视频结果、跳过非视频、HTML 高亮剥离
    payloads = {
        "search/type": {"code": 0, "data": {"result": [
            {"type": "activity_mar", "bvid": "BVad", "title": "广告"},
            {"type": "video", "bvid": "BV1ok",
             "title": '<em class="keyword">白金</em>ディスコ  高清',
             "author": "某UP"},
            {"type": "video", "bvid": "BV1next", "title": "第二首", "author": "b"},
        ]}},
        "web-interface/nav": {"code": 0, "data": {"wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/abc123def456abc123def456abc123de.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/987654321098765432109876543210ab.png",
        }}},
    }
    sess = FakeSession(payloads)
    first = bili._search_first(sess, "白金")
    check("搜索取首个视频", first is not None and first["bvid"] == "BV1ok", str(first))
    check("搜索标题剥离HTML", first and "<em" not in first["title"] and " " in first["title"])
    check("搜索UP主缺省", first and first["uploader"] == "某UP")

    # 4) _search_first：空结果 / 接口报错
    nav = {"code": 0, "data": {"wbi_img": {
        "img_url": "https://i0.hdslb.com/bfs/wbi/abc123def456abc123def456abc123de.png",
        "sub_url": "https://i0.hdslb.com/bfs/wbi/987654321098765432109876543210ab.png",
    }}}
    empty = FakeSession({"search/type": {"code": 0, "data": {"result": []}},
                         "web-interface/nav": nav})
    check("搜索空结果", bili._search_first(empty, "x") is None)
    err = FakeSession({"search/type": {"code": -412, "message": "请求被拦截"},
                       "web-interface/nav": nav})
    try:
        bili._search_first(err, "x")
        check("搜索接口报错抛异常", False)
    except RuntimeError as e:
        check("搜索接口报错抛异常", "请求被拦截" in str(e))

    # 5) WBI 签名：wts/w_rid 存在且参数不含被过滤字符
    signed = bili._signed_params(sess, {"keyword": "a'b(c)", "page": 1})
    check("WBI签名字段", "wts" in signed and "w_rid" in signed and "(" not in signed["keyword"])

    # 6) 缓存读写：meta + index
    save_dir = os.path.join(TMP, "cache_online")
    os.makedirs(save_dir)
    check("缓存未命中", bili._load_cached(save_dir, "BVnone") is None)
    mp3 = os.path.join(save_dir, "song-BV1ok.mp3")
    with open(mp3, "w") as fp:
        fp.write("x")
    meta = {"path": mp3, "title": "白金ディスコ", "uploader": "某UP", "bvid": "BV1ok"}
    with open(os.path.join(save_dir, "BV1ok.json"), "w", encoding="utf-8") as fp:
        json.dump(meta, fp, ensure_ascii=False)
    hit = bili._load_cached(save_dir, "BV1ok")
    check("缓存命中", hit is not None and hit["title"] == "白金ディスコ")
    # mp3 丢失 → 缓存失效
    os.unlink(mp3)
    check("缓存mp3丢失失效", bili._load_cached(save_dir, "BV1ok") is None)

    idx = os.path.join(save_dir, "_index.json")
    bili._save_index(idx, "白金", "BV1ok")
    with open(idx, encoding="utf-8") as fp:
        check("索引写入", json.load(fp).get("白金") == "BV1ok")
    bili._save_index(os.path.join(TMP, "no_dir", "x", "_i.json"), "q", "BV")  # 不抛异常即可
    check("索引异常目录容错", True)

    # 7) 汇总
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    if fails:
        for name, _, detail in fails:
            print("  FAIL:", name, detail)
        sys.exit(1)


if __name__ == "__main__":
    main()
