"""排版引擎：Markdown → EPUB XHTML（EPUB3）。

python-markdown 负责 Markdown → HTML（含表格/围栏代码/脚注），
lxml 负责 HTML → 严格 XHTML（EPUB3 要求 XHTML5 序列化），
模板负责章题页、扉页、版权页，CSS 主题负责最终观感。
"""

from __future__ import annotations

import html as html_mod
import os
import re
from dataclasses import dataclass
from pathlib import Path

from lxml import etree, html as lxml_html
import markdown as md_lib

from .archive import Archive, Article, split_frontmatter
from .utils import log, now_iso, count_words

XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"

# 主题自带的补充样式：扉页 / 版权页 / 目录（与主题无关，永远附加）
_FORGE_EXTRA_CSS = """
/* ---- book-forge 前置页样式 ---- */
.titlepage { text-align: center; margin-top: 22%; }
.titlepage .book-title {
  font-size: 2.2em; font-weight: 700; line-height: 1.32;
  margin: 0 0 0.4em; letter-spacing: 0.04em; text-align: center;
}
.titlepage .book-subtitle {
  font-size: 1.12em; color: #666; margin: 0 0 1.6em;
  letter-spacing: 0.06em; text-align: center;
}
.titlepage .rule {
  width: 3.4em; height: 2px; margin: 1.4em auto; opacity: 0.5;
  background: currentColor; border: none;
}
.titlepage .book-author {
  font-size: 1.05em; letter-spacing: 0.14em; margin: 1.2em 0 0;
  text-align: center;
}
.titlepage .book-publisher {
  font-size: 0.82em; color: #888; letter-spacing: 0.2em;
  margin-top: 3.2em; text-align: center;
}
.copyright-page {
  font-size: 0.8em; color: #555; line-height: 1.8;
  margin-top: 38%;
}
.copyright-page p { text-indent: 0; margin: 0.4em 0; text-align: left; }
.copyright-page hr { width: 30%; opacity: 0.3; margin: 1.2em auto; }
nav#toc ol { list-style: none; padding-left: 0.4em; margin: 0.6em 0; }
nav#toc li { margin: 0.42em 0; }
nav#toc a { text-decoration: none; color: inherit; }
nav#toc .toc-num { color: #999; font-size: 0.85em; margin-right: 0.6em; }
.chapter-heading { text-align: center; margin: 2.8em 0 1.6em; }
.chapter-heading .chap-no {
  display: block; font-size: 0.85em; letter-spacing: 0.4em;
  color: #8a8378; margin-bottom: 0.7em;
}
/* ---- 分组文集：部 / 章 分隔页 ---- */
.part-page { text-align: center; margin-top: 26%; page-break-before: always; }
.part-page .part-no {
  font-size: 0.92em; letter-spacing: 0.55em; color: #8a8378;
  margin: 0 0 1.4em; text-indent: 0;
}
.part-page .part-title {
  font-size: 2.05em; font-weight: 700; letter-spacing: 0.14em;
  margin: 0; line-height: 1.4;
}
.part-page .part-rule {
  width: 3em; height: 2px; margin: 1.7em auto; border: none;
  background: currentColor; opacity: 0.45;
}
.part-page .part-meta {
  font-size: 0.85em; color: #888; letter-spacing: 0.1em;
  margin-top: 1em; text-indent: 0;
}
.section-page { text-align: center; margin-top: 30%; page-break-before: always; }
.section-page .section-title {
  font-size: 1.6em; font-weight: 700; letter-spacing: 0.12em;
  margin: 0; line-height: 1.4;
}
.section-page .section-meta {
  font-size: 0.85em; color: #888; letter-spacing: 0.1em;
  margin-top: 1.1em; text-indent: 0;
}
"""


@dataclass
class TypesetResult:
    article_id: str
    filename: str            # Text/ 内的文件名
    title: str
    xhtml: str
    images: list[str]
    words: int


class Typesetter:
    """把归档包排版成 EPUB 内部文件。"""

    def __init__(self, archive: Archive, *, theme: str = "classic",
                 lang: str = "zh-CN", drop_frontmatter_h1: bool = True,
                 cjk_space: str = "off", theme_dir: str | Path | None = None):
        self.ar = archive
        self.theme = theme
        self.lang = lang
        self.cjk_space = cjk_space      # off | thin  （中西文之间插薄空格）
        self.theme_dir = theme_dir

    # ------------------------------------------------ CSS

    def css(self) -> str:
        """读取排版主题 CSS。

        解析顺序（这样 pip 安装、源码运行、被别的 agent 调用都能找到）：
        1. 显式传入的 ``theme_dir``；
        2. 环境变量 ``BOOKFORGE_THEMES`` 指向的目录（用户自定义主题）；
        3. 包内自带的 ``bookforge/themes/<theme>.css``。
        """
        candidates: list[Path] = []
        if self.theme_dir:
            candidates.append(Path(self.theme_dir) / f"{self.theme}.css")
        env_dir = os.environ.get("BOOKFORGE_THEMES")
        if env_dir:
            candidates.append(Path(env_dir) / f"{self.theme}.css")
        candidates.append(Path(__file__).resolve().parent / "themes" / f"{self.theme}.css")

        for cand in candidates:
            try:
                if cand.is_file():
                    return cand.read_text(encoding="utf-8") + "\n" + _FORGE_EXTRA_CSS
            except OSError:
                continue

        log(f"主题文件没找到（{self.theme}），使用内置基础样式", "warn")
        return _FALLBACK_CSS + _FORGE_EXTRA_CSS

    @staticmethod
    def available_themes() -> list[str]:
        """列出可用的排版主题名。"""
        d = Path(__file__).resolve().parent / "themes"
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.css"))

    # ------------------------------------------------ 单篇

    def typeset_article(self, art: Article, *, chapter_no: str = "",
                        include_heading: bool = True) -> TypesetResult:
        raw = (self.ar.root / art.file).read_text(encoding="utf-8")
        fm, body = split_frontmatter(raw)
        title = str(fm.get("title") or art.title)

        # 图片路径：assets/x.jpg → ../Images/x.jpg
        # 注意替换串是 "](../Images/"，多一个 ')' 就会把每个图片链接写坏成
        # "])(../Images/..."，markdown 不再识别为图片——整本书的插图会全部消失。
        body = re.sub(r"\]\((assets/)", "](../Images/", body)

        html_body = md_lib.markdown(
            body,
            extensions=["extra", "sane_lists", "admonition"],
            output_format="html5",
        )
        html_body = self._xhtmlify(html_body)
        if self.cjk_space == "thin":
            html_body = _insert_cjk_thin_space(html_body)

        chap_head = ""
        if chapter_no:
            chap_head = f'<p class="chap-no">{html_mod.escape(chapter_no)}</p>'

        h1 = f'<h1>{html_mod.escape(title)}</h1>' if include_heading else ""
        content = f'<section class="chapter">{h1}{chap_head}{html_body}</section>'

        xhtml = self._wrap(title, content)
        images = re.findall(r'src="([^"]+)"', html_body)
        return TypesetResult(
            article_id=art.id, filename=f"chap{art.index:03d}.xhtml",
            title=title, xhtml=xhtml, images=images,
            words=art.word_count,
        )

    # ------------------------------------------------ 前置页

    def title_page(self) -> str:
        b = self.ar.book
        parts = ['<section class="titlepage" epub:type="titlepage">']
        parts.append(f'<h1 class="book-title">{_e(b.get("title", ""))}</h1>')
        if b.get("subtitle"):
            parts.append(f'<p class="book-subtitle">{_e(b["subtitle"])}</p>')
        parts.append('<hr class="rule"/>')
        if b.get("author"):
            parts.append(f'<p class="book-author">{_e(b["author"])}</p>')
        if b.get("publisher"):
            parts.append(f'<p class="book-publisher">{_e(b["publisher"])}</p>')
        parts.append("</section>")
        return self._wrap(str(b.get("title", "Title")), "".join(parts))

    # ------------------------------------------------ 分组文集：部 / 章 分隔页

    def part_page(self, no: int, title: str, *, n_sections: int = 0,
                  n_articles: int = 0, words: int = 0) -> str:
        """「部」分隔页（主题，如「项目」）。"""
        meta: list[str] = []
        if n_sections > 1:
            meta.append(f"{n_sections} 个分类")
        if n_articles:
            meta.append(f"{n_articles} 篇")
        if words:
            meta.append(f"约 {words:,} 字")
        inner = [f'<p class="part-no">第{_cn_num(no)}部</p>',
                 f'<h1 class="part-title">{_e(title)}</h1>',
                 '<hr class="part-rule"/>']
        if meta:
            inner.append(f'<p class="part-meta">{" · ".join(meta)}</p>')
        return self._wrap(
            title,
            '<section class="part-page" epub:type="part">' + "".join(inner)
            + "</section>")

    def section_page(self, title: str, *, n_articles: int = 0,
                     words: int = 0) -> str:
        """「章」分隔页（细分类型，如「网络推广」）。"""
        meta: list[str] = []
        if n_articles:
            meta.append(f"{n_articles} 篇")
        if words:
            meta.append(f"约 {words:,} 字")
        inner = [f'<h1 class="section-title">{_e(title)}</h1>']
        if meta:
            inner.append(f'<p class="section-meta">{" · ".join(meta)}</p>')
        return self._wrap(
            title, '<section class="section-page" epub:type="division">'
            + "".join(inner) + "</section>")

    def copyright_page(self) -> str:
        b = self.ar.book
        lines = [f"<p>{_e(b.get('title', ''))}</p>"]
        if b.get("author"):
            lines.append(f"<p>{_e(b['author'])} 著</p>")
        lines.append("<hr/>")
        if b.get("description"):
            lines.append(f"<p>{_e(b['description'])}</p>")
        n = len(self.ar.articles)
        words = self.ar.manifest.get("stats", {}).get("words", 0)
        lines.append(
            f"<p>本书由 book-forge 流水线整理排版，收录文章 {n} 篇，"
            f"约 {words:,} 字。</p>")
        srcs = sorted({a.source_url.split("/")[2] for a in self.ar.articles
                       if a.source_url})[:8]
        if srcs:
            lines.append("<p>内容来源：" + _e("、".join(srcs)) + "</p>")
        if b.get("rights"):
            lines.append(f"<p>{_e(b['rights'])}</p>")
        lines.append(f"<p>排版版本 {now_iso()[:10]} · "
                     f"主题 {self.theme}</p>")
        lines.append("<p>仅供个人学习与研究使用，请勿商用或再分发。</p>")
        return self._wrap("版权信息",
                          '<section class="copyright-page" epub:type="copyright-page">'
                          + "".join(lines) + "</section>")

    # ------------------------------------------------ 内部

    def _wrap(self, title: str, body: str) -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            f'<html xmlns="{XHTML_NS}" xmlns:epub="{EPUB_NS}" '
            f'xml:lang="{self.lang}" lang="{self.lang}">\n'
            "<head>\n"
            f"  <title>{_e(title)}</title>\n"
            '  <link rel="stylesheet" type="text/css" href="../Style/main.css"/>\n'
            "</head>\n"
            f"<body>\n{body}\n</body>\n</html>\n"
        )

    def _xhtmlify(self, html: str) -> str:
        """HTML5 → 严格 XHTML（EPUB 校验器会挑刺的地方都在这）。"""
        try:
            doc = lxml_html.fragment_fromstring(html, create_parent="div")
        except Exception:
            return html
        # void 元素自闭合 + 属性规范化由 lxml serializer 处理
        out = etree.tostring(doc, encoding="unicode", method="xml",
                             with_tail=False)
        # lxml 会给 div 加 xmlns，去掉（外层模板已有）
        out = re.sub(r'^<div[^>]*>', "", out)
        out = re.sub(r"</div>\s*$", "", out)
        return out


def _insert_cjk_thin_space(html: str) -> str:
    """在 CJK 与拉丁字符之间插入薄空格（U+2009）。

    注意：这会改动正文文本。只在明确需要时开启（--cjk-space thin）。
    只处理文本节点，不碰标签与属性。
    """
    parts = re.split(r"(<[^>]+>)", html)
    out = []
    cjk = r"\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af"
    pat_l = re.compile(f"([{cjk}”』」》）】])([A-Za-z0-9₩$€£¥@&%（(])")
    pat_r = re.compile(f"([A-Za-z0-9$€£¥@&%.\\)）”』」】])([{cjk}“『「（(])")
    for p in parts:
        if p.startswith("<"):
            out.append(p)
            continue
        p = pat_l.sub(r"\1\u2009\2", p)
        p = pat_r.sub(r"\1\u2009\2", p)
        out.append(p)
    return "".join(out)


def _e(s: str) -> str:
    return html_mod.escape(str(s or ""), quote=False)


def _cn_num(n: int) -> str:
    """阿拉伯数字 → 中文数字（1-99，用于「第一部」这类编号）。"""
    d = "零一二三四五六七八九"
    if n <= 0:
        return str(n)
    if n < 10:
        return d[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + d[n - 10]
    if n < 100:
        t, o = divmod(n, 10)
        return d[t] + "十" + (d[o] if o else "")
    return str(n)


_FALLBACK_CSS = """
body { margin:0; padding:0 4%; line-height:1.8; font-family: serif; }
h1,h2,h3 { line-height:1.35; }
p { margin: 0 0 0.9em; }
pre { white-space: pre-wrap; word-wrap: break-word; }
img { max-width: 100%; }
"""
