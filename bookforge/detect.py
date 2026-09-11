"""站点自动探测：给一个网址，判断"这是什么站、文章怎么找、书叫什么名"。

这一步是"丢个网址就出书"的关键。没有它，agent 得先人肉判断站点类型、
去找 RSS / sitemap、再猜书名和作者 —— 每一步都要多一轮对话、多烧 token。

探测顺序（越靠前越可靠）：

1. **已注册的站点适配器**（含用户放在 ``bookforge-adapters/`` 的声明式适配器）
2. **WordPress REST API**：``/wp-json/wp/v2/posts``。
   最大的好处不只是能列文章，而是**能顺便把分类体系拿来做分章**（见 ``groups.py``）。
3. **RSS / Atom**：从首页 ``<link rel="alternate">`` 找，再试常见路径
4. **sitemap.xml**：试常见路径与 robots.txt 里的 Sitemap 声明
5. **HTML 列表页**：兜底，从给定页面里抽同站链接

同时会解析出书名 / 简介 / 作者等元数据，让 agent 少问几个问题。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .utils import clean_text, log, strip_tags

# 常见 RSS / sitemap 路径
FEED_PATHS = ["/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml",
              "/feed.xml", "/index.xml", "/rss/index.xml", "/feed/atom"]
SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                 "/sitemap.xml.gz", "/wp-sitemap.xml"]

_ARTICLES_CACHE: dict[str, list[str]] = {}


@dataclass
class SiteProfile:
    """一次探测的结论。"""

    url: str
    kind: str = "unknown"        # adapter | wordpress | feed | sitemap | list | article
    title: str = ""
    author: str = ""
    description: str = ""
    language: str = ""
    entry_urls: list[str] = field(default_factory=list)
    feed_url: str = ""
    sitemap_url: str = ""
    api_root: str = ""
    adapter: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "url": self.url, "kind": self.kind, "title": self.title,
            "author": self.author, "description": self.description,
            "language": self.language, "entry_urls": self.entry_urls,
            "feed_url": self.feed_url, "sitemap_url": self.sitemap_url,
            "api_root": self.api_root, "adapter": self.adapter,
            "notes": self.notes,
        }


# ---------------------------------------------------------------- 工具


def _origin(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _meta(html: str, *names: str) -> str:
    """按 meta name/property 抽内容（小而够用，不引入 bs4）。"""
    for name in names:
        for pat in (
            rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]*'
            rf'content=["\']([^"\']*)["\']',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*'
            rf'(?:name|property)=["\']{re.escape(name)}["\']',
        ):
            m = re.search(pat, html, re.I)
            if m:
                v = clean_text(m.group(1))
                if v:
                    return v
    return ""


def _page_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return clean_text(strip_tags(m.group(1))) if m else ""


def _clean_site_name(raw: str) -> str:
    """把 '<title>' 里的站名收拾干净：去掉「首页」「Home」「| 副标题」之类。"""
    t = clean_text(raw)
    t = re.sub(r"\s*[-|–—·:：]\s*(首页|主页|Home|home|Homepage)\s*$", "", t)
    t = re.sub(r"\s*[-|–—]\s*[^-|–—]{0,40}$", "", t) if "|" in t else t
    return t.strip() or clean_text(raw)


def _looks_like_feed(text: str) -> bool:
    head = (text or "")[:1200].lower()
    return ("<rss" in head or "<feed" in head
            or "<rdf:rdf" in head or "<?xml" in head and "<channel" in head)


def _looks_like_sitemap(text: str) -> bool:
    head = (text or "")[:1200].lower()
    return "<urlset" in head or "<sitemapindex" in head


# ---------------------------------------------------------------- WordPress


def _try_wordpress(fetcher, base: str) -> tuple[str, dict] | None:
    """探测 WordPress REST API。返回 (api_root, site_info) 或 None。"""
    for path in ("/wp-json", "/?rest_route=/"):
        try:
            r = fetcher.get(base + path, use_cache=True)
        except Exception:
            continue
        if not r.ok or not r.text:
            continue
        try:
            info = json.loads(r.text)
        except json.JSONDecodeError:
            continue
        if not isinstance(info, dict) or "routes" not in info:
            continue
        names = info.get("namespaces") or []
        if "wp/v2" not in names and not any("wp/v2" in str(n) for n in names):
            continue
        api_root = base + "/wp-json/wp/v2"
        # 确认 posts 端点真的能返回文章
        try:
            probe = fetcher.get(
                api_root + "/posts?per_page=1&_fields=link,title,date")
            if not probe.ok:
                continue
            data = json.loads(probe.text)
            if not isinstance(data, list):
                continue
        except Exception:
            continue
        return api_root, info
    return None


# ---------------------------------------------------------------- feed / sitemap


def _find_feed(fetcher, base: str, html: str = "") -> str:
    if html:
        m = re.search(
            r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>',
            html, re.I)
        if m:
            href = re.search(r'href=["\']([^"\']+)["\']', m.group(0), re.I)
            if href:
                return urllib.parse.urljoin(base + "/", href.group(1))
    for p in FEED_PATHS:
        try:
            r = fetcher.get(base + p, use_cache=True)
        except Exception:
            continue
        if r.ok and r.text and _looks_like_feed(r.text):
            return base + p
    return ""


def _find_sitemap(fetcher, base: str, html: str = "") -> str:
    # robots.txt 里的声明最权威
    try:
        r = fetcher.get(base + "/robots.txt", use_cache=True)
        if r.ok and r.text:
            for m in re.finditer(r"(?im)^\s*sitemap:\s*(\S+)", r.text):
                cand = m.group(1).strip()
                if cand.lower().endswith(".xml") or "sitemap" in cand.lower():
                    return cand
    except Exception:
        pass
    for p in SITEMAP_PATHS:
        if p.endswith(".gz"):
            continue
        try:
            r = fetcher.get(base + p, use_cache=True)
        except Exception:
            continue
        if r.ok and r.text and _looks_like_sitemap(r.text):
            return base + p
    return ""


# ---------------------------------------------------------------- 主入口


def detect(fetcher, url: str, *, try_sitemap: bool = True) -> SiteProfile:
    """探测一个网址，返回站点画像。不会抛异常（失败时 kind='list'）。"""
    from . import sites as _sites

    prof = SiteProfile(url=url)
    base = _origin(url)

    # 1) 命中已注册适配器？
    adapter = _sites.find_adapter(url)
    if adapter is not None and adapter.name != "generic":
        prof.kind = "adapter"
        prof.adapter = adapter.name
        try:
            prof.entry_urls = list(adapter.entry_urls(base) or [])
        except Exception:
            prof.entry_urls = []
        if not prof.entry_urls:
            prof.entry_urls = [url]
        prof.notes.append(f"命中站点适配器 {adapter.name}")
        _enrich_meta(fetcher, url, prof)
        # 适配器只管正文提取，不影响"能不能分章"——
        # 如果它同时也是 WordPress，顺手把 API 拿上，分章就有分类体系可用。
        wp = _try_wordpress(fetcher, base)
        if wp:
            prof.api_root = wp[0]
            info = wp[1]
            prof.notes.append("同时检测到 WordPress REST API，可用分类体系分章")
            if not prof.title:
                prof.title = clean_text(info.get("name") or "")
            if not prof.description:
                prof.description = clean_text(info.get("description") or "")
        return prof

    # 取首页 HTML（顺便给后面的 meta 解析用）
    html = ""
    try:
        r = fetcher.get(url, use_cache=True)
        if r.ok:
            html = r.text or ""
    except Exception as e:
        prof.notes.append(f"首页抓取失败：{type(e).__name__}")

    _parse_meta(html, prof, url)

    # 2) WordPress
    wp = _try_wordpress(fetcher, base)
    if wp:
        api_root, info = wp
        prof.kind = "wordpress"
        prof.api_root = api_root
        site_name = clean_text(info.get("name") or "")
        site_desc = clean_text(info.get("description") or "")
        if site_name and not prof.title:
            prof.title = site_name
        if site_desc and not prof.description:
            prof.description = site_desc
        prof.notes.append("检测到 WordPress REST API，可直接用分类体系分章")
        return prof

    # 3) RSS / Atom
    feed = _find_feed(fetcher, base, html)
    if feed:
        prof.kind = "feed"
        prof.feed_url = feed
        prof.entry_urls = [feed]
        prof.notes.append(f"使用 RSS/Atom：{feed}")
        return prof

    # 4) sitemap
    if try_sitemap:
        sm = _find_sitemap(fetcher, base, html)
        if sm:
            prof.kind = "sitemap"
            prof.sitemap_url = sm
            prof.entry_urls = [sm]
            prof.notes.append(f"使用 sitemap：{sm}")
            return prof

    # 5) 兜底：当列表页爬
    prof.kind = "list"
    prof.entry_urls = [url]
    prof.notes.append("未找到 feed/sitemap，按 HTML 列表页抽链接")
    return prof


def _parse_meta(html: str, prof: SiteProfile, url: str) -> None:
    if not html:
        return
    title = (_meta(html, "og:site_name", "application-name", "twitter:title")
             or _page_title(html))
    if title:
        prof.title = _clean_site_name(title)
    desc = _meta(html, "description", "og:description")
    if desc:
        prof.description = desc
    author = _meta(html, "author", "article:author", "og:article:author")
    if author:
        prof.author = author
    lang = ""
    m = re.search(r"<html[^>]+lang=[\"']([a-zA-Z\-_]+)[\"']", html, re.I)
    if m:
        lang = m.group(1)
    if lang:
        lang = lang.split("-")[0].lower()
        prof.language = "zh-CN" if lang == "zh" else lang


def _enrich_meta(fetcher, url: str, prof: SiteProfile) -> None:
    try:
        r = fetcher.get(url, use_cache=True)
        if r.ok:
            _parse_meta(r.text or "", prof, url)
    except Exception:
        pass


def split_title_author(raw: str) -> tuple[str, str]:
    """把「书名 — 作者」这类站点标题拆成两半（尽力而为）。"""
    t = clean_text(raw)
    for sep in (" - ", " – ", " — ", " by ", " 著 ", "·"):
        if sep in t:
            left, _, right = t.rpartition(sep)
            if 0 < len(right) <= 20 and len(left) > 1:
                return left.strip(), right.strip()
    return t, ""
