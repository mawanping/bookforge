---
name: bookforge
description: >-
  把网站/博客的文章抓取、分组、排版并打包成 EPUB 电子书。当用户想把某个网址
  （博客、专栏、WordPress 站点）的文章整理成一本电子书、想做 EPUB、想批量抓取
  网页转 Markdown、想按主题给文章分章、想生成图书封面时使用。
  关键词：电子书、EPUB、抓取文章、网页转 Markdown、博客出书、图书封面、排版。
agent_created: false
version: 2.0.0
---

# bookforge · 铸书

**一条命令把网址变成电子书。**

## 先确认环境（只做一次）

```bash
bookforge doctor
```

它会检查依赖 / 中文字体 / 站点适配器，并且**直接给出你当前平台上该怎么修**。
没装的话：`pip install -e .`（或 `pip install bookforge`）。
`doctor` 报缺失依赖时不要自己猜，按它给的命令装。

## 主命令

```bash
bookforge build <网址> -o 书名.epub
```

这一条命令会依次完成：**站点探测 → 目录结构 → 抓正文与图片 → 生成封面 → 打包 EPUB**。
不需要你判断站点类型、不需要手写 URL 清单、书名作者会自动识别。

### 干活时的推荐姿势

```bash
# 1) 先小跑验证效果（跨组抽样，几十秒；能看出正文干不干净、目录对不对）
bookforge build <网址> --max 20 -o 试读.epub --quiet

# 2) 交给用户确认后再全量（已抓过的自动跳过，不会重复打扰站点）
bookforge build <网址> -o 书名.epub --quiet --json
```

`--json` 时 stdout 只有一段 JSON，进度全在 stderr —— **直接解析 stdout 即可**，
不要额外读日志文件。

### 读结果

**只读这一份文件**：`<归档包>/reports/summary.json`（二三十行）。
里面有 `ok` / `articles` / `groups` / `epub` / `warnings` / `next_actions`。
`next_actions` 已经写好了下一步建议，照它说做。

归档包默认在 `<输出>.epub` 同级的 `<书名>-archive/`。

## 常用开关

| 开关 | 用途 |
|---|---|
| `--max N` | 只做 N 篇（**跨组轮流取**，小跑也能验证目录结构） |
| `--by auto\|category\|date\|path\|flat` | 分组策略；WordPress 默认用站点分类 |
| `--theme classic\|modern\|magazine\|academic` | 排版主题（中文文学类首选 `classic`） |
| `--cover-style auto\|editorial\|classic\|minimal\|band` | 封面风格 |
| `--no-images` | 不下载图片（纯文字书） |
| `--no-robots` | 忽略 robots.txt（**请先自行确认合规**） |
| `--quiet` / `--json` | 降噪 / 只要 JSON |
| `--force` | 忽略已有归档包重抓（HTML 仍走缓存，很快） |

## 分步（需要精细控制时）

```bash
bookforge info   <网址>                       # 只看探测：什么站、书名、入口
bookforge group  <网址> --urls-out urls.txt   # 只看目录结构
bookforge fetch  <网址> --archive ./book      # 只抓
bookforge cover  --archive ./book --style editorial
bookforge pack   --archive ./book --theme modern -o out.epub
bookforge reorder --archive ./book --order date-asc
bookforge adapters                            # 站点适配器列表与搜索目录
bookforge themes                              # 排版主题
```

## 用户想要的目录结构对不上怎么办

WordPress 站点会自动搬它的两级分类，编成
「部（主题）→ 章（细分类型）→ 节（文章标题）」，并且每种都是**可点击的真实页面**。
要人工干预顺序：

```bash
bookforge build <网址> \
  --theme-order "slug1,slug2" \
  --subtype-order "slugA,slugB" \
  --priority "slugX,slugY"     # 文章属于多个分类时谁赢
```

非 WordPress 站点用 `--by date`（按年份）或 `--by path`（按 URL 路径）。

## 正文抓得不对时

**优先给这个站点写一个 YAML 适配器，而不是去调通用启发式。**
不改 Python，丢一个文件即可：

```bash
mkdir -p bookforge-adapters
# 拷一份示例改：hosts / content_xpath / remove / 标题作者日期的选择器
bookforge info <网址>      # 确认适配器已命中
```

字段说明见 `docs/ADDING_A_SITE.md`。

## 注意事项（踩过的坑）

1. **别一上来就跑全量**。先 `--max 20`。700 篇带图的站点全量要跑十几分钟到几十分钟。
2. **图片失败先看原因**。摘要和质量报告会区分「被 robots.txt 拒绝」和「真的挂了」——
   前者加 `--no-robots` 可解决（请确认合规），后者才需要排查。
   下载失败的图片会保留外链，书仍合法但离线缺图，`summary.json` 里会明确警告。
3. **`.cache/` 不要删**。里面是原始 HTML；改提取逻辑后用 `--force` 重跑会走缓存，
   231 篇从 3–4 分钟降到 47 秒。
4. **中文书要数中文字符再交付**。脚本不报错 ≠ 内容是中文。
5. **封面文字永远是程序排的**。`cover/PROMPT.txt` 是给文生图模型的提示词
   （已强制 "no text"），生图模型画字必错。生成结果存成 `cover/background.png` 再渲染。
6. **长输出不要用 `| head` 截断**：会给脚本发 SIGPIPE，让它在写文件写到一半时崩掉。
   要看长输出就重定向到文件再读。

## 版权

只抓用户有权获取的内容，用于个人学习与备份。
默认尊重 robots.txt、默认限速 `--delay 0.6`。
抓取并再次分发他人作品可能侵权 —— 公开传播前请确认授权。
