"""正文抽取与 HTML → Markdown 转换。

为什么不用现成的 html2text / readability？
  1. 需要把图片替换成本地路径（离线归档）
  2. 需要精确保留代码块语言、表格、题注
  3. 需要在转换前用占位符保护代码，避免后续空白归一化破坏缩进

所以这里自己实现一套：先定位正文区块，再逐节点转换。
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

from lxml import etree, html as lxml_html

from .utils import clean_text, count_words, strip_tags

# ---------------------------------------------------------------- 常量

# 一定不是正文的标签
_DROP_TAGS = {
    "script", "style", "noscript", "iframe", "svg", "canvas", "form",
    "input", "button", "select", "textarea", "nav", "aside", "footer",
    "header", "menu", "template", "link", "meta", "object", "embed",
}

# 噪声 class/id 关键词（命中则整块丢弃）
_NOISE_PAT = re.compile(
    r"(^|[-_ ])("
    r"nav|navbar|navigation|menu|sidebar|side-bar|footer|header|masthead|"
    r"comment|comments|disqus|share|sharing|social|related|recommend|"
    r"subscribe|newsletter|signup|promo|advert|ads?|sponsor|banner|"
    r"cookie|gdpr|breadcrumb|pagination|pager|tags?-list|widget|"
    r"skip-link|screen-reader|sr-only|visually-hidden"
    r")($|[-_ ])",
    re.I,
)

# 正文容器候选
_CONTENT_HINT = re.compile(
    r"(^|[-_ ])(article|post|content|entry|main|body|essay|text|markdown|story)($|[-_ ])",
    re.I,
)

_BLOCK_TAGS = {
    "p", "div", "section", "article", "blockquote", "pre", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "table", "figure", "figcaption",
    "hr", "br", "dl", "dt", "dd", "main", "details", "summary",
}

_LANG_ALIASES = {
    "py": "python", "python3": "python", "js": "javascript", "ts": "typescript",
    "sh": "bash", "shell": "bash", "zsh": "bash", "console": "bash",
    "yml": "yaml", "rb": "ruby", "rs": "rust", "golang": "go",
    "c++": "cpp", "c#": "csharp", "objc": "objectivec", "md": "markdown",
    "html5": "html", "htm": "html",
}


@dataclass
class ExtractResult:
    title: str = ""
    author: str = ""
    published_at: str = ""
    summary: str = ""
    markdown: str = ""
    images: list[str] = field(default_factory=list)
    language: str = ""
    confidence: float = 0.0
    strategy: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return count_words(self.markdown)


# ---------------------------------------------------------------- 工具

def parse_html(raw: str | bytes) -> lxml_html.HtmlElement:
    """容错解析 HTML。优先 lxml 严格模式，失败退回宽松模式。"""
    if isinstance(raw, bytes):
        # 尝试从 meta 嗅探编码
        m = re.search(rb'charset=["\']?([\w\-]+)', raw[:4096], re.I)
        enc = None
        if m:
            enc = m.group(1).decode("ascii", "ignore")
        for e in filter(None, [enc, "utf-8", "gbk", "latin-1"]):
            try:
                raw = raw.decode(e)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str) and "<" not in raw[:200]:
        pass
    try:
        return lxml_html.fromstring(raw)
    except Exception:
        parser = lxml_html.HTMLParser(recover=True, encoding="utf-8")
        return lxml_html.fromstring(raw.encode("utf-8", "replace"), parser=parser)


def _classes(el) -> str:
    return " ".join(filter(None, [el.get("class", ""),
                                  el.get("id", ""),
                                  el.get("role", "")]))


def _is_noise(el) -> bool:
    return bool(_NOISE_PAT.search(_classes(el)))


def _text_len(el) -> int:
    return len(strip_tags(etree.tostring(el, encoding="unicode", method="text")))


# ---------------------------------------------------------------- 清洗

def prune(tree) -> None:
    """移除脚本、导航、评论区等噪声节点。"""
    for el in list(tree.iter()):
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.lower()
        if tag in _DROP_TAGS:
            _drop(el)
            continue
        if el is tree:
            continue
        # 链接密度极高的容器（导航/标签云）
        if tag in ("div", "ul", "section") and _is_noise(el):
            _drop(el)
            continue
        if tag in ("div", "section", "ul") and _link_density(el) > 0.7 and _text_len(el) < 800:
            _drop(el)


def _drop(el) -> None:
    parent = el.getparent()
    if parent is not None:
        parent.remove(el)


def _link_density(el) -> float:
    total = _text_len(el)
    if total < 40:
        return 0.0
    link_chars = sum(_text_len(a) for a in el.iter("a"))
    return link_chars / max(total, 1)


# ---------------------------------------------------------------- 评分 / 定位正文

def find_content(tree, adapter=None) -> tuple[object, float, str]:
    """给候选容器打分，返回 (节点, 置信度, 策略名)。

    打分参考 readability 思路：
      段落文本长度 + 标点密度 + 内容线索 class，减去链接密度惩罚。
    若传入站点适配器，其 content_xpath 拥有最高优先级。
    """
    best, best_score, best_strategy = None, -1.0, "none"

    # ---- 策略 0：站点适配器提供的选择器（最高优先）
    if adapter is not None:
        for sel in (adapter.content_xpath or []):
            try:
                nodes = tree.xpath(sel)
            except Exception:
                continue
            for n in nodes:
                if _text_len(n) < 150:
                    continue
                return n, 0.95, f"adapter:{adapter.name}"
            # 容器确实存在、只是内容很短（贴图帖 / 密码帖 / 占位文）。
            # 既然适配器点名了这个容器，它就是权威答案——不能因为「短」就
            # 放弃它去找通用启发式，否则会捞到「回顶部」这类导航垃圾。
            if nodes:
                for n in nodes:
                    if _text_len(n) > 0 or n.findall(".//img"):
                        return n, 0.55, f"adapter:{adapter.name}:short"
                return nodes[0], 0.4, f"adapter:{adapter.name}:empty"
        # 适配器的 preprocess 已经清理过导航，这里放宽判定
        for n in tree.iter("td"):
            if _text_len(n) > 800 and len(n.findall(".//br")) > 5:
                if _score_node(n) > 0:
                    return n, 0.8, f"adapter:{adapter.name}:td-fallback"

    # ---- 策略 A：显式语义标签
    for sel, strategy, bonus in (
        ("//article", "article-tag", 220),
        ("//main", "main-tag", 160),
        ("//*[@role='main']", "role-main", 160),
        ("//*[contains(@class,'post-content')]", "class:post-content", 200),
        ("//*[contains(@class,'entry-content')]", "class:entry-content", 200),
        ("//*[contains(@class,'article-content')]", "class:article-content", 200),
        ("//*[contains(@class,'markdown-body')]", "class:markdown-body", 200),
    ):
        try:
            nodes = tree.xpath(sel)
        except Exception:
            continue
        for n in nodes:
            if _text_len(n) < 200:
                continue
            score = _score_node(n) + bonus
            if score > best_score:
                best, best_score, best_strategy = n, score, strategy

    # ---- 策略 B：全量候选打分（兜底）
    for n in tree.iter():
        if not isinstance(n.tag, str):
            continue
        if n.tag.lower() not in ("div", "section", "article", "main", "td", "body"):
            continue
        score = _score_node(n)
        if score > best_score:
            best, best_score, best_strategy = n, score, "heuristic"

    if best is None:
        body = tree.find("body")
        best = body if body is not None else tree
        best_score, best_strategy = 0.0, "body-fallback"

    confidence = _confidence(best, best_strategy)
    return best, confidence, best_strategy


def _score_node(el) -> float:
    """节点内容质量打分。"""
    text = strip_tags(etree.tostring(el, encoding="unicode", method="text"))
    length = len(text)
    if length < 120:
        return -1.0

    score = min(length, 24000) / 12.0

    # 段落贡献
    ps = el.findall(".//p")
    para_chars = sum(len(strip_tags(etree.tostring(p, encoding="unicode", method="text")))
                     for p in ps)
    score += min(para_chars, 24000) / 20.0
    score += min(len(ps), 60) * 2.0

    # 标点密度（真正的散文有句号、逗号）
    punct = len(re.findall(r"[,，。.！!？?；;：:]", text))
    score += min(punct, 600) * 0.5
    if length > 400 and punct / length < 0.005:
        score -= 90  # 全是短句/词条，不是文章

    # 反向惩罚
    score -= _link_density(el) * 200
    score -= max(0, len(el.findall(".//a")) - 30) * 3

    # class 线索
    cls = _classes(el)
    if _CONTENT_HINT.search(cls):
        score += 70
    if re.search(r"(comment|sidebar|footer|nav|meta|share)", cls, re.I):
        score -= 120
    if el.tag.lower() == "body":
        score -= 45
    return score


def _confidence(el, strategy: str) -> float:
    length = _text_len(el)
    base = {"article-tag": 0.92, "main-tag": 0.85, "role-main": 0.85,
            "class:post-content": 0.9, "class:entry-content": 0.9,
            "class:article-content": 0.9, "class:markdown-body": 0.9,
            "heuristic": 0.62, "body-fallback": 0.28}.get(strategy, 0.5)
    if length > 6000:
        base = min(1.0, base + 0.05)
    elif length < 500:
        base *= 0.55
    paras = len(el.findall(".//p"))
    if paras >= 5:
        base = min(1.0, base + 0.04)
    elif paras <= 1:
        base *= 0.7
    return round(base, 3)


# ---------------------------------------------------------------- 元数据

# 标题尾部的分隔符（英文短横、中文破折号、竖线、中点等）
_TITLE_SEPS = (" - ", " – ", " — ", " — ", " | ", " · ", " :: ", "：", " -")


def strip_site_suffix(title: str, site_name: str, raw_page_title: str = "") -> str:
    """把「文章标题 - 站名」里的站名剥掉。

    只在后缀**确实等于站名**时才动手，避免把标题里正常的破折号误删。
    有些站点没给 og:site_name，就退而用 <title> 自带的「站点首页标题」
    做对比（例如首页标题是「某某的博客」，文章标题是「X - 某某的博客」）。
    """
    title = (title or "").strip()
    if not title:
        return title

    names = {n.strip() for n in (site_name, raw_page_title) if n and n.strip()}
    # <title> 里可能还带「首页/Home」之类的尾巴，一并当候选
    for n in list(names):
        for tail in (" - 首页", " | 首页", " - Home", " | Home"):
            if n.endswith(tail):
                names.add(n[: -len(tail)].strip())

    for sep in _TITLE_SEPS:
        for n in names:
            if not n:
                continue
            suffix = sep + n
            if title.endswith(suffix) and len(title) > len(suffix) + 1:
                return title[: -len(suffix)].strip()
    return title


def strip_common_title_suffix(titles: list[str], *, min_ratio: float = 0.6,
                              min_count: int = 3) -> dict[str, str]:
    """批量剥掉"整站统一的标题尾巴"。

    有些站点既不写 ``og:site_name``，``<title>`` 又统一是
    「文章标题 - 站名」。单看一篇文章无法判断哪一段是站名，
    **但把整批标题放在一起看就很明显**：反复出现在末尾的那一段就是站名。

    只在这个尾巴出现在 ``>= min_ratio`` 的标题里、且数量达到 ``min_count``
    时才动手，避免误删正常标题里的破折号。返回 ``{原标题: 新标题}``。
    """
    if len(titles) < min_count:
        return {}

    tails: dict[str, int] = {}
    seps = (" - ", " – ", " — ", " | ", " · ")
    for t in titles:
        t = (t or "").strip()
        if len(t) < 4:
            continue
        for sep in seps:
            if sep in t:
                tail = t.rsplit(sep, 1)[-1].strip()
                if 1 < len(tail) <= 30:
                    tails[tail] = tails.get(tail, 0) + 1
                break

    if not tails:
        return {}
    tail, n = max(tails.items(), key=lambda kv: kv[1])
    if n < min_count or n < len(titles) * min_ratio:
        return {}

    out: dict[str, str] = {}
    for t in titles:
        t = (t or "").strip()
        for sep in seps:
            suffix = sep + tail
            if t.endswith(suffix) and len(t) > len(suffix) + 1:
                out[t] = t[: -len(suffix)].strip()
                break
    return out


def extract_meta(tree, base_url: str = "") -> dict:
    """从 <head> 与 JSON-LD 提取标题/作者/日期/摘要。"""
    meta: dict[str, str] = {}

    def gm(*names) -> str:
        for name in names:
            for prop in ("name", "property", "itemprop"):
                n = tree.xpath(f"//meta[@{prop}={_xq(name)}]/@content")
                if n and n[0].strip():
                    return n[0].strip()
        return ""

    meta["title"] = gm("og:title", "twitter:title", "dc.title") or \
        (strip_tags(tree.findtext(".//title") or ""))
    meta["author"] = gm("author", "article:author", "og:article:author",
                        "dc.creator", "twitter:creator")
    meta["published_at"] = gm("article:published_time", "datePublished",
                              "date", "dc.date", "og:published_time",
                              "article:modified_time")
    meta["summary"] = gm("description", "og:description", "twitter:description")
    meta["site_name"] = gm("og:site_name")
    # 很多站点的 <title> 是「文章标题 - 站名」。不剥掉的话，整本书的每一章
    # 标题后面都拖着一个站名（做通用抓取时第一次就撞上了这个）。
    meta["title"] = strip_site_suffix(meta["title"], meta["site_name"],
                                      tree.findtext(".//title") or "")
    lang = tree.xpath("//html/@lang")
    if lang:
        meta["language"] = lang[0].strip()

    # JSON-LD 覆盖（更可靠）
    for script in tree.xpath("//script[@type='application/ld+json']"):
        try:
            import json
            data = json.loads(script.text or "{}")
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type", "")
            if isinstance(t, list):
                t = " ".join(t)
            if "Article" not in t and "BlogPosting" not in t and "NewsArticle" not in t:
                continue
            meta["title"] = obj.get("headline") or meta.get("title", "")
            au = obj.get("author")
            if isinstance(au, dict):
                meta["author"] = au.get("name", meta.get("author", ""))
            elif isinstance(au, list) and au and isinstance(au[0], dict):
                meta["author"] = au[0].get("name", meta.get("author", ""))
            elif isinstance(au, str):
                meta["author"] = au
            meta["published_at"] = obj.get("datePublished") or meta.get("published_at", "")
            meta["summary"] = obj.get("description") or meta.get("summary", "")

    if base_url and not meta.get("site_name"):
        meta["site_name"] = urllib.parse.urlparse(base_url).netloc

    # 清理
    for k, v in list(meta.items()):
        meta[k] = clean_text(v)
    return {k: v for k, v in meta.items() if v}


def _xq(s: str) -> str:
    return "'" + s.replace("'", "") + "'"


def normalize_date(s: str) -> str:
    """把各种日期写法归一成 YYYY-MM-DD（失败则原样返回）。"""
    if not s:
        return ""
    s = s.strip()
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})[-/.](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(s[:24].strip(), fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return s


def guess_language(text: str) -> str:
    """粗粒度语种判断：zh / en / other。"""
    sample = text[:4000]
    if not sample:
        return "en"
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    total = cjk + latin
    if total == 0:
        return "en"
    if cjk / total > 0.25:
        return "zh"
    return "en"


# ---------------------------------------------------------------- HTML → Markdown

class HtmlToMarkdown:
    """把 DOM 子树转成 Markdown。

    image_resolver(url, alt, el) -> str | None
        返回本地相对路径则替换，返回 None 表示丢弃该图。
    """

    def __init__(self, *, image_resolver=None, base_url: str = "",
                 keep_images: bool = True, drop_links: bool = False):
        self.image_resolver = image_resolver
        self.base_url = base_url
        self.keep_images = keep_images
        self.drop_links = drop_links
        self.images: list[str] = []
        self._blocks: list[str] = []      # 代码块占位池
        self._list_depth = 0

    # ------------------------------------------------ 入口

    def convert(self, node) -> str:
        body = self._children(node)
        md = self._normalize(body)
        for i, block in enumerate(self._blocks):
            md = md.replace(self._placeholder(i), block)
        return md.strip() + "\n"

    def _placeholder(self, i: int) -> str:
        return f"\x00BFBLOCK{i}\x00"

    # ------------------------------------------------ 遍历

    def _children(self, node) -> str:
        out = []
        if node.text:
            out.append(self._escape(node.text))
        for child in node:
            if not isinstance(child.tag, str):
                if child.tail:
                    out.append(self._escape(child.tail))
                continue
            out.append(self._element(child))
            if child.tail:
                out.append(self._escape(child.tail))
        return "".join(out)

    def _element(self, el) -> str:
        tag = el.tag.lower()
        if tag in _DROP_TAGS:
            return ""

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            # 正文内部的 h1 降级为 h2，避免与书名冲突（由调用方决定偏移）
            text = self._inline(el).strip()
            return self._block(f"{'#' * level} {text}") if text else ""

        if tag == "p":
            # 老站点常在一个 <p> 里继续用 <br><br> 分段（PG 的 rootsoflisp 就是）。
            # 直接 _inline() 会把 br 压成空格，整段被合成一坨，所以先按 br 切。
            parts = self._split_br(el)
            if len(parts) > 1:
                out = [self._block(x) for x in parts if x.strip()]
                return "".join(out)
            text = self._inline(el).strip()
            return self._block(text) if text else ""

        if tag == "br":
            return "  \n"

        if tag == "hr":
            return self._block("---")

        if tag in ("strong", "b"):
            t = self._inline(el).strip()
            return f"**{t}**" if t else ""

        if tag in ("em", "i"):
            t = self._inline(el).strip()
            return f"*{t}*" if t else ""

        if tag in ("del", "s", "strike"):
            t = self._inline(el).strip()
            return f"~~{t}~~" if t else ""

        if tag in ("code", "kbd", "samp", "tt"):
            t = self._text_of(el)
            if "\n" in t:
                return self._code_block(t, "")
            return self._inline_code(t)

        if tag == "pre":
            return self._pre(el)

        if tag == "blockquote":
            inner = self._children(el).strip()
            if not inner:
                return ""
            quoted = "\n".join("> " + ln if ln.strip() else ">"
                               for ln in inner.splitlines())
            return self._block(quoted)

        if tag in ("ul", "ol"):
            return self._list(el, ordered=(tag == "ol"))

        if tag == "li":
            return self._inline(el)

        if tag == "a":
            return self._anchor(el)

        if tag == "img":
            return self._image(el)

        if tag == "figure":
            return self._figure(el)

        if tag == "figcaption":
            t = self._inline(el).strip()
            return self._block(f"*{t}*") if t else ""

        if tag == "table":
            return self._table(el)

        if tag in ("dl",):
            parts = []
            for child in el:
                t = self._inline(child).strip()
                if not t:
                    continue
                if child.tag.lower() == "dt":
                    parts.append(f"**{t}**")
                else:
                    parts.append(f": {t}")
            return self._block("\n".join(parts))

        if tag in ("details",):
            summary = el.find("summary")
            body = self._children(el).strip()
            head = f"**{self._inline(summary).strip()}**" if summary is not None else ""
            return self._block("\n".join(x for x in (head, body) if x))

        if tag in ("video", "audio"):
            src = el.get("src") or ""
            if not src:
                s = el.find("source")
                src = s.get("src", "") if s is not None else ""
            return self._block(f"[{tag}: {self._abs(src)}]") if src else ""

        if tag in _BLOCK_TAGS:
            inner = self._children(el).strip()
            return self._block(inner) if inner else ""

        # 默认：内联处理
        return self._children(el)

    def _inline(self, el) -> str:
        """渲染为内联文本（块级子元素会被压平）。"""
        out = []
        if el.text:
            out.append(self._escape(el.text))
        for child in el:
            if not isinstance(child.tag, str):
                if child.tail:
                    out.append(self._escape(child.tail))
                continue
            tag = child.tag.lower()
            if tag in ("br",):
                out.append(" ")
            elif tag in ("ul", "ol"):
                out.append(" " + self._list(child, ordered=(tag == "ol"),
                                            inline=True).strip() + " ")
            elif tag in _BLOCK_TAGS:
                out.append(" " + self._element(child).strip() + " ")
            else:
                out.append(self._element(child))
            if child.tail:
                out.append(self._escape(child.tail))
        return re.sub(r"[ \t]{2,}", " ", "".join(out))

    def _text_of(self, el) -> str:
        return etree.tostring(el, encoding="unicode", method="text")

    def _split_br(self, el) -> list[str]:
        """把元素按「连续 <br>」切成若干段，逐段渲染为内联文本。

        用于处理「外层已经是 <p>，内部却还在用 <br><br> 分段」的老式排版。
        切不出多段时返回长度 1 的列表（调用方退回原逻辑）。
        """
        try:
            frag = etree.tostring(el, encoding="unicode", method="html")
        except Exception:
            return [""]
        frag = re.sub(r"^\s*<p\b[^>]*>", "", frag, flags=re.I)
        frag = re.sub(r"</p>\s*$", "", frag, flags=re.I)
        chunks = re.split(r"(?:\s*<br\s*/?>\s*){2,}", frag, flags=re.I)
        if len(chunks) <= 1:
            return [""]
        out: list[str] = []
        for c in chunks:
            if not c.strip():
                continue
            try:
                node = lxml_html.fragment_fromstring(
                    f"<span>{c}</span>", create_parent="div")
            except Exception:
                out.append(re.sub(r"<[^>]+>", " ", c))
                continue
            out.append(self._inline(node))
        return out

    # ------------------------------------------------ 具体元素

    def _anchor(self, el) -> str:
        text = self._inline(el).strip()
        href = self._abs(el.get("href", ""))
        if not text:
            return ""
        if self.drop_links or not href or href.startswith("javascript:"):
            return text
        # 锚点文本与 URL 相同则裸链
        if text == href or text.rstrip("/") == href.rstrip("/"):
            return f"<{href}>"
        title = el.get("title", "")
        suffix = f' "{title}"' if title else ""
        return f"[{text}]({href}{suffix})"

    def _image(self, el) -> str:
        if not self.keep_images:
            return ""
        src = (el.get("src") or el.get("data-src") or el.get("data-original")
               or el.get("data-lazy-src") or "")
        # srcset 兜底
        if not src and el.get("srcset"):
            src = el.get("srcset", "").split(",")[0].strip().split(" ")[0]
        if not src:
            return ""
        src = self._abs(src)
        if src.startswith("data:"):
            return ""
        # 本地绝对路径（WordPress 编辑器会把作者粘进来的剪贴板图存成本机路径，
        # 如 file:///C:\Users\xxx\AppData\Local\Temp\...）。这种引用在书里
        # 永远是坏图，顺带还会把作者的用户名泄出去，直接丢掉。
        if re.match(r"^(file:|[A-Za-z]:[\\/]|\\\\)", src):
            return ""
        alt = clean_text(el.get("alt", "") or el.get("title", "") or "")
        local = None
        if self.image_resolver:
            try:
                local = self.image_resolver(src, alt, el)
            except Exception:
                local = None
        if local is False or local is None:
            # resolver 明确拒绝 → 保留外链（便于人工判断）
            return self._block(f"![{alt}]({src})") if src else ""
        self.images.append(local)
        return self._block(f"![{alt}]({local})")

    def _figure(self, el) -> str:
        img = el.find(".//img")
        cap = el.find(".//figcaption")
        parts = []
        if img is not None:
            parts.append(self._image(img).strip())
        elif self.keep_images:
            parts.append("")
        if cap is not None:
            t = self._inline(cap).strip()
            if t:
                parts.append(f"*{t}*")
        return self._block("\n\n".join(p for p in parts if p))

    def _pre(self, el) -> str:
        code_el = el.find(".//code")
        target = code_el if code_el is not None else el
        lang = self._detect_lang(el, target)
        text = self._text_of(target)
        return self._code_block(text, lang)

    def _detect_lang(self, pre, code) -> str:
        for el in (code, pre):
            for attr in ("data-language", "data-lang", "lang"):
                v = el.get(attr)
                if v:
                    return _LANG_ALIASES.get(v.strip().lower(), v.strip().lower())
            for c in (el.get("class", "") or "").split():
                for prefix in ("language-", "lang-", "highlight-", "brush:"):
                    if c.lower().startswith(prefix):
                        v = c[len(prefix):].lower()
                        return _LANG_ALIASES.get(v, v)
                if c.lower() in _LANG_ALIASES:
                    return _LANG_ALIASES[c.lower()]
        return ""

    def _code_block(self, text: str, lang: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.strip("\n")
        # 计算最长反引号串，选择更长的围栏
        longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
        fence = "`" * max(3, longest + 1)
        block = f"{fence}{lang}\n{text}\n{fence}"
        self._blocks.append(block)
        return self._block(self._placeholder(len(self._blocks) - 1))

    def _inline_code(self, text: str) -> str:
        text = text.replace("\n", " ").strip()
        if not text:
            return ""
        longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
        ticks = "`" * max(1, longest + 1)
        pad = " " if text.startswith("`") or text.endswith("`") else ""
        return f"{ticks}{pad}{text}{pad}{ticks}"

    def _list(self, el, ordered: bool, inline: bool = False) -> str:
        lines: list[str] = []
        self._list_depth += 1
        indent = "  " * (self._list_depth - 1)
        idx = 1
        start = el.get("start")
        if ordered and start and start.isdigit():
            idx = int(start)
        for li in el:
            if not isinstance(li.tag, str) or li.tag.lower() != "li":
                continue
            # 分离 li 的直接文本与其嵌套列表
            marker = f"{idx}. " if ordered else "- "
            if li.get("value") and li.get("value").isdigit():
                idx = int(li.get("value"))
            nested = []
            own = []
            if li.text:
                own.append(self._escape(li.text))
            for child in li:
                if not isinstance(child.tag, str):
                    if child.tail:
                        own.append(self._escape(child.tail))
                    continue
                ctag = child.tag.lower()
                if ctag in ("ul", "ol"):
                    nested.append(self._list(child, ordered=(ctag == "ol")))
                elif ctag in _BLOCK_TAGS and ctag not in ("p",):
                    own.append(" " + self._element(child).strip())
                else:
                    own.append(self._element(child))
                if child.tail:
                    own.append(self._escape(child.tail))
            body = re.sub(r"\s+", " ", "".join(own)).strip()
            # 任务列表
            cb = li.find(".//input[@type='checkbox']")
            if cb is not None:
                checked = cb.get("checked") is not None
                body = f"[{'x' if checked else ' '}] {body}".strip()
            lines.append(f"{indent}{marker}{body}".rstrip())
            for n in nested:
                lines.append(n.rstrip())
            idx += 1
        self._list_depth -= 1
        out = "\n".join(x for x in lines if x.strip())
        if inline:
            return re.sub(r"\n\s*", "; ", out)
        return self._block(out)

    def _table(self, el) -> str:
        # 先处理嵌套：el.iter("tr") 会把子表的行也一起捞出来，导致同一段内容
        # 重复渲染、且竖线被转义成一堆 \|。Markdown 本来也表达不了嵌套表格，
        # 所以只要发现表里还套着表（PG 的「题词框」就是这么做的），整块展平。
        if el.findall(".//table"):
            return self._block(self._children(el))

        rows: list[list[str]] = []
        header: list[str] = []
        for tr in el.iter("tr"):
            cells = [c for c in tr if isinstance(c.tag, str)
                     and c.tag.lower() in ("td", "th")]
            if not cells:
                continue
            vals = [re.sub(r"\s+", " ", self._inline(c)).strip()
                    .replace("|", "\\|") for c in cells]
            if not header and any(c.tag.lower() == "th" for c in cells):
                header = vals
            else:
                rows.append(vals)
        if not header:
            if not rows:
                # 一个 <tr> 都没有 → 这不是数据表，而是被拿来当容器的空壳 <table>。
                # 直接展平内容，否则里面的正文会被整段丢掉。
                return self._block(self._children(el))
            # 没有 <th>：按惯例把第一行当表头，否则会渲染出一个空表头行。
            header = rows[0]
            rows = rows[1:]
        width = max([len(header)] + [len(r) for r in rows])
        header += [""] * (width - len(header))
        # 单行单列（多半就是个引用/题词框）没必要硬做成表格，直接当段落更好读
        if width == 1 and not rows and header[0].strip():
            return self._block(header[0])
        out = ["| " + " | ".join(header) + " |",
               "| " + " | ".join("---" for _ in range(width)) + " |"]
        for r in rows:
            r = r + [""] * (width - len(r))
            out.append("| " + " | ".join(r[:width]) + " |")
        return self._block("\n".join(out))

    # ------------------------------------------------ 辅助

    def _abs(self, url: str) -> str:
        if not url:
            return ""
        url = url.strip()
        if url.startswith(("http://", "https://", "mailto:", "data:", "//")):
            if url.startswith("//"):
                return "https:" + url
            return url
        if not self.base_url:
            return url
        return urllib.parse.urljoin(self.base_url, url)

    @staticmethod
    def _escape(text: str) -> str:
        # 只做最小转义，避免中文文档被搞得满屏反斜杠
        return text

    @staticmethod
    def _block(text: str) -> str:
        return "\n\n" + text + "\n\n"

    @staticmethod
    def _normalize(md: str) -> str:
        md = md.replace("\r\n", "\n").replace("\r", "\n")
        md = re.sub(r"[ \t]+\n", "\n", md)
        md = re.sub(r"\n{3,}", "\n\n", md)
        # 块级标记前不留空格
        md = re.sub(r"\n +(#{1,6} |[-*+] |\d+\. |> |```)", r"\n\1", md)
        return md.strip() + "\n"


# ---------------------------------------------------------------- 抽取主函数

def extract_article(html: str | bytes, url: str = "",
                    *, image_resolver=None, keep_images: bool = True,
                    heading_offset: int | None = None,
                    adapter=None, preprocess_hook=None) -> ExtractResult:
    """从一篇网页 HTML 中抽出结构化文章。

    adapter          站点适配器（None 则自动按 URL 匹配）
    preprocess_hook  额外的 DOM 修补函数，签名为 hook(tree)
    heading_offset   为 None 时使用适配器建议值
    """
    from . import sites as _sites

    if adapter is None:
        adapter = _sites.find_adapter(url)

    tree = parse_html(html)
    meta = extract_meta(tree, url)

    # --- DOM 修补（顺序重要：先通用，再站点专用）
    notes: list[str] = []
    before_len = _text_len(tree)
    _sites.generic_preprocess(tree)
    if adapter is not None:
        try:
            adapter.preprocess(tree)
        except Exception as e:
            notes.append(f"站点适配器预处理失败：{type(e).__name__}: {e}")
    if preprocess_hook is not None:
        try:
            preprocess_hook(tree)
        except Exception as e:
            notes.append(f"自定义预处理失败：{type(e).__name__}: {e}")

    # 适配器元数据必须在 prune 之前读：标题/作者/日期往往就在 <header> 里，
    # 而 prune 会把 header 当导航噪声整块删掉（1230.la 的 h1.article-title
    # 就是这样，之前只能退回去抓 <title>，标题尾巴上挂着「-无极领域」）。
    adapter_meta: dict[str, str] = {"title": "", "author": "", "date": ""}
    if adapter is not None:
        try:
            adapter_meta["title"] = adapter.title_from(tree) or ""
        except Exception:
            pass
        try:
            adapter_meta["author"] = adapter.author_from(tree) or ""
        except Exception:
            pass
        try:
            adapter_meta["date"] = adapter.date_from(tree, "") or ""
        except Exception:
            pass

    prune(tree)
    # 整页清洗后大幅缩水 → 可能是误删。有站点适配器时不做这个判断：
    # 适配器点名了正文容器，剩下的导航/侧栏本来就该被删掉，否则每篇都会误报。
    if (adapter is None and _text_len(tree) < before_len * 0.25
            and before_len > 2000):
        notes.append("清洗后正文大幅缩短，请人工抽查是否误删")

    # --- 正文定位
    node, conf, strategy = find_content(tree, adapter=adapter)
    conv = HtmlToMarkdown(image_resolver=image_resolver, base_url=url,
                          keep_images=keep_images)
    md = conv.convert(node)

    # 没有适配器时 adapter 是 None —— 旧代码直接取 adapter.heading_offset 会
    # 抛 AttributeError，导致**所有没有专用适配器的站点**整站抓取失败。
    # （这个坑直到做通用版、第一次抓没有适配器的站点才暴露出来。）
    offset = heading_offset if heading_offset is not None else \
        getattr(adapter, "heading_offset", 0)
    if offset:
        md = shift_headings(md, offset)
    md = _sites.reflow_paragraphs(md)
    md = drop_repeated_title(md, meta.get("title", ""))
    if adapter is not None:
        try:
            md = adapter.postprocess(md, meta)
            md = _sites.reflow_paragraphs(md)
        except Exception as e:
            notes.append(f"站点适配器后处理失败：{type(e).__name__}: {e}")
    md = _sites.tidy_markdown(md)

    # --- 元数据（适配器优先覆盖）
    title = adapter_meta["title"] or meta.get("title", "")
    author = adapter_meta["author"] or meta.get("author", "")
    published = adapter_meta["date"] or meta.get("published_at", "")
    if adapter is not None:
        try:
            published = adapter.date_from(tree, md) or published
        except Exception:
            pass

    res = ExtractResult(
        title=clean_text(title),
        author=clean_text(author),
        published_at=normalize_date(published),
        summary=clean_text(meta.get("summary", "")),
        markdown=md,
        images=conv.images,
        language=guess_language(md),
        confidence=conf,
        strategy=f"adapter:{adapter.name}" if adapter else strategy,
        notes=notes,
    )
    if conf < 0.5 and adapter is None:
        res.notes.append(f"正文定位置信度偏低（{conf}，策略 {strategy}），建议人工抽查")
    if res.word_count < 80:
        res.notes.append("正文过短，可能未正确识别正文区域")
    return res


def drop_repeated_title(md: str, title: str) -> str:
    """如果正文第一行就是标题本身，去掉（标题已进 frontmatter）。"""
    if not title:
        return md
    lines = md.splitlines()
    for i, ln in enumerate(lines[:6]):
        s = ln.strip()
        if not s:
            continue
        if re.fullmatch(r"#{1,6}\s*(.+)", s):
            head = re.fullmatch(r"#{1,6}\s*(.+)", s).group(1).strip()
            if _similar(head, title):
                return "\n".join(lines[:i] + lines[i + 1:]).lstrip("\n")
        break
    return md


def _similar(a: str, b: str) -> bool:
    a = re.sub(r"[\W_]+", "", a).lower()
    b = re.sub(r"[\W_]+", "", b).lower()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def shift_headings(md: str, offset: int) -> str:
    """整体下移标题层级（保护代码块）。"""
    if offset <= 0:
        return md
    out, in_fence = [], False
    for ln in md.split("\n"):
        if re.match(r"\s*(```|~~~)", ln):
            in_fence = not in_fence
            out.append(ln)
            continue
        if not in_fence:
            m = re.match(r"^(#{1,6})(\s+.*)$", ln)
            if m:
                lvl = min(6, len(m.group(1)) + offset)
                out.append("#" * lvl + m.group(2))
                continue
        out.append(ln)
    return "\n".join(out)
