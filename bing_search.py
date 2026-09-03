# -*- coding: utf-8 -*-
"""
Bing 联网搜索 — 纯 HTTP 抓取（不依赖浏览器）

用 aiohttp 直接请求 bing.com 的搜索结果页，正则解析标题/摘要/链接。
比 Playwright 启动浏览器快得多，也省去浏览器安装这一步。

返回: [{title, snippet, url}, ...]
"""

import re
import html as _html
from urllib.parse import quote

import aiohttp

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_SEARCH_URL = "https://www.bing.com/search?q={q}&setlang=zh-CN&count=20&mkt=zh-CN"


def _strip_tags(s):
    return re.sub(r"<[^>]+>", "", s)


def _clean(s):
    s = _strip_tags(s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _parse(html, max_results):
    results = []
    # Bing 的结果在 <li class="b_algo"> 里
    blocks = re.split(r'<li class="b_algo"', html)[1:]
    for block in blocks:
        if len(results) >= max_results:
            break
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url = _html.unescape(m.group(1))
        title = _clean(m.group(2))
        snippet = ""
        sm = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        if sm:
            snippet = _clean(sm.group(1))
        if title:
            results.append({"title": title, "snippet": snippet, "url": url})
    return results


async def search(query, max_results=5, timeout=15):
    """在 Bing 搜索 query，返回 [{title, snippet, url}, ...]。失败返回空列表，不抛异常。"""
    url = _SEARCH_URL.format(q=quote(query))
    headers = {
        "User-Agent": _UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as r:
                if r.status != 200:
                    return []
                html = await r.text()
    except Exception:
        return []
    return _parse(html, max_results)


if __name__ == "__main__":
    import asyncio
    import sys
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    async def _main():
        rs = await search("今天天气", 3)
        print(f"共 {len(rs)} 条")
        for r in rs:
            print("-", r["title"])
            print(" ", r["snippet"][:100])
            print(" ", r["url"])

    asyncio.run(_main())
