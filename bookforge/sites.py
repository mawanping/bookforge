"""站点适配器。

通用启发式能覆盖大部分现代博客，但老站点常常完全不按套路出牌。
典型例子：paulgraham.com 的文章页**零个 <p> 标签**，段落全部用
`<br /><br />` 分隔，标题还是一张图片。这种情况必须专门适配。

适配器接口（全部可选，未实现则退回通用逻辑）：

    name              : str
    match(url)        : bool              是否命中该站点
    entry_urls(base)  : list[str]         默认入口页（列表页 / sitemap / feed）
    discover(...)     : list[str]         从入口页发现文章链接
    content_xpath     : list[str]         正文容器候选（按优先级）
    title_from        : callable          从 DOM 补标题
    heading_offset    : int               正文标题整体下移层数
    preprocess(tree)  : None              解析前的 DOM 修补
    postprocess(md, meta) -> str          最终 Markdown 润色
"""

from __future__ import annotations

import json
import re
import urllib.parse

from .utils import clean_text, log, strip_tags

_REGISTRY: list["SiteAdapter"] = []


def register(target):
    """注册站点适配器。

    既可以当装饰器用（传类），也可以直接传一个**实例**
    —— 声明式适配器（``site_config.YamlAdapter``）就是这样注册的。
    """
    if isinstance(target, type):
        _REGISTRY.append(target())
    else:
        _REGISTRY.append(target)
    return target


def find_adapter(url: str) -> "SiteAdapter | None":
    for a in _REGISTRY:
        try:
            if a.match(url):
                return a
        except Exception:
            continue
    return None


def adapter_names() -> list[str]:
    return [a.name for a in _REGISTRY]


# ---------------------------------------------------------------- 基类

class SiteAdapter:
    name = "generic"
    content_xpath: list[str] = []
    heading_offset = 0

    def match(self, url: str) -> bool:
        return False

    def entry_urls(self, base_url: str) -> list[str]:
        return []

    def discover(self, fetcher, entry_url: str, html: str, **kw) -> list[str]:
        from .fetch import discover_links
        return discover_links(fetcher, entry_url, html, **kw)

    def title_from(self, tree) -> str:
        return ""

    def author_from(self, tree) -> str:
        return ""

    def date_from(self, tree, text: str = "") -> str:
        return ""

    def preprocess(self, tree) -> None:
        """解析前修补 DOM（补段落、去导航等）。"""

    def postprocess(self, md: str, meta: dict) -> str:
        return md


# ---------------------------------------------------------------- paulgraham.com

@register
class PaulGraham(SiteAdapter):
    """Paul Graham 个人站。老式 table 布局 + <br><br> 分段 + 图片标题。"""

    name = "paulgraham"
    content_xpath = [
        "//td[@width='435']",
        "//table[@width='435']//td",
    ]
    heading_offset = 1     # 正文里的 h* 一律降级，避免和书名层级打架

    _HOSTS = ("paulgraham.com", "www.paulgraham.com")

    def match(self, url: str) -> bool:
        return urllib.parse.urlparse(url).netloc.lower() in self._HOSTS

    def entry_urls(self, base_url: str) -> list[str]:
        root = "https://paulgraham.com/"
        return [root + "articles.html", root + "rss.html"]

    # -------------------------------------------------- 发现

    def discover(self, fetcher, entry_url: str, html: str, **kw) -> list[str]:
        from .fetch import discover_from_feed

        # RSS 优先：顺序即官方发布顺序，且带标题
        if "rss" in entry_url.lower() or "<rss" in html[:500].lower() \
                or "<feed" in html[:500].lower():
            urls = discover_from_feed(fetcher, entry_url)
            if urls:
                return urls

        urls: list[str] = []
        seen: set[str] = set()
        for m in re.finditer(r'<a\s+href="([^"#?]+\.html)"[^>]*>(.*?)</a>',
                             html, re.I | re.S):
            href, label = m.group(1), clean_text(strip_tags(m.group(2)))
            if not label or len(label) < 6:
                continue
            if href.lower() in ("index.html", "articles.html", "rss.html",
                                "books.html", "bio.html", "faq.html", "raq.html",
                                "quo.html", "arc.html", "bel.html", "lisp.html",
                                "antispam.html", "kedrosky.html"):
                continue
            u = urllib.parse.urljoin(entry_url, href)
            if u in seen:
                continue
            seen.add(u)
            urls.append(u)
        return urls

    # -------------------------------------------------- 元数据

    def title_from(self, tree) -> str:
        # 标题是一张 gif 的 alt 属性
        for img in tree.xpath("//td//img[@alt]"):
            alt = clean_text(img.get("alt", ""))
            if len(alt) > 3 and not alt.lower().startswith(("http", "y combinator")):
                return alt
        t = tree.findtext(".//title") or ""
        return clean_text(t)

    def author_from(self, tree) -> str:
        return "Paul Graham"

    def date_from(self, tree, text: str = "") -> str:
        # 正文首行形如 "July 2023"
        m = re.search(
            r"\b(January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+(\d{4})\b", text[:600])
        if m:
            months = ["January", "February", "March", "April", "May", "June",
                      "July", "August", "September", "October", "November",
                      "December"]
            return f"{m.group(2)}-{months.index(m.group(1)) + 1:02d}"
        # 只有年份的情况：首行就是裸年份（PG 早年文章，如 Programming Bottom-Up 的 1993）。
        # 只认「第一行」，避免正文里的年份被误当成发表日期。
        first = (text.strip().splitlines() or [""])[0].strip()
        m2 = re.fullmatch(r"((?:19|20)\d{2})", first)
        if m2:
            return f"{m2.group(1)}-01"
        return ""

    # -------------------------------------------------- DOM 修补

    def preprocess(self, tree) -> None:
        # 1) 干掉左侧 image map 导航与顶部 logo
        for el in list(tree.xpath("//map | //area")):
            p = el.getparent()
            if p is not None:
                p.remove(el)
        for el in list(tree.xpath("//img[contains(@src,'bel-')]")):
            p = el.getparent()
            if p is not None:
                p.remove(el)
        # 2) 去掉标题图（标题已经进 frontmatter）
        for el in list(tree.xpath("//td//img[@alt]")):
            alt = clean_text(el.get("alt", ""))
            if len(alt) > 3 and "http" not in alt.lower():
                p = el.getparent()
                if p is not None:
                    p.remove(el)
        # 3) 拆掉「没有 <tr> 的 table」——PG 部分老页面拿 <table width=100%>
        #    当纯布局容器，而且闭合标签被写进了 HTML 注释，导致整段正文变成
        #    <table> 的直接子文本，被表格转换器忽略（foundervisa.html 即如此）。
        #    反复拆，直到没有空壳 table 为止（外层拆掉后可能露出内层）。
        for _ in range(6):
            shells = [t for t in tree.xpath("//table") if not t.findall(".//tr")]
            if not shells:
                break
            for t in shells:
                try:
                    t.drop_tag()
                except Exception:
                    pass
        # 4) 1x1 透明占位图（PG 拿它们当间距用）——纯噪音，删掉
        for el in list(tree.xpath(
                "//img[contains(@src,'trans_1x1') or contains(@src,'/Img/trans')"
                " or contains(@src,'spacer')]")):
            p = el.getparent()
            if p is not None:
                p.remove(el)
        # 5) 掏空后只剩空壳的 table/tr 一并清掉（否则会渲染出空表格）
        for _ in range(3):
            empties = [e for e in tree.xpath("//table | //tr")
                       if not e.findall(".//img")
                       and not re.sub(r"\s+", "", "".join(e.itertext()))]
            if not empties:
                break
            for e in empties:
                if e.getparent() is not None:
                    e.getparent().remove(e)

    def postprocess(self, md: str, meta: dict) -> str:
        md = _convert_pg_footnotes(md)
        md = _strip_pg_related(md)
        return md


def _strip_pg_related(md: str) -> str:
    """去掉文末的 "Related:" 相关阅读区块——那是站点推荐位，不是正文。"""
    return re.sub(r"\n{1,}\*\*Related:\*\*[\s\S]*$", "\n", md)


def _convert_pg_footnotes(md: str) -> str:
    """PG 的灰色角标 + 文末脚注区 → Markdown footnote 语法。

    原文形态：
        正文 ... some claim[1]            （角标是链到 #f1n 的灰字链接）
        ...
        [1] 脚注内容，可能跨多段，也可能跟着引用块
        [2] ...

    注意：不能从文末往前扫，因为脚注区后面常常还有一句
    "Thanks to ..." 致谢，会把扫描打断。改为按"定义行的首尾"框定脚注区。
    """
    if not re.search(r"^\[\d+\]\s+\S", md, re.M):
        return md

    lines = md.split("\n")
    def_idx = [i for i, l in enumerate(lines)
               if re.match(r"^\[(\d+)\]\s+\S", l.strip())]
    if len(def_idx) < 2:
        return md

    first, last = def_idx[0], def_idx[-1]
    zone = lines[first:last + 1]
    body_lines = lines[:first] + lines[last + 1:]

    defs: dict[str, list[str]] = {}
    order: list[str] = []
    cur: str | None = None
    for ln in zone:
        m = re.match(r"^\[(\d+)\]\s+(.*)$", ln.strip())
        if m:
            cur = m.group(1)
            if cur not in defs:
                defs[cur] = []
                order.append(cur)
            defs[cur].append(m.group(2).strip())
        elif cur is not None:
            defs[cur].append(ln.rstrip())
    if not defs:
        return md

    body = "\n".join(body_lines)

    # 1) 带链接的角标：[[1](...#f1n)] → [^1]
    body = re.sub(r"\[\[\s*(\d+)\s*\]\(\s*[^)]*#f\1n\s*\)\]", r"[^\1]", body)
    # 2) 残留的 [1](#f1n) 形式
    body = re.sub(r"\[\s*(\d+)\s*\]\(\s*[^)]*#f\1n\s*\)", r"[^\1]", body)
    # 3) 裸角标 [1]（仅当该编号确实有定义）
    nums = set(defs)
    body = re.sub(r"(?<!\[)\[(\d{1,3})\](?!\()",
                  lambda m: f"[^{m.group(1)}]" if m.group(1) in nums else m.group(0),
                  body)

    body = re.sub(r"\n{3,}", "\n\n", body).rstrip()

    defs_md: list[str] = []
    for n in order:
        raw = list(defs[n])
        # 去掉首尾空行，段落之间空一行
        while raw and not raw[0].strip():
            raw.pop(0)
        while raw and not raw[-1].strip():
            raw.pop()
        if not raw:
            continue
        text = "\n".join(raw)
        text = re.sub(r"\n{3,}", "\n\n", text)
        # Markdown footnote 正文需要缩进 4 空格（首行跟在 [^n]: 后）
        parts = text.split("\n")
        formatted = parts[0]
        for extra in parts[1:]:
            formatted += "\n" + ("    " + extra if extra.strip() else "")
        defs_md.append(f"[^{n}]: {formatted}")

    if not defs_md:
        return md
    return body + "\n\n" + "\n\n".join(defs_md) + "\n"


# ---------------------------------------------------------------- 1230.la（无极领域）

def _drop_node(el) -> None:
    p = el.getparent()
    if p is not None:
        p.remove(el)


def _text_of_xpath(tree, xp: str) -> str:
    for el in tree.xpath(xp):
        try:
            return clean_text(el.text_content())
        except Exception:
            continue
    return ""


@register
class Wujie1230(SiteAdapter):
    """中文 WordPress 博客「无极领域」（1230.la）。

    正文在一个非常整齐的 `<article class="article-content">` 里；
    标题/作者/日期/分类则在 article 之外的 `<header class="article-header">`，
    所以只要把正文容器卡准，就天然不会把面包屑、阅读数、分享按钮带进来。
    """

    name = "wujie1230"
    content_xpath = [
        "//article[contains(@class,'article-content')]",
        "//div[contains(@class,'article-content')]",
        "//article[contains(@class,'post')]",
    ]
    heading_offset = 1     # 正文 h2 降一级，给章节标题让位

    _HOSTS = ("1230.la", "www.1230.la")
    _ROOT = "https://1230.la"

    def match(self, url: str) -> bool:
        return urllib.parse.urlparse(url).netloc.lower() in self._HOSTS

    def entry_urls(self, base_url: str) -> list[str]:
        return [self._ROOT + "/wp-sitemap-posts-post-1.xml"]

    # -------------------------------------------------- 发现

    def discover(self, fetcher, entry_url: str, html: str, **kw) -> list[str]:
        """优先走 WP REST API：一次拿全站文章，比翻列表页/分类页完整得多。

        拿不到（接口被关）时退回 sitemap / 通用链接发现。
        """
        limit = kw.get("limit") or 0
        urls: list[str] = []
        for page in range(1, 13):
            api = (f"{self._ROOT}/wp-json/wp/v2/posts"
                   f"?per_page=100&page={page}&_fields=link")
            r = fetcher.get_text(api)
            if not r.ok:
                break
            try:
                data = json.loads(r.text)
            except Exception:
                break
            if not isinstance(data, list) or not data:
                break
            urls += [d["link"] for d in data if isinstance(d, dict) and d.get("link")]
            if len(data) < 100:
                break
        if urls:
            urls = list(dict.fromkeys(urls))
            return urls[:limit] if limit else urls
        # 兜底
        from .fetch import discover_from_sitemap
        return discover_from_sitemap(fetcher, self._ROOT + "/wp-sitemap.xml",
                                     include=kw.get("include", ""), limit=limit)

    # -------------------------------------------------- 元数据

    def title_from(self, tree) -> str:
        for xp in ("//h1[contains(@class,'article-title')]",
                   "//header[contains(@class,'article-header')]//h1",
                   "//article//h1"):
            t = _text_of_xpath(tree, xp)
            if t:
                return t
        return ""

    def author_from(self, tree) -> str:
        meta = _text_of_xpath(tree, "//ul[contains(@class,'article-meta')]")
        m = re.match(r"\s*([^\s]{1,20}?)\s*发布于", meta)
        return m.group(1) if m else ""

    def date_from(self, tree, text: str = "") -> str:
        meta = _text_of_xpath(tree, "//ul[contains(@class,'article-meta')]")
        m = re.search(r"发布于\s*((?:19|20)\d{2}-\d{1,2}-\d{1,2})", meta)
        if not m:
            m = re.search(r"((?:19|20)\d{2}-\d{1,2}-\d{1,2})", meta)
        if m:
            return m.group(1)
        m = re.search(r"((?:19|20)\d{2}-\d{1,2}-\d{1,2})", text[:400])
        return m.group(1) if m else ""

    # -------------------------------------------------- DOM 修补

    def preprocess(self, tree) -> None:
        # 0) 密码保护帖：正文容器里只有一个登录表单，prune 会把 form/input/label
        #    全部删掉，最后剩一个空壳，只能捞到导航垃圾。这里先把表单换成一句
        #    说明，保证正文容器非空、且语义正确。
        for el in list(tree.xpath("//*[contains(@class,'post-password-form')]")):
            note = el.makeelement("p", {})
            note.text = "（本文受密码保护，正文未公开。）"
            p = el.getparent()
            if p is not None:
                p.replace(el, note)
        # 1) 版权行（"转载请保留出处：…"）——每篇都挂在正文末尾
        for pat in ("post-copyright", "article-tags", "article-social",
                    "action-share", "related-posts", "comments-area",
                    "article-footer", "post-navigation"):
            for el in list(tree.xpath(f"//*[contains(@class,'{pat}')]")):
                _drop_node(el)
        # 2) 正文里的 iframe（视频/外链嵌入）无法进 Markdown，去掉避免留空壳
        for el in list(tree.xpath("//article//iframe | //article//script")):
            _drop_node(el)
        # 3) 站内推广横条（公众号二维码之类，常在正文中段）
        for el in list(tree.xpath("//article//*[contains(@class,'ad-')]")):
            _drop_node(el)


# ---------------------------------------------------------------- 通用 WordPress

@register
class WordPress(SiteAdapter):
    """WordPress 系站点：article/entry-content 结构规整，主要是去掉评论与分享。"""

    name = "wordpress"
    content_xpath = [
        "//article[contains(@class,'post')]//div[contains(@class,'entry-content')]",
        "//div[contains(@class,'entry-content')]",
        "//article[contains(@class,'post')]",
    ]

    def match(self, url: str) -> bool:
        # 无法从 URL 判定，交给通用逻辑；此处不主动匹配
        return False

    def preprocess(self, tree) -> None:
        for pat in ("comments-area", "comment-respond", "sharedaddy",
                    "post-navigation", "entry-footer", "jp-relatedposts"):
            for el in list(tree.xpath(f"//*[contains(@class,'{pat}')]")):
                p = el.getparent()
                if p is not None:
                    p.remove(el)


# ---------------------------------------------------------------- 通用修正

def generic_preprocess(tree) -> None:
    """适用于所有站点的 DOM 修补（不接受适配器时也生效）。

    核心是处理"用 <br><br> 当段落分隔符"的老式排版。
    很多博客、论坛、老站点都这么写，若不处理，正文会被压成一大坨。
    """
    _promote_br_paragraphs(tree)


def _promote_br_paragraphs(tree, min_br: int = 6) -> None:
    """把连续 `<br><br>` 提升为真正的段落分隔。

    只在明显"段落靠 br 分隔"时生效（p 极少 + br 很多），且仅处理
    那些内部不含块级元素的片段，避免把结构搞乱。
    """
    from lxml import etree, html as lxml_html

    for container in list(tree.iter("td", "div", "article", "section", "body")):
        p_count = len(container.findall(".//p"))
        brs = container.findall(".//br")
        if len(brs) < min_br or p_count >= 3:
            continue

        # 该容器内不应已有大量块级分节，否则不是"br 分段"式排版
        blocks = container.findall(".//div") + container.findall(".//table")
        if len(blocks) > 12:
            continue

        frag = etree.tostring(container, encoding="unicode", method="html")
        if not frag:
            continue
        # 连续两个以上 br → 段落边界
        new = re.sub(r"(?:\s*<br\s*/?>\s*){2,}", "</p>\n<p>", frag, flags=re.I)
        if new == frag:
            continue
        # 单个 br 一律当作空格（老站点在段落内硬换行）
        new = re.sub(r"\s*<br\s*/?>\s*", " ", new, flags=re.I)
        new = re.sub(r"</p>\s*<p>", "</p><p>", new)

        try:
            rebuilt = lxml_html.fragment_fromstring("<p>" + new + "</p>",
                                                    create_parent="div")
        except Exception:
            continue
        # 安全性检查：新段落里不能残留块级元素
        bad = [e.tag for e in rebuilt.iter() if isinstance(e.tag, str)
               and e.tag.lower() in ("div", "table", "ul", "ol", "p")
               and e is not rebuilt]
        if bad:
            # 有嵌套块级 → 放弃这一个容器，继续看别的（别整个函数退出）
            continue

        parent = container.getparent()
        if parent is None:
            continue
        idx = parent.index(container)
        parent.remove(container)
        children = list(rebuilt)
        for off, child in enumerate(children):
            parent.insert(idx + off, child)
        # 改完继续处理其它容器；直接 return 会让后面的容器永远得不到处理
        continue


# ---------------------------------------------------------------- Markdown 润色

def reflow_paragraphs(md: str) -> str:
    """合并段落内的软换行。

    老站点（如 PG）正文是硬换行排版，转成 Markdown 后每行一个换行，
    既不利于阅读也不利于翻译分块。这里把段内单换行合并为空格，
    但保留：
      - 代码块内的一切
      - 列表项内的换行（有缩进）
      - 表格行
      - 显式硬换行（行尾两个空格）
      - 引用块内的换行
    """
    out: list[str] = []
    in_fence = False
    fence_pat = re.compile(r"^\s*(```|~~~)")

    for ln in md.split("\n"):
        if fence_pat.match(ln):
            in_fence = not in_fence
            out.append(ln)
            continue
        if in_fence:
            out.append(ln)
            continue

        s = ln.rstrip()
        prev = out[-1] if out else ""

        if not s:
            out.append("")
            continue

        is_struct = bool(re.match(
            r"^\s*([-*+]|\d{1,3}[.)])\s|^\s*>|^\s*#{1,6}\s|^\s*\||^\s*!\[|^\s*\[!|^\s*---+\s*$"
            r"|^\s*\[\^[^\]]+\]:|^\s{2,}\S", s))
        prev_is_struct = bool(re.match(
            r"^\s*([-*+]|\d{1,3}[.)])\s|^\s*>|^\s*#{1,6}\s|^\s*\||^\s*---+\s*$"
            r"|^\s*\[\^[^\]]+\]:|^\s{2,}\S", prev))
        prev_hard_break = prev.endswith("  ") or prev.endswith("\\")

        if (not is_struct and not prev_is_struct and not prev_hard_break
                and prev and prev.strip() and not prev.startswith("#")):
            out[-1] = prev + " " + s.lstrip()
        else:
            out.append(s)
    return "\n".join(out)


def tidy_markdown(md: str) -> str:
    """收尾润色：空行、列表标记、中文标点周边空白。"""
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    # 统一列表标记
    md = re.sub(r"^(\s*)[*+]\s+", r"\1- ", md, flags=re.M)
    # 中文之间的多余空格（不碰 Markdown 标记与代码）
    md = re.sub(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])", "", md)
    md = re.sub(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[，。！？；：、）】》」』])", "", md)
    md = re.sub(r"(?<=[（【《「『])[ \t]+(?=[\u4e00-\u9fff])", "", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"
