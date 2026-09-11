"""WordPress 站点支持：REST API 枚举文章 + 由分类体系生成「主题/类型」分组清单。

用途：把 WordPress 博客做成**分类文集**（主题 → 细分类型 → 文章标题）。

WordPress 的分类天生是两级（父分类 / 子分类），正好对应电子书的
"部（主题）→ 章（类型）→ 节（文章）"结构，所以不需要人工打标签，
直接把站点自己的分类体系搬过来即可，既忠实又可复现。

    from bookforge.wp import fetch_categories, fetch_posts, build_groups

    cats = fetch_categories("https://example.com")
    posts = fetch_posts("https://example.com")
    groups = build_groups(cats, posts)

分组清单格式（下游 stage1 --groups-file 与 stage5 都认这个）：

    {
      "site": "https://example.com",
      "groups": [
        {"title": "项目",
         "subgroups": [
            {"title": "网络推广", "urls": ["https://...", ...]},
            ...
         ]},
        ...
      ]
    }
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .utils import clean_text, log

DEFAULT_FALLBACK_SUBTYPE = "综合"


# ---------------------------------------------------------------- REST 抓取

def _api_root(site: str) -> str:
    site = site.strip().rstrip("/")
    if site.endswith("/wp-json/wp/v2"):
        return site
    return site + "/wp-json/wp/v2"


def _paged(fetcher, api: str, path: str, fields: str, *, per_page: int = 100,
           max_pages: int = 60, quiet: bool = False) -> list[dict]:
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        url = (f"{api}{path}?per_page={per_page}&page={page}"
               f"&_fields={fields}")
        r = fetcher.get_text(url)
        if not r.ok:
            # 400 = 页码越界（WP 的正常行为），到此为止
            break
        try:
            data = json.loads(r.text)
        except Exception:
            break
        if not isinstance(data, list) or not data:
            break
        out += data
        if not quiet:
            log(f"  REST {path} 第 {page} 页：+{len(data)}", "info")
        if len(data) < per_page:
            break
        time.sleep(0.2)
    return out


def fetch_categories(site: str, fetcher=None) -> list[dict]:
    """拉取全部分类（id/slug/name/parent/count/description）。"""
    fetcher = fetcher or _default_fetcher(site)
    return _paged(fetcher, _api_root(site), "/categories",
                  "id,slug,name,parent,count,description")


def fetch_posts(site: str, fetcher=None, *, with_content: bool = False,
                quiet: bool = False) -> list[dict]:
    """拉取全部文章。with_content=False 时只要元数据（体积小得多）。"""
    fetcher = fetcher or _default_fetcher(site)
    fields = "id,link,title,date,categories,tags"
    if with_content:
        fields += ",content,excerpt"
    return _paged(fetcher, _api_root(site), "/posts", fields, quiet=quiet)


def _default_fetcher(site: str):
    from .fetch import Fetcher
    return Fetcher(delay=0.4, verbose=False)


# ---------------------------------------------------------------- 分类树

def _clean_title(s: str) -> str:
    """分类名清尾：WordPress 分类名常带装饰性标点（如「资源！」）。"""
    t = clean_text(s or "").strip()
    t = re.sub(r"[！!～~·\s]+$", "", t)
    return t or clean_text(s or "").strip()


def _build_tree(categories: list[dict]) -> tuple[dict, list[dict]]:
    by_id = {int(c["id"]): dict(c) for c in categories}
    for c in by_id.values():
        c["children"] = []
    roots: list[dict] = []
    for c in by_id.values():
        p = int(c.get("parent") or 0)
        if p and p in by_id:
            by_id[p]["children"].append(c)
        else:
            roots.append(c)
    for c in by_id.values():
        c["children"].sort(key=lambda x: int(x["id"]))
    roots.sort(key=lambda x: int(x["id"]))
    return by_id, roots


def _subtree_min_id(c: dict) -> int:
    ids = [int(c["id"])] + [_subtree_min_id(x) for x in c.get("children", [])]
    return min(ids)


def _top_of(c: dict, by_id: dict) -> dict:
    cur = c
    for _ in range(12):
        p = int(cur.get("parent") or 0)
        if not p or p not in by_id:
            return cur
        cur = by_id[p]
    return cur


def _depth_of(c: dict, by_id: dict) -> int:
    d, cur = 0, c
    for _ in range(12):
        p = int(cur.get("parent") or 0)
        if not p or p not in by_id:
            return d
        cur = by_id[p]
        d += 1
    return d


# ---------------------------------------------------------------- 分组

def build_groups(categories: list[dict], posts: list[dict], *,
                 site: str = "", theme_order: list[str] | None = None,
                 subtype_order: list[str] | None = None,
                 priority: list[str] | None = None,
                 fallback_subtype: str = DEFAULT_FALLBACK_SUBTYPE,
                 verbose: bool = True) -> dict:
    """把「分类体系 + 文章列表」变成分组清单。

    归属规则（可复现，逐条说明）：
      1. 一篇文章只进一个细分类型，避免同一篇在多章重复出现。
      2. 优先选**最深**的分类（子分类 > 父分类），因为父分类往往是
         收纳用的容器（如「项目」「资源」）。
      3. 深度相同则按 `priority` 给出的 slug 顺序裁决；未列出的排在后面，
         再按分类 id 兜底。把「随笔」这类泛主题放到 priority 末尾，
         它自然只会收到"纯随笔"的文章。
      4. 文章落在某个顶层分类本身（没有子分类）时，进入该主题下的
         `fallback_subtype` 小节（默认「综合」）。

    theme_order / subtype_order 传 slug 列表可指定部与节的先后；
    不传则按分类树 id 顺序。
    """
    by_id, roots = _build_tree(categories)
    posts = [p for p in posts if (p.get("link") or "").strip()]
    posts.sort(key=lambda p: (p.get("date") or "", int(p.get("id") or 0)))
    priority = list(priority or [])

    def pick(cat_ids):
        cands = [by_id[i] for i in cat_ids if i in by_id]
        if not cands:
            return None

        def key(c):
            try:
                pr = priority.index(c["slug"])
            except ValueError:
                pr = len(priority)
            return (-_depth_of(c, by_id), pr, int(c["id"]))

        return sorted(cands, key=key)[0]

    # theme slug -> section slug -> [posts]（保持插入顺序）
    bucket: dict[str, dict[str, list[dict]]] = {}
    meta: dict[str, dict] = {}
    dropped: list[dict] = []

    for p in posts:
        cat = pick([int(i) for i in (p.get("categories") or [])])
        if cat is None:
            dropped.append(p)
            continue
        theme = _top_of(cat, by_id)
        if cat is theme or _depth_of(cat, by_id) == _depth_of(theme, by_id):
            section = None            # 直接挂在主题上的文章
        else:
            section = cat
        tkey = theme["slug"]
        bucket.setdefault(tkey, {}).setdefault(
            section["slug"] if section else "__self__", []).append(p)
        meta[tkey] = theme

    # ---- 组装（排序）
    def order_key(item, wanted):
        slug = item["slug"]
        if wanted and slug in wanted:
            return (0, wanted.index(slug))
        if wanted:
            return (1, int(item["id"]))
        return (0, int(item["id"]))

    themes = sorted(meta.values(), key=lambda c: order_key(c, theme_order or []))

    groups = []
    for theme in themes:
        subs = bucket.get(theme["slug"], {})
        entries: list[tuple[dict | None, list[dict]]] = []
        child_entries = [(c, subs[c["slug"]]) for c in theme["children"]
                         if c["slug"] in subs]
        child_entries.sort(key=lambda x: order_key(x[0], subtype_order or []))
        entries += child_entries
        if "__self__" in subs:
            entries.append((None, subs["__self__"]))

        out_subs = []
        for cat, plist in entries:
            title = cat["name"] if cat else fallback_subtype
            out_subs.append({
                "title": _clean_title(title),
                "slug": cat["slug"] if cat else "__self__",
                "urls": [p["link"] for p in plist],
            })
        groups.append({"title": _clean_title(theme["name"]),
                       "slug": theme["slug"], "subgroups": out_subs})

    if verbose:
        for g in groups:
            n = sum(len(s["urls"]) for s in g["subgroups"])
            log(f"  {g['title']}（{n} 篇）", "info")
            for s in g["subgroups"]:
                log(f"      {s['title']}：{len(s['urls'])} 篇", "info")
        if dropped:
            log(f"  {len(dropped)} 篇没有可用的分类，已跳过", "warn")

    return {
        "site": site,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rule": ("一篇文章只归一个细分类型；优先最深分类，深度相同按 priority "
                 "顺序裁决；直接挂在顶层分类下的文章进入「" + fallback_subtype + "」小节。"),
        "counts": {"posts": len(posts), "assigned": len(posts) - len(dropped),
                   "dropped": len(dropped)},
        "groups": groups,
    }


# ---------------------------------------------------------------- 读写

def load_groups(path: str | Path) -> dict:
    """读取分组清单（同时兼容裸列表写法）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {"site": "", "groups": data}
    if not isinstance(data, dict) or "groups" not in data:
        raise ValueError(f"分组清单格式不对（缺少 groups）：{path}")
    return data


def iter_group_urls(groups: dict) -> list[tuple[str, str, str]]:
    """展开成 [(主题, 类型, url)]，顺序即书内顺序。"""
    out: list[tuple[str, str, str]] = []
    for g in groups.get("groups", []):
        gt = g.get("title", "")
        for s in g.get("subgroups") or [{"title": "", "urls": g.get("urls", [])}]:
            for u in s.get("urls", []):
                out.append((gt, s.get("title", ""), u))
    return out


def write_urls(groups: dict, path: str | Path) -> int:
    rows = iter_group_urls(groups)
    Path(path).write_text("\n".join(u for _, _, u in rows) + "\n", encoding="utf-8")
    return len(rows)


def summarise(groups: dict) -> str:
    lines = []
    for g in groups.get("groups", []):
        n = sum(len(s.get("urls", [])) for s in g.get("subgroups", []))
        lines.append(f"{g.get('title','')}  ({n} 篇)")
        for s in g.get("subgroups", []):
            lines.append(f"    {s.get('title','')}  {len(s.get('urls', []))} 篇")
    return "\n".join(lines)


# ---------------------------------------------------------------- 文件名清理

def slug_ok(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]+", s or ""))
