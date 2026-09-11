#!/usr/bin/env python
"""Stage 1 · web-to-markdown

从网页抓取文章，转成结构化本地内容库（归档包）。

    python stage1_fetch.py --entry https://example.com/articles.html --out ./out/my-book \\
        --title "My Book" --author "Someone"

支持的入口类型（自动识别）：
    列表页 / 索引页     自动发现文章链接
    sitemap.xml         递归收集 URL
    RSS / Atom feed     按发布时间收集
    单篇文章页          直接抓这一篇

输出（归档包规范）：
    <out>/
    ├── content/001-xxx.md ...     正文 Markdown（含 YAML frontmatter）
    ├── assets/                    本地化后的图片
    ├── metadata/book.json         书籍级元数据
    ├── manifest.json              单一可信源
    └── reports/quality-report.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse



from . import sites as sites_mod
from .archive import (Archive, Article, article_meta, build_frontmatter)
from .extract import (extract_article, strip_common_title_suffix,
                     strip_site_suffix)
from .fetch import (DEFAULT_UA, Fetcher, collect_image_urls,
                            discover_from_feed, discover_from_sitemap,
                            discover_links, download_images)
from .utils import (banner, clean_text, count_words, ensure_dir, human_size,
                            log, now_iso, relpath, safe_filename, short_hash, slugify)

IMG_PH = "%%BFIMG:{}%%"


# ---------------------------------------------------------------- 参数

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stage1_fetch",
        description="抓取网页文章并转成结构化 Markdown 归档包",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python stage1_fetch.py --entry https://paulgraham.com/articles.html \\\n"
               "      --out ./out/pg --title \"Paul Graham Essays\" --author \"Paul Graham\"\n")
    src = p.add_argument_group("来源")
    src.add_argument("--entry", action="append", default=[],
                     help="入口页 URL（可重复指定）")
    src.add_argument("--urls-file", help="URL 列表文件，每行一个")
    src.add_argument("--from-sitemap", action="append", default=[],
                     help="sitemap.xml URL（可重复）")
    src.add_argument("--from-feed", action="append", default=[],
                     help="RSS/Atom URL（可重复）")
    src.add_argument("--groups-file",
                     help="分组清单 JSON（主题/细分类型 → URL）。给了它就以清单顺序"
                          "为书内顺序，并把主题写进 manifest，供 stage5 排嵌套目录")

    out = p.add_argument_group("输出与书籍元数据")
    out.add_argument("--out", required=True, help="归档包输出目录")
    out.add_argument("--title", default="", help="书名")
    out.add_argument("--subtitle", default="", help="副标题")
    out.add_argument("--author", default="", help="作者")
    out.add_argument("--language", default="zh-CN", help="目标语言（默认 zh-CN）")
    out.add_argument("--source-language", default="", help="原文语言（留空自动判断）")
    out.add_argument("--description", default="", help="书籍简介")
    out.add_argument("--publisher", default="", help="出版方")
    out.add_argument("--date", default="", help="出版日期 YYYY-MM-DD")
    out.add_argument("--subject", default="", help="主题标签，逗号分隔")

    sel = p.add_argument_group("筛选")
    sel.add_argument("--max", type=int, default=0, help="最多抓取篇数（0=不限）")
    sel.add_argument("--include", default="", help="仅保留 URL 匹配该正则的链接")
    sel.add_argument("--exclude", default="", help="排除 URL 匹配该正则的链接")
    sel.add_argument("--same-host", dest="same_host", action="store_true", default=True)
    sel.add_argument("--any-host", dest="same_host", action="store_false",
                     help="允许跨域链接")
    sel.add_argument("--order", choices=["site", "reverse", "date-desc", "date-asc",
                                         "alpha"], default="site",
                     help="文章顺序（默认按站点出现顺序）")

    net = p.add_argument_group("网络")
    net.add_argument("--workers", type=int, default=5, help="文章级并行数（默认 5）")
    net.add_argument("--img-workers", type=int, default=6, help="图片级并行数（默认 6）")
    net.add_argument("--delay", type=float, default=0.6,
                     help="同域名请求最小间隔秒数（默认 0.6）")
    net.add_argument("--retry", type=int, default=3, help="失败重试次数（默认 3）")
    net.add_argument("--timeout", type=float, default=25.0, help="单请求超时秒数")
    net.add_argument("--no-cache", action="store_true", help="禁用抓取缓存（默认启用）")
    net.add_argument("--no-robots", action="store_true", help="忽略 robots.txt")

    img = p.add_argument_group("图片与正文")
    img.add_argument("--no-images", action="store_true", help="不下载图片")
    img.add_argument("--image-max-mb", type=float, default=12.0, help="单图大小上限")
    img.add_argument("--image-max-width", type=int, default=0,
                     help="图片最大宽度（像素）。>0 时自动缩放+重编码，"
                          "图文教程类书籍强烈建议开（如 900）")
    img.add_argument("--image-quality", type=int, default=80,
                     help="JPEG 重编码质量（默认 80，配合 --image-max-width 使用）")
    img.add_argument("--no-image-dedupe", action="store_true",
                     help="关闭图片按内容去重（默认开启）")
    img.add_argument("--heading-offset", type=int, default=None,
                     help="正文标题层级整体下移层数（默认用适配器建议值）")
    img.add_argument("--min-confidence", type=float, default=0.0,
                     help="低于该置信度的文章标记为 partial")

    misc = p.add_argument_group("其他")
    misc.add_argument("--cookie", help="自定义 Cookie 请求头")
    misc.add_argument("--user-agent", default="", help="自定义 User-Agent")
    misc.add_argument("--dry-run", action="store_true", help="只发现链接，不抓正文")
    misc.add_argument("--force", action="store_true", help="即使归档包已存在也重抓")
    misc.add_argument("--quiet", action="store_true")
    misc.add_argument("--site-name", default="",
                       help="站点名（用于剥掉标题尾部的「- 站名」）")
    return p


# ---------------------------------------------------------------- 链接发现

def resolve_sources(args, fetcher: Fetcher) -> list[dict]:
    """把各种入口统一解析成 [{url, section}] 列表。"""
    found: list[dict] = []
    seen: set[str] = set()

    def push(url: str, section: str = "") -> None:
        url = url.split("#")[0].strip()
        if not url or not url.startswith(("http://", "https://")):
            return
        if url in seen:
            return
        seen.add(url)
        found.append({"url": url, "section": section})

    # 1) 显式 URL 列表
    if args.urls_file:
        lines = Path(args.urls_file).read_text(encoding="utf-8").splitlines()
        for ln in lines:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                push(ln, "urls-file")

    # 2) sitemap
    for sm in args.from_sitemap:
        log(f"读取 sitemap：{sm}", "step")
        urls = discover_from_sitemap(fetcher, sm, include=args.include,
                                     limit=args.max)
        log(f"  → 发现 {len(urls)} 个 URL", "info")
        for u in urls:
            push(u, "sitemap")

    # 3) feed
    for fd in args.from_feed:
        log(f"读取 feed：{fd}", "step")
        urls = discover_from_feed(fetcher, fd, limit=args.max)
        log(f"  → 发现 {len(urls)} 个 URL", "info")
        for u in urls:
            push(u, "feed")

    # 4) 入口页
    for entry in args.entry:
        entry = entry.strip()
        if not entry:
            continue
        low = entry.lower()
        if low.endswith(".xml") or "sitemap" in low:
            urls = discover_from_sitemap(fetcher, entry, include=args.include,
                                         limit=args.max)
            for u in urls:
                push(u, "sitemap")
            continue
        if re.search(r"(\.rss$|\.atom$|/feed/?$|/rss/?$|rss\.html$)", low) or "feed" in low:
            urls = discover_from_feed(fetcher, entry, limit=args.max)
            if urls:
                for u in urls:
                    push(u, "feed")
                continue

        log(f"打开入口页：{entry}", "step")
        r = fetcher.get_text(entry)
        if not r.ok:
            log(f"入口页抓取失败：{r.error}", "err")
            continue

        adapter = sites_mod.find_adapter(entry)
        urls = adapter.discover(fetcher, entry, r.text, base_url=entry,
                                same_host=args.same_host,
                                include=args.include, exclude=args.exclude,
                                limit=args.max) if adapter else \
            discover_links(fetcher, entry, r.text, base_url=entry,
                           same_host=args.same_host, include=args.include,
                           exclude=args.exclude, limit=args.max)

        if len(urls) < 2:
            # 入口页本身就是一篇文章
            log("未发现子链接，按单篇文章处理", "info")
            push(entry, "single")
        else:
            log(f"  → 发现 {len(urls)} 篇候选（适配器："
                f"{adapter.name if adapter else 'generic'}）", "ok")
            for u in urls:
                if args.max and len(found) >= args.max:
                    break
                push(u, entry)

    if args.max:
        found = found[: args.max]
    return found


# ---------------------------------------------------------------- 单篇处理

def process_one(fetcher: Fetcher, item: dict, args, assets_dir: Path,
                img_placeholder_pool: dict) -> dict:
    """抓取并解析一篇文章。返回不含编号的中间结果。"""
    url = item["url"]
    r = fetcher.get_text(url)
    if not r.ok:
        return {"url": url, "ok": False, "error": r.error or "抓取失败",
                "status": "failed"}

    # 图片占位：先占位收集，稍后统一并行下载再回填
    def resolver(src, alt, el):
        ph = IMG_PH.format(short_hash(src, 12))
        img_placeholder_pool[ph] = src
        return ph

    res = extract_article(r.text, url,
                          image_resolver=None if args.no_images else resolver,
                          keep_images=not args.no_images,
                          heading_offset=args.heading_offset)

    status = "ok"
    notes = list(res.notes)
    if res.confidence < args.min_confidence:
        status = "partial"
        notes.append(f"置信度 {res.confidence} 低于阈值 {args.min_confidence}")

    return {
        "url": url,
        "ok": True,
        "status": status,
        "title": clean_text(res.title) or _title_from_url(url),
        "author": clean_text(res.author),
        "published_at": res.published_at,
        "summary": clean_text(res.summary)[:600],
        "markdown": res.markdown,
        "images": list(res.images),
        "language": res.language,
        "confidence": res.confidence,
        "strategy": res.strategy,
        "notes": notes,
        "word_count": count_words(res.markdown),
    }


def _title_from_url(url: str) -> str:
    path = urlparse(url).path
    stem = Path(path).stem or urlparse(url).netloc
    stem = re.sub(r"[-_]+", " ", stem).strip()
    return stem.title() if stem else url


# ---------------------------------------------------------------- 主流程

def _clean_titles(results: list[dict], site_name: str = "") -> int:
    """把「文章标题 - 站名」里的站名统一剥掉。

    两级策略：
      1. 已知站名（`--site-name`，由站点探测从首页拿到）→ 精确匹配后缀；
      2. 未知站名 → 看整批标题里有没有反复出现的尾巴，有就集体剥掉。

    只改 `title` 字段；正文里的重复标题早在提取阶段就处理过了。
    """
    fixed = 0
    if site_name:
        for r in results:
            new = strip_site_suffix(r.get("title", ""), site_name)
            if new and new != r.get("title"):
                r["title"] = new
                fixed += 1

    tails = strip_common_title_suffix([r.get("title", "") for r in results])
    if tails:
        for r in results:
            new = tails.get((r.get("title") or "").strip())
            if new:
                r["title"] = new
                fixed += 1
    if fixed:
        log(f"标题清洗：{fixed} 篇去掉了站点后缀", "info")
    return fixed


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "quiet", False):
        from .utils import set_color, set_quiet
        set_color(False)
        set_quiet(True)

    out_dir = Path(args.out).resolve()
    banner("book-forge · Stage 1 抓取", "网页 → 结构化 Markdown 内容库")

    if out_dir.exists() and (out_dir / "manifest.json").exists() and not args.force:
        ar = Archive.open(out_dir)
        log(f"归档包已存在（{len(ar.articles)} 篇），增量续抓", "warn")
    else:
        ar = Archive.create(out_dir)

    subject = [s.strip() for s in args.subject.split(",") if s.strip()]
    ar.set_book(
        id=slugify(args.title or out_dir.name, 60),
        title=args.title or out_dir.name,
        subtitle=args.subtitle, author=args.author or "",
        language=args.language, source_language=args.source_language,
        description=args.description, publisher=args.publisher, date=args.date,
        subject=subject,
    )

    cache_dir = None if args.no_cache else (out_dir / ".cache")
    fetcher = Fetcher(delay=args.delay, retry=args.retry, timeout=args.timeout,
                      cache_dir=cache_dir, respect_robots=not args.no_robots,
                      user_agent=args.user_agent or DEFAULT_UA)
    if args.cookie:
        fetcher.session.headers["Cookie"] = args.cookie

    # ---------------- 发现
    known = {a.source_url.split("#")[0] for a in ar.articles if a.source_url}
    log("解析来源入口…", "step")
    t_disc = time.monotonic()
    items = resolve_sources(args, fetcher)
    log(f"共 {len(items)} 篇候选（耗时 {time.monotonic() - t_disc:.1f}s）", "ok")

    # ---------------- 分组（主题 / 细分类型）
    groups_payload = None
    url2group: dict[str, tuple[str, str]] = {}
    if args.groups_file:
        from bookforge.wp import iter_group_urls, load_groups
        groups_payload = load_groups(args.groups_file)
        rows = iter_group_urls(groups_payload)
        for g, s, u in rows:
            url2group[u.split("#")[0]] = (g, s)
        if args.order == "site":
            # 分组清单的顺序就是书内顺序（部 → 章 → 节），不要让并行完成顺序打乱
            rank_by_url = {u.split("#")[0]: i for i, (_, _, u) in enumerate(rows)}
            items.sort(key=lambda it: rank_by_url.get(it["url"].split("#")[0], 10 ** 9))
        log(f"分组清单：{len(rows)} 条 URL，覆盖 {len(items)} 篇候选中的 "
            f"{sum(1 for it in items if it['url'].split('#')[0] in url2group)} 篇", "ok")

    if not items:
        log("没有发现任何文章。建议：换一个入口页，或用 --urls-file 直接给列表。", "err")
        return 2

    todo = [it for it in items if it["url"].split("#")[0] not in known]
    if len(todo) < len(items):
        log(f"其中 {len(items) - len(todo)} 篇已在归档包中，跳过", "info")

    if args.dry_run:
        print(json.dumps([it["url"] for it in todo], ensure_ascii=False, indent=2))
        log(f"[dry-run] 共 {len(todo)} 个待抓 URL", "ok")
        return 0

    # ---------------- 抓取 + 解析（文章级并行）
    log(f"并行抓取（{args.workers} 并发，间隔 {args.delay}s）…", "step")
    assets_dir = ensure_dir(ar.assets_dir)
    pool: dict[str, str] = {}
    image_failures: dict[str, int] = {}
    results: list[dict] = []
    t0 = time.monotonic()
    failed = 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(process_one, fetcher, it, args, assets_dir, pool): it
                for it in todo}
        done = 0
        for fut in as_completed(futs):
            it = futs[fut]
            done += 1
            try:
                res = fut.result()
            except Exception as e:
                res = {"url": it["url"], "ok": False,
                       "error": f"{type(e).__name__}: {e}", "status": "failed"}
            results.append(res)
            if res.get("ok"):
                flag = "" if res["status"] == "ok" else " !"
                log(f"[{done}/{len(todo)}] {res['title'][:48]} "
                    f"({res['word_count']} 词){flag}", "info")
            else:
                failed += 1
                log(f"[{done}/{len(todo)}] 失败：{it['url']} — {res.get('error')}", "err")

    log(f"抓取完成：成功 {len(results) - failed}／失败 {failed}"
        f"（{time.monotonic() - t0:.1f}s）", "ok" if failed == 0 else "warn")

    # ---------------- 图片并行下载
    if pool and not args.no_images:
        log(f"并行下载图片（{len(pool)} 张，{args.img_workers} 并发）…", "step")
        url_map = download_images(fetcher, list(pool.values()), assets_dir,
                                  workers=args.img_workers,
                                  max_bytes=int(args.image_max_mb * 1024 * 1024),
                                  dedupe=not args.no_image_dedupe,
                                  max_width=args.image_max_width,
                                  quality=args.image_quality)
        ist = getattr(fetcher, "last_image_stats", None)
        if ist:
            log(f"图片优化：压缩 {ist['shrunk']} 张，去重 {ist['dedup']} 张，"
                f"省下 {human_size(ist['saved'])}", "info")
        # 占位符 → 本地路径
        ph_to_local: dict[str, str] = {}
        for ph, src in pool.items():
            name = url_map.get(src)
            if name:
                ph_to_local[ph] = f"assets/{name}"
        miss = len(pool) - len(ph_to_local)
        log(f"图片完成：{len(ph_to_local)} 张本地化"
            + (f"，{miss} 张失败（保留原链接）" if miss else ""),
            "ok" if miss == 0 else "warn")
        # 失败原因分类：robots.txt 被拒是可以解决的（--no-robots），
        # 网络/404 才是需要排查的，混在一起会误导。
        image_failures = {}
        if ist and ist.get("reasons"):
            image_failures = dict(ist["reasons"])
            parts = []
            if image_failures.get("robots"):
                parts.append(f"{image_failures['robots']} 张被 robots.txt 拒绝"
                             f"（加 --no-robots 可强行下载）")
            others = {k: v for k, v in image_failures.items() if k != "robots"}
            if others:
                parts.append("、".join(f"{v} 张 {k}" for k, v in
                                       sorted(others.items(),
                                              key=lambda kv: -kv[1])))
            for p in parts:
                log(f"  图片失败原因：{p}", "warn")
        for res in results:
            if not res.get("ok"):
                continue
            md = res["markdown"]
            used: list[str] = []
            for ph, local in ph_to_local.items():
                if ph in md:
                    md = md.replace(ph, local)
                    used.append(local)
            # 没下载成功的占位符回退成原图外链
            md = re.sub(r"%%BFIMG:[0-9a-f]{12}%%",
                        lambda m: pool.get(m.group(0), ""), md)
            res["markdown"] = md
            res["local_assets"] = used
    else:
        for res in results:
            if res.get("ok"):
                res["local_assets"] = []

    # ---------------- 落盘
    log("写入归档包…", "step")
    ok_results = [r for r in results if r.get("ok")]
    # 注意：results 是并行抓取的「完成顺序」，不是输入顺序，且每次都不一样。
    # 所以任何排序都必须先能回到输入顺序（rank），否则结果不可复现。
    rank = {it["url"]: i for i, it in enumerate(items)}
    if args.order == "site":
        # 保持来源顺序 —— 尤其 --urls-file 时，用户给的列表顺序就是书内顺序
        ok_results.sort(key=lambda r: rank.get(r["url"], 10 ** 9))
    else:
        ok_results = _sort_results(ok_results, args.order, rank)
    order = ok_results
    _clean_titles(order, getattr(args, "site_name", "") or "")
    start_index = len(ar.articles) + 1
    written = 0
    # manifest 以 id 为键去重，id 又来自标题 slug。博客里同题文章很常见
    # （"随笔"、"资源更新"之类），不保证唯一的话后一篇会静默覆盖前一篇，
    # 前一篇的文件还会在下面被当"孤儿"删掉——等于丢文章。
    used_slugs = {a.id for a in ar.articles}
    for i, res in enumerate(order, start=start_index):
        slug = slugify(res["title"], 48) or short_hash(res["url"], 8)
        base_slug = slug
        n = 2
        while slug in used_slugs:
            slug = f"{base_slug}-{n}"
            n += 1
        used_slugs.add(slug)
        fname = f"{i:03d}-{slug}.md"
        grp, sub = url2group.get(res["url"].split("#")[0], ("", ""))
        art = Article(
            index=i, id=slug, title=res["title"], file=f"content/{fname}",
            source_url=res["url"], author=res["author"] or ar.book.get("author", ""),
            published_at=res["published_at"], summary=res["summary"],
            tags=[], assets=res.get("local_assets", []),
            word_count=res["word_count"], fetch_status=res["status"],
            notes="；".join(res.get("notes") or []),
            group=grp, subgroup=sub,
        )
        fm = build_frontmatter(article_meta(art, ar.book))
        body = f"{fm}\n{res['markdown'].strip()}\n"
        (ar.content_dir / fname).write_text(body, encoding="utf-8")
        ar.add_article(art)
        for rel in res.get("local_assets", []):
            p = ar.root / rel
            ar.add_asset(rel, size=p.stat().st_size if p.exists() else 0,
                         source_url=res["url"])
        written += 1

    # 清掉孤儿文件：用 --force 重抓时，上一轮写下的旧编号文件会留在 content/，
    # 但 manifest 已不再引用它们。不清理的话归档包会越滚越大、且出现重复篇章。
    referenced = {Path(a.file).name for a in ar.articles}
    orphans = [p for p in ar.content_dir.glob("*.md")
               if p.name not in referenced]
    for p in orphans:
        try:
            p.unlink()
        except OSError:
            pass
    if orphans:
        log(f"清理孤儿文件 {len(orphans)} 个（上一轮遗留的旧编号）", "warn")

    # 失败登记（便于重试）
    for res in results:
        if not res.get("ok"):
            ar.record_stage("fetch", {"last_failed": res["url"]})

    # ---------------- 分组清单落盘
    # 只保留归档包里真实存在的 URL：清单可能覆盖全站，而本次可能只抓了一部分。
    # stage5 见到 metadata/groups.json 就会排成「部 → 章 → 节」的嵌套目录。
    if groups_payload:
        have = {a.source_url.split("#")[0] for a in ar.articles}
        kept = []
        for g in groups_payload.get("groups", []):
            subs = []
            for s in g.get("subgroups", []):
                us = [u for u in s.get("urls", []) if u.split("#")[0] in have]
                if us:
                    subs.append({"title": s.get("title", ""), "urls": us})
            if subs:
                kept.append({"title": g.get("title", ""), "subgroups": subs})
        payload = {k: v for k, v in groups_payload.items() if k != "groups"}
        payload["groups"] = kept
        payload["urls_in_archive"] = len(have)
        ensure_dir(ar.metadata_dir)
        (ar.metadata_dir / "groups.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"分组清单已写入 metadata/groups.json"
            f"（{len(kept)} 个主题 / {sum(len(s['urls']) for g in kept for s in g['subgroups'])} 篇）",
            "info")

    # ---------------- 报告
    langs: dict[str, int] = {}
    strategies: dict[str, int] = {}
    for r in results:
        if r.get("ok"):
            langs[r["language"]] = langs.get(r["language"], 0) + 1
            strategies[r["strategy"]] = strategies.get(r["strategy"], 0) + 1
    total_words = sum(r.get("word_count", 0) for r in order)
    if not ar.book.get("source_language") and langs:
        ar.set_book(source_language=max(langs, key=langs.get))

    report = {
        "stage": "fetch",
        "at": now_iso(),
        "entry": args.entry + args.from_sitemap + args.from_feed,
        "candidates": len(items),
        "fetched": len(results),
        "ok": len(results) - failed,
        "failed": failed,
        "written": written,
        "articles_total": len(ar.articles),
        "words_total": ar.manifest["stats"].get("words", 0),
        "images_localized": sum(len(r.get("local_assets") or []) for r in results),
        "image_failures": image_failures,
        "languages": langs,
        "extract_strategies": strategies,
        "failures": [{"url": r["url"], "error": r.get("error", "")}
                     for r in results if not r.get("ok")],
        "low_confidence": [
            {"url": r["url"], "title": r["title"], "confidence": r["confidence"],
             "notes": r.get("notes", [])}
            for r in results if r.get("ok") and r.get("confidence", 1) < 0.6],
        "elapsed_sec": round(time.monotonic() - t0, 1),
    }
    ar.write_report("quality-report", report)
    ar.record_stage("fetch", {
        "entries": args.entry + args.from_sitemap + args.from_feed,
        "fetched": len(results), "ok": len(results) - failed, "failed": failed,
        "elapsed_sec": report["elapsed_sec"],
    })
    ar.set_stats(words=total_words,
                 articles=len(ar.articles))
    # 书籍元数据快照
    ensure_dir(ar.metadata_dir)
    (ar.metadata_dir / "book.json").write_text(
        json.dumps(ar.book, ensure_ascii=False, indent=2), encoding="utf-8")
    ar.save()

    # ---------------- 收尾
    audit = ar.audit()
    print()
    log(f"归档包：{ar.root}", "done")
    log(f"文章 {len(ar.articles)} 篇 · 字数 {total_words:,} · "
        f"图片 {report['images_localized']} 张", "info")
    if audit["errors"]:
        log(f"自检发现 {audit['errors']} 个错误、{audit['warnings']} 个警告"
            f"（详见 reports/quality-report.json）", "warn")
    for r in results:
        for n in (r.get("notes") or [])[:1]:
            if r.get("ok"):
                log(f"  {r['title'][:36]}：{n}", "warn")
    log(f"报告：{relpath(ar.reports_dir / 'quality-report.json', ar.root)}", "info")
    return 0 if failed == 0 else 1


def _sort_results(results: list[dict], order: str,
                  rank: dict | None = None) -> list[dict]:
    """对抓取结果排序。

    rank: {url: 输入顺序序号}。并行抓取后 results 是「完成顺序」，必须靠 rank
    才能回到可复现的输入顺序；不传则退回 results 自身顺序（仅兜底）。
    """
    if rank is None:
        rank = {r["url"]: i for i, r in enumerate(results)}
    rk = lambda r: rank.get(r["url"], 10 ** 9)  # noqa: E731
    if order == "reverse":
        # 「输入顺序的倒序」。直接 reversed(results) 等于反转完成顺序，是错的。
        return sorted(results, key=rk, reverse=True)
    if order == "alpha":
        return sorted(sorted(results, key=rk), key=lambda r: r["title"].lower())
    if order == "date-asc":
        # 稳定排序：先按输入顺序铺底，再按日期升序（无日期的排到最后）
        return sorted(sorted(results, key=rk),
                      key=lambda r: r.get("published_at") or "9999")
    if order == "date-desc":
        return sorted(sorted(results, key=rk),
                      key=lambda r: r.get("published_at") or "0000",
                      reverse=True)
    return results


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。已抓取的内容保留在归档包中，重跑即可续抓。", file=sys.stderr)
        sys.exit(130)
