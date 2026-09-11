"""通用工具：日志、slug、HTML 文本清洗、文件名安全化。"""

from __future__ import annotations

import hashlib
import html
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- 终端输出

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

_COLORS = {
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def c(text: str, color: str) -> str:
    if not _USE_COLOR:
        return text
    return f"{_COLORS.get(color, '')}{text}{_COLORS['reset']}"


# 进度信息默认走 stdout；`bookforge --json` 时会被切到 stderr，
# 这样 stdout 上就只剩下一段干净的 JSON，agent 可以直接解析。
_OUT_STREAM = None

# `--quiet` 时只放行 warn/err，其余进度一律不打印。
_QUIET = False

_QUIET_LEVELS = {"warn", "err"}


def set_log_stream(stream) -> None:
    """把 info/step/ok/done 级日志重定向到指定流（None = 恢复 stdout）。"""
    global _OUT_STREAM
    _OUT_STREAM = stream


def set_quiet(enabled: bool = True) -> None:
    """只保留 warn/err（真正的"安静"模式）。"""
    global _QUIET
    _QUIET = bool(enabled)


def set_color(enabled: bool) -> None:
    global _USE_COLOR
    _USE_COLOR = bool(enabled) and sys.stdout.isatty()


def log(msg: str, level: str = "info") -> None:
    """打印一行带标签的进度日志。"""
    tags = {
        "info": ("  ·  ", "dim"),
        "step": ("  ▸  ", "cyan"),
        "ok": ("  ✓  ", "green"),
        "warn": ("  !  ", "yellow"),
        "err": ("  ✗  ", "red"),
        "done": ("  ★  ", "magenta"),
    }
    if _QUIET and level not in _QUIET_LEVELS:
        return
    tag, color = tags.get(level, tags["info"])
    if level in ("warn", "err"):
        stream = sys.stderr
    else:
        stream = _OUT_STREAM or sys.stdout
    print(f"{c(tag, color)}{msg}", file=stream, flush=True)


def banner(title: str, subtitle: str = "") -> None:
    if _QUIET:
        return
    line = "─" * 62
    stream = _OUT_STREAM or sys.stdout
    print(f"\n{c(line, 'dim')}", flush=True, file=stream)
    print(f"  {c(title, 'bold')}", flush=True, file=stream)
    if subtitle:
        print(f"  {c(subtitle, 'dim')}", flush=True, file=stream)
    print(f"{c(line, 'dim')}\n", flush=True, file=stream)


# ---------------------------------------------------------------- 时间 / slug

def now_iso() -> str:
    """本地时区的 ISO8601 时间戳（秒精度）。"""
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


_CJK = r"\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af"


def slugify(text: str, maxlen: int = 60) -> str:
    """生成 URL/文件名友好的 slug，保留 CJK 字符。"""
    text = html.unescape(text or "").strip()
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w\s\-" + _CJK + "]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text).strip("-").lower()
    text = re.sub(r"-{2,}", "-", text)
    if len(text) > maxlen:
        text = text[:maxlen].rstrip("-")
    return text or "untitled"


def safe_filename(name: str, maxlen: int = 80, ext: str = "") -> str:
    """把任意字符串变成安全的文件名（Windows 友好）。"""
    name = html.unescape(name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > maxlen:
        stem, dot, tail = name.rpartition(".")
        if dot and len(tail) <= 5:
            name = stem[: maxlen - len(tail) - 1] + "." + tail
        else:
            name = name[:maxlen]
    if ext and not name.lower().endswith(ext.lower()):
        name += ext
    return name or "untitled"


def short_hash(text: str, n: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:n]


# ---------------------------------------------------------------- HTML 清洗

_WS_RE = re.compile(r"[ \t\u00a0]+")
_NL_RE = re.compile(r"\n{3,}")


def clean_text(s: str) -> str:
    """压缩多余空白，但保留段落换行。"""
    if not s:
        return ""
    s = s.replace("\u200b", "").replace("\ufeff", "")
    s = _WS_RE.sub(" ", s)
    s = _NL_RE.sub("\n\n", s)
    return s.strip()


def strip_tags(s: str) -> str:
    return clean_text(re.sub(r"<[^>]+>", "", s or ""))


def count_words(text: str) -> int:
    """中英混排字数：CJK 按字计，拉丁按词计。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[" + _CJK + r"]", text))
    latin = len(re.findall(r"[A-Za-z0-9]+(?:['\-][A-Za-z0-9]+)*", text))
    return cjk + latin


def reading_minutes(words: int, lang: str = "en") -> int:
    """估算阅读时间（分钟）。中文 400 字/分，英文 230 词/分。"""
    rate = 400 if lang.startswith("zh") else 230
    return max(1, round(words / rate))


# ---------------------------------------------------------------- 路径

def ensure_dir(p: Path) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p


def relpath(p: Path, base: Path) -> str:
    """相对路径，统一用 / 分隔（归档包内一律 POSIX 风格）。"""
    try:
        return Path(p).resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return Path(p).as_posix()


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"
