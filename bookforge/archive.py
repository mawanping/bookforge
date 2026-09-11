"""归档包（Archive）规范与读写。

这是整条流水线的骨架。四个阶段全部围绕同一套目录结构工作，
所以下游永远不需要"猜路径"，也不存在人工整理文件这一步。

    <archive>/
    ├── content/            # 每篇文章一个 .md，编号前缀决定书内顺序
    │   ├── 001-xxx.md
    │   └── 002-xxx.md
    ├── assets/             # 图片等二进制资源
    ├── metadata/           # 逐篇元数据 sidecar + book.json
    ├── manifest.json       # 单一可信源（single source of truth）
    └── reports/            # 各阶段质量报告
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

from . import ARCHIVE_SPEC_VERSION
from .utils import ensure_dir, log, now_iso, relpath, safe_filename, slugify

MANIFEST_NAME = "manifest.json"

DEFAULT_BOOK = {
    "id": "untitled-book",
    "title": "Untitled",
    "subtitle": "",
    "author": "Unknown",
    "language": "zh-CN",
    "source_language": "en",
    "publisher": "",
    "date": "",
    "description": "",
    "rights": "",
    "subject": [],
    "series": "",
    "series_index": "",
}

# frontmatter 中与 manifest 同步的键
_FM_KEYS = (
    "title",
    "subtitle",
    "author",
    "published",
    "source",
    "slug",
    "order",
    "language",
    "summary",
    "tags",
)


# ---------------------------------------------------------------- 数据结构

@dataclass
class Article:
    """一篇文章在归档包中的登记项。"""

    index: int
    id: str
    title: str
    file: str                       # 相对归档根，POSIX 风格
    source_url: str = ""
    author: str = ""
    published_at: str = ""
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    word_count: int = 0
    translated: bool = False
    translation_file: str = ""
    fetch_status: str = "ok"        # ok | partial | failed | skipped
    notes: str = ""
    group: str = ""                 # 主题（部），如「项目」；空表示不分组
    subgroup: str = ""              # 细分类型（章），如「网络推广」

    @classmethod
    def from_dict(cls, d: dict) -> "Article":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- Archive

class Archive:
    """归档包的读写入口。"""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.content_dir = self.root / "content"
        self.assets_dir = self.root / "assets"
        self.metadata_dir = self.root / "metadata"
        self.reports_dir = self.root / "reports"
        self.cover_dir = self.root / "cover"
        self._manifest: dict | None = None

    # -------------------------------------------------- 生命周期

    @classmethod
    def create(cls, root: str | Path, book: dict | None = None) -> "Archive":
        ar = cls(root)
        for d in (ar.content_dir, ar.assets_dir, ar.metadata_dir, ar.reports_dir):
            ensure_dir(d)
        ar._manifest = {
            "spec_version": ARCHIVE_SPEC_VERSION,
            "book": {**DEFAULT_BOOK, **(book or {})},
            "source": {},
            "articles": [],
            "assets": [],
            "stats": {},
            "stages": {},
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        return ar

    @classmethod
    def open(cls, root: str | Path) -> "Archive":
        ar = cls(root)
        if not ar.manifest_path.exists():
            raise FileNotFoundError(
                f"归档包缺少 manifest：{ar.manifest_path}\n"
                f"请先运行 stage1 抓取，或确认路径是否正确。"
            )
        ar._manifest = json.loads(ar.manifest_path.read_text(encoding="utf-8"))
        return ar

    @classmethod
    def open_or_create(cls, root: str | Path, book: dict | None = None) -> "Archive":
        try:
            return cls.open(root)
        except FileNotFoundError:
            return cls.create(root, book)

    # -------------------------------------------------- 属性

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def manifest(self) -> dict:
        if self._manifest is None:
            if self.manifest_path.exists():
                self._manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            else:
                self._manifest = {"spec_version": ARCHIVE_SPEC_VERSION,
                                  "book": dict(DEFAULT_BOOK), "articles": [],
                                  "assets": [], "stats": {}, "stages": {}}
        return self._manifest

    @property
    def book(self) -> dict:
        return self.manifest.setdefault("book", dict(DEFAULT_BOOK))

    @property
    def articles(self) -> list[Article]:
        return [a if isinstance(a, Article) else Article.from_dict(a)
                for a in self.manifest.get("articles", [])]

    # -------------------------------------------------- 修改

    def set_book(self, **kwargs) -> None:
        """更新书籍级元数据（None 值忽略，空串覆盖）。"""
        for k, v in kwargs.items():
            if v is not None:
                self.book[k] = v

    def add_article(self, art: Article) -> None:
        arts = [a.to_dict() for a in self.articles if a.id != art.id]
        arts.append(art.to_dict())
        arts.sort(key=lambda d: d.get("index", 0))
        self.manifest["articles"] = arts

    def add_asset(self, rel: str, *, size: int = 0, kind: str = "image",
                  source_url: str = "") -> None:
        assets = self.manifest.setdefault("assets", [])
        for a in assets:
            if a["path"] == rel:
                a["size"] = size or a.get("size", 0)
                return
        assets.append({"path": rel, "size": size, "kind": kind,
                       "source_url": source_url})

    def record_stage(self, stage: str, payload: dict) -> None:
        stages = self.manifest.setdefault("stages", {})
        stages[stage] = {**payload, "at": now_iso()}

    def set_stats(self, **kwargs) -> None:
        self.manifest.setdefault("stats", {}).update(kwargs)

    # -------------------------------------------------- 保存

    def save(self) -> Path:
        self.manifest["updated_at"] = now_iso()
        arts = self.manifest.get("articles", [])
        self.manifest["stats"].setdefault("articles", len(arts))
        self.manifest["stats"].setdefault(
            "words", sum(int(a.get("word_count") or 0) for a in arts))
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return self.manifest_path

    # -------------------------------------------------- 便捷路径

    def content_path(self, name: str) -> Path:
        """解析正文文件路径。

        既接受纯文件名（`001-x.md`），也接受 manifest 里的相对路径
        （`content/001-x.md`）——否则调用方很容易拼出
        `content/content/001-x.md` 这种不存在的路径。
        """
        p = Path(name)
        if p.is_absolute():
            return p
        parts = p.parts
        if parts and parts[0] == self.content_dir.name:
            return self.content_dir.joinpath(*parts[1:])
        return self.content_dir / p

    def asset_path(self, rel: str) -> Path:
        """把 manifest 里的 'assets/xxx.jpg' 解析成绝对路径。"""
        p = Path(rel)
        return p if p.is_absolute() else (self.root / rel)

    def read_content(self, art: Article) -> str:
        p = self.root / (art.translation_file or art.file)
        if not p.exists():
            p = self.root / art.file
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def write_report(self, name: str, data: Any) -> Path:
        ensure_dir(self.reports_dir)
        p = self.reports_dir / (name if name.endswith(".json") else f"{name}.json")
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    # -------------------------------------------------- 一致性自检

    def audit(self) -> dict:
        """检查 manifest 与磁盘是否一致。返回问题列表。"""
        issues: list[dict] = []
        seen_ids: set[str] = set()
        for i, a in enumerate(self.articles, 1):
            if a.id in seen_ids:
                issues.append({"level": "error", "article": a.id,
                               "msg": "id 重复"})
            seen_ids.add(a.id)
            f = self.root / a.file
            if not f.exists():
                issues.append({"level": "error", "article": a.id,
                               "msg": f"正文文件缺失：{a.file}"})
            else:
                actual = f.read_text(encoding="utf-8", errors="replace")
                if not actual.strip():
                    issues.append({"level": "warn", "article": a.id,
                                   "msg": "正文为空"})
            for rel in a.assets:
                if not (self.root / rel).exists():
                    issues.append({"level": "warn", "article": a.id,
                                   "msg": f"资源缺失：{rel}"})
            if a.fetch_status == "failed":
                issues.append({"level": "error", "article": a.id,
                               "msg": "抓取失败"})
            elif a.fetch_status == "partial":
                issues.append({"level": "warn", "article": a.id,
                               "msg": a.notes or "抓取不完整"})
        if not self.articles:
            issues.append({"level": "error", "article": "-", "msg": "归档包内没有文章"})
        return {
            "archive": str(self.root),
            "articles": len(self.articles),
            "assets": len(self.manifest.get("assets", [])),
            "errors": sum(1 for i in issues if i["level"] == "error"),
            "warnings": sum(1 for i in issues if i["level"] == "warn"),
            "issues": issues,
            "audited_at": now_iso(),
        }

    # -------------------------------------------------- 复制

    def clone_to(self, dest: str | Path, *, copy_assets: bool = True) -> "Archive":
        """把归档包复制到新位置（翻译阶段用：保留原始存档）。"""
        dest = Path(dest)
        ensure_dir(dest)
        for d in ("content", "metadata", "reports"):
            src = self.root / d
            if src.exists():
                shutil.copytree(src, dest / d, dirs_exist_ok=True)
        if copy_assets:
            src = self.assets_dir
            if src.exists():
                shutil.copytree(src, dest / "assets", dirs_exist_ok=True)
        new = Archive(dest)
        new._manifest = json.loads(json.dumps(self.manifest))
        new._manifest["source_archive"] = str(self.root)
        new.save()
        return new


# ---------------------------------------------------------------- frontmatter

def split_frontmatter(text: str) -> tuple[dict, str]:
    """拆出 YAML frontmatter。返回 (meta, body)。不依赖 PyYAML 也能工作。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, text
    raw = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    try:
        import yaml  # type: ignore
        meta = yaml.safe_load(raw) or {}
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = _mini_yaml(raw)
    return meta, body


def build_frontmatter(meta: dict) -> str:
    """生成 YAML frontmatter 块。手写序列化，避免 yaml 换行风格差异。"""
    if not meta:
        return ""
    lines = ["---"]
    for k, v in meta.items():
        if v is None or v == "":
            continue
        if isinstance(v, (list, tuple)):
            if not v:
                continue
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {_yaml_scalar(item)}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _yaml_scalar(v: Any) -> str:
    s = str(v).replace("\\", "\\\\")
    if (s != s.strip() or any(ch in s for ch in ':#"\'{}[],&*?|<>=!%@`')
            or s.lower() in ("true", "false", "null", "yes", "no", "~")):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _mini_yaml(raw: str) -> dict:
    """极简 YAML 解析兜底（仅顶层 key: value 与列表）。"""
    meta: dict[str, Any] = {}
    key = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith(("  - ", "- ")) and key:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(line.split("- ", 1)[1].strip().strip('"\''))
            continue
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"\'')
            meta[key] = val if val else []
    return meta


def article_meta(art: Article, book: dict) -> dict:
    """把 Article 还原成 frontmatter 字典（供写回 Markdown）。"""
    m = {
        "title": art.title,
        "author": art.author or book.get("author", ""),
        "source": art.source_url,
        "published": art.published_at,
        "slug": art.id,
        "order": art.index,
        "language": book.get("language", "zh-CN"),
    }
    if art.summary:
        m["summary"] = art.summary
    if art.group:
        m["group"] = art.group
    if art.subgroup:
        m["subgroup"] = art.subgroup
    if art.tags:
        m["tags"] = list(art.tags)
    return m
