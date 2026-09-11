"""bookforge 命令行入口。

设计目标只有一个：**让 agent 用最少的 token 把书做出来**。

所以整个工具对外只暴露一条命令：

    bookforge build https://example.com -o book.epub

它会自动完成「探测站点 → 定目录结构 → 抓正文与图片 → 生成封面 → 打包 EPUB」，
并且在最后产出一份 ``summary.json``（二三十行），agent 读它就能判断成败，
不需要翻阅几千行日志。

为什么能把 token 压到最低：

* **零 LLM 参与**。抓取、提取、分组、排版、封面全是确定性代码，
  跑 700 篇和跑 7 篇对模型的花费几乎一样（只有一两条命令 + 读一份摘要）。
* **一条命令**。不用先判断站点类型、不用手写 URL 清单、不用调参数。
  书名 / 作者 / 简介从站点 meta 自动拿。
* **失败自愈**。抓取可断点续传；``build`` 可反复重跑，已抓的不重抓。
* **输出收敛**。``--quiet`` 关掉全部进度；``--json`` 让 stdout 只剩 JSON。
* **先小后大**。``--max 20`` 几十秒就能验一遍效果，再决定要不要全量。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .utils import (banner, ensure_dir, human_size, log, set_color,
                    set_log_stream, set_quiet, slugify)

# ---------------------------------------------------------------- 常量

DEFAULT_THEME = "classic"
DEPS = [
    ("requests", "requests", "HTTP 抓取"),
    ("lxml", "lxml", "HTML 解析"),
    ("markdown", "markdown", "Markdown → HTML"),
    ("ebooklib", "ebooklib", "EPUB 打包"),
    ("PIL", "Pillow", "图片处理与封面"),
    ("numpy", "numpy", "封面渐变"),
]
OPTIONAL_DEPS = [
    ("yaml", "pyyaml", "声明式站点适配器（YAML）"),
    ("fontTools", "fonttools", "封面字形校验/可变字重"),
]


# ---------------------------------------------------------------- 小工具


def _split(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _mk_fetcher(args, cache_dir: Path | None = None):
    from .fetch import Fetcher
    return Fetcher(
        delay=args.delay, retry=args.retry, timeout=args.timeout,
        cache_dir=cache_dir, verbose=not getattr(args, "quiet", False),
        respect_robots=not getattr(args, "no_robots", False),
    )


def _missing_deps() -> list[tuple[str, str, str]]:
    import importlib
    out = []
    for mod, pip, why in DEPS:
        try:
            importlib.import_module(mod)
        except ImportError:
            out.append((mod, pip, why))
    return out


def _pip_hint(pkgs: list[str]) -> str:
    return f"{sys.executable} -m pip install {' '.join(pkgs)}"


def _short_error(msg: str, limit: int = 240) -> str:
    msg = " ".join(str(msg).split())
    return msg if len(msg) <= limit else msg[: limit - 1] + "…"


# ---------------------------------------------------------------- doctor


def cmd_doctor(args) -> int:
    import platform

    banner("bookforge · 环境体检", f"v{__version__}")
    ok = True

    log(f"Python {platform.python_version()} ({sys.executable})", "info")
    if sys.version_info < (3, 9):
        log("需要 Python 3.9 或更高版本", "err")
        ok = False

    missing = _missing_deps()
    if missing:
        ok = False
        log(f"缺少 {len(missing)} 个必需依赖：", "err")
        for mod, pip, why in missing:
            log(f"  {mod:10s} ({why})  → pip install {pip}", "err")
        log(f"一键安装：{_pip_hint([p for _, p, _ in missing])}", "err")
    else:
        log(f"必需依赖齐备（{len(DEPS)} 个）", "ok")

    import importlib
    opt_missing = []
    for mod, pip, why in OPTIONAL_DEPS:
        try:
            importlib.import_module(mod)
        except ImportError:
            opt_missing.append((mod, pip, why))
    for mod, pip, why in opt_missing:
        log(f"可选依赖缺失：{mod}（{why}）→ pip install {pip}", "warn")
    if not opt_missing:
        log("可选依赖齐备", "ok")

    if missing:
        log("依赖没装齐，先解决上面的问题再跑 build", "err")
        return 2

    # ---- 字体
    from .fonts import FontManager, font_install_hint
    fm = FontManager()
    rep = fm.diagnose()
    log(f"字体目录：{'、'.join(rep['font_dirs']) or '（无）'}", "info")
    for role, path in rep["roles"].items():
        if path:
            log(f"  {role:10s} {path}", "info")
    if rep["cjk_ok"]:
        log("中文字体可用（封面能正常渲染中文）", "ok")
    else:
        log("没有可用的中文字体，封面会退化成无衬线/方框", "warn")
        log(font_install_hint(), "warn")

    # ---- 排版主题
    from .typeset import Typesetter
    log(f"排版主题：{'、'.join(Typesetter.available_themes())}", "info")

    # ---- 站点适配器
    from .site_config import adapter_summary, load_adapters
    load_adapters(verbose=False)
    rows = adapter_summary()
    builtin = [r["name"] for r in rows if r["kind"] == "builtin"]
    yaml_names = [r["name"] for r in rows if r["kind"] == "yaml"]
    log(f"内置站点适配器：{'、'.join(builtin) or '（无）'}", "info")
    log(f"声明式适配器：{'、'.join(yaml_names) or '（无，可用 bookforge-adapters/*.yaml 添加）'}",
        "info")

    print()
    log("环境可用，直接跑：bookforge build <网址> -o 书名.epub", "done")
    return 0 if ok else 1


# ---------------------------------------------------------------- adapters


def cmd_adapters(args) -> int:
    from .site_config import adapter_search_paths, adapter_summary, load_adapters

    if args.json:
        set_log_stream(sys.stderr)

    load_adapters(verbose=not (args.quiet or args.json))
    rows = adapter_summary()

    if args.json:
        print(json.dumps({"adapters": rows,
                          "search_paths": [str(p) for p in adapter_search_paths()]},
                         ensure_ascii=False, indent=2))
        return 0

    banner("bookforge · 站点适配器", f"共 {len(rows)} 个")
    for r in rows:
        log(f"{r['name']:16s} [{r['kind']:7s}] "
            f"hosts={','.join(r['hosts']) or '-'}", "info")
        if r["source"]:
            log(f"{'':16s}  来自 {r['source']}", "info")
    print()
    log("自定义适配器搜索目录：", "step")
    for p in adapter_search_paths():
        log(f"  {p}", "info")
    log("格式见 docs/ADDING_A_SITE.md", "info")
    return 0


# ---------------------------------------------------------------- info


def cmd_info(args) -> int:
    from .detect import detect
    from .site_config import load_adapters

    if args.json:
        set_log_stream(sys.stderr)
    load_adapters(verbose=False)
    f = _mk_fetcher(args)
    prof = detect(f, args.url)
    d = prof.to_dict()

    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0

    banner("bookforge · 站点探测", args.url)
    log(f"类型：{d['kind']}" + (f"（适配器 {d['adapter']}）" if d["adapter"] else ""), "info")
    log(f"书名：{d['title'] or '（未识别）'}", "info")
    log(f"作者：{d['author'] or '（未识别）'}", "info")
    log(f"语言：{d['language'] or '（未识别）'}", "info")
    if d["description"]:
        log(f"简介：{_short_error(d['description'], 120)}", "info")
    if d["api_root"]:
        log(f"WordPress API：{d['api_root']}", "info")
    if d["feed_url"]:
        log(f"RSS：{d['feed_url']}", "info")
    if d["sitemap_url"]:
        log(f"sitemap：{d['sitemap_url']}", "info")
    log(f"入口：{', '.join(d['entry_urls']) or '（无）'}", "info")
    for n in d["notes"]:
        log(n, "warn")
    return 0


# ---------------------------------------------------------------- group


def cmd_group(args) -> int:
    from .detect import detect
    from .groups import build_plan, profile_urls, summarise, write_groups, write_urls
    from .site_config import load_adapters

    if args.json:
        set_log_stream(sys.stderr)
    load_adapters(verbose=False)
    f = _mk_fetcher(args)
    prof = detect(f, args.url)

    gargs = dict(theme_order=_split(args.theme_order),
                 subtype_order=_split(args.subtype_order),
                 priority=_split(args.priority),
                 verbose=not args.quiet)
    if prof.api_root:
        plan = build_plan(f, prof, by=args.by, **gargs)
    else:
        entries = _entries_from_site(f, prof, args)
        plan = build_plan(f, prof, by=args.by, entries=entries, **gargs)

    out = Path(args.out or "groups.json")
    write_groups(plan, out)
    n = 0
    if args.urls_out:
        n = write_urls(plan, args.urls_out)

    if args.json:
        print(json.dumps({**plan, "groups_file": str(out),
                          "urls_file": args.urls_out or "",
                          "url_count": len(profile_urls(plan))},
                         ensure_ascii=False, indent=2))
        return 0

    banner("bookforge · 分组清单", f"策略 {plan.get('strategy')}")
    print(summarise(plan))
    print()
    log(f"共 {len(profile_urls(plan))} 篇", "ok")
    log(f"分组清单：{out}", "done")
    if args.urls_out:
        log(f"URL 列表（{n} 条）：{args.urls_out}", "ok")
    return 0


def _entries_from_site(f, prof, args) -> list[dict]:
    """非 WordPress 站点：先探出 URL，尽力拿标题/日期。"""
    from .fetch import discover_from_feed, discover_from_sitemap, discover_links

    urls: list[str] = []
    if prof.kind == "feed" and prof.feed_url:
        urls = discover_from_feed(f, prof.feed_url)
    elif prof.kind == "sitemap" and prof.sitemap_url:
        urls = discover_from_sitemap(f, prof.sitemap_url)
    else:
        for entry in prof.entry_urls or [prof.url]:
            r = f.get(entry)
            if r.ok:
                urls = discover_links(f, entry, r.text,
                                      same_host=True,
                                      include=args.include, exclude=args.exclude)
                if urls:
                    break
    if args.max:
        urls = urls[: args.max]
    return [{"url": u, "title": "", "date": ""} for u in urls]


# ---------------------------------------------------------------- fetch


def cmd_fetch(args) -> int:
    from . import fetch_stage

    argv = _fetch_argv(args)
    return fetch_stage.main(argv)


def _fetch_argv(args) -> list[str]:
    argv: list[str] = ["--out", args.archive]
    if args.url:
        argv += ["--entry", args.url]
    if getattr(args, "urls_file", ""):
        argv += ["--urls-file", args.urls_file]
    if getattr(args, "groups_file", ""):
        argv += ["--groups-file", args.groups_file]
    for flag, val in (("--title", args.title), ("--subtitle", args.subtitle),
                      ("--author", args.author),
                      ("--description", args.description),
                      ("--publisher", args.publisher),
                      ("--language", args.language)):
        if val:
            argv += [flag, val]
    if args.max:
        argv += ["--max", str(args.max)]
    argv += ["--workers", str(args.workers), "--delay", str(args.delay),
             "--retry", str(args.retry), "--timeout", str(args.timeout)]
    if args.no_images:
        argv.append("--no-images")
    if args.image_max_width:
        argv += ["--image-max-width", str(args.image_max_width)]
    if args.image_quality:
        argv += ["--image-quality", str(args.image_quality)]
    if args.force:
        argv.append("--force")
    if args.dry_run:
        argv.append("--dry-run")
    if args.no_robots:
        argv.append("--no-robots")
    if args.quiet:
        argv.append("--quiet")
    return argv


# ---------------------------------------------------------------- cover


def cmd_cover(args) -> int:
    from .archive import Archive
    from .cover import render_to_archive
    from .fonts import FontManager
    from . import cover as cover_mod

    if args.json:
        set_log_stream(sys.stderr)
    cover_mod.FM = FontManager(args.font_dir, extra_dirs=_split(args.font_dirs),
                               allow_download=args.download_fonts)
    ar = Archive.open(args.archive)
    res = render_to_archive(
        ar, style=args.style, title=args.title, author=args.author,
        background=args.background, basename=args.basename,
        set_default=not args.no_set_default,
    )
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    banner("bookforge · 封面", f"风格 {res['style']}")
    log(f"背景来源：{res['background_source']}", "info")
    log(f"字体角色：{res['title_role']} · 对齐 {res['align']}", "info")
    for k, v in res["files"].items():
        log(f"{k:6s} {v}", "info")
    if res["glyph_check"].get("missing"):
        log(f"字体缺字：{''.join(res['glyph_check']['missing'])}", "warn")
    log(f"封面已生成（{res['elapsed_sec']}s）", "done")
    return 0


# ---------------------------------------------------------------- pack


def cmd_pack(args) -> int:
    from .publish import publish

    if args.json:
        set_log_stream(sys.stderr)
    try:
        rep = publish(
            args.archive, args.out, theme=args.theme, title=args.title,
            author=args.author, subtitle=args.subtitle, publisher=args.publisher,
            description=args.description, rights=args.rights,
            language=args.language, cover=args.cover, no_cover=args.no_cover,
            cjk_space=args.cjk_space, verbose=not args.quiet,
        )
    except Exception as e:
        log(f"打包失败：{type(e).__name__}: {e}", "err")
        return 2
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    banner("bookforge · 打包", rep["title"])
    log(f"EPUB：{rep['output']}", "done")
    log(f"{rep['size_human']} · {rep['chapters']} 章 · {rep['words']:,} 字", "info")
    if rep["problems"]:
        for p in rep["problems"]:
            log(p, "warn")
    return 0


# ---------------------------------------------------------------- build（主力）


def _resolve_paths(args) -> tuple[Path, Path]:
    """定出 EPUB 输出路径与归档包目录。"""
    url = args.url
    host = ""
    try:
        import urllib.parse
        host = urllib.parse.urlparse(url).netloc.split(".")[0]
    except Exception:
        pass
    stem = slugify(args.out_stem or host or "book", 40) or "book"

    if args.out:
        epub = Path(args.out)
    else:
        epub = Path.cwd() / f"{stem}.epub"

    if args.archive:
        arch = Path(args.archive)
    else:
        arch = epub.parent / f"{epub.stem}-archive"
    return epub, arch


def _truncate_plan(plan: dict, limit: int) -> dict:
    """把分组清单裁到前 ``limit`` 篇，**跨组轮流取**。

    直接截前 N 篇会得到一个"只有某一章"的书，看不出目录结构对不对；
    轮流取能让 ``--max 20`` 这种小跑预览到每个部/章的真实样子。
    """
    if not limit or limit <= 0:
        return plan

    buckets: list[list] = []
    for g in plan.get("groups", []):
        subs = g.get("subgroups")
        if subs:
            for s in subs:
                buckets.append(s)
        else:
            buckets.append(g)

    for b in buckets:
        b["urls"] = list(b.get("urls") or [])
    picked = 0
    i = 0
    while picked < limit:
        progressed = False
        for b in buckets:
            if picked >= limit:
                break
            if i < len(b["urls"]):
                progressed = True
                b.setdefault("__keep", []).append(b["urls"][i])
                picked += 1
        if not progressed:
            break
        i += 1

    for b in buckets:
        b["urls"] = b.pop("__keep", [])

    # 丢掉被裁空的部
    kept_groups = []
    for g in plan.get("groups", []):
        subs = g.get("subgroups")
        if subs:
            g["subgroups"] = [s for s in subs if s.get("urls")]
            if g["subgroups"]:
                kept_groups.append(g)
        elif g.get("urls"):
            kept_groups.append(g)
    plan["groups"] = kept_groups
    return plan


def cmd_build(args) -> int:
    from . import cover as cover_mod
    from .archive import Archive
    from .detect import detect
    from .fonts import FontManager
    from .groups import build_plan, profile_urls, summarise, write_groups, write_urls
    from .publish import publish
    from .site_config import load_adapters

    t_start = time.monotonic()
    json_mode = args.json
    if json_mode or args.quiet:
        set_log_stream(sys.stderr)
    if json_mode:
        set_color(False)

    warnings: list[str] = []
    summary: dict = {"ok": False, "url": args.url, "version": __version__}

    def fail(msg: str, code: int = 2) -> int:
        summary["error"] = msg
        summary["elapsed_sec"] = round(time.monotonic() - t_start, 1)
        if json_mode:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            log(msg, "err")
        return code

    missing = _missing_deps()
    if missing:
        return fail("缺少依赖：" + "、".join(m for m, _, _ in missing)
                    + f"。安装：{_pip_hint([p for _, p, _ in missing])}")

    load_adapters(verbose=not (args.quiet or json_mode))

    # 字体
    cover_mod.FM = FontManager(args.font_dir, extra_dirs=_split(args.font_dirs),
                               allow_download=args.download_fonts)

    epub_path, arch = _resolve_paths(args)
    ensure_dir(epub_path.parent)
    summary["epub_path"] = str(epub_path)
    summary["archive"] = str(arch)

    if not args.quiet and not json_mode:
        banner("bookforge · 一键成书", args.url)

    # ---------- 1) 探测
    probe_cache = arch / ".cache-probe"
    f = _mk_fetcher(args, cache_dir=probe_cache)
    prof = detect(f, args.url)
    title = args.title or prof.title or ""
    author = args.author or prof.author or ""
    description = args.description or prof.description or ""
    language = args.language or prof.language or "zh-CN"

    summary["detected"] = {"kind": prof.kind, "adapter": prof.adapter,
                           "strategy": None, "api_root": prof.api_root}
    if not args.quiet and not json_mode:
        log(f"站点类型：{prof.kind}"
            + (f"（适配器 {prof.adapter}）" if prof.adapter else ""), "step")
        log(f"书名：{title or '（未能识别，先用占位名）'} · "
            f"作者：{author or '（未识别）'}", "info")

    # ---------- 2) 目录结构
    strategy = args.by
    plan: dict = {}
    urls_file = arch.parent / f"{arch.name}-urls.txt"
    groups_file = arch.parent / f"{arch.name}-groups.json"

    def do_category_plan() -> dict:
        return build_plan(
            f, prof, by="category",
            theme_order=_split(args.theme_order),
            subtype_order=_split(args.subtype_order),
            priority=_split(args.priority),
            verbose=not (args.quiet or json_mode),
        )

    if strategy in ("auto", "category") and prof.api_root:
        try:
            plan = do_category_plan()
            strategy = plan.get("strategy", "category")
        except Exception as e:
            warnings.append(f"分类分组失败（{type(e).__name__}）：{_short_error(e)}")
            plan = {}
            strategy = "date" if strategy == "auto" else strategy
    elif strategy == "auto":
        strategy = "date" if prof.kind != "path" else "path"

    if plan:
        if args.max:
            plan = _truncate_plan(plan, args.max)
        write_groups(plan, groups_file)
        write_urls(plan, urls_file)
        fetched_urls = profile_urls(plan)
        summary["groups"] = [{
            "title": g.get("title", ""),
            "count": sum(len(s.get("urls", [])) for s in (g.get("subgroups") or []))
            + len(g.get("urls") or []),
            "subgroups": [{"title": s.get("title", ""),
                           "count": len(s.get("urls", []))}
                          for s in (g.get("subgroups") or []) if s.get("title")],
        } for g in plan.get("groups", [])]
        if not args.quiet and not json_mode:
            log(f"目录结构：{summarise(plan)}", "step")
    else:
        fetched_urls = []

    summary["detected"]["strategy"] = strategy

    # ---------- 3) 抓取
    fargv = ["--out", str(arch)]
    if fetched_urls:
        fargv += ["--urls-file", str(urls_file), "--groups-file", str(groups_file)]
    else:
        fargv += ["--entry", args.url]
    for flag, val in (("--title", title), ("--subtitle", args.subtitle),
                      ("--author", author), ("--description", description),
                      ("--publisher", args.publisher or prof.title),
                      ("--language", language)):
        if val:
            fargv += [flag, val]
    # 站名交给抓取阶段，用来剥掉标题尾部的「- 站名」
    if prof.title:
        fargv += ["--site-name", prof.title]
    if args.max:
        fargv += ["--max", str(args.max)]
    fargv += ["--workers", str(args.workers), "--delay", str(args.delay),
              "--retry", str(args.retry), "--timeout", str(args.timeout)]
    if args.no_images:
        fargv.append("--no-images")
    else:
        fargv += ["--image-max-width", str(args.image_max_width),
                  "--image-quality", str(args.image_quality)]
    if args.force:
        fargv.append("--force")
    if args.dry_run:
        fargv.append("--dry-run")
    if args.no_robots:
        fargv.append("--no-robots")
    if args.quiet or json_mode:
        fargv.append("--quiet")

    if not args.quiet and not json_mode:
        log(f"开始抓取 {len(fetched_urls) or '（自动发现）'} "
            f"{'篇' if fetched_urls else ''}…", "step")

    from . import fetch_stage
    rc = fetch_stage.main(fargv)
    if rc != 0:
        return fail(f"抓取阶段失败（退出码 {rc}），详见 {arch}/reports/")

    try:
        ar = Archive.open(arch)
    except Exception as e:
        return fail(f"归档包打不开：{type(e).__name__}: {e}")
    if not ar.articles:
        return fail(f"没有抓到任何文章，请检查入口地址（{args.url}）")

    summary["articles"] = {
        "count": len(ar.articles),
        "words": sum(a.word_count for a in ar.articles),
        "failed": 0,
        "low_confidence": 0,
    }
    # 把质量报告的要点提到摘要里，agent 就不必再读 quality-report.json
    try:
        qr = json.loads((ar.reports_dir / "quality-report.json")
                        .read_text(encoding="utf-8"))
        summary["articles"]["failed"] = len(qr.get("failed") or [])
        low = qr.get("low_confidence") or []
        summary["articles"]["low_confidence"] = len(low)
        summary["articles"]["images_localized"] = qr.get("images_localized", 0)
        for item in low[:3]:
            warnings.append(
                f"正文置信度低（{item.get('confidence')}）："
                f"{item.get('title') or item.get('url')}")
    except Exception:
        pass

    if args.dry_run:
        summary["ok"] = True
        summary["dry_run"] = True
        summary["elapsed_sec"] = round(time.monotonic() - t_start, 1)
        if json_mode:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            log(f"干跑完成：将抓取 {len(ar.articles)} 篇", "done")
        return 0

    # ---------- 4) 非 WordPress：抓完之后按日期/路径补分组
    if not plan:
        try:
            entries = [{"url": a.source_url, "title": a.title,
                        "date": a.published_at}
                       for a in ar.articles if a.source_url]
            plan = build_plan(None, None, by=strategy, entries=entries,
                              fallback_subtype="综合",
                              verbose=not (args.quiet or json_mode))
            write_groups(plan, groups_file)
            ensure_dir(ar.metadata_dir)
            (ar.metadata_dir / "groups.json").write_text(
                groups_file.read_text(encoding="utf-8"), encoding="utf-8")
            summary["groups"] = [{
                "title": g.get("title", ""),
                "count": len(g.get("urls") or []) or sum(
                    len(s.get("urls", [])) for s in (g.get("subgroups") or [])),
                "subgroups": [{"title": s.get("title", ""),
                               "count": len(s.get("urls", []))}
                              for s in (g.get("subgroups") or []) if s.get("title")],
            } for g in plan.get("groups", [])]
            if not args.quiet and not json_mode:
                log(f"按「{strategy}」分了 {len(plan.get('groups', []))} 部", "info")
        except Exception as e:
            warnings.append(f"分组失败（{type(e).__name__}）：{_short_error(e)}")

    # ---------- 5) 封面
    if not args.no_cover and not args.cover:
        try:
            res = cover_mod.render_to_archive(ar, style=args.cover_style,
                                             write_brief=not (args.quiet or json_mode))
            summary["cover"] = {"style": res["style"],
                                "background": res["background_source"],
                                "file": res["files"].get("jpg")}
            if res["glyph_check"].get("missing"):
                warnings.append("封面字体缺字："
                                + "".join(res["glyph_check"]["missing"]))
        except Exception as e:
            warnings.append(f"封面生成失败（{type(e).__name__}）：{_short_error(e)}")
    elif args.cover:
        ar.set_book(cover=args.cover)
        ar.save()

    # ---------- 6) 打包
    try:
        rep = publish(
            ar, epub_path, theme=args.theme, title=title, author=author,
            subtitle=args.subtitle, publisher=args.publisher or prof.title,
            description=description, language=language,
            cover=args.cover, no_cover=args.no_cover,
            cjk_space=args.cjk_space, verbose=not (args.quiet or json_mode),
        )
    except Exception as e:
        return fail(f"打包失败：{type(e).__name__}: {e}")

    summary["epub"] = {
        "path": rep["output"], "size": rep["size_human"],
        "size_bytes": rep["size_bytes"], "chapters": rep["chapters"],
        "parts": rep["parts"], "sections": rep["sections"],
        "grouped": rep["grouped"], "words": rep["words"],
        "theme": rep["theme"], "cover": rep["cover"],
        "index_md": str(ar.root / "index.md"),
    }
    if rep["problems"]:
        warnings += [f"EPUB 结构：{p}" for p in rep["problems"][:5]]
    summary["warnings"] = warnings
    summary["ok"] = not rep["problems"]
    summary["elapsed_sec"] = round(time.monotonic() - t_start, 1)
    summary["next_actions"] = _next_actions(summary)

    # 摘要落盘（agent 之后可以只读这一份，不必回看日志）
    try:
        ensure_dir(ar.reports_dir)
        (ar.reports_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    if json_mode:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["ok"] else 1

    _print_summary(summary)
    return 0 if summary["ok"] else 1


def _next_actions(s: dict) -> list[str]:
    acts: list[str] = []
    if s.get("warnings"):
        acts.append("检查 warnings；正文异常可加 --max 20 先小跑复现")
    if not s.get("cover"):
        acts.append("补封面：bookforge cover --archive <归档包> --style editorial")
    acts.append("换排版主题重打：bookforge pack --archive "
                f"{s.get('archive', '<归档包>')} --theme modern -o out.epub")
    acts.append("把摘要给 agent 即可，无需再读日志："
                f"{s.get('archive', '<归档包>')}/reports/summary.json")
    return acts


def _print_summary(s: dict) -> None:
    """最终结果摘要：走 stdout 的普通 print（不受 --quiet 影响）。

    刻意做得短 —— agent 读这段就够判断成败，不必回看上面的进度日志。
    """
    e = s.get("epub") or {}
    out = sys.stdout
    print("", file=out)
    print(f"✓ 完成  {e.get('path', '')}", file=out)
    print(f"  {e.get('size', '')} · {e.get('chapters', 0)} 章 · "
          f"{e.get('words', 0):,} 字 · 主题 {e.get('theme', '')}", file=out)
    if e.get("grouped"):
        print(f"  目录：{e.get('parts', 0)} 部 · "
              f"{e.get('sections', 0)} 个细分类型", file=out)
    print(f"  篇目清单：{s.get('archive', '')}/index.md", file=out)
    print(f"  JSON 摘要：{s.get('archive', '')}/reports/summary.json", file=out)
    print(f"  耗时 {s.get('elapsed_sec', 0)}s", file=out)
    for w in s.get("warnings", [])[:6]:
        print(f"  ! {w}", file=out)
    if s.get("next_actions"):
        print("  下一步：", file=out)
        for a in s["next_actions"]:
            print(f"    - {a}", file=out)
    print("", file=out)
    out.flush()


# ---------------------------------------------------------------- 其它子命令


def cmd_themes(args) -> int:
    from .typeset import Typesetter
    if args.json:
        set_log_stream(sys.stderr)
        print(json.dumps({"themes": Typesetter.available_themes()},
                         ensure_ascii=False, indent=2))
        return 0
    banner("bookforge · 排版主题")
    for t in Typesetter.available_themes():
        log(t, "info")
    log("用法：bookforge pack --archive <归档包> --theme <名字>", "info")
    return 0


def cmd_reorder(args) -> int:
    from . import reorder
    argv = ["--archive", args.archive, "--order", args.order]
    if args.dry_run:
        argv.append("--dry-run")
    if args.dates:
        argv += ["--dates", args.dates]
    return reorder.main(argv)


def cmd_translate(args) -> int:
    from . import translate_stage
    argv = [args.action, "--archive", args.archive, "--workdir", args.workdir]
    for flag, val in (("--out", args.out), ("--translations", args.translations),
                      ("--target-chars", args.target_chars),
                      ("--provider", args.provider)):
        if val:
            argv += [flag, str(val)]
    if args.split_by_article:
        argv.append("--split-by-article")
    return translate_stage.main(argv)


# ---------------------------------------------------------------- 参数表


def _common_net(p):
    g = p.add_argument_group("网络")
    g.add_argument("--delay", type=float, default=0.6, help="同一站点请求间隔秒数")
    g.add_argument("--retry", type=int, default=3, help="失败重试次数")
    g.add_argument("--timeout", type=float, default=25.0, help="单请求超时（秒）")
    g.add_argument("--workers", type=int, default=5, help="文章级并发（默认 5）")
    g.add_argument("--no-robots", action="store_true", help="忽略 robots.txt")


def _fetch_common(p):
    _common_net(p)
    g = p.add_argument_group("抓取与图片")
    g.add_argument("--max", type=int, default=0, help="最多抓几篇（0=全部）")
    g.add_argument("--no-images", action="store_true", help="不下载图片")
    g.add_argument("--image-max-width", type=int, default=900,
                   help="图片最大宽度（默认 900，0=不缩放）")
    g.add_argument("--image-quality", type=int, default=78, help="JPEG 质量（默认 78）")
    g.add_argument("--include", default="", help="只保留 URL 匹配该正则的链接")
    g.add_argument("--exclude", default="", help="排除 URL 匹配该正则的链接")
    g.add_argument("--force", action="store_true", help="忽略已有归档包，重新抓")
    g.add_argument("--dry-run", action="store_true", help="只探测/发现，不抓正文")
    g.add_argument("--quiet", action="store_true", help="不打印进度（只留结果）")
    g.add_argument("--json", action="store_true",
                   help="stdout 只输出 JSON 摘要（进度转 stderr）")


def _book_meta(p):
    g = p.add_argument_group("书籍信息（留空则自动从站点识别）")
    g.add_argument("--title", default="", help="书名")
    g.add_argument("--subtitle", default="", help="副标题")
    g.add_argument("--author", default="", help="作者")
    g.add_argument("--description", default="", help="书籍简介")
    g.add_argument("--publisher", default="", help="出版方")
    g.add_argument("--language", default="", help="语言（如 zh-CN / en）")


def _style_opts(p):
    g = p.add_argument_group("排版与封面")
    g.add_argument("--theme", default=DEFAULT_THEME,
                   choices=["classic", "modern", "magazine", "academic"],
                   help="EPUB 排版主题（默认 classic）")
    g.add_argument("--cover-style", default="auto",
                   choices=["auto", "editorial", "classic", "minimal", "band"],
                   help="封面风格（默认 auto）")
    g.add_argument("--cover", default="", help="用指定的封面图（跳过自动生成）")
    g.add_argument("--no-cover", action="store_true", help="不要封面")
    g.add_argument("--cjk-space", choices=["off", "thin"], default="off",
                   help="中西文之间插薄空格")
    g.add_argument("--font-dir", default="", help="封面字体目录（默认自动查找）")
    g.add_argument("--font-dirs", default="", help="附加字体目录，逗号分隔")
    g.add_argument("--download-fonts", action="store_true",
                   help="系统没有中文字体时尝试联网下载")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bookforge",
        description="把网站文章变成一本排版精良的 EPUB 电子书",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
常用示例：
  bookforge build https://example.com -o 我的书.epub
  bookforge build https://blog.com --max 20 -o 试读.epub      # 先小跑验效果
  bookforge build https://blog.com --by date                  # 按年份分章
  bookforge build https://blog.com --theme modern --json      # 只要 JSON 摘要
  bookforge info  https://blog.com                            # 只看站点探测结果
  bookforge doctor                                            # 环境体检
""")
    p.add_argument("-V", "--version", action="version",
                   version=f"bookforge {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- build
    b = sub.add_parser("build", help="★ 一条命令从网址到 EPUB")
    b.add_argument("url", help="站点首页或文章列表页")
    b.add_argument("-o", "--out", default="", help="输出的 .epub 路径")
    b.add_argument("--archive", default="", help="归档包目录（默认 <书名>-archive）")
    b.add_argument("--out-stem", default="", help="默认文件名所用的名字")
    _fetch_common(b)
    _book_meta(b)
    _style_opts(b)
    g = b.add_argument_group("目录结构")
    g.add_argument("--by", default="auto",
                   choices=["auto", "category", "date", "path", "flat"],
                   help="分组策略：auto（推荐）/ 站点分类 / 年份 / URL 路径 / 不分组")
    g.add_argument("--theme-order", default="", help="「部」的顺序（分类 slug，逗号分隔）")
    g.add_argument("--subtype-order", default="", help="「章」的顺序（分类 slug，逗号分隔）")
    g.add_argument("--priority", default="",
                   help="一篇文章属于多个分类时的裁决顺序（slug，越前越优先）")
    b.set_defaults(func=cmd_build)

    # ---- info
    i = sub.add_parser("info", help="只探测站点，看看能抓到什么、书名是什么")
    i.add_argument("url")
    i.add_argument("--json", action="store_true")
    _common_net(i)
    i.set_defaults(func=cmd_info)

    # ---- group
    gg = sub.add_parser("group", help="只产出分组清单（groups.json + urls.txt）")
    gg.add_argument("url")
    gg.add_argument("--out", default="groups.json", help="groups.json 输出路径")
    gg.add_argument("--urls-out", default="", help="同时导出有序 URL 列表")
    gg.add_argument("--by", default="auto",
                    choices=["auto", "category", "date", "path", "flat"])
    gg.add_argument("--theme-order", default="")
    gg.add_argument("--subtype-order", default="")
    gg.add_argument("--priority", default="")
    gg.add_argument("--max", type=int, default=0)
    gg.add_argument("--include", default="")
    gg.add_argument("--exclude", default="")
    gg.add_argument("--quiet", action="store_true")
    gg.add_argument("--json", action="store_true")
    _common_net(gg)
    gg.set_defaults(func=cmd_group)

    # ---- fetch
    fe = sub.add_parser("fetch", help="只抓取（生成归档包，不打包）")
    fe.add_argument("url", nargs="?", default="")
    fe.add_argument("--archive", required=True, help="归档包输出目录")
    fe.add_argument("--urls-file", default="")
    fe.add_argument("--groups-file", default="")
    _fetch_common(fe)
    _book_meta(fe)
    fe.set_defaults(func=cmd_fetch)

    # ---- cover
    cv = sub.add_parser("cover", help="只渲染封面")
    cv.add_argument("--archive", required=True)
    cv.add_argument("--style", default="auto",
                    choices=["auto", "editorial", "classic", "minimal", "band"])
    cv.add_argument("--title", default="")
    cv.add_argument("--author", default="")
    cv.add_argument("--background", default="", help="用指定背景图（AI 生成的那张）")
    cv.add_argument("--basename", default="cover")
    cv.add_argument("--no-set-default", action="store_true")
    cv.add_argument("--font-dir", default="")
    cv.add_argument("--font-dirs", default="")
    cv.add_argument("--download-fonts", action="store_true")
    cv.add_argument("--json", action="store_true")
    cv.set_defaults(func=cmd_cover)

    # ---- pack
    pk = sub.add_parser("pack", help="只打包（归档包 → EPUB）")
    pk.add_argument("--archive", required=True)
    pk.add_argument("-o", "--out", default="")
    _book_meta(pk)
    _style_opts(pk)
    pk.add_argument("--rights", default="")
    pk.add_argument("--quiet", action="store_true")
    pk.add_argument("--json", action="store_true")
    pk.set_defaults(func=cmd_pack)

    # ---- reorder
    ro = sub.add_parser("reorder", help="重排归档包（按时间/字母）")
    ro.add_argument("--archive", required=True)
    ro.add_argument("--order", default="date-asc",
                    choices=["date-asc", "date-desc", "alpha", "reverse"])
    ro.add_argument("--dates", default="", help='JSON：{"slug":"2020-01"}')
    ro.add_argument("--dry-run", action="store_true")
    ro.set_defaults(func=cmd_reorder)

    # ---- translate
    tr = sub.add_parser("translate", help="翻译（可选阶段）")
    tr.add_argument("action", choices=["prepare", "apply", "translate"])
    tr.add_argument("--archive", required=True)
    tr.add_argument("--workdir", required=True)
    tr.add_argument("--out", default="")
    tr.add_argument("--translations", default="")
    tr.add_argument("--target-chars", default="")
    tr.add_argument("--provider", default="")
    tr.add_argument("--split-by-article", action="store_true")
    tr.set_defaults(func=cmd_translate)

    # ---- 工具
    for name, fn, helptext in (("doctor", cmd_doctor, "检查依赖 / 字体 / 适配器"),
                               ("adapters", cmd_adapters, "列出站点适配器与搜索目录"),
                               ("themes", cmd_themes, "列出排版主题")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--quiet", action="store_true")
        sp.set_defaults(func=fn)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "json", False):
        set_log_stream(sys.stderr)
    set_color(not getattr(args, "quiet", False))
    set_quiet(getattr(args, "quiet", False) and not getattr(args, "json", False))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0
    except Exception as e:
        log(f"{type(e).__name__}: {e}", "err")
        if getattr(args, "verbose", False):
            import traceback
            traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
