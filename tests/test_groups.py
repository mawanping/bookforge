"""分组逻辑单测（不联网）。"""

from __future__ import annotations

from bookforge import groups
from bookforge.groups import (auto_priority, auto_theme_order, build_plan,
                              rescue_orphans, summarise, write_urls)


def _cat(cid, name, slug, parent=0, count=0):
    return {"id": cid, "name": name, "slug": slug, "parent": parent,
            "count": count}


class TestRescueOrphans:
    def test_rescues_missing_urls(self):
        plan = {"groups": [{"title": "A", "subgroups": [
            {"title": "a1", "urls": ["u1", "u2"]}]}]}
        n = rescue_orphans(plan, ["u1", "u2", "u3", "u4"])
        assert n == 2
        assert plan["groups"][-1]["title"] == groups.ORPHAN_THEME
        assert plan["groups"][-1]["subgroups"][0]["urls"] == ["u3", "u4"]
        assert plan["counts"]["orphans_rescued"] == 2

    def test_no_orphans_no_group_added(self):
        plan = {"groups": [{"title": "A", "subgroups": [
            {"title": "a1", "urls": ["u1"]}]}]}
        assert rescue_orphans(plan, ["u1"]) == 0
        assert len(plan["groups"]) == 1


class TestAutoPriority:
    def test_deeper_category_wins(self):
        cats = [_cat(1, "Top", "top", 0), _cat(2, "Sub", "sub", 1)]
        assert auto_priority(cats)[0] == "sub"

    def test_narrower_wins_at_same_depth(self):
        cats = [_cat(1, "Broad", "broad", 0, count=500),
                _cat(2, "Narrow", "narrow", 0, count=3)]
        assert auto_priority(cats)[0] == "narrow"


class TestAutoThemeOrder:
    def test_dominant_generic_top_goes_last(self):
        cats = [_cat(1, "项目", "xiangmu", 0, 400),
                _cat(2, "资源", "free", 0, 20),
                _cat(3, "随笔", "suibi", 0, 1200)]
        order = auto_theme_order(cats, None, "综合")
        assert order and order[-1] == "suibi"

    def test_explicit_order_wins(self):
        cats = [_cat(1, "A", "a", 0, 1), _cat(2, "B", "b", 0, 1)]
        assert auto_theme_order(cats, ["b", "a"], "综合") == ["b", "a"]

    def test_single_top_returns_empty(self):
        assert auto_theme_order([_cat(1, "A", "a", 0, 5)], None, "综合") == []


class TestByDate:
    def test_groups_by_year_sorted(self):
        entries = [
            {"url": "u3", "title": "", "date": "2021-05-01"},
            {"url": "u1", "title": "", "date": "2019-01-01"},
            {"url": "u2", "title": "", "date": "2021-02-01"},
        ]
        plan = build_plan(None, None, by="date", entries=entries, verbose=False)
        assert plan["strategy"] == "date"
        assert [g["title"] for g in plan["groups"]] == ["2019", "2021"]
        # 同年内按时间从早到晚
        assert plan["groups"][1]["urls"] == ["u2", "u3"]

    def test_undated_goes_to_tail(self):
        entries = [{"url": "u1", "title": "", "date": ""},
                   {"url": "u2", "title": "", "date": "2020-01-01"}]
        plan = build_plan(None, None, by="date", entries=entries, verbose=False)
        assert [g["title"] for g in plan["groups"]] == ["2020", "未标日期"]


class TestByPath:
    def test_groups_by_first_segment(self):
        entries = [{"url": "https://x.com/tutorial/a", "title": "", "date": ""},
                   {"url": "https://x.com/tutorial/b", "title": "", "date": ""},
                   {"url": "https://x.com/news/c", "title": "", "date": ""}]
        plan = build_plan(None, None, by="path", entries=entries, verbose=False)
        assert plan["groups"][0]["title"] == "tutorial"
        assert len(plan["groups"][0]["urls"]) == 2
        assert len(plan["groups"]) == 2


class TestFlatAndUrls:
    def test_flat_keeps_everything(self):
        entries = [{"url": f"u{i}", "title": "", "date": f"2020-0{i}-01"}
                   for i in range(1, 4)]
        plan = build_plan(None, None, by="flat", entries=entries, verbose=False)
        urls = groups.profile_urls(plan)
        assert sorted(urls) == ["u1", "u2", "u3"]

    def test_write_urls_roundtrip(self, tmp_path):
        entries = [{"url": "https://x.com/a", "title": "", "date": "2020-01-01"}]
        plan = build_plan(None, None, by="date", entries=entries, verbose=False)
        out = tmp_path / "urls.txt"
        n = write_urls(plan, out)
        assert n == 1
        assert out.read_text(encoding="utf-8").strip() == "https://x.com/a"

    def test_summarise_mentions_counts(self):
        plan = {"groups": [{"title": "项目", "subgroups": [
            {"title": "网络推广", "urls": ["a", "b"]}]}]}
        s = summarise(plan)
        assert "项目" in s and "2 篇" in s and "网络推广" in s


def test_unknown_strategy_raises():
    import pytest
    with pytest.raises(ValueError):
        build_plan(None, None, by="nope", entries=[], verbose=False)
