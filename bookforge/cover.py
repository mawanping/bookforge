"""封面渲染引擎。

关键原则（也是踩过坑的地方）：
    文生图模型画文字必然出错 —— 字母缺失、拼写错乱、笔画粘连。
    所以封面拆成两层：
        背景图  ← AI 生成，提示词里明确要求"不要任何文字"
        文字层  ← 程序排版，字体、字号、断行、字距、遮罩全部可控

本模块只负责第二层，外加一个程序化背景作为兜底（不依赖任何外部服务）。
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .fonts import (FM, VF_BLACK, VF_BOLD, VF_LIGHT, VF_MEDIUM,  # noqa: F401
                    VF_REGULAR, FontManager, font_install_hint,
                    renders_cjk, set_weight)
from .utils import ensure_dir, log, now_iso

# ---------------------------------------------------------------- 字体

# 字体发现逻辑全部搬到了 .fonts（跨平台：Windows / macOS / Linux / 容器）。
# 这里保留一个可配置的全局管理器，CLI 可以通过 configure_fonts() 覆盖。


def configure_fonts(font_dir: str | Path | None = None,
                    extra_dirs: list[str | Path] | None = None,
                    *, allow_download: bool = False) -> FontManager:
    """替换模块级字体管理器（``bookforge build --font-dir`` 会调它）。"""
    global FM
    FM = FontManager(font_dir, extra_dirs, allow_download=allow_download)
    return FM


_set_weight = set_weight      # 旧名兼容


# ---------------------------------------------------------------- 断行

# CJK 字符、CJK 标点、拉丁词、空白、其他
_TOKEN_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af]"
    r"|[\u3000-\u303f\uff00-\uffef\u2018\u2019\u201c\u201d\u2014\u2026]"
    r"|[A-Za-z0-9]+(?:['\u2019\-][A-Za-z0-9]+)*"
    r"|\s+"
    r"|.",
    re.UNICODE,
)

# 不允许出现在行首的字符（中文避头尾）
_NO_LINE_START = set("，。、；：？！）】》」』’”…—·%‰℃,.;:?!)]}>\"'")
# 不允许出现在行尾的字符
_NO_LINE_END = set("（【《「『‘“([{<")


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text) if t]


def measure(text: str, font: ImageFont.FreeTypeFont) -> float:
    """精确文本宽度。"""
    if not text:
        return 0.0
    try:
        return font.getlength(text)
    except Exception:
        bbox = font.getbbox(text)
        return bbox[2] - bbox[0]


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    """中英混排断行，带基本避头尾处理。"""
    lines: list[str] = []
    for raw_line in text.split("\n"):
        if not raw_line.strip():
            lines.append("")
            continue
        tokens = tokenize(raw_line)
        cur = ""
        for tok in tokens:
            candidate = cur + tok
            if measure(candidate, font) <= max_width or not cur:
                cur = candidate
                continue
            # 需要换行
            stripped = cur.rstrip()
            # 避头：行首不能是可恶的标点 → 把它推到下一行
            if stripped and stripped[-1] in _NO_LINE_START:
                moved = stripped[-1]
                stripped = stripped[:-1].rstrip()
                if stripped:
                    lines.append(stripped)
                    cur = moved + tok
                else:
                    lines.append(cur.rstrip())
                    cur = tok
                continue
            # 避尾：行尾不能是开括号 → 把它推到下一行
            if stripped and stripped[-1] in _NO_LINE_END:
                moved = stripped[-1]
                stripped = stripped[:-1].rstrip()
                if stripped:
                    lines.append(stripped)
                    cur = moved + tok
                else:
                    lines.append(cur.rstrip())
                    cur = tok
                continue
            # 超长单词：硬断
            if not stripped:
                lines.append(cur)
                cur = tok
                continue
            lines.append(stripped)
            cur = tok.lstrip() if tok.strip() else ""
        if cur.strip() or (not lines):
            lines.append(cur.rstrip())
    return [ln for ln in lines if ln is not None]


def _merge_latin_tokens(tokens: list[str]) -> list[str]:
    """把「拉丁词 空格 拉丁词」序列合并成不可拆的复合 token。

    平衡断行按宽度均分，会把 "Paul Graham 文章选读" 断成
    "Paul / Graham 文章选读"。人名/词组是不能拆的，所以先把
    相邻拉丁词粘成一个单元，只允许在词组边界换行。
    """
    def is_word(t: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9]+(?:['\u2019\-][A-Za-z0-9]+)*", t))

    out: list[str] = []
    i = 0
    while i < len(tokens):
        if is_word(tokens[i]):
            j = i
            while (j + 2 < len(tokens) and is_word(tokens[j])
                   and tokens[j + 1].strip() == "" and is_word(tokens[j + 2])):
                j += 2
            out.append("".join(tokens[i:j + 1]))
            i = j + 1
        else:
            out.append(tokens[i])
            i += 1
    return out


def _balanced_split(text: str, font: ImageFont.FreeTypeFont,
                    max_width: float, n: int) -> list[str] | None:
    """把单段文本均匀分成 n 行（行宽尽量接近），失败返回 None。

    标题断行用"平衡"而不是贪心：贪心会产生
    「Paul Graham 文章 / 选读」这种孤字尾行，平衡后是
    「Paul Graham / 文章选读」，观感好很多。
    """
    if n <= 1:
        return None
    tokens = _merge_latin_tokens(tokenize(text))
    if len(tokens) < n:
        return None
    total = sum(measure(t, font) for t in tokens)
    target = total / n
    rows: list[str] = []
    cur, cur_w = "", 0.0
    for tok in tokens:
        w = measure(tok, font)
        if cur and cur_w + w > max_width:
            rows.append(cur.rstrip())
            cur = tok.lstrip() if tok.strip() else ""
            cur_w = measure(cur, font)
            continue
        # 未到最后一行时，超过目标宽度就换行（留 6% 容差）
        if cur and len(rows) < n - 1 and cur_w + w > target * 1.06:
            rows.append(cur.rstrip())
            cur = tok.lstrip() if tok.strip() else ""
            cur_w = measure(cur, font)
            continue
        cur += tok
        cur_w += measure(tok, font)
    if cur.strip():
        rows.append(cur.rstrip())
    if len(rows) != n:
        return None
    if any(measure(r, font) > max_width for r in rows):
        return None
    # 避头尾：行首不能是收尾标点
    if any(r[0] in _NO_LINE_START for r in rows[1:] if r):
        return None
    return rows


def fit_font(role: str, text: str, max_width: float, max_lines: int,
             base_size: int, min_size: int = 24, *,
             weight: int | None = None, line_ratio: float = 1.28,
             allow_wrap: bool = True,
             balanced: bool = True) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """自适应字号：在给定行数内塞下文本，尽量用大字号。

    行数 > 1 时默认尝试平衡断行，避免孤字尾行。
    """
    size = base_size
    while size >= min_size:
        font = FM.get(role, size, weight)
        lines = wrap_text(text, font, max_width) if allow_wrap else [text]
        if len(lines) <= max_lines and all(
                measure(ln, font) <= max_width + 1 for ln in lines):
            if balanced and len(lines) > 1 and "\n" not in text:
                alt = _balanced_split(text, font, max_width, len(lines))
                if alt:
                    lines = alt
            return font, lines
        size = int(size * 0.94)
    font = FM.get(role, min_size, weight)
    lines = wrap_text(text, font, max_width) if allow_wrap else [text]
    if balanced and len(lines) > 1 and "\n" not in text:
        alt = _balanced_split(text, font, max_width, len(lines))
        if alt:
            lines = alt
    return font, lines


# ---------------------------------------------------------------- 文本绘制

def draw_lines(draw: ImageDraw.ImageDraw, xy: tuple[float, float],
               lines: list[str], font: ImageFont.FreeTypeFont, *,
               fill, align: str = "left", box_width: float | None = None,
               line_ratio: float = 1.28, letter_spacing: float = 0.0,
               stroke_width: int = 0, stroke_fill=None,
               shadow: tuple[int, tuple] | None = None) -> tuple[float, float]:
    """绘制多行文本，返回 (占用高度, 块宽)。

    xy 是文本块左上角。
    align 只决定每行在 box_width 内的水平位置：
        left   → 全部靠左
        center → 在 box_width 内居中（box_width 缺省用块自身宽度）
        right  → 靠 box_width 右缘
    """
    x, y = xy
    ascent, descent = font.getmetrics()
    line_h = int((ascent + descent) * line_ratio)
    widths = [measure(ln, font) + letter_spacing * max(len(ln) - 1, 0)
              for ln in lines]
    block_w = max(widths, default=0)
    frame = block_w if box_width is None else max(box_width, block_w)

    for i, (ln, lw) in enumerate(zip(lines, widths)):
        if not ln:
            continue
        if align == "center":
            lx = x + (frame - lw) / 2
        elif align == "right":
            lx = x + frame - lw
        else:
            lx = x
        ly = y + i * line_h
        if shadow:
            off, col = shadow
            _draw_line(draw, (lx + off, ly + off), ln, font, col,
                       letter_spacing, stroke_width, None)
        _draw_line(draw, (lx, ly), ln, font, fill, letter_spacing,
                   stroke_width, stroke_fill)
    return len(lines) * line_h, block_w


def _draw_line(draw, xy, text, font, fill, letter_spacing, stroke_width,
               stroke_fill) -> None:
    if not text:
        return
    if letter_spacing <= 0:
        draw.text(xy, text, font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        return
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        x += measure(ch, font) + letter_spacing


# ---------------------------------------------------------------- 遮罩与背景

def add_gradient(img: Image.Image, *, direction: str = "bottom",
                 start: float = 0.42, strength: float = 0.86,
                 color: tuple[int, int, int] = (0, 0, 0)) -> Image.Image:
    """叠加渐变遮罩，保证文字区有足够对比度。"""
    w, h = img.size
    base = np.asarray(img).astype(np.float32)
    if direction in ("bottom", "top"):
        n = h
        t = np.linspace(0.0, 1.0, n, dtype=np.float32)
        if direction == "top":
            t = t[::-1]
        # 前 start 段不遮，之后平滑上升到 strength
        alpha = np.clip((t - start) / max(1e-6, 1.0 - start), 0, 1) ** 1.15
        alpha = alpha * strength
        mask = alpha[:, None, None]
    else:  # left / right
        n = w
        t = np.linspace(0.0, 1.0, n, dtype=np.float32)
        if direction == "left":
            t = t[::-1]
        alpha = np.clip((t - start) / max(1e-6, 1.0 - start), 0, 1) ** 1.15
        alpha = alpha * strength
        mask = alpha[None, :, None]

    overlay = np.array(color, dtype=np.float32)[None, None, :]
    if direction in ("bottom", "top"):
        out = base * (1 - mask) + overlay * mask
    else:
        out = base * (1 - mask) + overlay * mask
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def add_vignette(img: Image.Image, strength: float = 0.35) -> Image.Image:
    """四角压暗，增加纵深。"""
    w, h = img.size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w / 2, h / 2
    d = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
    mask = np.clip((d - 0.55) / 1.25, 0, 1) ** 1.6 * strength
    base = np.asarray(img).astype(np.float32)
    out = base * (1 - mask[:, :, None])
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def region_luma(img: Image.Image, box: tuple[int, int, int, int]) -> float:
    """区域平均亮度（0-255）。"""
    w, h = img.size
    x0, y0, x1, y1 = [int(max(0, v)) for v in box]
    x1, y1 = min(x1, w), min(y1, h)
    if x1 <= x0 or y1 <= y0:
        return 128.0
    crop = np.asarray(img.crop((x0, y0, x1, y1)).convert("L"), dtype=np.float32)
    return float(crop.mean()) if crop.size else 128.0


def auto_text_color(img: Image.Image, box, *, dark=(250, 248, 244),
                    light=(20, 22, 26), threshold: float = 138.0
                    ) -> tuple[int, int, int]:
    """按区域亮度自动决定用深字还是浅字。"""
    return light if region_luma(img, box) > threshold else dark


def derive_palette(seed: str, style: str = "editorial") -> dict:
    """从书名稳定地推导一套配色（同名书永远同色）。"""
    rnd = random.Random(sum(ord(c) * (i + 7) for i, c in enumerate(seed)))
    schemes = [
        {"bg": "#0d1b2a", "accent": "#c9a227", "text": "#f6f2e8"},   # 深蓝金
        {"bg": "#1a1512", "accent": "#d97757", "text": "#f3ece4"},   # 墨棕赤陶
        {"bg": "#12211c", "accent": "#9dbf9e", "text": "#eef2ec"},   # 松绿
        {"bg": "#1c1a26", "accent": "#b8a1d9", "text": "#f2eef8"},   # 夜紫
        {"bg": "#241a1a", "accent": "#e0b0a0", "text": "#f7f0ec"},   # 胭脂
        {"bg": "#f3efe6", "accent": "#8c3b2e", "text": "#221f1b"},   # 米白朱砂
        {"bg": "#e8edf2", "accent": "#2f5d8a", "text": "#182430"},   # 冷灰靛青
        {"bg": "#0f0f10", "accent": "#7fd1c1", "text": "#f0f0f0"},   # 纯黑薄荷
    ]
    return rnd.choice(schemes)


def hex_to_rgb(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore


def relative_luma(c: tuple[int, int, int]) -> float:
    r, g, b = [v / 255 for v in c]
    f = lambda x: x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


# ---------------------------------------------------------------- 程序化背景

def generate_background(w: int, h: int, palette: dict, *,
                        style: str = "editorial", seed: str = "",
                        variant: int = 0) -> Image.Image:
    """不依赖任何外部服务，程序生成一个有质感的抽象背景。

    用于两种情况：没有图生图能力时兜底，或者想要更可控的极简风。
    """
    rnd = random.Random(hash((seed, variant)) & 0xFFFFFFFF)
    bg = hex_to_rgb(palette.get("bg", "#0d1b2a"))
    accent = hex_to_rgb(palette.get("accent", "#c9a227"))
    text = hex_to_rgb(palette.get("text", "#f6f2e8"))

    img = Image.new("RGB", (w, h), bg)

    # 1) 大尺度柔和光斑
    blob = Image.new("L", (w, h), 0)
    bd = ImageDraw.Draw(blob)
    for _ in range(rnd.randint(3, 5)):
        cx = rnd.uniform(0.05, 0.95) * w
        cy = rnd.uniform(0.02, 0.75) * h
        r = rnd.uniform(0.24, 0.55) * w
        bd.ellipse([cx - r, cy - r * 0.85, cx + r, cy + r * 0.85],
                   fill=rnd.randint(70, 165))
    blob = blob.filter(ImageFilter.GaussianBlur(w * 0.11))
    tint = Image.new("RGB", (w, h), accent)
    img = Image.composite(Image.blend(img, tint, 0.30), img, blob)

    # 2) 几何结构（不同风格给不同纹理）
    d = ImageDraw.Draw(img, "RGBA")
    if style == "minimal":
        # 一条细横线 + 一个大留白
        y = int(h * 0.615)
        d.line([(w * 0.5 - w * 0.06, y), (w * 0.5 + w * 0.06, y)],
               fill=accent + (200,), width=max(2, w // 700))
    elif style == "geometric":
        for _ in range(rnd.randint(5, 8)):
            x = rnd.uniform(-0.1, 0.9) * w
            y = rnd.uniform(0.0, 0.9) * h
            side = rnd.uniform(0.10, 0.34) * w
            rot = rnd.uniform(0, 90)
            layer = Image.new("RGBA", (int(side * 1.6), int(side * 1.6)), (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            col = accent if rnd.random() > 0.45 else text
            alpha = rnd.randint(16, 46)
            ld.regular_polygon((layer.width / 2, layer.height / 2,
                                side / 2), rnd.choice([3, 4, 6]), rotation=rot,
                               fill=col + (alpha,))
            layer = layer.rotate(rnd.uniform(0, 360), resample=Image.BICUBIC)
            img.paste(layer, (int(x), int(y)), layer)
    else:  # editorial：斜向色块
        for _ in range(rnd.randint(2, 4)):
            y0 = rnd.uniform(0.55, 1.0) * h
            d.polygon([(0, y0), (w, y0 - rnd.uniform(-0.18, 0.18) * h),
                       (w, h), (0, h)],
                      fill=accent + (rnd.randint(14, 30),))

    # 3) 细腻噪点，去掉「数字生成」的塑料感
    noise = np.random.default_rng(abs(hash(seed)) % (2 ** 32)).normal(
        0, 5.2, (h, w, 1)).repeat(3, axis=2)
    arr = np.asarray(img).astype(np.float32) + noise
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    return add_vignette(img, 0.42 if relative_luma(bg) < 0.4 else 0.18)


# ---------------------------------------------------------------- 布局定义

@dataclass
class CoverSpec:
    """封面文字层规格。所有几何量都是 1600x2400 基准下的像素值。"""

    title: str
    subtitle: str = ""
    author: str = ""
    series: str = ""
    publisher: str = ""
    tagline: str = ""
    style: str = "editorial"        # editorial | classic | minimal | band
    title_role: str = "serif_zh"    # serif_zh | sans_zh
    title_weight: int | None = VF_BOLD
    align: str = "left"             # left | center
    letter_spacing: float = 0.0
    accent_bar: bool = True
    palette: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)


W, H = 1600, 2400
MARGIN_X = 168
CONTENT_W = W - MARGIN_X * 2


# ---------------------------------------------------------------- 渲染

def render_cover(spec: CoverSpec, background: Image.Image | None = None,
                 *, size: tuple[int, int] = (W, H), seed: str = "") -> Image.Image:
    """渲染封面。background 为 None 时程序生成。"""
    w, h = size
    scale = w / W

    if background is None:
        background = generate_background(w, h, spec.palette or {}, style=spec.style,
                                         seed=seed or spec.title)
    else:
        background = _cover_fit(background, (w, h))

    img = background.copy()
    text_col = hex_to_rgb(spec.palette.get("text", "#f6f2e8"))
    accent = hex_to_rgb(spec.palette.get("accent", "#c9a227"))
    bg_col = hex_to_rgb(spec.palette.get("bg", "#0d1b2a"))

    # 文字区遮罩：保证对比度
    if spec.style in ("editorial", "classic"):
        img = add_gradient(img, direction="bottom", start=0.40, strength=0.90,
                           color=bg_col if relative_luma(bg_col) < 0.4
                           else (255, 255, 255))
        if spec.style == "classic":
            img = add_gradient(img, direction="top", start=0.72, strength=0.55,
                               color=bg_col if relative_luma(bg_col) < 0.4
                               else (255, 255, 255))
    elif spec.style == "band":
        img = add_gradient(img, direction="bottom", start=0.30, strength=0.80,
                           color=bg_col)
    else:  # minimal
        img = add_gradient(img, direction="bottom", start=0.55, strength=0.55,
                           color=bg_col)

    draw = ImageDraw.Draw(img, "RGBA")
    m = int(MARGIN_X * scale)
    cw = w - m * 2

    if spec.style == "editorial":
        _layout_editorial(draw, img, spec, m, cw, w, h, scale, text_col, accent)
    elif spec.style == "classic":
        _layout_classic(draw, img, spec, m, cw, w, h, scale, text_col, accent)
    elif spec.style == "minimal":
        _layout_minimal(draw, img, spec, m, cw, w, h, scale, text_col, accent)
    else:
        _layout_band(draw, img, spec, m, cw, w, h, scale, text_col, accent)

    return img


def _cover_fit(src: Image.Image, size: tuple[int, int]) -> Image.Image:
    """等比裁切填满目标尺寸（cover 语义）。"""
    tw, th = size
    sw, sh = src.size
    if (sw, sh) == (tw, th):
        return src.convert("RGB")
    scale = max(tw / sw, th / sh)
    nw, nh = max(1, int(sw * scale + 0.5)), max(1, int(sh * scale + 0.5))
    resized = src.convert("RGB").resize((nw, nh), Image.LANCZOS)
    left = (nw - tw) // 2
    top = int((nh - th) * 0.38)     # 略偏上，保留主体
    top = max(0, min(top, nh - th))
    return resized.crop((left, top, left + tw, top + th))


# ---------------------------------------------------------------- 各风格布局

def _style_cfg(spec: CoverSpec) -> dict:
    """风格参数：字号基准、字距、行数上限。"""
    cfg = {
        "editorial": dict(t=132, t_max=5, sub=54, au=46, ls=0.0, lr=1.16),
        "classic":   dict(t=112, t_max=4, sub=48, au=42, ls=0.0, lr=1.22),
        "minimal":   dict(t=88,  t_max=4, sub=40, au=36, ls=6.0, lr=1.34),
        "band":      dict(t=118, t_max=4, sub=46, au=40, ls=0.0, lr=1.20),
    }[spec.style]
    cfg.update(spec.overrides.get("cfg", {}))
    return cfg


def _cjk_role(role: str, text: str) -> str:
    """文本含中日韩字符时，把拉丁字体角色换成对应的 CJK 角色。

    版式里作者/出版方行原本固定用 `sans_en`（Arial）排版，遇到中文作者名会
    整行渲染成"豆腐块"（□□）。中文字体同样包含拉丁字形，所以只要文本里有
    汉字就换掉，没有任何副作用。
    """
    has_cjk = any(
        "\u3400" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff"
        or "\uac00" <= ch <= "\ud7af" for ch in (text or ""))
    if has_cjk:
        return {"sans_en": "sans_zh", "serif_en": "serif_zh",
                "bold_en": "bold_zh"}.get(role, role)
    return role


def _layout_editorial(draw, img, spec, m, cw, w, h, scale, text_col, accent):
    """大标题压在中下部，左对齐或居中，杂志感。"""
    cfg = _style_cfg(spec)
    align = spec.align
    x = m

    # 从下往上堆叠：作者 → 副标题 → 标题，整体底部锚定
    bottom = h - int(210 * scale)
    cursor = bottom

    if spec.publisher:
        f = FM.get(_cjk_role("sans_en", spec.publisher), int(30 * scale), VF_MEDIUM)
        cursor -= int(40 * scale)
        draw_lines(draw, (x, cursor), [spec.publisher.upper()], f,
                   fill=text_col + (170,), align=align, box_width=cw,
                   letter_spacing=3.0 * scale)

    if spec.author:
        f = FM.get(_cjk_role("sans_en", spec.author), int(cfg["au"] * scale), VF_MEDIUM)
        lines = wrap_text(spec.author, f, cw)
        cursor -= int(cfg["au"] * scale * 1.5)
        draw_lines(draw, (x, cursor), lines, f, fill=text_col,
                   align=align, box_width=cw,
                   letter_spacing=1.2 * scale)

    if spec.subtitle:
        f = FM.get("sans_zh", int(cfg["sub"] * scale), VF_LIGHT)
        lines = wrap_text(spec.subtitle, f, cw)
        cursor -= int(cfg["sub"] * scale * 1.9)
        draw_lines(draw, (x, cursor), lines, f, fill=text_col + (225,),
                   align=align, box_width=cw, line_ratio=1.34)

    # 强调线
    if spec.accent_bar:
        tw = min(cw, 240 * scale)
        cursor -= int(52 * scale)
        if align == "center":
            x0 = m + (cw - tw) / 2
        else:
            x0 = m
        draw.rectangle([x0, cursor, x0 + tw, cursor + max(3, int(5 * scale))],
                       fill=accent + (235,))

    # 标题
    f, lines = fit_font(spec.title_role, spec.title, cw, cfg["t_max"],
                        int(cfg["t"] * scale), int(46 * scale),
                        weight=spec.title_weight, line_ratio=cfg["lr"])
    ascent, descent = f.getmetrics()
    lh = int((ascent + descent) * cfg["lr"])
    cursor -= len(lines) * lh + int(28 * scale)
    draw_lines(draw, (x, cursor), lines, f, fill=text_col, align=align,
               box_width=cw, line_ratio=cfg["lr"],
               letter_spacing=spec.letter_spacing * scale,
               shadow=(3, (0, 0, 0, 140)))


def _layout_classic(draw, img, spec, m, cw, w, h, scale, text_col, accent):
    """上下留白对称，标题居中偏上，作者贴近底部，最像正式出版物。"""
    cfg = _style_cfg(spec)
    x = m          # 居中一律通过 align + box_width 表达

    # 顶部：系列名
    if spec.series:
        f = FM.get("sans_zh", int(32 * scale), VF_MEDIUM)
        draw_lines(draw, (x, int(140 * scale)), [spec.series], f,
                   fill=text_col + (190,), align="center", box_width=cw,
                   letter_spacing=4.0 * scale)

    # 中部：标题块（垂直居中偏下一点）
    f, lines = fit_font(spec.title_role, spec.title, cw, cfg["t_max"],
                        int(cfg["t"] * scale), int(44 * scale),
                        weight=spec.title_weight, line_ratio=cfg["lr"])
    ascent, descent = f.getmetrics()
    lh = int((ascent + descent) * cfg["lr"])
    block_h = len(lines) * lh

    sub_lines = []
    if spec.subtitle:
        fsub, sub_lines = fit_font("sans_zh", spec.subtitle, cw, 2,
                                   int(cfg["sub"] * scale), int(30 * scale),
                                   weight=VF_LIGHT, line_ratio=1.34)
        sub_lh = int(sum(fsub.getmetrics()) * 1.34)
    else:
        sub_lh = 0

    total = block_h + (int(44 * scale) + len(sub_lines) * sub_lh if sub_lines else 0)
    y = int(h * 0.46) - total // 2

    if spec.accent_bar:
        bar_w = int(120 * scale)
        draw.rectangle([m + (cw - bar_w) / 2, y - int(72 * scale),
                        m + (cw + bar_w) / 2, y - int(72 * scale) + max(3, int(5 * scale))],
                       fill=accent + (235,))

    draw_lines(draw, (x, y), lines, f, fill=text_col, align="center",
               box_width=cw, line_ratio=cfg["lr"],
               letter_spacing=spec.letter_spacing * scale,
               shadow=(3, (0, 0, 0, 130)))
    y += block_h

    if sub_lines:
        y += int(42 * scale)
        draw_lines(draw, (x, y), sub_lines, fsub, fill=text_col + (222,),
                   align="center", box_width=cw, line_ratio=1.34)
        y += len(sub_lines) * sub_lh

    # 底部：作者 + 出版方
    if spec.author:
        f = FM.get(_cjk_role("sans_en", spec.author), int(cfg["au"] * scale), VF_MEDIUM)
        draw_lines(draw, (x, h - int(250 * scale)), [spec.author], f,
                   fill=text_col, align="center", box_width=cw,
                   letter_spacing=2.4 * scale)
    if spec.publisher:
        f = FM.get(_cjk_role("sans_en", spec.publisher), int(26 * scale), VF_REGULAR)
        draw_lines(draw, (x, h - int(168 * scale)), [spec.publisher.upper()], f,
                   fill=text_col + (150,), align="center", box_width=cw,
                   letter_spacing=3.5 * scale)


def _layout_minimal(draw, img, spec, m, cw, w, h, scale, text_col, accent):
    """极简：大留白、细字重、宽字距，标题几乎在正中。"""
    cfg = _style_cfg(spec)
    align = spec.align
    x = m

    f, lines = fit_font(spec.title_role, spec.title, cw, cfg["t_max"],
                        int(cfg["t"] * scale), int(38 * scale),
                        weight=VF_LIGHT, line_ratio=cfg["lr"])
    ascent, descent = f.getmetrics()
    lh = int((ascent + descent) * cfg["lr"])
    block_h = len(lines) * lh

    y = int(h * 0.5) - block_h // 2
    draw_lines(draw, (x, y), lines, f, fill=text_col, align=align,
               box_width=cw, line_ratio=cfg["lr"],
               letter_spacing=(spec.letter_spacing or 8.0) * scale)

    y += block_h + int(56 * scale)

    if spec.subtitle:
        fsub, sl = fit_font("sans_zh", spec.subtitle, cw, 2,
                            int(cfg["sub"] * scale), int(26 * scale),
                            weight=VF_LIGHT, line_ratio=1.36)
        draw_lines(draw, (x, y), sl, fsub, fill=text_col + (200,),
                   align=align, box_width=cw, line_ratio=1.36)
        y += len(sl) * int(sum(fsub.getmetrics()) * 1.36)

    if spec.accent_bar:
        tw = int(64 * scale)
        x0 = m + (cw - tw) / 2 if align == "center" else m
        draw.rectangle([x0, y + int(34 * scale), x0 + tw,
                        y + int(34 * scale) + max(2, int(3 * scale))],
                       fill=accent + (220,))

    if spec.author:
        f = FM.get(_cjk_role("sans_en", spec.author), int(cfg["au"] * scale), VF_LIGHT)
        draw_lines(draw, (x, h - int(300 * scale)), [spec.author], f,
                   fill=text_col + (215,), align=align, box_width=cw,
                   letter_spacing=4.0 * scale)


def _layout_band(draw, img, spec, m, cw, w, h, scale, text_col, accent):
    """色带式：中下部一条半透明横带，文字压在带上。"""
    cfg = _style_cfg(spec)
    align = spec.align

    # 预先算好标题行数，决定色带高度
    f, lines = fit_font(spec.title_role, spec.title, cw, cfg["t_max"],
                        int(cfg["t"] * scale), int(42 * scale),
                        weight=spec.title_weight, line_ratio=cfg["lr"])
    ascent, descent = f.getmetrics()
    lh = int((ascent + descent) * cfg["lr"])

    pad_top = int(76 * scale)
    pad_bottom = int(72 * scale)
    sub_lh = 0
    fsub = None
    sub_lines: list[str] = []
    if spec.subtitle:
        fsub, sub_lines = fit_font("sans_zh", spec.subtitle, cw, 2,
                                   int(cfg["sub"] * scale), int(28 * scale),
                                   weight=VF_LIGHT, line_ratio=1.34)
        sub_lh = len(sub_lines) * int(sum(fsub.getmetrics()) * 1.34)

    band_h = pad_top + len(lines) * lh + (int(30 * scale) + sub_lh if sub_lines else 0) \
             + pad_bottom
    band_top = int(h * 0.56)
    band_top = min(band_top, h - band_h - int(180 * scale))
    band_bottom = band_top + band_h

    overlay = Image.new("RGBA", (w, band_bottom - band_top),
                        hex_to_rgb(spec.palette.get("bg", "#0d1b2a")) + (216,))
    img.paste(overlay, (0, band_top), overlay)

    # 带内强调条
    if spec.accent_bar:
        draw.rectangle([0, band_top, max(6, int(9 * scale)), band_bottom],
                       fill=accent + (255,))

    x = m
    y = band_top + pad_top
    draw_lines(draw, (x, y), lines, f, fill=text_col, align=align,
               box_width=cw, line_ratio=cfg["lr"],
               letter_spacing=spec.letter_spacing * scale)
    y += len(lines) * lh

    if sub_lines and fsub is not None:
        y += int(30 * scale)
        draw_lines(draw, (x, y), sub_lines, fsub, fill=text_col + (218,),
                   align=align, box_width=cw, line_ratio=1.34)

    if spec.author:
        f = FM.get(_cjk_role("sans_en", spec.author), int(cfg["au"] * scale), VF_MEDIUM)
        draw_lines(draw, (x, band_bottom + int(52 * scale)), [spec.author], f,
                   fill=text_col + (230,), align=align, box_width=cw,
                   letter_spacing=2.6 * scale)


# ---------------------------------------------------------------- 输出

def save_cover(img: Image.Image, out_dir: str | Path, *,
               basename: str = "cover", quality: int = 92) -> dict[str, Path]:
    """保存封面 PNG + JPEG（EPUB 用 JPEG 更省体积）。"""
    out_dir = ensure_dir(Path(out_dir))
    png = out_dir / f"{basename}.png"
    jpg = out_dir / f"{basename}.jpg"
    img.save(png, "PNG", optimize=True)
    img.convert("RGB").save(jpg, "JPEG", quality=quality, optimize=True,
                            progressive=True)
    # 缩略图，方便快速预览
    thumb = img.copy()
    thumb.thumbnail((480, 720), Image.LANCZOS)
    tp = out_dir / f"{basename}-thumb.jpg"
    thumb.convert("RGB").save(tp, "JPEG", quality=86)
    return {"png": png, "jpg": jpg, "thumb": tp}


# ---------------------------------------------------------------- 封面提示词

def build_cover_prompt(spec: CoverSpec, *, style_hint: str = "") -> str:
    """生成给文生图模型的提示词。

    重点：反复强调"不要任何文字"。这是踩过的坑 ——
    模型画字几乎必错，不如让它专心画背景。
    """
    palette = spec.palette or {}
    mood = {
        "editorial": "modern editorial publishing aesthetic, bold and confident, "
                     "high-end book jacket",
        "classic": "timeless classic literature cover, refined and restrained, "
                   "museum-quality restraint",
        "minimal": "ultra minimal, enormous negative space, quiet and contemplative",
        "band": "contemporary trade paperback, graphic and clean",
    }.get(spec.style, "modern book cover")

    subject = style_hint or (
        f"an abstract cover artwork for a book titled in the spirit of "
        f"\"{_short(spec.title, 60)}\""
    )

    prompt = (
        f"{subject}. {mood}. "
        f"Color palette: dominant {palette.get('bg', 'deep navy')}, "
        f"accent {palette.get('accent', 'muted gold')}. "
        f"Vertical portrait composition, 2:3 aspect ratio, "
        f"generous empty space in the upper and lower thirds for the title to be "
        f"placed later. Rich texture, subtle grain, cinematic soft lighting, "
        f"professional book cover art, high detail, print quality. "
        f"IMPORTANT: absolutely no text, no letters, no words, no typography, "
        f"no numbers, no logos, no watermarks, no signature, no captions — "
        f"pure imagery only. Do not attempt to render any title or author name."
    )
    return prompt


def _short(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- 高层 API
#
# 下面这些函数把「封面」这一整件事收成一个调用，cli.py 和外部 agent
# 都只依赖它们，不需要知道 CoverSpec 的细节。

STYLES = ["auto", "editorial", "classic", "minimal", "band"]


def recommend_style(title: str, subtitle: str = "", description: str = "",
                    n_articles: int = 0, words: int = 0) -> str:
    """按书名与体量给一个默认风格（用户可覆盖）。"""
    t = f"{title} {subtitle} {description}".lower()
    if any(k in t for k in ("essay", "文章", "随笔", "选集", "collection", "选读")):
        return "editorial"
    if any(k in t for k in ("handbook", "guide", "手册", "指南", "教程", "notes")):
        return "band"
    if any(k in t for k in ("poem", "诗", "meditation", "禅", "静")):
        return "minimal"
    if words > 120000:
        return "classic"
    return "editorial" if n_articles > 4 else "classic"


def auto_title_role(title: str, style: str) -> str:
    """中文标题用宋体更有出版感，纯英文/短标题用无衬线更现代。"""
    has_cjk = any("\u4e00" <= c <= "\u9fff" for c in title)
    if style == "minimal":
        return "sans_zh" if has_cjk else "sans_en"
    if style == "band":
        return "sans_zh" if has_cjk else "bold_en"
    return "serif_zh" if has_cjk else "serif_en"


def check_glyphs(spec: CoverSpec) -> dict:
    """检查标题字符是否都能被字体渲染（防豆腐块 □）。"""
    sample = f"{spec.title}{spec.subtitle}"
    missing: list[str] = []
    try:
        font_path = FM.resolve(spec.title_role)
        from fontTools.ttLib import TTFont
        tt = TTFont(font_path, fontNumber=0, lazy=True)
        cmap: set = set()
        for table in tt["cmap"].tables:
            cmap.update(table.cmap.keys())
        tt.close()
        for ch in sample:
            if ch.strip() and ord(ch) not in cmap:
                missing.append(ch)
    except Exception as e:
        return {"checked": False, "reason": f"{type(e).__name__}: {e}", "missing": []}
    return {"checked": True, "font": spec.title_role,
            "missing": sorted(set(missing))}


def make_spec(title: str, *, subtitle: str = "", author: str = "",
              series: str = "", publisher: str = "", description: str = "",
              style: str = "auto", n_articles: int = 0, words: int = 0,
              palette_override: dict | None = None,
              title_role: str | None = None, align: str | None = None,
              letter_spacing: float = 0.0, accent_bar: bool = True) -> CoverSpec:
    """由书籍元数据构造封面规格（``style="auto"`` 会自动推荐）。"""
    if style == "auto":
        style = recommend_style(title, subtitle, description, n_articles, words)
    palette = derive_palette(title, style)
    if palette_override:
        palette.update(palette_override)
    return CoverSpec(
        title=title, subtitle=subtitle, author=author,
        series=series, publisher=publisher, style=style,
        title_role=title_role or auto_title_role(title, style),
        align=align or ("center" if style in ("classic", "minimal") else "left"),
        letter_spacing=letter_spacing, accent_bar=accent_bar, palette=palette,
    )


def _find_background(cover_dir: Path, explicit: str | Path | None,
                     archive_root: Path) -> tuple[Image.Image | None, str]:
    """定位背景图；找不到就返回 (None, "procedural")。"""
    candidates: list[Path] = []
    if explicit:
        p = Path(explicit)
        candidates.append(p if p.is_absolute() else archive_root / p)
        candidates.append(p)
        if not any(c.is_file() for c in candidates):
            log(f"指定的背景图不存在：{explicit}，改用程序生成", "warn")
            return None, "procedural"
    else:
        for name in ("background.png", "background.jpg", "background.jpeg",
                     "background.webp", "background.webp"):
            candidates.append(cover_dir / name)

    for p in candidates:
        try:
            if p.is_file():
                return Image.open(p), str(p)
        except OSError:
            continue
    return None, "procedural"


def render_to_archive(archive, *, style: str = "auto", title: str = "",
                      subtitle: str | None = None, author: str | None = None,
                      series: str = "", publisher: str = "",
                      background: str | Path | None = None,
                      basename: str = "cover", palette_override: dict | None = None,
                      title_role: str | None = None, align: str | None = None,
                      letter_spacing: float = 0.0, accent_bar: bool = True,
                      set_default: bool = True, write_brief: bool = True) -> dict:
    """渲染封面并登记进归档包（一条调用搞定整个 Stage 3）。

    返回一个可直接塞进 summary.json 的 dict。
    """
    import time as _time
    from .utils import relpath as _relpath

    ar = archive
    cover_dir = ensure_dir(ar.root / "cover")
    book = ar.book

    t = title or book.get("title") or "Untitled"
    st = subtitle if subtitle is not None else (book.get("subtitle") or "")
    au = author if author is not None else (book.get("author") or "")
    stats = ar.manifest.get("stats", {}) or {}

    spec = make_spec(
        t, subtitle=st, author=au,
        series=series or book.get("series", ""),
        publisher=publisher or book.get("publisher", ""),
        description=book.get("description", ""),
        style=style, n_articles=len(ar.articles),
        words=int(stats.get("words", 0) or 0),
        palette_override=palette_override,
        title_role=title_role, align=align,
        letter_spacing=letter_spacing, accent_bar=accent_bar,
    )

    bg_img, bg_source = _find_background(cover_dir, background, ar.root)

    if write_brief:
        try:
            prompt = build_cover_prompt(spec)
            (cover_dir / "PROMPT.txt").write_text(prompt + "\n", encoding="utf-8")
        except Exception:
            pass

    t0 = _time.monotonic()
    img = render_cover(spec, bg_img, seed=t)
    paths = save_cover(img, cover_dir, basename=basename)
    elapsed = _time.monotonic() - t0
    glyphs = check_glyphs(spec)

    rel_cover = _relpath(paths["jpg"], ar.root)
    ar.add_asset(rel_cover, size=paths["jpg"].stat().st_size, kind="cover")
    if set_default:
        ar.set_book(cover=rel_cover, cover_style=spec.style)

    result = {
        "style": spec.style, "title_role": spec.title_role,
        "align": spec.align, "palette": spec.palette,
        "canvas": list(img.size),
        "background_source": "procedural" if bg_img is None else bg_source,
        "files": {k: _relpath(v, ar.root) for k, v in paths.items()},
        "cover": rel_cover if set_default else None,
        "glyph_check": glyphs,
        "elapsed_sec": round(elapsed, 2),
    }
    ar.record_stage("cover-render", result)
    ar.write_report("cover-report", {"stage": "cover-render", "at": now_iso(),
                                     **result})
    ar.save()
    return result
