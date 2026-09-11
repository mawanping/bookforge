"""跨平台字体发现与字重控制。

为什么要单独一个模块：原版把字体路径写死成 ``C:/Windows/Fonts``，
这让整个工具只能在中文 Windows 上生成封面。要做成通用工具，
必须覆盖三大平台 + 容器环境，并且在找不到中文字体时给出**可执行**的补救指令。

解析顺序：

1. 显式传入的 ``font_dir``（CLI ``--font-dir``）；
2. 环境变量 ``BOOKFORGE_FONTS``（多个目录用 ``os.pathsep`` 分隔）；
3. 用户字体缓存 ``~/.cache/bookforge/fonts``（下载的字体落在这里）；
4. 平台自带字体目录（Windows / macOS / Linux 各自一套）；
5. fontconfig 查询（Linux / macOS 上 ``fc-match``）；
6. 从 ``BOOKFORGE_FONT_URL`` 下载（可选，会校验能否真的渲染中文）。

任何一个角色解析失败都不会让程序崩掉 —— 封面会退回"无该角色"的排版方案。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .utils import log

# ---------------------------------------------------------------- 平台字体目录

_CACHE_DIR = Path(
    os.environ.get("BOOKFORGE_CACHE")
    or (Path.home() / ".cache" / "bookforge")
)


def _platform_dirs() -> list[Path]:
    """返回当前平台上值得扫描的字体目录（不保证都存在）。"""
    home = Path.home()
    if sys.platform.startswith("win"):
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        return [
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
            local / "Microsoft/Windows/Fonts",
        ]
    if sys.platform == "darwin":
        return [
            Path("/System/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
            Path("/Library/Fonts"),
            home / "Library/Fonts",
        ]
    # Linux / BSD / 容器
    return [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path("/opt/fonts"),
        home / ".local/share/fonts",
        home / ".fonts",
        _CACHE_DIR / "fonts",
    ]


# ---------------------------------------------------------------- 候选表

# role -> [(字体文件名, ttc 内的 face 序号)]，按优先级回退
CANDIDATES: dict[str, list[tuple[str, int]]] = {
    "serif_zh": [
        ("NotoSerifSC-VF.ttf", 0), ("NotoSerifCJKsc-Regular.otf", 0),
        ("SourceHanSerifSC-VF.ttf", 0), ("Songti.ttc", 0),
        ("STSong.ttf", 0), ("simsun.ttc", 0), ("msyh.ttc", 0),
        ("NotoSerifJP-Regular.otf", 0),
    ],
    "serif_en": [
        ("georgia.ttf", 0), ("Times New Roman.ttf", 0), ("times.ttf", 0),
        ("cambria.ttc", 0), ("constan.ttf", 0), ("LiberationSerif-Regular.ttf", 0),
        ("DejaVuSerif.ttf", 0), ("Times.ttc", 0), ("NimbusRoman-Regular.otf", 0),
    ],
    "sans_zh": [
        ("NotoSansSC-VF.ttf", 0), ("NotoSansCJKsc-Regular.otf", 0),
        ("SourceHanSansSC-VF.ttf", 0), ("PingFang.ttc", 0),
        ("Hiragino Sans GB.ttc", 0), ("STHeiti Medium.ttc", 0),
        ("msyh.ttc", 0), ("Deng.ttf", 0), ("simhei.ttf", 0),
        ("NotoSansJP-Regular.otf", 0), ("wqy-zenhei.ttc", 0),
    ],
    "sans_en": [
        ("arial.ttf", 0), ("Arial.ttf", 0), ("Helvetica.ttc", 0),
        ("segoeui.ttf", 0), ("calibri.ttf", 0),
        ("LiberationSans-Regular.ttf", 0), ("DejaVuSans.ttf", 0),
        ("NimbusSans-Regular.otf", 0),
    ],
    "bold_zh": [
        ("NotoSansSC-VF.ttf", 0), ("NotoSansCJKsc-Bold.otf", 0),
        ("SourceHanSansSC-VF.ttf", 0), ("PingFang.ttc", 0),
        ("msyhbd.ttc", 0), ("msyh.ttc", 0), ("simhei.ttf", 0),
        ("Hiragino Sans GB.ttc", 0), ("wqy-zenhei.ttc", 0),
    ],
    "bold_en": [
        ("arialbd.ttf", 0), ("Arial Bold.ttf", 0), ("segoeuib.ttf", 0),
        ("calibrib.ttf", 0), ("LiberationSans-Bold.ttf", 0),
        ("DejaVuSans-Bold.ttf", 0), ("Helvetica.ttc", 0),
        ("NimbusSans-Bold.otf", 0),
    ],
    "mono": [
        ("consola.ttf", 0), ("cour.ttf", 0), ("Menlo.ttc", 0),
        ("DejaVuSansMono.ttf", 0), ("LiberationMono-Regular.ttf", 0),
        ("NotoSansMono-Regular.ttf", 0),
    ],
}

# fontconfig 家族名（Linux/macOS 上用来问系统）
FC_FAMILIES: dict[str, str] = {
    "serif_zh": "Noto Serif CJK SC,Source Han Serif SC,Songti SC,serif",
    "sans_zh": "Noto Sans CJK SC,Source Han Sans SC,PingFang SC,sans-serif",
    "bold_zh": "Noto Sans CJK SC,Source Han Sans SC,PingFang SC,sans-serif:bold",
    "serif_en": "Georgia,DejaVu Serif,serif",
    "sans_en": "Arial,Helvetica,DejaVu Sans,sans-serif",
    "bold_en": "Arial,Helvetica,DejaVu Sans,sans-serif:bold",
    "mono": "Consolas,Menlo,DejaVu Sans Mono,monospace",
}

# 变量字体的字重轴取值（思源系列用 wght，100-900）
VF_LIGHT, VF_REGULAR, VF_MEDIUM, VF_BOLD, VF_BLACK = 300, 400, 500, 700, 900


def font_dirs(extra: list[str | Path] | None = None,
              primary: str | Path | None = None) -> list[Path]:
    """按优先级返回字体目录列表（去重，只保留存在的）。"""
    dirs: list[Path] = []

    def add(p: str | Path | None) -> None:
        if not p:
            return
        path = Path(p).expanduser()
        if path.is_dir() and path not in dirs:
            dirs.append(path)

    add(primary)
    for chunk in os.environ.get("BOOKFORGE_FONTS", "").split(os.pathsep):
        add(chunk)
    add(_CACHE_DIR / "fonts")
    for d in _platform_dirs():
        add(d)
    for d in extra or []:
        add(d)
    return dirs


# ---------------------------------------------------------------- 扫描

def _iter_font_files(d: Path, depth: int = 4):
    """递归找字体文件，但限制深度（Linux 的 /usr/share/fonts 很深）。"""
    if depth <= 0:
        return
    try:
        entries = list(d.iterdir())
    except OSError:
        return
    for p in entries:
        try:
            if p.is_dir():
                yield from _iter_font_files(p, depth - 1)
            elif p.suffix.lower() in (".ttf", ".ttc", ".otf", ".otc"):
                yield p
        except OSError:
            continue


def _index_fonts(dirs: list[Path]) -> dict[str, Path]:
    """建一个 ``文件名小写 → 路径`` 的索引，避免每个角色都重新扫盘。"""
    index: dict[str, Path] = {}
    for d in dirs:
        for p in _iter_font_files(d):
            key = p.name.lower()
            index.setdefault(key, p)
    return index


def _fc_match(pattern: str) -> Path | None:
    """用 fontconfig 问系统要字体（Linux/macOS 通常都装了）。"""
    exe = shutil.which("fc-match")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-f", "%{file}", pattern],
            capture_output=True, timeout=8, check=False,
        )
        cand = out.stdout.decode("utf-8", "ignore").strip()
        if cand and Path(cand).is_file():
            return Path(cand)
    except Exception:
        pass
    return None


def renders_cjk(path: str | Path, face: int = 0) -> bool:
    """这个字体到底能不能画出汉字？（下载/兜底时用来验收）"""
    try:
        from PIL import Image, ImageFont
    except ImportError:
        return True
    try:
        f = ImageFont.truetype(str(path), 32, index=face)
    except Exception:
        return False
    try:
        mask = f.getmask("中")
        return mask.size[0] > 0 and mask.size[1] > 0
    except Exception:
        return False


# ---------------------------------------------------------------- 下载（可选）

# 仅当系统一个中文字体都没有时才用；下载后会校验能否渲染中文
FONT_DOWNLOAD_URLS: dict[str, list[str]] = {
    "sans_zh": [
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/"
        "Sans/Variable/TTF/Subset/NotoSansSC-VF.ttf",
        "https://cdn.jsdelivr.net/gh/notofonts/noto-cjk@main/"
        "Sans/Variable/TTF/Subset/NotoSansSC-VF.ttf",
    ],
    "serif_zh": [
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/"
        "Serif/Variable/TTF/Subset/NotoSerifSC-VF.ttf",
        "https://cdn.jsdelivr.net/gh/notofonts/noto-cjk@main/"
        "Serif/Variable/TTF/Subset/NotoSerifSC-VF.ttf",
    ],
}


def download_font(role: str, *, timeout: float = 60.0) -> Path | None:
    """尝试下载一个中文字体到用户字体缓存。失败返回 None。

    故意做成"尽力而为"：网络不通 / 地址失效都不会影响主流程，
    调用方拿到 None 就走降级排版。
    """
    import urllib.request

    urls = list(FONT_DOWNLOAD_URLS.get(role, []))
    env = os.environ.get("BOOKFORGE_FONT_URL")
    if env:
        urls.insert(0, env)
    if not urls:
        return None

    dest_dir = _CACHE_DIR / "fonts"
    dest_dir.mkdir(parents=True, exist_ok=True)

    for url in urls:
        name = url.rsplit("/", 1)[-1] or f"{role}.ttf"
        dest = dest_dir / name
        if dest.is_file() and renders_cjk(dest):
            return dest
        try:
            log(f"下载中文字体：{url}", "step")
            req = urllib.request.Request(url, headers={"User-Agent": "bookforge"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
        except Exception as e:
            log(f"下载失败（{type(e).__name__}）：{url}", "warn")
            continue
        dest.write_bytes(data)
        if renders_cjk(dest):
            log(f"字体已就绪：{dest}", "ok")
            return dest
        log(f"下载到的文件无法渲染中文，丢弃：{dest}", "warn")
        dest.unlink(missing_ok=True)
    return None


# ---------------------------------------------------------------- 管理器


class FontManager:
    """字体解析、缓存、字重控制。

    比原版多出来的能力：跨平台目录、fontconfig 兜底、找不到中文字体时的
    明确诊断（``diagnose()``）。
    """

    def __init__(self, font_dir: str | Path | None = None,
                 extra_dirs: list[str | Path] | None = None,
                 *, allow_download: bool = False):
        self._primary = font_dir
        self._extra = extra_dirs or []
        self._allow_download = allow_download
        self._dirs: list[Path] | None = None
        self._index: dict[str, Path] | None = None
        self._cache: dict[tuple, object] = {}
        self._resolved: dict[str, str] = {}
        self._faces: dict[str, int] = {}
        self._tried_download: set[str] = set()

    # ---- 索引

    @property
    def dirs(self) -> list[Path]:
        if self._dirs is None:
            self._dirs = font_dirs(self._extra, self._primary)
        return self._dirs

    @property
    def index(self) -> dict[str, Path]:
        if self._index is None:
            self._index = _index_fonts(self.dirs)
        return self._index

    def search_dirs(self) -> list[Path]:
        return self.dirs

    # ---- 解析

    def resolve(self, role: str) -> str:
        """把角色名解析成实际字体文件路径。解析失败抛 FileNotFoundError。"""
        if role in self._resolved:
            return self._resolved[role]

        for name, face in CANDIDATES.get(role, []):
            p = self.index.get(name.lower())
            if p is not None:
                if role.endswith("_zh") and not renders_cjk(p, face):
                    continue
                self._resolved[role] = str(p)
                self._faces[role] = face
                return str(p)

        # fontconfig 兜底
        pat = FC_FAMILIES.get(role)
        if pat:
            p = _fc_match(pat)
            if p is not None and (not role.endswith("_zh") or renders_cjk(p)):
                self._resolved[role] = str(p)
                self._faces[role] = 0
                return str(p)

        # 可选下载
        if self._allow_download and role not in self._tried_download:
            self._tried_download.add(role)
            p = download_font(role)
            if p is not None:
                self._resolved[role] = str(p)
                self._faces[role] = 0
                self._index = None          # 让新字体进索引
                return str(p)

        raise FileNotFoundError(f"找不到可用字体（角色 {role}）")

    def get(self, role: str, size: int, weight: int | None = None):
        key = (role, size, weight)
        if key in self._cache:
            return self._cache[key]
        from PIL import ImageFont

        path = self.resolve(role)
        font = ImageFont.truetype(path, size, index=self._faces.get(role, 0))
        if weight is not None:
            set_weight(font, weight)
        self._cache[key] = font
        return font

    def has(self, role: str) -> bool:
        try:
            self.resolve(role)
            return True
        except FileNotFoundError:
            return False

    def path_of(self, role: str) -> str | None:
        try:
            return self.resolve(role)
        except FileNotFoundError:
            return None

    # ---- 诊断

    def diagnose(self) -> dict:
        """给 ``bookforge doctor`` 用的体检报告。"""
        report: dict = {
            "font_dirs": [str(d) for d in self.dirs],
            "roles": {},
            "missing": [],
            "cjk_ok": True,
        }
        for role in CANDIDATES:
            p = self.path_of(role)
            report["roles"][role] = p
            if p is None:
                report["missing"].append(role)
        for role in ("sans_zh", "serif_zh", "bold_zh"):
            p = report["roles"].get(role)
            if not p or not renders_cjk(p, self._faces.get(role, 0)):
                report["cjk_ok"] = False
                break
        return report


def set_weight(font, weight: int) -> bool:
    """给可变字体设置字重。失败静默返回 False（调用方用合成加粗兜底）。"""
    try:
        axes = font.get_variation_axes()
        if not axes:
            return False
        for ax in axes:
            name = ax.get("name", b"")
            if isinstance(name, bytes):
                name = name.decode("utf-8", "ignore")
            if "wght" in name.lower() or "weight" in name.lower():
                lo, hi = ax["minimum"], ax["maximum"]
                font.set_variation_by_axes([max(lo, min(hi, weight))])
                return True
    except Exception:
        pass
    return False


# 兼容旧代码：模块级默认管理器（cover.py 直接用它）
FM = FontManager()


def font_install_hint() -> str:
    """针对当前平台，给出"怎么装中文字体"的具体命令。"""
    if sys.platform.startswith("win"):
        return ("Windows 请安装中文语言包（设置 → 时间和语言 → 语言），"
                "或把任意中文字体 .ttf 放进 ~/.cache/bookforge/fonts/")
    if sys.platform == "darwin":
        return "macOS 自带 PingFang；若缺失请检查 /System/Library/Fonts/PingFang.ttc"
    return ("Linux 执行：sudo apt install fonts-noto-cjk"
            "（或 dnf install google-noto-sans-cjk-fonts），"
            "或把字体放进 ~/.cache/bookforge/fonts/")


# 旧名兼容（原 cover.py 里的私有名）
_set_weight = set_weight
