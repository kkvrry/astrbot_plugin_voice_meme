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
    """最小化 session 替身：按 URL 返回预置 payload；可按关键词区分响应。"""

    def __init__(self, payloads, by_keyword=None):
        self._payloads = payloads  # {url片段: payload}
        self._by_keyword = by_keyword or {}  # {关键词片段: payload}
        self.headers = {}

    def get(self, url, **kwargs):
        keyword = str(kwargs.get("params", {}).get("keyword", ""))
        for frag, payload in self._by_keyword.items():
            if frag in keyword:
                return FakeResp(payload)
        for frag, payload in self._payloads.items():
            if frag in url:
                return FakeResp(payload)
        raise AssertionError(f"unexpected url: {url} keyword={keyword}")


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

    # 3) _search_top：取前 N 条、跳过非视频、HTML 高亮剥离
    payloads = {
        "search/type": {"code": 0, "data": {"result": [
            {"type": "activity_mar", "bvid": "BVad", "title": "广告"},
            {"type": "video", "bvid": "BV1ok",
             "title": '<em class="keyword">白金</em>ディスコ  高清',
             "author": "某UP"},
            {"type": "video", "bvid": "BV1next", "title": "第二首", "author": "b"},
            {"type": "video", "bvid": "BV1third", "title": "第三首", "author": "c"},
            {"type": "video", "bvid": "BV1four", "title": "第四首", "author": "d"},
        ]}},
        "web-interface/nav": {"code": 0, "data": {"wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/abc123def456abc123def456abc123de.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/987654321098765432109876543210ab.png",
        }}},
    }
    sess = FakeSession(payloads)
    top3 = bili._search_top(sess, "白金", 3)
    check("搜索取前3条", [c["bvid"] for c in top3] == ["BV1ok", "BV1next", "BV1third"], str(top3))
    check("搜索标题剥离HTML", top3 and "<em" not in top3[0]["title"] and " " in top3[0]["title"])
    check("搜索UP主缺省", top3 and top3[0]["uploader"] == "某UP")
    check("搜索limit截断", len(bili._search_top(sess, "白金", 2)) == 2)

    # 4) parse_pick：LLM 回复解析
    check("解析纯数字", bili.parse_pick("2", 3) == 2)
    check("解析夹杂文本", bili.parse_pick("我选 3 号", 3) == 3)
    check("解析越界为None", bili.parse_pick("5", 3) is None)
    check("解析无数字None", bili.parse_pick("都不合适", 3) is None)
    check("解析空文本None", bili.parse_pick("", 3) is None)
    check("解析零无效", bili.parse_pick("0", 3) is None)

    # 5) _search_first：空结果 / 接口报错
    nav = {"code": 0, "data": {"wbi_img": {
        "img_url": "https://i0.hdslb.com/bfs/wbi/abc123def456abc123def456abc123de.png",
        "sub_url": "https://i0.hdslb.com/bfs/wbi/987654321098765432109876543210ab.png",
    }}}
    empty = FakeSession({"search/type": {"code": 0, "data": {"result": []}},
                         "web-interface/nav": nav})
    try:
        bili.search_candidates("x", None, 3, session=empty)
        check("搜索空结果抛异常", False)
    except RuntimeError as e:
        check("搜索空结果抛异常", "没有搜到" in str(e))
    err = FakeSession({"search/type": {"code": -412, "message": "请求被拦截"},
                       "web-interface/nav": nav})
    try:
        bili.search_candidates("x", None, 3, session=err)
        check("搜索接口报错抛异常", False)
    except RuntimeError as e:
        check("搜索接口报错抛异常", "请求被拦截" in str(e))

    # 5) WBI 签名：wts/w_rid 存在且参数不含被过滤字符
    signed = bili._signed_params(sess, {"keyword": "a'b(c)", "page": 1})
    check("WBI签名字段", "wts" in signed and "w_rid" in signed and "(" not in signed["keyword"])

    # 6) 缓存读写：meta + index + resolve_cached
    save_dir = os.path.join(TMP, "cache_online")
    os.makedirs(save_dir)
    check("缓存未命中", bili._load_cached(save_dir, "BVnone") is None)
    check("resolve_cached无索引None", bili.resolve_cached("晴天", save_dir) is None)
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
    check("resolve_cached缓存失效None", bili.resolve_cached("晴天", save_dir) is None)

    idx = os.path.join(save_dir, "_index.json")
    bili._save_index(idx, "白金", "BV1ok")
    with open(idx, encoding="utf-8") as fp:
        check("索引写入", json.load(fp).get("白金") == "BV1ok")
    bili._save_index(os.path.join(TMP, "no_dir", "x", "_i.json"), "q", "BV")  # 不抛异常即可
    check("索引异常目录容错", True)

    # 7) download_pick 缓存命中路径（mp3 已存在时不联网）
    with open(mp3, "w") as fp:
        fp.write("x")
    pick_meta = bili.download_pick(
        "晴天", {"bvid": "BV1ok", "title": "搜索标题", "uploader": "u"},
        None, save_dir)
    check("download_pick复用缓存", pick_meta["title"] == "白金ディスコ", str(pick_meta))
    try:
        bili.download_pick("晴天", {"bvid": "", "title": "x"}, None, save_dir)
        check("download_pick无效候选抛异常", False)
    except RuntimeError:
        check("download_pick无效候选抛异常", True)

    # 7) 汇总
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    if fails:
        for name, _, detail in fails:
            print("  FAIL:", name, detail)
        sys.exit(1)


if __name__ == "__main__":
    main()
