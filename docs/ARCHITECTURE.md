# 架构

## 数据流

```
                 ┌──────────────┐
   URL ─────────▶│ detect.py    │  站点探测：WordPress REST? RSS? sitemap? 列表页？
                 │              │  顺带把书名 / 作者 / 简介从 meta 读出来
                 └──────┬───────┘
                        │ SiteProfile
                 ┌──────▼───────┐
                 │ groups.py    │  目录结构：调 wp.py（分类树）或按 日期/路径/flat
                 │  + wp.py     │  产出 groups.json + urls.txt
                 └──────┬───────┘
                        │ 有序 URL 列表 + 分组归属
                 ┌──────▼───────┐
   HTML 缓存 ◀──▶│ fetch_stage  │  并行抓取 → extract.py 提正文 → fetch.py 下图片
   (.cache/)     │  fetch.py    │  → archive.py 写归档包（manifest 是单一可信源）
                 └──────┬───────┘
                        │ 归档包 <archive>/
                 ┌──────▼───────┐
                 │ cover.py     │  分层封面：底图（程序生成 或 AI 生成）
                 │  + fonts.py  │            + 文字（永远程序排版）
                 └──────┬───────┘
                        │ cover/cover.jpg
                 ┌──────▼───────┐
                 │ publish.py   │  typeset.py 排版 → ebooklib 打包 EPUB3
                 │ typeset.py   │  按 groups.json 排嵌套目录 + 部/章分隔页
                 └──────┬───────┘
                        │
              <书名>.epub + index.md + reports/summary.json
```

## 模块职责

| 模块 | 职责 | 关键点 |
|---|---|---|
| `cli.py` | 唯一入口。`build` 负责编排上面整条链 | 输出 `summary.json`，把 token 成本压到最低 |
| `detect.py` | 站点探测 + 元数据抽取 | 探测顺序：适配器 → WP REST → RSS → sitemap → 列表页 |
| `sites.py` | 站点适配器基类 + 内置适配器 + 注册表 | `find_adapter(url)` 按 URL 匹配 |
| `site_config.py` | 声明式（YAML/JSON）适配器 | 让"适配新站点"不用改代码 |
| `groups.py` | 通用分组编排 | 策略：category / date / path / flat；自动推导顺序；兜底救孤儿 |
| `wp.py` | WordPress 分类树 → 部/章映射 | 归属规则：最深分类优先，同级按 priority |
| `fetch.py` | HTTP 抓取、限速退避、缓存、图片下载与压缩 | 图片按内容哈希去重；webp/gif/svg 不乱压 |
| `extract.py` | HTML → Markdown，元数据抽取 | 正文定位、表格/嵌套表格、`<br><br>` 分段、标题清洗 |
| `archive.py` | 归档包读写、manifest、Article 数据类 | **单一可信源**，所有阶段围绕它 |
| `fonts.py` | 跨平台字体发现、字重控制、字体下载 | 中文字体找不到时给平台对应的安装命令 |
| `cover.py` | 封面渲染（分层设计） | 文字永远程序排；底图可为空（程序生成） |
| `typeset.py` | Markdown → XHTML + 主题 CSS | EPUB3 严格 XHTML 序列化 |
| `publish.py` | 打包 EPUB3、目录构建、结构校验 | 书脊：封面→扉页→版权页→目录→正文 |
| `translate.py` / `translate_stage.py` | 可选翻译阶段 | 结构保护式分片，占位符 ⟦n⟧ |
| `reorder.py` | 归档包重排 | 先整体校验再动手，可自愈 |

## 归档包规范（一切的基础）

```
<archive>/
├── content/NNN-slug.md   正文（YAML frontmatter + Markdown；编号即书内顺序）
├── assets/               图片（已压缩、去重）
├── cover/                封面与提示词
├── metadata/             book.json / groups.json / book-uuid.txt
├── manifest.json         单一可信源
├── index.md              篇目清单
├── .cache/               原始 HTML（键 sha1(url)[:16].html，内容是 JSON，text 字段即 HTML）
└── reports/              各阶段报告 + summary.json
```

**约定**：任何阶段都不直接改文件结构，一律通过 `manifest.json`。
这样每一步都能单独重跑，也能断点续传。

## 几个刻意的设计决策

### 为什么"零 LLM"

抓取、提取、分组、排版、封面全是确定性代码。好处是：

* **可复现** —— 同样的输入永远得到同样的输出，没有模型随机性
* **零 token 边际成本** —— 700 篇和 7 篇的模型花费几乎一样
* **可测试** —— 能写单元测试

LLM 只出现在两个**可选**位置：翻译（`translate`）、封面底图生成（`cover/PROMPT.txt`），
都不做也能出成品。

### 为什么封面要分层

> 文生图模型画文字几乎必然出错：缺笔画、拼错、粘连。

所以封面被拆成两层：**底图可以交给模型，文字必须程序排版**。
提示词里甚至强制写了 "no text"。用户没兴趣用 AI 时，
`generate_background()` 用 PIL + numpy 程序生成抽象底图 —— 零外部依赖。

### 为什么目录页要放在书的前面

`封面 → 扉页 → 版权页 → 目录 → 正文`。
700 多篇的书如果目录在书末，等于没有目录。

### 为什么"部""章"要做成真实页面

只在目录里当标题、点不进去的层级是没用的。
`publish.build_grouped_toc()` 把「部」「章」都渲染成真实的 `EpubHtml`
（带篇数、字数），并通过 ebooklib 的 `(父项, [子项...])` 元组表达嵌套。

### 为什么要有"兜底救孤儿"

分组规则再小心也可能漏文章（比如某篇没分类）。`groups.rescue_orphans()`
把所有没被分到的 URL 收进「未分类」部。
**宁可多一个不好看的分类，也不能让文章凭空消失。**

## 已知的取舍

* **JPG/PNG 之外不动**：多帧 GIF/WebP 与 SVG 不压缩（避免把动画压成静帧），
  代价是这类图占体积。
* **图片不缓存**：图片体积大，缓存不值得。所以 `--force` 重跑时 HTML 是秒回，
  但图片会重新下载。改提取逻辑（不动图片）时不受影响。
* **`--max` 取的是"跨组轮流"的前 N 篇**，不是随机抽样，也不是每组的固定比例 ——
  目的是让预览能覆盖到每个部/章，同时保持结果可复现。
