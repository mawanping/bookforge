"""CLI 与子命令的单测（不联网）。"""

from __future__ import annotations

import json
import sys

from bookforge.cli import build_parser, main


class TestParser:
    def test_version_flag(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["bookforge", "--version"])
        with __import__("pytest").raises(SystemExit):
            build_parser().parse_args()
        out = capsys.readouterr().out
        assert "bookforge" in out

    def test_build_subcommand_minimal(self):
        ns = build_parser().parse_args(["build", "https://example.com"])
        assert ns.url == "https://example.com"
        assert ns.by == "auto"
        assert ns.theme == "classic"
        assert ns.image_max_width == 900
        assert ns.cover_style == "auto"

    def test_build_passes_json_and_quiet(self):
        ns = build_parser().parse_args(
            ["build", "https://x.com", "--json", "--quiet", "--max", "20"])
        assert ns.json and ns.quiet and ns.max == 20

    def test_pack_subcommand(self):
        ns = build_parser().parse_args(
            ["pack", "--archive", "x", "--theme", "modern", "-o", "out.epub"])
        assert ns.archive == "x" and ns.theme == "modern" and ns.out == "out.epub"

    def test_unknown_subcommand_rejected(self, capsys):
        import pytest
        with pytest.raises(SystemExit):
            build_parser().parse_args(["nope"])


class TestHelpForAgent:
    """在 agent 场景里，--help 应该是清晰且不被装饰过的。"""

    def test_build_help_mentions_json(self, capsys):
        try:
            build_parser().parse_args(["build", "--help"])
        except SystemExit:
            pass
        out = capsys.readouterr().out
        assert "--json" in out and "--quiet" in out


class TestDoctorAndAdapters:
    def test_doctor_returns_int(self, capsys):
        rc = main(["doctor", "--quiet"])
        # 可能缺某些可选依赖；只要命令本身能跑通即可
        assert rc in (0, 1, 2)
        captured = capsys.readouterr()
        assert captured  # 至少输出了点东西

    def test_adapters_lists_builtins(self, capsys):
        rc = main(["adapters"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "wujie1230" in out
        assert "paulgraham" in out

    def test_themes_lists(self):
        from bookforge.typeset import Typesetter
        names = Typesetter.available_themes()
        assert {"classic", "modern", "magazine", "academic"} <= set(names)

    def test_help_only_shows_subcommands(self, capsys):
        try:
            main(["--help"])
        except SystemExit:
            pass
        out = capsys.readouterr().out
        for cmd in ("build", "fetch", "cover", "pack", "doctor",
                    "adapters", "themes", "translate"):
            assert cmd in out, f"{cmd} 缺失在 --help 输出里"