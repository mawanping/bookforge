"""归档包读写与 manifest 行为的离线单测。"""

from __future__ import annotations

from bookforge.archive import (ARCHIVE_SPEC_VERSION, Article, article_meta,
                               build_frontmatter, split_frontmatter)


def test_constants():
    assert ARCHIVE_SPEC_VERSION == "1.0"
    assert Article is not None


def test_build_frontmatter_includes_group_fields():
    art = Article(index=1, id="x", title="标题", file="content/001-x.md",
                  source_url="https://x.com/1", author="A",
                  published_at="2020-01-01", summary="",
                  tags=[], assets=[], word_count=100,
                  fetch_status="ok", notes="", group="项目", subgroup="网络推广")
    fm = article_meta(art, {"title": "书", "author": "未知"})
    assert fm["group"] == "项目"
    assert fm["subgroup"] == "网络推广"


def test_split_frontmatter_roundtrip():
    text = "---\ntitle: x\norder: 1\n---\n# 标题\n正文"
    fm, body = split_frontmatter(text)
    assert fm["title"] == "x"
    assert "正文" in body and "---" not in body


def test_build_frontmatter_unicode_safe():
    fm = build_frontmatter({"title": "无极领域文集", "order": 1})
    assert "无极领域文集" in fm


def test_article_repr_works():
    art = Article(index=2, id="y", title="another", file="content/002-y.md")
    r = repr(art)
    assert "another" in r and "002" in r