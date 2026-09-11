#!/usr/bin/env python
"""Stage 2 · batch-translator

把归档包里的文章批量翻译，同时保住 Markdown 结构。

三个子命令，对应三种工作方式：

  prepare    拆出翻译任务（保护占位符、分块），产出 tasks.json
  translate  直连 LLM 批量翻译（需要 API key，可选）
  apply      把译文回填成完整的 Markdown 归档包，并做结构校验

── 方式 A：agent / 人工翻译（无需 key，推荐）
    python stage2_translate.py prepare --archive ./out/pg --workdir ./out/pg-zh
    # 然后翻译 tasks/translate-tasks.json 里每个 chunk 的 text，
    # 把结果写成 {"chunk-id": "译文", ...} 存为 translations.json
    python stage2_translate.py apply --archive ./out/pg --workdir ./out/pg-zh \\
        --translations ./out/pg-zh/translations.json --out ./out/pg-zh/archive

── 方式 B：直连 API
    set OPENAI_API_KEY=sk-...
    python stage2_translate.py run --archive ./out/pg --out ./out/pg-zh --provider openai

── 方式 C：先跑 prepare 看任务，再决定
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path



from .archive import Archive, build_frontmatter
from .translate import (ArticleJob, Chunk, Protector, PROVIDERS, LLMClient,
                                apply_translations, build_prompt, build_jobs,
                                validate_chunk)
from .utils import (banner, count_words, ensure_dir, log, now_iso, relpath)

AGENT_INSTRUCTIONS = """# 翻译任务说明

本目录下的 `translate-tasks.json` 是待翻译内容。请按下面的规则完成翻译。

## 你要做什么

对 `articles[].chunks[]` 里的每一个 `text` 字段：

1. 把它翻译成 **{target_lang}**。
2. **原样保留**所有形如 `⟦0⟧` `⟦1⟧` 的占位符 —— 编号、括号都不能改，不能删，不能加。
   这些占位符代表代码、URL、脚注等不可翻译内容，翻译后会被自动还原。
3. 保持段落结构：原文有几个段落，译文就要有几个段落（`text` 里用空行分隔）。
4. 保持行首 Markdown 标记（`#` `##` `-` `>` `1.` 等）的数量与层级。
5. 只输出译文，不要加解释、注释、译者序。

## 输出什么

一个 JSON 文件（建议命名 `translations.json`），格式：

```json
{{
  "chunk-id-1": "第一段的译文……",
  "chunk-id-2": "第二段的译文……"
}}
```

key 用 `chunks[].id` 原值，value 是译文。

## 分册处理建议

如果任务量大，可以按 `articles[].id` 分多次处理，
每次只提交该文章下的 chunk，最后合并成一个 `translations.json` 即可。

## 完成后

```bash
python stage2_translate.py apply --archive {archive} --workdir {workdir} \\
    --translations {workdir}/translations.json --out {out}
```

## 本书信息

- 书名：{title}
- 原文语言：{src_lang}
- 目标语言：{target_lang}
- 文章数：{n_articles}
- 待翻译片段数：{n_chunks}
- 总字符数：{total_chars}
"""


# ---------------------------------------------------------------- 参数

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stage2_translate",
                                description="批量翻译归档包，保持 Markdown 结构")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- prepare
    a = sub.add_parser("prepare", help="拆出翻译任务")
    a.add_argument("--archive", required=True, help="输入归档包")
    a.add_argument("--workdir", required=True, help="任务文件输出目录")
    a.add_argument("--target-lang", default="zh-CN")
    a.add_argument("--target-chars", type=int, default=1400,
                   help="单个任务块目标字符数（默认 1400）")
    a.add_argument("--max-chars", type=int, default=2600,
                   help="单块硬上限，超出则按句子再切")
    a.add_argument("--only", action="append", default=[],
                   help="只处理指定 article id（可重复）")
    a.add_argument("--include-translated", action="store_true",
                   help="连已翻译的也重做")
    a.add_argument("--glossary", help="术语表 JSON 文件 {原文: 译文}")
    a.add_argument("--context", default="", help="给译者的背景说明")
    a.add_argument("--split-by-article", action="store_true",
                   help="额外按文章拆出小任务文件，便于分批处理")

    # ---- translate（API 直连）
    b = sub.add_parser("translate", help="直连 LLM 翻译（需要 API key）")
    b.add_argument("--archive", required=True)
    b.add_argument("--out", required=True, help="翻译后归档包输出目录")
    b.add_argument("--target-lang", default="zh-CN")
    b.add_argument("--provider", default="openai", choices=list(PROVIDERS))
    b.add_argument("--model", default="")
    b.add_argument("--base-url", default="")
    b.add_argument("--api-key", default="")
    b.add_argument("--glossary")
    b.add_argument("--context", default="")
    b.add_argument("--workers", type=int, default=3)
    b.add_argument("--only", action="append", default=[])
    b.add_argument("--target-chars", type=int, default=1400)
    b.add_argument("--bilingual", action="store_true", help="生成中英对照")
    b.add_argument("--keep-archive", action="store_true",
                   help="保留原始归档包（默认保留）")

    # ---- apply
    c = sub.add_parser("apply", help="把译文回填成归档包")
    c.add_argument("--archive", required=True, help="原始归档包")
    c.add_argument("--workdir", help="任务目录（含 translate-tasks.json）")
    c.add_argument("--tasks", help="直接指定 tasks.json 路径")
    c.add_argument("--translations", required=True, help="译文 JSON")
    c.add_argument("--out", required=True, help="翻译后归档包输出目录")
    c.add_argument("--bilingual", action="store_true", help="生成中英对照")
    c.add_argument("--strict", action="store_true",
                   help="结构校验不通过的片段不写入（保留原文）")
    c.add_argument("--target-lang", default="zh-CN")

    return p


# ---------------------------------------------------------------- 序列化

def jobs_to_dict(jobs: list[ArticleJob], meta: dict) -> dict:
    return {
        "meta": meta,
        "articles": [
            {
                "id": j.article_id,
                "file": j.file,
                "title": j.title,
                "frontmatter": j.frontmatter,
                "vault": j.protected.vault,
                "chunks": [c.to_dict() for c in j.chunks],
            }
            for j in jobs
        ],
    }


def jobs_from_dict(data: dict) -> list[ArticleJob]:
    jobs: list[ArticleJob] = []
    for a in data.get("articles", []):
        p = Protector()
        p.vault = list(a.get("vault", []))
        job = ArticleJob(article_id=a["id"], file=a["file"],
                         title=a.get("title", ""),
                         frontmatter=a.get("frontmatter", {}))
        job.protected = p
        job.chunks = [
            Chunk(id=c["id"], article=a["id"], index=c.get("index", 0),
                  text=c["text"], blocks=c.get("blocks", 1),
                  chars=c.get("chars", len(c["text"])))
            for c in a.get("chunks", [])
        ]
        job.total_chars = sum(c.chars for c in job.chunks)
        jobs.append(job)
    return jobs


# ---------------------------------------------------------------- prepare

def cmd_prepare(args) -> int:
    banner("book-forge · Stage 2 翻译（拆分任务）", "保护 Markdown 结构 → 分块")
    ar = Archive.open(args.archive)
    workdir = ensure_dir(Path(args.workdir))
    tasks_dir = ensure_dir(workdir / "tasks")

    glossary = {}
    if args.glossary:
        glossary = json.loads(Path(args.glossary).read_text(encoding="utf-8"))

    log(f"扫描归档包：{len(ar.articles)} 篇文章", "step")
    jobs = build_jobs(ar, target_lang=args.target_lang,
                      target_chars=args.target_chars, max_chars=args.max_chars,
                      only=args.only or None,
                      skip_translated=not args.include_translated)
    if not jobs:
        log("没有需要翻译的内容（可能都已翻译，或 --only 过滤掉了）", "warn")
        return 0

    n_chunks = sum(len(j.chunks) for j in jobs)
    total_chars = sum(j.total_chars for j in jobs)
    meta = {
        "target_lang": args.target_lang,
        "source_lang": ar.book.get("source_language", "en"),
        "book_title": ar.book.get("title", ""),
        "created_at": now_iso(),
        "articles": len(jobs),
        "chunks": n_chunks,
        "total_chars": total_chars,
        "glossary": glossary,
        "context": args.context,
    }
    data = jobs_to_dict(jobs, meta)
    tasks_path = tasks_dir / "translate-tasks.json"
    tasks_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    # 按文章拆分（便于分批）
    if args.split_by_article:
        by_dir = ensure_dir(tasks_dir / "by-article")
        for j in jobs:
            sub = {"meta": {**meta, "article": j.article_id},
                   "articles": [a for a in data["articles"] if a["id"] == j.article_id]}
            (by_dir / f"{j.article_id}.tasks.json").write_text(
                json.dumps(sub, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"已按文章拆出 {len(jobs)} 个任务文件：{relpath(by_dir, workdir)}/", "info")

    # 给 agent 的说明
    (tasks_dir / "INSTRUCTIONS.md").write_text(
        AGENT_INSTRUCTIONS.format(
            target_lang=args.target_lang, archive=args.archive,
            workdir=str(workdir).replace("\\", "/"),
            out=str(Path(args.out) if hasattr(args, "out") else workdir / "archive")
                .replace("\\", "/"),
            title=meta["book_title"], src_lang=meta["source_lang"],
            n_articles=meta["articles"], n_chunks=n_chunks,
            total_chars=f"{total_chars:,}"),
        encoding="utf-8")

    print()
    log(f"任务文件：{relpath(tasks_path, workdir)}", "done")
    log(f"{len(jobs)} 篇 · {n_chunks} 个片段 · 约 {total_chars:,} 字符", "info")
    avg = total_chars / max(n_chunks, 1)
    log(f"平均每块 {avg:.0f} 字符（目标 {args.target_chars}）", "info")
    print()
    log("下一步：翻译每个 chunk 的 text 字段 → 写成 translations.json → 运行 apply",
        "step")
    log(f"详细说明见 {relpath(tasks_dir / 'INSTRUCTIONS.md', workdir)}", "info")
    return 0


# ---------------------------------------------------------------- apply

def _do_apply(ar: Archive, jobs: list[ArticleJob], translations: dict,
              out_dir: Path, *, bilingual: bool, strict: bool,
              target_lang: str) -> tuple[Archive, dict]:
    out_ar = ar.clone_to(out_dir)
    reports: list[dict] = []
    total_issues = 0

    for job in jobs:
        md, rep = apply_translations(job, translations,
                                     keep_original=bilingual)
        if strict and rep["problems"]:
            total_issues += len(rep["problems"])
            reports.append({**rep, "status": "skipped-strict"})
            continue
        (out_ar.content_dir / Path(job.file).name).write_text(md, encoding="utf-8")
        # 更新 manifest：译文单独记录，原文 file 保留
        for a in out_ar.articles:
            if a.id == job.article_id:
                a.translated = True
                a.translation_file = job.file
                a.title = str(job.frontmatter.get("title", a.title))
                a.word_count = count_words(md)
                out_ar.add_article(a)
                break
        total_issues += len(rep["problems"])
        reports.append({**rep, "status": "ok"})

    out_ar.set_book(language=target_lang)
    out_ar.record_stage("translate", {
        "articles": len(jobs),
        "chunks": sum(len(j.chunks) for j in jobs),
        "issued": total_issues,
        "target_lang": target_lang,
    })
    out_ar.set_stats(words=sum(a.word_count for a in out_ar.articles))
    out_ar.save()
    return out_ar, {"articles": reports, "issues": total_issues}


def cmd_apply(args) -> int:
    banner("book-forge · Stage 2 翻译（回填）", "译文 → Markdown，含结构校验")
    ar = Archive.open(args.archive)
    tasks_path = Path(args.tasks) if args.tasks else \
        Path(args.workdir) / "tasks" / "translate-tasks.json"
    if not tasks_path.exists():
        log(f"找不到任务文件：{tasks_path}（先跑 prepare）", "err")
        return 2
    data = json.loads(tasks_path.read_text(encoding="utf-8"))
    jobs = jobs_from_dict(data)

    tr_path = Path(args.translations)
    if not tr_path.exists():
        log(f"找不到译文文件：{tr_path}", "err")
        return 2
    translations = json.loads(tr_path.read_text(encoding="utf-8"))
    if isinstance(translations, dict) and "translations" in translations:
        translations = translations["translations"]

    log(f"任务：{len(jobs)} 篇文章 · "
        f"{sum(len(j.chunks) for j in jobs)} 个片段", "step")
    log(f"译文：{len(translations)} 条", "info")

    empty = [k for k, v in translations.items() if not str(v).strip()]
    if empty:
        log(f"{len(empty)} 条译文为空，将保留原文", "warn")

    out_dir = Path(args.out)
    out_ar, report = _do_apply(ar, jobs, translations, out_dir,
                               bilingual=args.bilingual, strict=args.strict,
                               target_lang=args.target_lang)

    # 完整报告
    all_issues: list[dict] = []
    for r in report["articles"]:
        for p in r.get("problems", []):
            all_issues.append({"article": r["article"], "chunk": p["chunk"],
                               "issues": p["issues"]})
    full = {
        "stage": "translate-apply",
        "at": now_iso(),
        "target_lang": args.target_lang,
        "articles": len(jobs),
        "chunks": sum(len(j.chunks) for j in jobs),
        "translated_chunks": len(translations),
        "with_issues": len(all_issues),
        "details": report["articles"],
        "issues": all_issues,
    }
    out_ar.write_report("translate-report", full)

    print()
    log(f"翻译后归档包：{out_ar.root}", "done")
    log(f"{len(jobs)} 篇 · {report['issues']} 处结构告警", "info")
    if all_issues:
        log("以下片段结构与原文不一致，建议抽查：", "warn")
        for it in all_issues[:8]:
            log(f"  {it['chunk']}：{'；'.join(it['issues'][:2])}", "warn")
        if len(all_issues) > 8:
            log(f"  …… 其余 {len(all_issues) - 8} 处见报告", "warn")
    log(f"报告：{relpath(out_ar.reports_dir / 'translate-report.json', out_ar.root)}",
        "info")
    return 0


# ---------------------------------------------------------------- translate（API）

def cmd_translate(args) -> int:
    banner("book-forge · Stage 2 翻译（API 直连）",
           f"{args.provider} / {args.model or PROVIDERS[args.provider]['model']}")
    ar = Archive.open(args.archive)
    glossary = json.loads(Path(args.glossary).read_text(encoding="utf-8")) \
        if args.glossary else {}
    jobs = build_jobs(ar, target_lang=args.target_lang,
                      target_chars=args.target_chars,
                      only=args.only or None)
    if not jobs:
        log("没有需要翻译的内容", "warn")
        return 0

    client = LLMClient(provider=args.provider, model=args.model,
                       base_url=args.base_url, api_key=args.api_key)
    n_chunks = sum(len(j.chunks) for j in jobs)
    log(f"待翻译：{len(jobs)} 篇 · {n_chunks} 个片段 · 模型 {client.model}", "step")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    translations: dict[str, str] = {}
    failures: list[dict] = []
    done = 0
    t0 = time.monotonic()

    def one(chunk: Chunk) -> tuple[str, str, str]:
        msgs = build_prompt(chunk.text, target_lang=args.target_lang,
                            glossary=glossary, context=args.context)
        try:
            out = client.chat(msgs)
            return chunk.id, out, ""
        except Exception as e:
            return chunk.id, "", f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = [ex.submit(one, c) for j in jobs for c in j.chunks]
        for fut in as_completed(futs):
            cid, out, err = fut.result()
            done += 1
            if err or not out:
                failures.append({"chunk": cid, "error": err or "空输出"})
                log(f"[{done}/{n_chunks}] 失败 {cid}：{err}", "err")
            else:
                translations[cid] = out
                if done % 10 == 0 or done == n_chunks:
                    log(f"[{done}/{n_chunks}] 已翻译", "info")

    log(f"翻译完成：成功 {len(translations)}／失败 {len(failures)}"
        f"（{time.monotonic() - t0:.0f}s）", "ok" if not failures else "warn")

    # 校验 + 回填
    issues = 0
    for j in jobs:
        for c in j.chunks:
            if c.id in translations:
                issues += len(validate_chunk(c.text, translations[c.id]))
    log(f"结构校验：{issues} 处告警", "warn" if issues else "ok")

    out_dir = Path(args.out)
    out_ar, rep = _do_apply(ar, jobs, translations, out_dir,
                            bilingual=args.bilingual, strict=False,
                            target_lang=args.target_lang)
    out_ar.write_report("translate-report", {
        "stage": "translate-api", "at": now_iso(),
        "provider": args.provider, "model": client.model,
        "chunks": n_chunks, "translated": len(translations),
        "failures": failures, "structure_issues": issues,
        "details": rep["articles"],
    })
    print()
    log(f"翻译后归档包：{out_ar.root}", "done")
    if failures:
        log(f"{len(failures)} 个片段失败，已保留原文", "warn")
    return 0 if not failures else 1


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "prepare":
        return cmd_prepare(args)
    if args.cmd == "apply":
        return cmd_apply(args)
    if args.cmd == "translate":
        return cmd_translate(args)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。已完成的翻译保留在文件中。", file=sys.stderr)
        sys.exit(130)
