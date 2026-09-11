#!/usr/bin/env python3
"""重排归档包内的文章顺序（不改内容，只改编号 / 文件名 / 登记信息）。

典型用途：
  - Stage 1 抓完才发现顺序不对（并行抓取的完成顺序不等于站点顺序）
  - 想按发表时间做成一本书（从早到晚 / 从晚到早）
  - 想按标题字母序

用法：
  python reorder_archive.py --archive <归档包> --order date-asc
  python reorder_archive.py --archive <归档包> --order date-asc \
      --dates '{"programming-bottom-up":"1993-01"}'      # 补缺失日期
  python reorder_archive.py --archive <归档包> --order date-asc --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path



from .archive import Archive, article_meta, build_frontmatter, split_frontmatter  # noqa: E402
from .utils import banner, log, relpath, slugify  # noqa: E402


def sort_key(art, order: str, seq: dict[str, int]):
    """返回排序键。seq 是「当前书内顺序」序号，用作稳定兜底。"""
    i = seq.get(art.id, 10 ** 9)
    if order == "date-asc":
        return (art.published_at or "9999", i)
    if order == "alpha":
        return (art.title.lower(), i)
    if order == "reverse":
        return (-i,)
    return (i,)


def main() -> int:
    p = argparse.ArgumentParser(prog="reorder_archive",
                                description="重排归档包内的文章顺序")
    p.add_argument("--archive", required=True, help="归档包目录")
    p.add_argument("--order", required=True,
                   choices=["date-asc", "date-desc", "alpha", "reverse"],
                   help="date-asc=按时间从早到晚；date-desc=从晚到早；alpha=标题字母序；reverse=当前顺序倒序")
    p.add_argument("--dates", default="",
                   help='补日期用的 JSON，如 {"slug": "1993-01"}；slug 见 manifest')
    p.add_argument("--dry-run", action="store_true", help="只打印新顺序，不改文件")
    args = p.parse_args()

    banner("book-forge · 重排归档包", f"order = {args.order}")
    ar = Archive.open(args.archive)
    arts = list(ar.articles)
    if not arts:
        log("归档包里没有文章", "err")
        return 1

    # 补日期
    overrides = {}
    if args.dates:
        try:
            overrides = json.loads(args.dates)
        except json.JSONDecodeError:
            log("--dates 不是合法 JSON", "err")
            return 1
    filled = 0
    for a in arts:
        if a.id in overrides and not a.published_at:
            a.published_at = str(overrides[a.id])
            filled += 1
    if filled:
        log(f"补全 {filled} 篇的日期", "info")

    seq = {a.id: a.index for a in arts}
    order_key = {"date-asc": lambda a: sort_key(a, "date-asc", seq),
                 "date-desc": lambda a: _date_desc(a, seq),
                 "alpha": lambda a: sort_key(a, "alpha", seq),
                 "reverse": lambda a: sort_key(a, "reverse", seq)}[args.order]
    new = sorted(arts, key=order_key)

    log(f"共 {len(new)} 篇", "step")
    for n, a in enumerate(new, 1):
        d = a.published_at or "—"
        log(f"[{n:3d}] {d:8s} {a.title}", "info")

    if args.dry_run:
        log("dry-run：未改动任何文件", "done")
        return 0

    # 崩溃自愈：上一次重排若在中途挂掉（改到一半），会留下一批
    # `xxx.md.reorder-tmp`。先把它们还原成正式文件，保证本脚本可重复执行。
    recovered = 0
    for p in sorted(ar.content_dir.glob("*.reorder-tmp")):
        target = p.with_name(p.name[: -len(".reorder-tmp")])
        if not target.exists():
            p.rename(target)
            recovered += 1
    if recovered:
        log(f"检测到上次未完成的改名，已还原 {recovered} 个文件", "warn")

    # 先整体校验：任何一篇的文件找不到就整体放弃，绝不半途改动。
    # （否则会出现"重命名到一半失败 → manifest 被清空"这种不可逆的烂摊子）
    missing = [a.file for a in new if not ar.content_path(a.file).exists()]
    if missing:
        log(f"有 {len(missing)} 篇的正文文件找不到，已放弃重排（未改动任何文件）：", "err")
        for m in missing[:10]:
            log(f"   {m}", "err")
        if len(missing) > 10:
            log(f"   … 还有 {len(missing) - 10} 个", "err")
        return 1

    # 两阶段改名，避免新旧编号互相占用
    for a in new:
        src = ar.content_path(a.file)
        src.rename(src.with_name(src.name + ".reorder-tmp"))

    rebuilt: list[dict] = []
    for n, a in enumerate(new, 1):
        src = ar.content_path(a.file).with_name(Path(a.file).name + ".reorder-tmp")
        slug = slugify(a.title, 48) or a.id
        fname = f"{n:03d}-{slug}.md"
        dst = ar.content_dir / fname
        raw = src.read_text(encoding="utf-8")
        fm, body = split_frontmatter(raw)
        a.index = n
        new_meta = article_meta(a, ar.book)
        for k, v in fm.items():          # 保留原文里额外字段
            new_meta.setdefault(k, v)
        dst.write_text(f"{build_frontmatter(new_meta)}\n{body.strip()}\n",
                       encoding="utf-8")
        src.unlink()
        a.file = f"content/{fname}"
        rebuilt.append(a.to_dict())

    # 全部写完再替换登记，确保 manifest 与磁盘始终一致
    ar.manifest["articles"] = rebuilt
    ar.save()
    log(f"完成：重命名 {len(rebuilt)} 篇，顺序已写入 manifest", "done")
    return 0


def _date_desc(a, seq):
    """按日期从晚到早；无日期的排最后；同日按当前书内顺序。"""
    d = a.published_at or ""
    if not d:
        return (1, 0, seq.get(a.id, 10 ** 9))
    try:
        y, m = (d.split("-") + ["00"])[:2]
        return (0, -(int(y) * 100 + int(m)), seq.get(a.id, 10 ** 9))
    except ValueError:
        return (0, 0, seq.get(a.id, 10 ** 9))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
