"""网络层：抓取、限速、重试、图片下载、链接发现。

设计取向是"稳定可恢复"而不是"暴力抓取"：
  - 每域名最小请求间隔（delay），避免触发 429/403
  - 指数退避重试（retry），并对 429/503 尊重 Retry-After
  - 双层并行：文章级 + 图片级，各自独立线程池
  - 缓存：已抓过的 URL 直接命中本地缓存，支持断点续跑
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .utils import clean_text, ensure_dir, human_size, log, safe_filename, short_hash

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 book-forge/1.0"
)

_IMG_EXT_OK = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif"}
_MIME_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
    "image/bmp": ".bmp", "image/avif": ".avif",
}


# ---------------------------------------------------------------- 限速器

class RateLimiter:
    """按域名限速：保证同一域名两次请求间隔 >= delay。"""

    def __init__(self, delay: float = 0.6, jitter: float = 0.3):
        self.delay = max(0.0, delay)
        self.jitter = max(0.0, jitter)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        if self.delay <= 0:
            return
        host = urllib.parse.urlparse(url).netloc
        with self._lock:
            now = time.monotonic()
            prev = self._last.get(host, 0.0)
            gap = self.delay + random.uniform(0, self.jitter)
            sleep_for = prev + gap - now
            self._last[host] = max(now, prev + gap)
        if sleep_for > 0:
            time.sleep(min(sleep_for, 30.0))


# ---------------------------------------------------------------- 结果

@dataclass
class FetchResult:
    url: str
    ok: bool = False
    status: int = 0
    text: str = ""
    content: bytes = b""
    content_type: str = ""
    from_cache: bool = False
    attempts: int = 0
    error: str = ""
    elapsed: float = 0.0


# ---------------------------------------------------------------- Fetcher

class Fetcher:
    def __init__(self, *, delay: float = 0.6, retry: int = 3,
                 timeout: float = 25.0, cache_dir: str | Path | None = None,
                 user_agent: str = DEFAULT_UA,
                 respect_robots: bool = True, verbose: bool = True):
        self.delay = delay
        self.retry = max(0, retry)
        self.timeout = timeout
        self.verbose = verbose
        self.limiter = RateLimiter(delay)
        self.respect_robots = respect_robots
        self.cache_dir = ensure_dir(Path(cache_dir)) if cache_dir else None
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        })

    # ------------------------------------------------ 缓存

    def _cache_path(self, url: str, kind: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / f"{short_hash(url, 16)}.{kind}"

    def _cache_get(self, url: str, kind: str) -> FetchResult | None:
        p = self._cache_path(url, kind)
        if not p or not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return FetchResult(
                url=url, ok=True, status=data.get("status", 200),
                text=data.get("text", ""),
                content=bytes.fromhex(data["body"]) if data.get("body") else b"",
                content_type=data.get("content_type", ""),
                from_cache=True,
            )
        except Exception:
            return None

    def _cache_put(self, url: str, kind: str, r: FetchResult) -> None:
        p = self._cache_path(url, kind)
        if not p:
            return
        try:
            p.write_text(json.dumps({
                "url": url, "status": r.status, "text": r.text,
                "content_type": r.content_type,
                "body": r.content.hex() if r.content else "",
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------ robots

    def _robots_for(self, url: str):
        if not self.respect_robots:
            return None
        parts = urllib.parse.urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            if origin in self._robots:
                return self._robots[origin]
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(origin + "/robots.txt")
        try:
            self.limiter.wait(origin + "/robots.txt")
            resp = self.session.get(origin + "/robots.txt", timeout=10)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp = None
        except Exception:
            rp = None
        with self._robots_lock:
            self._robots[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.session.headers.get("User-Agent", "*"), url)
        except Exception:
            return True

    # ------------------------------------------------ 主入口

    def get(self, url: str, *, kind: str = "html", use_cache: bool = True,
            store_cache: bool = True, skip_robots: bool = False) -> FetchResult:
        if use_cache:
            hit = self._cache_get(url, kind)
            if hit is not None:
                if self.verbose:
                    log(f"缓存命中 {url}", "dim" if False else "info")
                return hit
        if not skip_robots and not self.allowed(url):
            return FetchResult(url=url, ok=False, error="robots.txt 不允许抓取")

        headers = {}
        if kind == "image":
            headers["Accept"] = "image/avif,image/webp,image/*,*/*;q=0.8"
            if self.cache_dir:
                headers["Referer"] = url

        last_err = ""
        t0 = time.monotonic()
        for attempt in range(1, self.retry + 2):
            self.limiter.wait(url)
            try:
                resp = self.session.get(url, timeout=self.timeout,
                                        headers=headers, allow_redirects=True)
                res = FetchResult(url=url, status=resp.status_code,
                                  content_type=resp.headers.get("Content-Type", ""),
                                  attempts=attempt)
                if resp.status_code == 200:
                    res.ok = True
                    if kind == "image":
                        res.content = resp.content
                    else:
                        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                            resp.encoding = resp.apparent_encoding or "utf-8"
                        res.text = resp.text
                    if store_cache:
                        self._cache_put(url, kind, res)
                    res.elapsed = time.monotonic() - t0
                    return res

                res.error = f"HTTP {resp.status_code}"
                last_err = res.error
                if resp.status_code in (401, 403, 404, 410, 451):
                    res.elapsed = time.monotonic() - t0
                    return res          # 重试无意义
                if resp.status_code in (429, 503):
                    ra = resp.headers.get("Retry-After", "")
                    wait = float(ra) if ra.replace(".", "").isdigit() else 2 ** attempt
                    wait = min(wait, 60)
                    if self.verbose:
                        log(f"限流 {resp.status_code}，等待 {wait:.0f}s 后重试：{url}", "warn")
                    time.sleep(wait)
                    continue
            except requests.exceptions.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"

            if attempt <= self.retry:
                back = min(2 ** (attempt - 1) + random.uniform(0, 0.5), 20)
                time.sleep(back)

        return FetchResult(url=url, ok=False, error=last_err or "抓取失败",
                           attempts=self.retry + 1,
                           elapsed=time.monotonic() - t0)

    def get_text(self, url: str, **kw) -> FetchResult:
        return self.get(url, kind="html", **kw)

    def get_image(self, url: str, **kw) -> FetchResult:
        kw.setdefault("kind", "image")
        return self.get(url, **kw)


# ---------------------------------------------------------------- 图片优化

def optimize_image_bytes(content: bytes, ext: str, *, max_width: int = 900,
                         quality: int = 80) -> tuple[bytes, str]:
    """把图片压到适合电子书的大小。返回 (bytes, 新后缀)。

    只做有把握的事，任何一步不确定就原样返回：
      - 动图（多帧 GIF/WebP）不动，避免把动画压成静帧
      - SVG 不动（矢量图本来就不大）
      - 有透明通道 → 存 PNG；否则 → 存 JPEG（体积小得多）
      - 只有变小的结果才会被采用
    """
    if not content or ext.lower() in (".svg", ".svgz", ".avif"):
        return content, ext
    try:
        from PIL import Image
    except Exception:
        return content, ext
    try:
        im = Image.open(io.BytesIO(content))
        if getattr(im, "n_frames", 1) > 1:
            return content, ext
        im.load()
        w, h = im.size
        if w <= 0 or h <= 0:
            return content, ext
        if max_width and w > max_width:
            im = im.resize((max_width, max(1, int(h * max_width / w))),
                           Image.LANCZOS)
        has_alpha = im.mode in ("RGBA", "LA") or (
            im.mode == "P" and "transparency" in im.info)
        buf = io.BytesIO()
        if has_alpha:
            im.convert("RGBA").save(buf, "PNG", optimize=True)
            new_ext = ".png"
        else:
            im.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True,
                                   progressive=True)
            new_ext = ".jpg"
        out = buf.getvalue()
        if len(out) >= len(content):
            return content, ext
        return out, new_ext
    except Exception:
        return content, ext


# ---------------------------------------------------------------- 图片下载

def download_images(fetcher: Fetcher, urls: list[str], dest_dir: str | Path,
                    *, workers: int = 6, max_bytes: int = 12 * 1024 * 1024,
                    min_bytes: int = 512, dedupe: bool = True,
                    max_width: int = 0, quality: int = 80) -> dict[str, str]:
    """并行下载图片。返回 {原始URL: 相对文件名}。

    失败的图片只是不进入 map，由调用方决定是否保留外链。
    dedupe=True 时按内容哈希去重：多篇文章复用的同一张图只存一份。
    max_width>0 时顺带压缩（电子书场景强烈建议开）。
    """
    dest_dir = ensure_dir(Path(dest_dir))
    result: dict[str, str] = {}
    lock = threading.Lock()
    used_names: set[str] = {p.name.lower() for p in dest_dir.iterdir() if p.is_file()}
    by_hash: dict[str, str] = {}
    stats = {"dedup": 0, "shrunk": 0, "saved": 0, "failed": 0, "reasons": {}}

    def _fail(reason: str) -> tuple[str, str]:
        stats["failed"] += 1
        stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
        return "", ""

    def one(url: str) -> tuple[str, str]:
        try:
            r = fetcher.get_image(url, use_cache=False, store_cache=False)
            if not r.ok or not r.content:
                err = (r.error or "")
                # 区分"被 robots.txt 拒绝"和"真的挂了"——前者用户加 --no-robots
                # 就能解决，后者才需要排查。混在一起会让人误以为工具坏了。
                if "robots" in err.lower():
                    return _fail("robots")
                return _fail(f"http-{r.status}" if r.status else "network")
            if len(r.content) > max_bytes:
                return _fail("too_big")
            if len(r.content) < min_bytes and not r.content.startswith(b"<svg"):
                return _fail("too_small")
            ext = _MIME_EXT.get(r.content_type.split(";")[0].strip().lower(), "")
            if not ext:
                parsed = urllib.parse.urlparse(url).path
                ext = Path(parsed).suffix.lower()
                if ext not in _IMG_EXT_OK:
                    ext = mimetypes.guess_extension(r.content_type.split(";")[0].strip()) or ".jpg"
            body = r.content
            if max_width:
                body, ext = optimize_image_bytes(body, ext, max_width=max_width,
                                                 quality=quality)
                if len(body) < len(r.content):
                    stats["shrunk"] += 1
                    stats["saved"] += len(r.content) - len(body)
            if dedupe:
                h = hashlib.sha1(body).hexdigest()
                with lock:
                    if h in by_hash:
                        stats["dedup"] += 1
                        return url, by_hash[h]
            stem = safe_filename(Path(urllib.parse.urlparse(url).path).stem or "image",
                                 maxlen=48)
            if not stem or stem == "image":
                stem = "img-" + short_hash(url, 8)
            name = f"{stem}{ext}"
            with lock:
                n = 2
                while name.lower() in used_names:
                    name = f"{stem}-{n}{ext}"
                    n += 1
                used_names.add(name.lower())
            (dest_dir / name).write_bytes(body)
            if dedupe:
                with lock:
                    by_hash[h] = name
            return url, name
        except Exception as e:
            return _fail(type(e).__name__)

    if not urls:
        return {}
    ulist = list(dict.fromkeys(u for u in urls if u and not u.startswith("data:")))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(one, u) for u in ulist]
        for fut in as_completed(futs):
            u, name = fut.result()
            if u and name:
                result[u] = name
    if dedupe or max_width or stats["failed"]:
        fetcher.last_image_stats = dict(stats)
    return result


# ---------------------------------------------------------------- 图片 URL 收集

def collect_image_urls(md_or_html: str, base_url: str = "",
                       *, pattern: str = "") -> list[str]:
    """从 Markdown 或 HTML 中收集图片 URL，可再用正则进一步筛选。"""
    urls: list[str] = []
    for m in re.finditer(r"!\[[^\]]*\]\(([^)\s]+)", md_or_html):
        urls.append(urllib.parse.urljoin(base_url, m.group(1)))
    for m in re.finditer(r"<img[^>]+src=[\"']([^\"']+)", md_or_html, re.I):
        urls.append(urllib.parse.urljoin(base_url, m.group(1)))
    for m in re.finditer(r"\b(?:src|data-src|data-original)=[\"']([^\"']+\.(?:jpg|jpeg|png|gif|webp|svg|avif))[\"']",
                         md_or_html, re.I):
        urls.append(urllib.parse.urljoin(base_url, m.group(1)))
    urls = [u for u in urls if u.startswith(("http://", "https://"))]
    out = list(dict.fromkeys(urls))
    if pattern:
        rx = re.compile(pattern, re.I)
        out = [u for u in out if rx.search(u)]
    return out


# ---------------------------------------------------------------- 入口页发现

_SKIP_LINK_PAT = re.compile(
    r"(\.(?:pdf|zip|rar|7z|tar|gz|exe|dmg|mp4|mp3|mov|avi|css|js|json|xml|ico|woff2?|ttf|otf)"
    r"|/(?:tag|tags|category|categories|author|page|search|login|signup|subscribe|feed|rss|"
    r"about|contact|privacy|terms|archive|donate|shop|cart)(?:/|$)"
    r"|^(?:mailto:|javascript:|#))",
    re.I,
)

_ARTICLE_HINT = re.compile(
    r"("
    r"/\d{4}/\d{1,2}/"          # 日期路径
    r"|/posts?/" r"|/articles?/" r"|/essays?/" r"|/blog/"
    r"|/\d{4}/" r"|/p/" r"|/notes?/"
    r"|\d{4}-\d{2}-\d{2}"       # 日期 slug
    r"|\.html?$"
    r")", re.I)


def discover_links(fetcher: Fetcher, entry_url: str, html: str,
                   base_url: str = "", *, same_host: bool = True,
                   min_hint: int = 0, include: str = "", exclude: str = "",
                   limit: int = 0) -> list[str]:
    """从入口页发现候选文章链接，按"像文章"的程度排序。"""
    from .extract import parse_html, prune, strip_tags

    base_url = base_url or entry_url
    root_host = urllib.parse.urlparse(base_url).netloc
    try:
        tree = parse_html(html)
        prune(tree)
        anchors = tree.xpath("//a[@href]")
    except Exception:
        anchors = []

    entries: list[tuple[str, int, str]] = []
    seen = set()
    for a in anchors:
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "#", "tel:")):
            continue
        url = urllib.parse.urljoin(base_url, href)
        url = url.split("#")[0]
        if not url.startswith(("http://", "https://")):
            continue
        if same_host and urllib.parse.urlparse(url).netloc != root_host:
            continue
        if _SKIP_LINK_PAT.search(url):
            continue
        if url.rstrip("/") == base_url.rstrip("/"):
            continue
        if include and not re.search(include, url, re.I):
            continue
        if exclude and re.search(exclude, url, re.I):
            continue
        if url in seen:
            continue
        seen.add(url)

        try:
            text = clean_text(strip_tags(etree_tostring(a)))
        except Exception:
            text = clean_text(a.text_content() if hasattr(a, "text_content") else "")
        score = 0
        if _ARTICLE_HINT.search(url):
            score += 3
        if len(text) >= 12:
            score += 2
        if len(text) >= 25:
            score += 1
        # 单层路径通常不是文章
        if url.rstrip("/").count("/") <= urllib.parse.urlparse(root_host).path.count("/") + 2:
            score += 1
        entries.append((url, score, text))

    entries.sort(key=lambda x: (-x[1], x[0]))

    # 先按 URL 自身像不像文章筛一遍；样本不足时放宽
    if min_hint and entries:
        strong = [e for e in entries if e[1] >= min_hint]
        if len(strong) >= 3:
            entries = strong

    out = [e[0] for e in entries]
    return out[:limit] if limit else out


def etree_tostring(el) -> str:
    from lxml import etree
    try:
        return etree.tostring(el, encoding="unicode", method="text")
    except Exception:
        return ""


def discover_from_sitemap(fetcher: Fetcher, sitemap_url: str, *,
                          include: str = "", limit: int = 0) -> list[str]:
    """从 sitemap.xml（含 sitemapindex 递归）收集 URL。"""
    out: list[str] = []
    queue = [sitemap_url]
    seen_sm = set()
    while queue and len(out) < (limit or 10 ** 9):
        sm = queue.pop(0)
        if sm in seen_sm:
            continue
        seen_sm.add(sm)
        r = fetcher.get_text(sm)
        if not r.ok:
            continue
        body = r.text
        if "<sitemapindex" in body.lower():
            for m in re.finditer(r"<loc>\s*([^<\s]+)\s*</loc>", body, re.I):
                queue.append(m.group(1).strip())
            continue
        for m in re.finditer(r"<loc>\s*([^<\s]+)\s*</loc>", body, re.I):
            u = m.group(1).strip()
            if include and not re.search(include, u, re.I):
                continue
            out.append(u)
    return out[:limit] if limit else out


def discover_from_feed(fetcher: Fetcher, feed_url: str, *, limit: int = 0) -> list[str]:
    """从 RSS / Atom 收集文章链接。"""
    r = fetcher.get_text(feed_url)
    if not r.ok:
        return []
    body = r.text
    urls: list[str] = []
    # RSS: <link>URL</link>  /  Atom: <link href="URL"/>
    for m in re.finditer(r"<link>\s*(https?://[^<\s]+)\s*</link>", body, re.I):
        urls.append(m.group(1))
    for m in re.finditer(r"<link[^>]+href=[\"'](https?://[^\"']+)[\"']", body, re.I):
        urls.append(m.group(1))
    for m in re.finditer(r"<guid[^>]*>\s*(https?://[^<\s]+)\s*</guid>", body, re.I):
        urls.append(m.group(1))
    out = list(dict.fromkeys(urls))
    return out[:limit] if limit else out
