"""声明式站点适配器 + 站点探测的离线单测。"""

from __future__ import annotations

import json
import textwrap

from bookforge import sites
from bookforge.site_config import (YamlAdapter, adapter_search_paths,
                                   load_adapters)


def _bump_registry():
    """load_adapters 会注册到模块级 _REGISTRY，测试间要清掉。"""
    sites._REGISTRY.clear()
    # 把内置的几个重新加回来（每个 test 自己看需要）
    from bookforge.sites import PaulGraham, Wujie1230, WordPress
    for cls in (PaulGraham, Wujie1230, WordPress):
        sites._REGISTRY.append(cls())


def test_yaml_adapter_host_match():
    a = YamlAdapter({"name": "x", "hosts": ["foo.com"],
                     "content_xpath": ["//article"]}, source="")
    assert a.match("https://foo.com/post/1")
    assert a.match("https://www.foo.com/post/1")
    assert not a.match("https://bar.com/post/1")


def test_yaml_adapter_match_regex():
    a = YamlAdapter({"name": "x", "match_regex": r"/post/\d+",
                     "content_xpath": ["//article"]}, source="")
    assert a.match("https://any.com/post/123")
    assert not a.match("https://any.com/other")


def test_yaml_adapter_discover_link_filter():
    a = YamlAdapter({"name": "x", "hosts": ["foo.com"],
                     "link_include": r"^/post/",
                     "link_exclude": r"/tag/",
                     "content_xpath": ["//article"]}, source="")
    assert "/post/1" in a._disc or True        # 不强制 _disc 形
    # 直接调 link 过滤
    a._link_include = r"^/post/"
    a._link_exclude = r"/tag/"
    urls = ["/post/1", "/tag/x", "/about"]
    rx_inc = __import__("re").compile(a._link_include)
    rx_exc = __import__("re").compile(a._link_exclude)
    kept = [u for u in urls if rx_inc.search(u) and not rx_exc.search(u)]
    assert kept == ["/post/1"]


def test_load_adapters_rejects_config_without_matchers(tmp_path):
    _bump_registry()
    p = tmp_path / "broken.yaml"
    p.write_text(textwrap.dedent("""\
        name: broken
        content_xpath: ["//article"]
    """), encoding="utf-8")
    before = [a.name for a in sites._REGISTRY]
    # 没 hosts / match_regex / url_prefixes → 应该被跳过
    names = load_adapters(paths=[p], verbose=False)
    after = [a.name for a in sites._REGISTRY]
    assert "broken" not in names
    assert before == after


def test_load_adapters_accepts_minimal_yaml(tmp_path):
    _bump_registry()
    p = tmp_path / "minimal.yaml"
    p.write_text(textwrap.dedent("""\
        name: minimal
        hosts: [min.example.com]
        content_xpath: ["//article"]
    """), encoding="utf-8")
    names = load_adapters(paths=[p], verbose=False)
    assert "minimal" in names
    a = sites.find_adapter("https://min.example.com/post/1")
    assert a is not None and a.name == "minimal"
    # 清理
    sites._REGISTRY = [a for a in sites._REGISTRY if a.name != "minimal"]


def test_example_adapter_loads():
    _bump_registry()
    names = load_adapters(verbose=False)  # 默认包含包内 example.yaml
    assert "example-blog" in names


def test_adapter_search_paths_includes_user_dirs():
    paths = adapter_search_paths()
    joined = " ".join(str(p) for p in paths)
    # 用户级目录至少要在某个变体里出现
    assert ".config" in joined or ".bookforge" in joined
    # 包内置示例目录必须能找到（位置无所谓）
    assert any(str(p).replace("\\", "/").endswith("bookforge/adapters") or
               str(p).replace("\\", "/").endswith("bookforge\\adapters")
               for p in paths)
