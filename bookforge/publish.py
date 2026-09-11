"""打包：归档包 → EPUB3。

这是流水线的最后一站，把「正文 + 图片 + 封面 + 目录结构」装进一个
符合 EPUB3 规范的 zip。对外只暴露一个 ``publish()``，CLI 和外部
agent 都只调它。

目录结构由归档包里的 ``metadata/groups.json`` 决定：
  * 有分组清单 → 嵌套目录「部（主题）→ 章（细分类型）→ 节（文章）」，
    并且把部 / 章渲染成**可点击的分隔页**（不是点不进去的空标题）
  * 没有 → 平铺目录，一篇一章

书脊顺序固定为 ``封面 → 扉页 → 版权页 → 目录 → 正文``。
目录页必须靠前：700 多篇的书如果目录在书末，等于没有目录。
"""

from __future__ import annotations

import json
import re
import time
import uuid
import zipfile
from pathlib import Path

from ebooklib import epub

from .archive import Archive
from .typeset import Typesetter
from .utils import count_words, ensure_dir, human_size, log, now_iso, relpath

THEMES = ["classic", "modern", "magazine", "academic"]


# ---------------------------------------------------------------- 分组归一化


def _normalise_groups(groups: dict) -> list[dict]:
    """把分组清单统一成 [{title, sections:[{title, urls}]}]。

    兼容两种写法：
      * ``{"title": "项目", "subgroups": [...]}``   ← WordPress 用这种
      * ``{"title": "2024", "urls": [...]}``        ← 按年份/路径分组用这种
    """
    out: list[dict] = []
    for g in groups.get("groups", []):
        subs = g.get("subgroups")
        if subs:
            sections = [{"title": s.get("title") or "", "urls": s.get("urls") or []}
                        for s in subs]
        else:
            sections = [{"title": "", "urls": g.get("urls") or []}]
        sections = [s for s in sections if s["urls"]]
        if sections:
            out.append({"title": g.get("title") or "", "sections": sections})
    return out


def load_archive_groups(root: Path) -> dict:
    p = root / "metadata" / "groups.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"metadata/groups.json 解析失败：{e}", "warn")
        return {}
    if not isinstance(data, dict) or not data.get("groups"):
        return {}
    return data


# ---------------------------------------------------------------- 目录构建


def build_grouped_toc(groups: dict, url2ch: dict, url2words: dict,
                      ts, language: str, bk) -> tuple[list, list, list]:
    """把分组清单变成 ebooklib 的嵌套目录。

    返回 ``(toc 树, 书脊中间段, 未归类章节)``。

    ebooklib 的目录支持 ``(父项, [子项...])`` 这种元组嵌套，父项可以是
    ``EpubHtml``（可点进正文），所以「部」「章」都是真正的分隔页。
    """
    toc: list = []
    spine: list[epub.EpubHtml] = []
    placed: set[int] = set()
    part_no = 0

    def words_of(chs: list) -> int:
        ids = {id(c) for c in chs}
        return sum(w for u, w in url2words.items() if id(url2ch.get(u)) in ids)

    for g in _normalise_groups(groups):
        sections: list[tuple[str, list[epub.EpubHtml]]] = []
        for s in g["sections"]:
            chs: list[epub.EpubHtml] = []
            for u in s["urls"]:
                ch = url2ch.get(u.split("#")[0])
                if ch is None or id(ch) in placed:
                    continue
                placed.add(id(ch))
                chs.append(ch)
            if chs:
                sections.append((s["title"], chs))
        if not sections:
            continue

        part_no += 1
        ptitle = g["title"] or f"第{part_no}部"
        total = sum(len(c) for _, c in sections)
        pp = epub.EpubHtml(title=ptitle,
                           file_name=f"Text/part{part_no:02d}.xhtml", lang=language)
        pp.content = ts.part_page(
            part_no, ptitle, n_sections=len(sections), n_articles=total,
            words=sum(words_of(c) for _, c in sections)).encode("utf-8")
        pp.add_link(href="Style/main.css", rel="stylesheet", type="text/css")
        bk.add_item(pp)
        spine.append(pp)

        if len(sections) == 1:
            # 只有一个细分类型（如「随笔」/「2024 年」）→ 不插章分隔页
            kids = sections[0][1]
            toc.append((pp, kids))
            spine += kids
            continue

        node: list = []
        for si, (stitle, kids) in enumerate(sections, 1):
            sp = epub.EpubHtml(
                title=stitle or f"第{si}章",
                file_name=f"Text/sec{part_no:02d}-{si:02d}.xhtml", lang=language)
            sp.content = ts.section_page(
                stitle, n_articles=len(kids), words=words_of(kids)).encode("utf-8")
            sp.add_link(href="Style/main.css", rel="stylesheet", type="text/css")
            bk.add_item(sp)
            node.append((sp, kids))
            spine.append(sp)
            spine += kids
        toc.append((pp, node))

    leftover: list[epub.EpubHtml] = []
    seen: set[int] = set()
    for ch in url2ch.values():
        if id(ch) not in placed and id(ch) not in seen:
            seen.add(id(ch))
            leftover.append(ch)
    leftover.sort(key=lambda c: c.file_name)
    return toc, spine, leftover


# ---------------------------------------------------------------- 校验


def validate_epub(path: Path) -> list[str]:
    """轻量结构校验（关键项；严格校验请另跑 epubcheck）。"""
    problems: list[str] = []
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "mimetype" not in names:
                problems.append("缺少 mimetype 文件")
            else:
                mt = z.read("mimetype").decode("utf-8", "replace").strip()
                if mt != "application/epub+zip":
                    problems.append(f"mimetype 错误：{mt}")
            if "META-INF/container.xml" not in names:
                problems.append("缺少 META-INF/container.xml")
            content_docs = [n for n in names if n.endswith((".xhtml", ".html"))]
            if not content_docs:
                problems.append("没有任何内容文档")
            all_names = {Path(x).name for x in names}
            missing = 0
            for n in content_docs:
                doc = z.read(n).decode("utf-8", "replace")
                for m in re.finditer(r'src="([^"]+)"', doc):
                    src = m.group(1)
                    if src.startswith(("http:", "https:", "data:")):
                        continue
                    target = (Path(n).parent / src).as_posix()
                    if target not in names and Path(target).name not in all_names:
                        missing += 1
                        if missing <= 5:
                            problems.append(f"{n} 引用了缺失图片：{src}")
            if missing > 5:
                problems.append(f"…另有 {missing - 5} 处图片引用缺失")
    except zipfile.BadZipFile:
        problems.append("不是合法的 zip 文件")
    except Exception as e:
        problems.append(f"校验异常：{type(e).__name__}: {e}")
    return list(dict.fromkeys(problems))[:20]


def count_external_images(path: Path) -> int:
    """统计正文里还剩多少张"没本地化成功"的远程图片。

    抓图失败的图片会被保留原链接，EPUB 本身仍然合法，但离线阅读会缺图，
    所以要在构建摘要里明确提示，不能让用户以为书是完整的。
    """
    n = 0
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if not name.endswith((".xhtml", ".html")):
                    continue
                doc = z.read(name).decode("utf-8", "replace")
                n += len(re.findall(r'src="https?://', doc))
    except Exception:
        return 0
    return n


# ---------------------------------------------------------------- 索引


def write_index(ar: Archive, out_path: Path, title: str, subtitle: str,
                author: str, theme: str, n_ch: int) -> None:
    lines = [f"# {title}", ""]
    if subtitle:
        lines += [f"> {subtitle}", ""]
    lines.append(f"- **作者**：{author or '—'}")
    lines.append(f"- **章节数**：{n_ch}")
    lines.append(f"- **总字数**：约 {ar.manifest.get('stats', {}).get('words', 0):,}")
    lines.append(f"- **排版主题**：{theme}")
    lines.append(f"- **EPUB**：`{out_path.name}`")
    lines.append(f"- **生成时间**：{now_iso()[:19]}")
    lines += ["", "## 目录", ""]

    spec = load_archive_groups(ar.root)
    if spec:
        by_url = {a.source_url.split("#")[0]: a for a in ar.articles if a.source_url}
        placed: set[int] = set()
        for g in _normalise_groups(spec):
            n = sum(len(s["urls"]) for s in g["sections"])
            lines.append(f"### {g['title'] or '（全部）'}（{n} 篇）")
            lines.append("")
            collapse = len(g["sections"]) == 1
            for s in g["sections"]:
                arts = []
                for u in s["urls"]:
                    hit = by_url.get(u.split("#")[0])
                    if hit is not None and hit.index not in placed:
                        placed.add(hit.index)
                        arts.append(hit)
                if not arts:
                    continue
                if not collapse and s["title"]:
                    lines += [f"#### {s['title']}（{len(arts)} 篇）", ""]
                for a in sorted(arts, key=lambda x: x.index):
                    lines.append(_index_line(a))
                if not collapse:
                    lines.append("")
            lines.append("")
        rest = [a for a in ar.articles if a.index not in placed]
        if rest:
            lines += [f"### 其他（{len(rest)} 篇）", ""]
            lines += [_index_line(a) for a in rest]
            lines.append("")
    else:
        lines += [_index_line(a) for a in ar.articles]

    lines.append("")
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    (ar.root / "index.md").write_text(text, encoding="utf-8")


def _index_line(a) -> str:
    line = f"{a.index:>3d}. [{a.title}]({a.file})"
    extra = []
    if a.published_at:
        extra.append(a.published_at)
    if a.word_count:
        extra.append(f"{a.word_count:,} 字")
    if extra:
        line += f" —— {' · '.join(extra)}"
    return line


def _mime_of(p: Path) -> str:
    ext = p.suffix.lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
            ".avif": "image/avif"}.get(ext, "application/octet-stream")


def _find_doc_h1(text: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S | re.I)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


# ---------------------------------------------------------------- 主入口


def publish(archive, out: str | Path = "", *, theme: str = "classic",
            title: str = "", subtitle: str = "", author: str = "",
            publisher: str = "", description: str = "", rights: str = "",
            language: str = "", cover: str = "", no_cover: bool = False,
            no_toc_page: bool = False, cjk_space: str = "off",
            book_uuid: str = "", verbose: bool = True) -> dict:
    """打包 EPUB3，返回报告 dict（同时写入 reports/publish-report.json）。

    ``archive`` 可以是归档包目录，也可以是一个已经打开的 ``Archive`` 对象。
    """
    ar = archive if isinstance(archive, Archive) else Archive.open(archive)
    if not ar.articles:
        raise ValueError("归档包为空（content/ 里没有文章）")

    book_meta = ar.book
    title = title or book_meta.get("title") or "Untitled"
    subtitle = subtitle or book_meta.get("subtitle", "")
    author = author or book_meta.get("author", "")
    language = language or book_meta.get("language") or "zh-CN"
    description = description or book_meta.get("description", "")
    rights = rights or book_meta.get("rights", "")
    publisher = publisher or book_meta.get("publisher", "")

    out_path = Path(out) if out else \
        ar.root.parent / f"{(book_meta.get('id') or 'book')}.epub"
    ensure_dir(out_path.parent)

    if verbose:
        log(f"书籍：{title} · {len(ar.articles)} 篇 · 主题 {theme}", "step")

    # ---------- 稳定的书号（同一本书反复导出不变）
    uid_file = ar.root / "metadata" / "book-uuid.txt"
    if book_uuid:
        uid = book_uuid
    elif uid_file.exists():
        uid = uid_file.read_text(encoding="utf-8").strip()
    else:
        uid = f"urn:uuid:{uuid.uuid4()}"
        ensure_dir(uid_file.parent)
        uid_file.write_text(uid + "\n", encoding="utf-8")

    bk = epub.EpubBook()
    bk.set_identifier(uid)
    bk.set_title(title if not subtitle else f"{title}：{subtitle}")
    bk.set_language(language)
    if author:
        bk.add_author(author)
    if publisher:
        bk.add_metadata("DC", "publisher", publisher)
    if description:
        bk.add_metadata("DC", "description", description)
    if rights:
        bk.add_metadata("DC", "rights", rights)
    bk.add_metadata("DC", "date", now_iso()[:10])
    for s in book_meta.get("subject", []) or []:
        bk.add_metadata("DC", "subject", str(s))
    bk.set_direction("default")

    # ---------- 封面
    cover_path: Path | None = None
    if not no_cover:
        cand = cover or book_meta.get("cover", "")
        if cand:
            p = Path(cand)
            if not p.is_absolute():
                p = ar.root / cand
            if p.exists():
                cover_path = p
            elif verbose:
                log(f"封面图不存在：{cand}", "warn")
        if cover_path is None:
            if verbose:
                log("没有封面图 → 程序渲染（不依赖任何外部服务）", "info")
            try:
                from .cover import render_to_archive
                res = render_to_archive(ar, style=book_meta.get("cover_style") or "auto",
                                        basename="cover-auto", write_brief=False)
                cover_path = ar.root / res["files"]["jpg"]
            except Exception as e:
                log(f"程序封面渲染失败：{type(e).__name__}: {e}", "warn")

    if cover_path is not None:
        bk.set_cover("cover.jpg", cover_path.read_bytes())
        if verbose:
            log(f"封面：{relpath(cover_path, ar.root)}", "info")

    # ---------- 排版主题
    ts = Typesetter(ar, theme=theme, lang=language, cjk_space=cjk_space)
    css_content = ts.css()
    css_item = epub.EpubItem(uid="css-main", file_name="Style/main.css",
                             media_type="text/css",
                             content=css_content.encode("utf-8"))
    bk.add_item(css_item)

    # ---------- 图片资源
    img_map: dict[str, str] = {}
    for i, asset in enumerate(ar.manifest.get("assets", [])):
        rel = asset.get("path", "")
        if not rel.startswith("assets/"):
            continue
        p = ar.root / rel
        if not p.exists():
            continue
        fname = "Images/" + rel.split("/", 1)[1]
        bk.add_item(epub.EpubItem(uid=f"img-{i:03d}", file_name=fname,
                                  media_type=_mime_of(p), content=p.read_bytes()))
        img_map[f"../{rel}"] = f"../{fname}"

    # ---------- 章节
    chapters: list[epub.EpubHtml] = []
    used_titles: dict[str, int] = {}
    url2ch: dict[str, epub.EpubHtml] = {}
    url2words: dict[str, int] = {}
    for i, art in enumerate(ar.articles, 1):
        p = ar.root / art.file
        if not p.exists():
            log(f"正文缺失，跳过：{art.id}", "warn")
            continue
        res = ts.typeset_article(art, chapter_no=f"{i:02d}")
        for old, new in img_map.items():
            res.xhtml = res.xhtml.replace(f'src="{old}"', f'src="{new}"')

        t = res.title
        used_titles[t] = used_titles.get(t, 0) + 1
        if used_titles[t] > 1:
            t = f"{t}（{used_titles[t]}）"

        ch = epub.EpubHtml(title=t, file_name=f"Text/{res.filename}", lang=language)
        # ebooklib 的解析器不接受带 encoding 声明的 str，必须传 bytes
        ch.content = res.xhtml.encode("utf-8")
        ch.add_link(href="Style/main.css", rel="stylesheet", type="text/css")
        bk.add_item(ch)
        chapters.append(ch)
        if art.source_url:
            key = art.source_url.split("#")[0]
            url2ch[key] = ch
            url2words[key] = art.word_count

    if not chapters:
        raise ValueError("没有可打包的章节")

    # ---------- 前置页
    front: list[epub.EpubHtml] = []
    tp = epub.EpubHtml(title="扉页", file_name="Text/titlepage.xhtml", lang=language)
    tp.content = ts.title_page().encode("utf-8")
    tp.add_link(href="Style/main.css", rel="stylesheet", type="text/css")
    bk.add_item(tp)
    front.append(tp)

    cp = epub.EpubHtml(title="版权信息", file_name="Text/colophon.xhtml",
                       lang=language)
    cp.content = ts.copyright_page().encode("utf-8")
    cp.add_link(href="Style/main.css", rel="stylesheet", type="text/css")
    bk.add_item(cp)
    front.append(cp)

    # ---------- 目录与书脊
    group_spec = load_archive_groups(ar.root)
    toc_tree: list = []
    middle: list[epub.EpubHtml] = []
    leftover: list[epub.EpubHtml] = []
    n_parts = n_sections = 0
    if group_spec:
        toc_tree, middle, leftover = build_grouped_toc(
            group_spec, url2ch, url2words, ts, language, bk)
        n_parts = len(toc_tree)
        # 只数真正的"章分隔页"（Text/sec*.xhtml）。部下面只有一个细分类型时
        # 我们会刻意跳过章这一层，此时部页直接挂文章，不该被算成章。
        n_sections = sum(1 for ch in middle
                         if ch.file_name.startswith("Text/sec"))
        if verbose:
            if toc_tree:
                log(f"分组目录：{n_parts} 部 · {n_sections} 个细分类型 · "
                    f"{len(url2ch) - len(leftover)} 篇已归类", "info")
                if leftover:
                    log(f"{len(leftover)} 篇不在分组清单里，附在书末", "warn")
            else:
                log("groups.json 里没有任何能对上的文章，退回平铺目录", "warn")

    nav = epub.EpubNav()
    bk.add_item(nav)
    bk.add_item(epub.EpubNcx())

    if toc_tree:
        bk.toc = tuple(list(toc_tree) + [ch for ch in leftover])
        bk.spine = ["cover"] + front + [nav] + middle + leftover
    else:
        bk.toc = tuple(chapters)
        bk.spine = ["cover"] + front + [nav] + chapters

    t0 = time.monotonic()
    epub.write_epub(str(out_path), bk, {})
    elapsed = time.monotonic() - t0

    size = out_path.stat().st_size
    problems = validate_epub(out_path)
    external = count_external_images(out_path)
    if external:
        problems.append(f"{external} 处图片仍是外链（下载失败未本地化，离线阅读会缺图）")
    if verbose:
        for pr in problems:
            log(f"EPUB 结构问题：{pr}", "warn")

    total_words = sum(c.word_count for c in ar.articles)
    report = {
        "stage": "publish", "at": now_iso(),
        "output": str(out_path), "size_bytes": size,
        "size_human": human_size(size),
        "title": title, "author": author, "language": language,
        "theme": theme, "chapters": len(chapters), "words": total_words,
        "parts": n_parts, "sections": n_sections,
        "grouped": bool(toc_tree),
        "cover": bool(cover_path), "uuid": uid,
        "external_images": external,
        "elapsed_sec": round(elapsed, 2),
        "problems": problems,
    }
    ar.record_stage("publish", {"output": str(out_path), "theme": theme,
                                "chapters": len(chapters), "size": size})
    ar.write_report("publish-report", report)
    ar.save()
    write_index(ar, out_path, title, subtitle, author, theme, len(chapters))

    if verbose:
        log(f"EPUB 已生成（{elapsed:.1f}s）：{out_path}", "done")
        log(f"体积 {human_size(size)} · 章节 {len(chapters)} · "
            f"字数约 {total_words:,}", "info")
        log("EPUB 结构校验通过" if not problems
            else f"{len(problems)} 处结构告警，详见 publish-report.json",
            "ok" if not problems else "warn")
    return report
