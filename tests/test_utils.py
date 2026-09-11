"""工具函数与标题清洗的单测（不联网）。"""

from __future__ import annotations

import pytest

from bookforge.extract import strip_common_title_suffix, strip_site_suffix
from bookforge.utils import (count_words, human_size, reading_minutes,
                             safe_filename, slugify)


class TestSlugify:
    def test_keeps_cjk(self):
        assert slugify("无极领域文集") == "无极领域文集"

    def test_strips_punctuation(self):
        assert slugify("Hello, World!") == "hello-world"

    def test_collapses_separators(self):
        assert slugify("a  -  b") == "a-b"

    def test_truncates(self):
        assert len(slugify("x" * 200, maxlen=10)) <= 10

    def test_empty_falls_back(self):
        assert slugify("") == "untitled"
        assert slugify("!!!") == "untitled"


class TestCountWords:
    def test_cjk_counted_per_char(self):
        assert count_words("你好世界") == 4

    def test_latin_counted_per_word(self):
        assert count_words("hello world") == 2

    def test_mixed(self):
        assert count_words("你好 world") == 3

    def test_empty(self):
        assert count_words("") == 0


class TestSafeFilename:
    def test_strips_windows_illegal(self):
        assert "/" not in safe_filename("a/b:c*d?e")
        assert ":" not in safe_filename("a:b")

    def test_keeps_extension(self):
        assert safe_filename("x" * 200, maxlen=20, ext=".jpg").endswith(".jpg")


def test_human_size():
    assert human_size(512) == "512 B"
    assert human_size(2048) == "2.0 KB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"


def test_reading_minutes_cjk_is_faster_per_char():
    # 400 字/分 vs 230 词/分
    assert reading_minutes(400, "zh-CN") == 1
    assert reading_minutes(460, "en") == 2


class TestStripSiteSuffix:
    def test_strips_exact_site_name(self):
        assert strip_site_suffix("Article Title - My Blog", "My Blog") == "Article Title"

    def test_handles_pipe_and_dash_variants(self):
        assert strip_site_suffix("标题 | 某某博客", "某某博客") == "标题"
        assert strip_site_suffix("标题 – 某某博客", "某某博客") == "标题"

    def test_uses_homepage_title_when_site_name_missing(self):
        got = strip_site_suffix("Hello World - My Blog", "", "My Blog - Home")
        assert got == "Hello World"

    def test_does_not_touch_unrelated_dash(self):
        t = "标题里有 - 破折号"
        assert strip_site_suffix(t, "别的站名") == t

    def test_empty_input(self):
        assert strip_site_suffix("", "X") == ""

    def test_only_strips_once(self):
        # 站名本身也可能带横线，不能越剥越短
        assert strip_site_suffix("A - B - My Blog", "My Blog") == "A - B"


class TestStripCommonTitleSuffix:
    def test_detects_shared_tail(self):
        titles = [f"文章{i} - 阮一峰的网络日志" for i in range(5)]
        m = strip_common_title_suffix(titles)
        assert len(m) == 5
        assert all(v.startswith("文章") and "阮一峰" not in v for v in m.values())

    def test_no_tail_returns_empty(self):
        assert strip_common_title_suffix(["甲", "乙", "丙"]) == {}

    def test_below_min_count_returns_empty(self):
        assert strip_common_title_suffix(
            ["a - X", "b - X"], min_count=3) == {}

    def test_minority_tail_is_ignored(self):
        titles = ["a - X", "b - Y", "c - Z", "d - W", "e - V", "f - U"]
        assert strip_common_title_suffix(titles) == {}

    def test_ignores_overlong_tail(self):
        long_tail = "y" * 40
        titles = [f"文章{i} - {long_tail}" for i in range(5)]
        assert strip_common_title_suffix(titles) == {}


@pytest.mark.parametrize("text,expected", [
    ("  spaced  ", "spaced"),
])
def test_clean_text_like_behaviour(text, expected):
    from bookforge.utils import clean_text
    assert clean_text(text) == expected
