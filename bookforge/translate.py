"""Markdown 结构保护与分块翻译。

为什么不能把整篇 Markdown 直接丢给模型？
    代码块缩进被吃掉、链接 URL 被翻译、frontmatter 被改写、
    脚注标记丢失 —— 这些都是实际会发生的事。

这里的做法分三层：
    1. 保护（protect）  ：把不该翻的东西替换成占位符 ⟦0⟧，翻完再还原
    2. 分块（chunk）    ：按"内容块"聚合，控制单块体量，块内保持结构
    3. 校验（validate） ：块数、占位符、结构标记逐项比对，不一致就报警

设计上刻意让"翻译"这一步与调用方解耦：
    - agent 模式：产出任务文件，由主 agent 逐块翻译后回填
    - api 模式：直接调 LLM 接口（需要环境变量里的 key）
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

from .utils import clean_text, log, count_words

# 占位符：用罕见的数学括号，模型通常不会改写
PH_L = "⟦"
PH_R = "⟧"
PH_RE = re.compile(PH_L + r"(\d+)" + PH_R)

# frontmatter 中需要翻译的字段
FM_TRANSLATABLE = {"title", "subtitle", "summary", "description", "tags", "subject"}


# ---------------------------------------------------------------- 保护

class Protector:
    """把不可翻译的片段抽成占位符。"""

    def __init__(self):
        self.vault: list[str] = []

    def stash(self, s: str) -> str:
        key = f"{PH_L}{len(self.vault)}{PH_R}"
        self.vault.append(s)
        return key

    # ------------------------------------------------ 主流程

    def protect(self, text: str) -> str:
        text = self._fenced_code(text)
        text = self._latex(text)
        text = self._html(text)
        text = self._images_and_links(text)
        text = self._inline_code(text)
        text = self._bare_urls(text)
        text = self._footnote_refs(text)
        text = self._misc_tokens(text)
        return text

    def restore(self, text: str, *, tolerate: bool = True) -> tuple[str, list[int]]:
        """还原占位符。返回 (文本, 未还原/丢失的占位符编号列表)。

        容错只做一件事：把 `⟦ 0 ⟧`（模型加了空格）归一成 `⟦0⟧`。
        刻意**不**把 `[1]` `（1）` 之类的普通括号当占位符还原 ——
        正文里正常的引用编号会被误伤，这个代价远大于收益。
        """
        if tolerate:
            text = re.sub(PH_L + r"\s*(\d+)\s*" + PH_R, PH_L + r"\1" + PH_R, text)

        used: set[int] = set()
        missing: list[int] = []

        def sub(m):
            i = int(m.group(1))
            if 0 <= i < len(self.vault):
                used.add(i)
                return self.vault[i]
            missing.append(i)
            return m.group(0)

        out = PH_RE.sub(sub, text)
        # 只回报"编号越界"的无效占位符；vault 里未被用到的编号不算问题，
        # 因为一个 vault 是跨 chunk 共享的，其他 chunk 可能用到。
        return out, sorted(set(missing))

    # ------------------------------------------------ 各类保护规则

    _fenced_re = re.compile(
        r"(?:^[ \t]*(?:```|~~~)[^\n]*\n)"
        r".*?"
        r"(?:^[ \t]*(?:```|~~~)[ \t]*$)",
        re.M | re.S)

    def _fenced_code(self, text: str) -> str:
        """整个围栏代码块保护起来，前后补换行避免与正文粘连。"""
        def repl(m):
            return "\n" + self.stash(m.group(0).strip("\n")) + "\n"
        return self._fenced_re.sub(repl, text)

    def _latex(self, text: str) -> str:
        text = re.sub(r"\$\$.+?\$\$", lambda m: self.stash(m.group(0)), text,
                      flags=re.S)
        text = re.sub(r"(?<!\$)\$(?!\$)[^\n$]{1,200}?\$(?!\$)",
                      lambda m: self.stash(m.group(0)), text)
        return text

    def _html(self, text: str) -> str:
        return re.sub(r"</?[A-Za-z][^>\n]{0,200}>",
                      lambda m: self.stash(m.group(0)), text)

    def _images_and_links(self, text: str) -> str:
        # 把 "](url ...)" 整体（含括号）收进保险库。
        # 只保护 URL 还不够 —— 译者（人或模型）很容易把这对括号弄丢，
        # 导致还原后的 Markdown 链接语法损坏。藏起整个 ](...) 就无此风险。
        def img(m):
            # ![alt](url "title") —— alt 翻译，其余整体保护
            return f"![{m.group(1)}]{self.stash(m.group(2))}"

        text = re.sub(r"!\[([^\]]*)\](\([^)]*\))", img, text)

        def link(m):
            label = m.group(1)
            if m.group(2).startswith("](#"):
                return m.group(0)          # 文内跳转整体保留
            return f"[{label}]{self.stash(m.group(2))}"

        text = re.sub(r"\[([^\]^][^\]]*)\](\([^)\s][^)]*\))", link, text)
        return text

    def _inline_code(self, text: str) -> str:
        return re.sub(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)",
                      lambda m: self.stash(m.group(0)), text, flags=re.S)

    def _bare_urls(self, text: str) -> str:
        text = re.sub(r"<(https?://[^>\s]+)>",
                      lambda m: self.stash(m.group(0)), text)
        return re.sub(r"(?<![\w/\"'(\[])https?://[^\s<>\)\]\"']+",
                      lambda m: self.stash(m.group(0)), text)

    def _footnote_refs(self, text: str) -> str:
        # [^1] 角标本身不翻译
        return re.sub(r"\[\^[^\]]{1,20}\]",
                      lambda m: self.stash(m.group(0)), text)

    def _misc_tokens(self, text: str) -> str:
        # 孤立的 Markdown 分隔线与水平线
        text = re.sub(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$",
                      lambda m: self.stash(m.group(0)), text, flags=re.M)
        # 文件路径 / 命令样式
        text = re.sub(r"`[^`]+`", lambda m: self.stash(m.group(0)), text)
        return text


def _stash_pattern(text: str, pattern: re.Pattern, protector) -> str:
    raise NotImplementedError


# ---------------------------------------------------------------- 分块

@dataclass
class Chunk:
    id: str
    article: str
    index: int
    text: str
    blocks: int = 1
    chars: int = 0

    def to_dict(self) -> dict:
        return {"id": self.id, "article": self.article, "index": self.index,
                "blocks": self.blocks, "chars": self.chars, "text": self.text}


@dataclass
class ArticleJob:
    article_id: str
    file: str
    title: str
    frontmatter: dict
    chunks: list[Chunk] = field(default_factory=list)
    protected: Protector = field(default_factory=Protector)
    total_chars: int = 0


def split_blocks(body: str) -> list[str]:
    """按空行切成内容块，但代码块内部不切。"""
    blocks: list[str] = []
    cur: list[str] = []
    in_fence = False
    fence = ""
    for ln in body.split("\n"):
        m = re.match(r"^\s*(```|~~~)", ln)
        if m:
            if not in_fence:
                in_fence, fence = True, m.group(1)
            elif ln.strip().startswith(fence):
                in_fence = False
        if not ln.strip() and not in_fence:
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(ln)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def split_long_block(block: str, max_chars: int) -> list[str]:
    """超长块按句子切分，避免单个 chunk 过大。"""
    if len(block) <= max_chars:
        return [block]
    # 中文句末 / 英文句末都考虑
    parts = re.split(r"(?<=[。！？；])\s*|(?<=[.!?])\s+(?=[A-Z\"'(])", block)
    out: list[str] = []
    cur = ""
    for p in parts:
        if not p:
            continue
        if len(cur) + len(p) > max_chars and cur:
            out.append(cur)
            cur = p
        else:
            cur += p
    if cur.strip():
        out.append(cur)
    return out or [block]


def build_jobs(archive, *, target_lang: str = "zh-CN",
               target_chars: int = 1400, max_chars: int = 2600,
               only: list[str] | None = None,
               skip_translated: bool = True) -> list[ArticleJob]:
    """把归档包里的文章拆成翻译任务。"""
    jobs: list[ArticleJob] = []
    from .archive import article_meta, build_frontmatter, split_frontmatter

    for art in archive.articles:
        if only and art.id not in only:
            continue
        if skip_translated and art.translated and art.translation_file:
            continue
        raw = (archive.root / art.file).read_text(encoding="utf-8")
        fm, body = split_frontmatter(raw)
        if not fm:
            fm = article_meta(art, archive.book)

        job = ArticleJob(article_id=art.id, file=art.file,
                         title=str(fm.get("title", art.title)),
                         frontmatter=fm)
        p = job.protected

        # frontmatter 的 title/summary 作为第 0 块一起翻
        fm_extra: list[str] = []
        for k in ("title", "subtitle", "summary"):
            v = fm.get(k)
            if isinstance(v, str) and v.strip() and _is_translatable_value(v):
                fm_extra.append(v)

        blocks = split_blocks(body)
        chunks: list[Chunk] = []
        buf: list[str] = []
        buf_len = 0
        ci = 0

        def flush():
            nonlocal buf, buf_len, ci
            if not buf:
                return
            ci += 1
            text = "\n\n".join(buf)
            chunks.append(Chunk(id=f"{art.id}:c{ci:04d}", article=art.id,
                                index=ci, text=text,
                                blocks=len(buf), chars=len(text)))
            buf, buf_len = [], 0

        # frontmatter 文本块优先
        if fm_extra:
            ci += 1
            ft = "\n\n".join(fm_extra)
            chunks.append(Chunk(id=f"{art.id}:fm", article=art.id, index=0,
                                text=p.protect(ft), blocks=len(fm_extra),
                                chars=len(ft)))

        for b in blocks:
            if _skip_block(b):
                continue
            for sub in split_long_block(b, max_chars):
                protected = p.protect(sub)
                if buf and buf_len + len(protected) > target_chars:
                    flush()
                buf.append(protected)
                buf_len += len(protected)
                if buf_len >= target_chars:
                    flush()
        flush()

        job.chunks = chunks
        job.total_chars = sum(c.chars for c in chunks)
        if chunks:
            jobs.append(job)

    return jobs


def _skip_block(b: str) -> bool:
    """纯占位符块（如整段代码）不需要翻译。"""
    stripped = PH_RE.sub("", b).strip()
    return not stripped


def _is_translatable_value(v: str) -> bool:
    """判断 frontmatter 字段值是否值得翻译（纯英文/中文才翻，URL/日期不翻）。"""
    v = v.strip()
    if not v or len(v) < 2:
        return False
    if re.fullmatch(r"[\d\-/:.\s]+", v):
        return False
    if re.match(r"^https?://", v):
        return False
    return True


# ---------------------------------------------------------------- 校验

def validate_chunk(src: str, dst: str) -> list[str]:
    """比对源块与译块的结构一致性。返回问题列表（空 = 通过）。"""
    issues: list[str] = []

    # 1) 占位符
    src_ph = {m.group(1) for m in PH_RE.finditer(src)}
    dst_ph = {m.group(1) for m in PH_RE.finditer(dst)}
    if src_ph - dst_ph:
        issues.append(f"译文丢失占位符：{sorted(src_ph - dst_ph)}")
    if dst_ph - src_ph:
        issues.append(f"译文凭空多出占位符：{sorted(dst_ph - src_ph)}")

    # 2) 块数
    src_n = len([x for x in re.split(r"\n\s*\n", src.strip()) if x.strip()])
    dst_n = len([x for x in re.split(r"\n\s*\n", dst.strip()) if x.strip()])
    if src_n != dst_n:
        issues.append(f"段落块数不一致：原文 {src_n} → 译文 {dst_n}")

    # 3) 行首结构标记
    def marks(t: str) -> dict[str, int]:
        d: dict[str, int] = {}
        in_fence = False
        for ln in t.split("\n"):
            if re.match(r"^\s*(```|~~~)", ln):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            m = re.match(r"^(\s*)(#{1,6}|>|[-*+]|\d{1,3}[.)])\s", ln)
            if m:
                k = m.group(2)
                k = "#" if k.startswith("#") else (
                    "ol" if re.match(r"\d", k) else
                    ("ul" if k in "-*+" else k))
                d[k] = d.get(k, 0) + 1
        return d

    sm, dm = marks(src), marks(dst)
    for k in set(sm) | set(dm):
        if sm.get(k, 0) != dm.get(k, 0):
            issues.append(f"结构标记 `{k}` 数量不一致：{sm.get(k, 0)} → {dm.get(k, 0)}")

    # 4) 长度异常（多为模型跑偏）——中英互译按"词数口径"比，不能用字符数
    if len(src) > 120:
        src_cjk = _cjk_ratio(src) > 0.25
        dst_cjk = _cjk_ratio(dst) > 0.25
        if src_cjk != dst_cjk:
            # 跨语种：英文 1 词 ≈ 中文 1.5～1.9 字，取 1.7 做中轴
            ratio = count_words(dst) / max(count_words(src) * 1.7, 1)
            lo, hi = 0.42, 1.9
        else:
            ratio = len(dst) / max(len(src), 1)
            lo, hi = 0.35, 3.2
        if ratio < lo:
            issues.append(f"译文疑似缺漏（等效长度比 {ratio:.2f}，阈值 {lo}）")
        elif ratio > hi:
            issues.append(f"译文疑似注水（等效长度比 {ratio:.2f}，阈值 {hi}）")

    return issues


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
    return cjk / max(len(text), 1)


# ---------------------------------------------------------------- 术语表

DEFAULT_SYSTEM_PROMPT = """你是一位专业的技术图书译者，正在翻译一本要正式出版的电子书。

要求：
1. 忠实、准确、通顺，符合中文技术图书的表达习惯，不要逐字硬译。
2. **必须原样保留**所有形如 ⟦0⟧ ⟦1⟧ 的占位符（连同编号一起，一个字符都不能改、不能删、不能加）。
   这些占位符代表代码、链接、脚注等不可翻译内容。
3. 严格保持原文的段落结构与行首 Markdown 标记（# ## - > 1. 等），
   原文有几个段落，译文就要有几个段落。
4. 保留所有中英文标点的排版习惯：中文用全角标点，中英文之间不加空格。
5. 不要添加任何解释、注释、译者序，只输出译文本身。
6. 专有名词按术语表处理；术语表未覆盖的人名/公司名保留原文。"""


def build_prompt(chunk_text: str, *, target_lang: str = "zh-CN",
                 glossary: dict[str, str] | None = None,
                 context: str = "") -> list[dict]:
    sys = DEFAULT_SYSTEM_PROMPT.replace("中文", _lang_name(target_lang))
    if glossary:
        rows = "\n".join(f"- {k} → {v}" for k, v in glossary.items())
        sys += f"\n\n术语表（必须遵守）：\n{rows}"
    if context:
        sys += f"\n\n本书背景：{context}"
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"请翻译下面这段内容，只输出译文：\n\n{chunk_text}"},
    ]


def _lang_name(code: str) -> str:
    return {"zh-CN": "简体中文", "zh-TW": "繁体中文", "en": "英文",
            "ja": "日文", "ko": "韩文"}.get(code, code)


# ---------------------------------------------------------------- LLM 直连（可选）

PROVIDERS = {
    "openai": {
        "base": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
    },
    "deepseek": {
        "base": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
    },
    "moonshot": {
        "base": "https://api.moonshot.cn/v1",
        "key_env": "MOONSHOT_API_KEY",
        "model": "moonshot-v1-8k",
    },
    "siliconflow": {
        "base": "https://api.siliconflow.cn/v1",
        "key_env": "SILICONFLOW_API_KEY",
        "model": "Qwen/Qwen2.5-7B-Instruct",
    },
    "custom": {"base": "", "key_env": "LLM_API_KEY", "model": ""},
}


class LLMClient:
    """极简 OpenAI 兼容客户端（只依赖 requests，不引入额外 SDK）。"""

    def __init__(self, provider: str = "openai", model: str = "",
                 base_url: str = "", api_key: str = "", timeout: float = 120.0):
        cfg = PROVIDERS.get(provider, PROVIDERS["openai"])
        self.base = (base_url or cfg["base"]).rstrip("/")
        self.key = api_key or os.environ.get(cfg["key_env"], "")
        self.model = model or cfg["model"]
        self.timeout = timeout
        if not self.key:
            raise RuntimeError(
                f"未找到 API key（环境变量 {cfg['key_env']}）。"
                f"要么设置它，要么改用 agent 模式（prepare + 人工/agent 翻译 + apply）。")
        if not self.base:
            raise RuntimeError("缺少 base_url（--base-url）")

    def chat(self, messages: list[dict], *, temperature: float = 0.3,
             retry: int = 3) -> str:
        import requests
        url = f"{self.base}/chat/completions"
        headers = {"Authorization": f"Bearer {self.key}",
                   "Content-Type": "application/json"}
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature}
        last = ""
        for attempt in range(1, retry + 1):
            try:
                r = requests.post(url, headers=headers, json=payload,
                                  timeout=self.timeout)
                if r.status_code == 200:
                    data = r.json()
                    return data["choices"][0]["message"]["content"].strip()
                last = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(min(2 ** attempt, 20))
                    continue
                break
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                time.sleep(min(2 ** attempt, 15))
        raise RuntimeError(f"LLM 调用失败：{last}")


# ---------------------------------------------------------------- 回填

def apply_translations(job: ArticleJob, translations: dict[str, str],
                       *, keep_original: bool = False) -> tuple[str, dict]:
    """把译文按 chunk 拼回完整 Markdown。返回 (markdown, 报告)。"""
    from .archive import build_frontmatter

    fm = dict(job.frontmatter)
    out_parts: list[str] = []
    problems: list[dict] = []
    missing_chunks: list[str] = []

    for ch in job.chunks:
        dst = translations.get(ch.id, "")
        if not dst or not str(dst).strip():
            missing_chunks.append(ch.id)
            dst = ch.text          # 保留原文（占位符原样）
        else:
            issues = validate_chunk(ch.text, dst)
            if issues:
                problems.append({"chunk": ch.id, "issues": issues})

        restored, invalid = job.protected.restore(dst)
        if invalid:
            problems.append({"chunk": ch.id,
                             "issues": [f"出现无效占位符编号 {invalid}"]})
        if ch.id.endswith(":fm"):
            # frontmatter 译文分开处理
            lines = [x.strip() for x in restored.split("\n\n") if x.strip()]
            for k, v in zip(("title", "subtitle", "summary"), lines):
                if k in fm:
                    fm[k] = v
            continue
        out_parts.append(restored.strip())

    body = "\n\n".join(p for p in out_parts if p)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    if keep_original:
        body = _bilingual(body, job)

    md = build_frontmatter(fm) + "\n" + body + "\n"
    report = {
        "article": job.article_id,
        "chunks": len(job.chunks),
        "translated": len(job.chunks) - len(missing_chunks),
        "missing_chunks": missing_chunks,
        "problems": problems,
    }
    return md, report


def _bilingual(translated: str, job: ArticleJob) -> str:
    """生成中英对照：原文块与译文块交替。"""
    src_blocks = []
    for ch in job.chunks:
        if ch.id.endswith(":fm"):
            continue
        src_blocks.append(ch.text)
    raw = "\n\n".join(src_blocks)
    try:
        restored, _ = job.protected.restore(raw)
    except Exception:
        restored = raw
    src_list = [b.strip() for b in re.split(r"\n\s*\n", restored) if b.strip()]
    dst_list = [b.strip() for b in re.split(r"\n\s*\n", translated) if b.strip()]
    if len(src_list) != len(dst_list):
        return translated          # 块数对不上就放弃对照，避免错位
    out: list[str] = []
    for s, d in zip(src_list, dst_list):
        out.append(d)
        if not re.match(r"^#{1,6}\s", s):
            out.append(f"> {s}")
    return "\n\n".join(out)
