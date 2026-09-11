"""声明式站点适配器：不写代码就能适配一个新站点。

原版把每个站点的适配逻辑写成 Python 类（``sites.py``）—— 这要求使用者
会改 Python、会重新部署。想让工具真正通用，必须让"适配新站点"变成
**加一个配置文件**。

只要在下面任一位置放一个 ``*.yaml``（或 ``*.json``）即可：

1. ``./bookforge-adapters/``（当前工作目录，适合跟书籍项目放一起）
2. ``~/.config/bookforge/adapters/``（用户级，跨项目复用）
3. ``$BOOKFORGE_ADAPTERS`` 指向的目录/文件

格式（所有字段都可选，但至少要给 ``hosts`` 或 ``match_regex``）：

```yaml
name: myblog
hosts: [myblog.com, www.myblog.com]
content_xpath:
  - "//div[contains(@class,'post-content')]"
  - "//article"
title_selector: "h1.post-title"
author_selector: ".author-name"
date_selector: "time"            # 优先取 datetime 属性，其次取文本
date_regex: "\\d{4}-\\d{2}-\\d{2}"
remove: [".sidebar", ".comments", "nav", ".share"]
heading_offset: 0
discover:
  feed: https://myblog.com/feed
  sitemap: https://myblog.com/sitemap.xml
  list_page: https://myblog.com/archives
```

写完之后 ``bookforge adapters`` 就能看到它，``bookforge build`` 会自动匹配。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .sites import SiteAdapter, register
from .utils import clean_text, log, strip_tags

try:                                    # YAML 是可选依赖
    import yaml as _yaml
except ImportError:                     # pragma: no cover
    _yaml = None


# ---------------------------------------------------------------- 查找位置


def adapter_search_paths() -> list[Path]:
    """返回所有会被扫描的适配器目录/文件。"""
    out: list[Path] = []
    cwd = Path.cwd()
    out.append(cwd / "bookforge-adapters")
    out.append(cwd / ".bookforge" / "adapters")
    out.append(Path.home() / ".config" / "bookforge" / "adapters")
    out.append(Path.home() / ".bookforge" / "adapters")
    env = os.environ.get("BOOKFORGE_ADAPTERS", "")
    for chunk in env.split(os.pathsep):
        if chunk.strip():
            out.append(Path(chunk.strip()).expanduser())
    # 包内自带（放这里的是内置示例，用户不必改）
    out.append(Path(__file__).resolve().parent / "adapters")
    # 去重保序
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        k = str(p)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _iter_config_files(p: Path):
    if p.is_file() and p.suffix.lower() in (".yaml", ".yml", ".json"):
        yield p
        return
    if not p.is_dir():
        return
    for f in sorted(p.iterdir()):
        if f.suffix.lower() in (".yaml", ".yml", ".json"):
            yield f


def _load_config(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    if _yaml is None:
        raise RuntimeError(
            f"{path} 是 YAML，但没装 PyYAML。请 `pip install pyyaml`，"
            f"或把它改成 JSON 格式。"
        )
    data = _yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 顶层必须是映射（key: value）")
    return data


# ---------------------------------------------------------------- 适配器


class YamlAdapter(SiteAdapter):
    """由配置文件驱动的通用适配器。"""

    def __init__(self, cfg: dict, source: str = ""):
        self.cfg = cfg
        self.source = source
        self.name = str(cfg.get("name") or "yaml")
        self.content_xpath = [str(x) for x in (cfg.get("content_xpath") or [])]
        self.heading_offset = int(cfg.get("heading_offset") or 0)
        self.remove = [str(x) for x in (cfg.get("remove") or [])]

        self._hosts = [str(h).lower().lstrip(".") for h in (cfg.get("hosts") or [])]
        self._match_re = cfg.get("match_regex") or cfg.get("match")
        self._url_prefixes = [str(u) for u in (cfg.get("url_prefixes") or [])]

        self._title_sel = cfg.get("title_selector") or ""
        self._author_sel = cfg.get("author_selector") or ""
        self._date_sel = cfg.get("date_selector") or ""
        self._date_attr = cfg.get("date_attr") or ""
        self._date_re = cfg.get("date_regex") or ""
        self._author_text_re = cfg.get("author_regex") or ""

        d = cfg.get("discover") or {}
        self._disc = d if isinstance(d, dict) else {}
        self._entry_urls = [str(u) for u in (cfg.get("entry_urls") or [])]
        self._link_include = cfg.get("link_include") or ""
        self._link_exclude = cfg.get("link_exclude") or ""

    # ---- 匹配

    def match(self, url: str) -> bool:
        if self._match_re:
            try:
                if re.search(str(self._match_re), url):
                    return True
            except re.error:
                pass
        if self._url_prefixes:
            if any(url.startswith(p) for p in self._url_prefixes):
                return True
        if self._hosts:
            import urllib.parse
            netloc = urllib.parse.urlparse(url).netloc.lower()
            host = netloc.split(":")[0]
            for h in self._hosts:
                if host == h or host.endswith("." + h):
                    return True
        return False

    # ---- 发现

    def entry_urls_for(self, base_url: str) -> list[str]:
        return list(self._entry_urls)

    def entry_urls(self, base_url: str) -> list[str]:
        return list(self._entry_urls)

    def discover(self, fetcher, entry_url: str, html: str, **kw) -> list[str]:
        from .fetch import discover_from_feed, discover_from_sitemap, discover_links

        urls: list[str] = []
        disc = self._disc

        for key, fn in (("feed", discover_from_feed),
                        ("sitemap", discover_from_sitemap)):
            target = disc.get(key)
            if not target:
                continue
            try:
                urls = fn(fetcher, str(target))
            except Exception as e:
                log(f"适配器 {self.name} 的 {key} 发现失败：{e}", "warn")
                urls = []
            if urls:
                break

        if not urls:
            urls = discover_links(fetcher, entry_url, html, **kw)

        if self._link_include:
            rx = re.compile(self._link_include)
            urls = [u for u in urls if rx.search(u)]
        if self._link_exclude:
            rx = re.compile(self._link_exclude)
            urls = [u for u in urls if not rx.search(u)]
        return urls

    # ---- 预处理

    def preprocess(self, tree) -> None:
        for sel in self.remove:
            try:
                for n in tree.xpath(sel):
                    parent = n.getparent()
                    if parent is not None:
                        parent.remove(n)
            except Exception:
                continue

    # ---- 元数据

    def _first(self, tree, sel: str):
        if not sel:
            return None
        try:
            nodes = tree.xpath(sel)
        except Exception:
            return None
        return nodes[0] if nodes else None

    def title_from(self, tree) -> str:
        n = self._first(tree, self._title_sel)
        if n is not None:
            t = clean_text(strip_tags(
                n.text_content() if hasattr(n, "text_content") else str(n)))
            if t:
                return t
        return clean_text(tree.findtext(".//title") or "")

    def author_from(self, tree) -> str:
        if self._author_text_re:
            txt = clean_text(tree.text_content() or "")
            m = re.search(self._author_text_re, txt)
            if m:
                return clean_text(m.group(1) if m.groups() else m.group(0))
        n = self._first(tree, self._author_sel)
        if n is not None:
            return clean_text(strip_tags(
                n.text_content() if hasattr(n, "text_content") else str(n)))
        return ""

    def date_from(self, tree, text: str = "") -> str:
        n = self._first(tree, self._date_sel)
        raw = ""
        if n is not None:
            raw = str(n.get(self._date_attr)) if self._date_attr else ""
            if not raw:
                raw = clean_text(strip_tags(
                    n.text_content() if hasattr(n, "text_content") else str(n)))
        if not raw:
            raw = clean_text(text or tree.text_content() or "")
        pat = self._date_re or r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{4}[-/.]\d{1,2}"
        m = re.search(pat, raw)
        if not m:
            return ""
        s = m.group(0).replace("/", "-").replace(".", "-")
        parts = s.split("-")
        if len(parts) == 2:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}"
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"


# ---------------------------------------------------------------- 加载


def load_adapters(paths: list[Path] | None = None, *, verbose: bool = True) -> list[str]:
    """扫描并注册所有声明式适配器，返回注册成功的名字列表。"""
    names: list[str] = []
    seen_names: set[str] = set()

    for base in (paths if paths is not None else adapter_search_paths()):
        for f in _iter_config_files(base):
            try:
                cfg = _load_config(f)
            except Exception as e:
                log(f"跳过适配器配置 {f}：{e}", "warn")
                continue
            cfg.setdefault("name", f.stem)
            name = str(cfg["name"])
            if name in seen_names:
                continue
            try:
                adapter = YamlAdapter(cfg, source=str(f))
            except Exception as e:
                log(f"跳过适配器 {f}：{e}", "warn")
                continue
            if not (adapter._hosts or adapter._match_re or adapter._url_prefixes):
                if verbose:
                    log(f"跳过 {f}：需要 hosts / match_regex / url_prefixes 之一", "warn")
                continue
            register(adapter)
            seen_names.add(name)
            names.append(name)
            if verbose:
                log(f"已加载站点适配器：{name}  ← {f}", "ok")
    return names


def adapter_summary() -> list[dict]:
    """列出全部已注册适配器（内置 + 声明式），给 ``bookforge adapters`` 用。"""
    from . import sites as _sites

    rows: list[dict] = []
    for a in _sites._REGISTRY:
        rows.append({
            "name": a.name,
            "kind": "yaml" if isinstance(a, YamlAdapter) else "builtin",
            "hosts": list(getattr(a, "_hosts", []) or []),
            "content_xpath": list(getattr(a, "content_xpath", []) or []),
            "source": getattr(a, "source", "") or "",
        })
    return rows
