"""通用分组：把"一堆文章"编成「部（主题）→ 章（细分类型）→ 节（文章）」的目录树。

``wp.py`` 解决的是"WordPress 分类树怎么映射"；本模块解决**更上一层的问题**：
不是每个站点都有分类体系，"按主题分类"这个需求要能在任何站点上给出一个像样的结果。

策略由 ``by`` 决定：

==================  ============================================================
``category``        WordPress 分类树（默认，效果最好，能还原站点自己的编辑意图）
``date``            按年份分「部」，每部内文章按时间排（适合无分类的纯博客）
``path``            按 URL 第一段路径分「部」（适合 /tutorial/xxx 这类结构化站点）
``flat``            不分组，全部平铺（就是一本按时间排的文集）
``auto``            按站点画像自动挑一个（有 WP 分类就用 category，否则 date）
==================  ============================================================

另外还有一条**安全底线**：任何一篇没被分到组的文章，都会被兜进一个
「未分类」部，绝不会因为分组规则而凭空消失。
"""

from __future__ import annotations

import time
import urllib.parse
from collections import OrderedDict

from .utils import clean_text, log

# 未分类兜底部的名字
ORPHAN_THEME = "未分类"
ORPHAN_SUBGROUP = "综合"


# ---------------------------------------------------------------- 自动排序


def category_depth(cat: dict, by_id: dict[int, dict]) -> int:
    depth, cur, guard = 0, cat, 0
    while cur is not None and cur.get("parent") and guard < 32:
        cur = by_id.get(int(cur["parent"]))
        depth += 1
        guard += 1
    return depth


def auto_priority(categories: list[dict]) -> list[str]:
    """自动推导冲突裁决顺序。

    思路：**更深、更窄的分类优先**。深 = 更具体的主题；窄（文章数少）=
    更可能是专门为这批文章建的分类。泛主题（顶层、文章巨多）自然沉底。
    这是对"人工调 priority"的自动化近似，用户仍可用 ``--priority`` 覆盖。
    """
    by_id = {int(c["id"]): c for c in categories if c.get("id") is not None}
    ranked = sorted(
        by_id.values(),
        key=lambda c: (-category_depth(c, by_id),
                       int(c.get("count") or 0),
                       int(c["id"])),
    )
    return [c["slug"] for c in ranked if c.get("slug")]


def auto_theme_order(categories: list[dict], theme_order: list[str] | None,
                     fallback_subtype: str) -> list[str]:
    """顶层「部」的默认顺序。

    默认按分类树 id 排。但有个常见毛病：站点会建一个「随笔 / 杂谈」之类的
    万能分类，文章量远超其它部。把它排在末尾，读者体验更好。
    """
    if theme_order:
        return theme_order
    tops = [c for c in categories if not c.get("parent")]
    if len(tops) < 2:
        return []
    counts = sorted(int(c.get("count") or 0) for c in tops)
    median = counts[len(counts) // 2]
    biggest = max(tops, key=lambda c: int(c.get("count") or 0))
    # 只有一骑绝尘（> 中位数的 2.5 倍）时才动它，避免误伤
    if median > 0 and int(biggest.get("count") or 0) > median * 2.5:
        rest = [c["slug"] for c in sorted(tops, key=lambda c: int(c["id"]))
                if c is not biggest]
        return rest + [biggest["slug"]]
    return []


# ---------------------------------------------------------------- 兜底补漏


def rescue_orphans(groups: dict, all_urls: list[str],
                   theme: str = ORPHAN_THEME) -> int:
    """把没分到组的 URL 收进一个兜底部，返回抢救的篇数。"""
    assigned: set[str] = set()
    for g in groups.get("groups", []):
        for s in g.get("subgroups") or []:
            assigned.update(s.get("urls") or [])
        assigned.update(g.get("urls") or [])
    missing = [u for u in all_urls if u not in assigned]
    if not missing:
        return 0
    groups.setdefault("groups", []).append({
        "title": theme,
        "slug": "__orphan__",
        "subgroups": [{"title": ORPHAN_SUBGROUP, "slug": "__orphan__",
                       "urls": missing}],
    })
    counts = groups.setdefault("counts", {})
    counts["orphans_rescued"] = len(missing)
    return len(missing)


# ---------------------------------------------------------------- 各策略


def _by_category(fetcher, api_root: str, *, theme_order, subtype_order,
                 priority, fallback_subtype: str,
                 auto_orders: bool = True, verbose: bool = True) -> dict:
    from .wp import (build_groups, fetch_categories, fetch_posts)

    cats = fetch_categories(api_root, fetcher)
    posts = fetch_posts(api_root, fetcher, quiet=not verbose)

    if auto_orders:
        if not theme_order:
            theme_order = auto_theme_order(cats, theme_order, fallback_subtype)
        if not priority:
            priority = auto_priority(cats)

    groups = build_groups(
        cats, posts, site=api_root,
        theme_order=theme_order or [], subtype_order=subtype_order or [],
        priority=priority or [], fallback_subtype=fallback_subtype,
        verbose=verbose,
    )
    groups["strategy"] = "category"
    rescue_orphans(groups, [p["link"] for p in posts])
    return groups


def _by_date(entries: list[dict], *, verbose: bool = True) -> dict:
    """entries: [{url, title, date}]"""
    buckets: OrderedDict[str, list[dict]] = OrderedDict()
    unknown: list[dict] = []
    for e in entries:
        d = (e.get("date") or "").strip()
        year = d[:4] if len(d) >= 4 and d[:4].isdigit() else ""
        if year:
            buckets.setdefault(year, []).append(e)
        else:
            unknown.append(e)

    def ykey(y: str):
        return (0, int(y)) if y.isdigit() else (1, 0)

    groups = []
    for year in sorted(buckets, key=ykey):
        items = sorted(buckets[year], key=lambda x: (x.get("date") or "", x["url"]))
        groups.append({
            "title": year,
            "slug": f"y{year}",
            "urls": [i["url"] for i in items],
        })
        if verbose:
            log(f"  {year}（{len(items)} 篇）", "info")
    if unknown:
        groups.append({
            "title": "未标日期", "slug": "undated",
            "urls": [i["url"] for i in unknown],
        })
        if verbose:
            log(f"  未标日期（{len(unknown)} 篇）", "warn")
    return {
        "site": "", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "strategy": "date",
        "rule": "按文章发布年份分「部」，部内按时间从早到晚。",
        "counts": {"posts": len(entries)},
        "groups": groups,
    }


def _title_from_path(url: str) -> str:
    seg = urllib.parse.urlparse(url).path.strip("/").split("/")
    if len(seg) > 1:
        raw = seg[0]
    elif seg and seg[0]:
        raw = seg[0]
    else:
        return "根目录"
    raw = urllib.parse.unquote(raw)
    raw = clean_text(raw.replace("-", " ").replace("_", " "))
    return raw[:24] or "根目录"


def _by_path(entries: list[dict], *, verbose: bool = True) -> dict:
    buckets: OrderedDict[str, list[dict]] = OrderedDict()
    for e in entries:
        key = _title_from_path(e["url"])
        buckets.setdefault(key, []).append(e)

    ordered = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    groups = []
    for title, items in ordered:
        items = sorted(items, key=lambda x: (x.get("date") or "", x["url"]))
        groups.append({
            "title": title, "slug": _slug_of(title),
            "urls": [i["url"] for i in items],
        })
        if verbose:
            log(f"  {title}（{len(items)} 篇）", "info")
    return {
        "site": "", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "strategy": "path",
        "rule": "按 URL 第一段路径分「部」，部内按时间排序。",
        "counts": {"posts": len(entries)},
        "groups": groups,
    }


def _slug_of(text: str) -> str:
    from .utils import slugify
    return slugify(text, 40) or "group"


def _by_flat(entries: list[dict]) -> dict:
    items = sorted(entries, key=lambda x: (x.get("date") or "", x["url"]))
    return {
        "site": "", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "strategy": "flat",
        "rule": "不分组，按时间从早到晚平铺。",
        "counts": {"posts": len(entries)},
        "groups": [{
            "title": "", "slug": "__flat__",
            "subgroups": [{"title": "", "slug": "__self__",
                           "urls": [i["url"] for i in items]}],
        }],
    }


# ---------------------------------------------------------------- 入口


def choose_strategy(profile, requested: str = "auto") -> str:
    if requested and requested != "auto":
        return requested
    if profile is not None and getattr(profile, "api_root", ""):
        return "category"
    return "date"


def build_plan(fetcher, profile, *, by: str = "auto",
               theme_order: list[str] | None = None,
               subtype_order: list[str] | None = None,
               priority: list[str] | None = None,
               fallback_subtype: str = ORPHAN_SUBGROUP,
               entries: list[dict] | None = None,
               auto_orders: bool = True,
               verbose: bool = True) -> dict:
    """按策略产出分组清单。

    ``profile`` 可以是 ``detect.SiteProfile``，也可以是 ``None``
    （表示"只按 URL/日期分组，跟站点类型无关"）。
    ``entries`` 是已知的 ``[{url, title, date}]``。
    """
    strategy = choose_strategy(profile, by)
    if verbose:
        log(f"分组策略：{strategy}", "step")

    if strategy == "category":
        api_root = getattr(profile, "api_root", "")
        if not api_root:
            log("没有 WordPress API，退化为按时间分组", "warn")
            strategy = "date"
        else:
            return _by_category(
                fetcher, api_root, theme_order=theme_order,
                subtype_order=subtype_order, priority=priority,
                fallback_subtype=fallback_subtype,
                auto_orders=auto_orders, verbose=verbose)

    if not entries:
        raise ValueError(f"策略 {strategy} 需要事先知道文章列表（entries 为空）")

    if strategy == "date":
        return _by_date(entries, verbose=verbose)
    if strategy == "path":
        return _by_path(entries, verbose=verbose)
    if strategy == "flat":
        return _by_flat(entries)
    raise ValueError(f"未知分组策略：{strategy}")


def profile_urls(groups: dict) -> list[str]:
    """展开成有序 URL 列表。"""
    return [u for _, _, u in iter_urls(groups)]


def iter_urls(groups: dict):
    for g in groups.get("groups", []):
        gt = g.get("title", "")
        subs = g.get("subgroups") or [{"title": "", "urls": g.get("urls", [])}]
        for s in subs:
            for u in s.get("urls", []):
                yield gt, s.get("title", ""), u


def summarise(groups: dict) -> str:
    lines = []
    for g in groups.get("groups", []):
        n = sum(len(s.get("urls", [])) for s in (g.get("subgroups") or []))
        n += len(g.get("urls") or [])
        lines.append(f"{g.get('title', '') or '（全部）'}  ({n} 篇)")
        for s in g.get("subgroups") or []:
            if s.get("title"):
                lines.append(f"    {s['title']}  {len(s.get('urls', []))} 篇")
    return "\n".join(lines)


def write_groups(groups: dict, path) -> None:
    import json
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(groups, ensure_ascii=False, indent=2),
                 encoding="utf-8")


def write_urls(groups: dict, path) -> int:
    from pathlib import Path
    urls = profile_urls(groups)
    Path(path).write_text("\n".join(urls) + "\n", encoding="utf-8")
    return len(urls)
