# bookforge · 铸书

**丢一个网址，拿一本电子书。**

```bash
bookforge build https://example.com -o 我的书.epub
```

抓正文、下图片、定目录结构、画封面、排版、打包成 EPUB3 —— 一条命令跑完，
不需要 LLM 参与，不需要写代码，不需要手工整理文件。

> English: [README.en.md](README.en.md) · 本次改造的通用性分析见 [docs/PORTABILITY.md](docs/PORTABILITY.md)

---

## 为什么它"通用"

很多"网页转电子书"的工具只能在自己的宿主里跑，或者必须手工给每个站点写规则。
bookforge 从设计上就把这些限制去掉了：

| 维度 | 做法 |
|---|---|
| **不绑定 agent** | 核心是一个普通 Python CLI（`bookforge`），任何能执行 shell 的宿主都能用：WorkBuddy / Claude Code / Codex / Cursor / Gemini CLI / 自建 agent / 甚至 crontab |
| **不依赖 LLM** | 抓取、提取、分组、排版、封面全是确定性代码。**token 消耗与站点规模无关**——700 篇和 7 篇花的钱几乎一样 |
| **不绑定平台** | 字体在 Windows / macOS / Linux / 容器里自动发现；没有中文字体时给出可执行的安装指令 |
| **不绑定站点** | 自动探测 WordPress REST / RSS / sitemap / HTML 列表页；内置适配器之外还能用**声明式 YAML** 适配新站点（不改代码） |
| **不绑定语言** | 中英文混排按 CJK 口径统计字数与断行；标题尾部「- 站名」会自动剥离 |

关于"能不能在豆包 / Codex / WorkBuddy 之间通用"这个具体问题，
请看 **[docs/PORTABILITY.md](docs/PORTABILITY.md)** —— 那里把能力分层讲清楚了。

---

## 安装

```bash
git clone https://github.com/<you>/bookforge.git
cd bookforge
pip install -e .

# 可选：YAML 适配器 + 字体字形校验
pip install -e ".[all]"

# 体检（检查依赖 / 字体 / 适配器，并给出修复命令）
bookforge doctor
```

不想安装也行，直接跑：

```bash
PYTHONPATH=. python -m bookforge build https://example.com -o book.epub
```

依赖：Python ≥ 3.9，`requests` `lxml` `beautifulsoup4` `markdown` `EbookLib` `Pillow` `numpy`。

---

## 用法

### 1. 最常用的一条

```bash
bookforge build https://blog.example.com -o 文集.epub
```

会自动：

1. **探测站点** —— 判断是 WordPress / RSS / sitemap，还是普通列表页；顺便把书名、作者、简介从页面 meta 里读出来
2. **定目录结构** —— WordPress 站点直接搬它的两级分类，编成「部（主题）→ 章（细分类型）→ 节（文章标题）」；没有分类就按年份或 URL 路径分组
3. **抓正文与图片** —— 下载图片、限宽 900px 压缩、内容哈希去重
4. **画封面** —— 程序生成，零外部服务
5. **打包 EPUB3** —— 带可点击的部/章分隔页、嵌套目录、篇目清单 `index.md`

跑完在 stdout 上留一段十几行的摘要，同时把机器可读的
`<归档包>/reports/summary.json` 落盘。

### 2. 先小跑验效果（**强烈建议**）

```bash
# 只做 20 篇，几十秒就能看出正文干不干净、目录对不对
bookforge build https://blog.example.com --max 20 -o 试读.epub

# 满意了再全量（已抓过的会自动跳过，不会重复打扰站点）
bookforge build https://blog.example.com -o 文集.epub
```

`--max` 是**跨组轮流取**的：20 篇也会覆盖到每个部/章，而不是只给你某一章，
所以小跑就能验证目录结构。

### 3. 换目录结构

```bash
bookforge build <url> --by category   # 站点分类（WordPress 默认）
bookforge build <url> --by date       # 按年份分「部」
bookforge build <url> --by path       # 按 URL 第一段路径分「部」
bookforge build <url> --by flat       # 不分组，按时间平铺
bookforge build <url> --by auto       # 自动挑（默认）
```

WordPress 站点还可以手动定顺序、定归属优先级：

```bash
bookforge build <url> \
  --theme-order "xiangmu,free,jinrongheike,suibi" \
  --subtype-order "wltg,dianshang,zqxm" \
  --priority "jinrong,hack,dianshang,wltg"      # 一篇文章属于多个分类时谁赢
```

不传这些参数也没关系，会自动推导（见下文「分组规则」）。

### 4. 给 agent 用（省 token 的用法）

```bash
# stdout 只出 JSON，进度全部转到 stderr —— agent 直接解析即可
bookforge build https://blog.example.com -o book.epub --json

# 只要结果，不要过程
bookforge build https://blog.example.com -o book.epub --quiet
```

### 5. 分步跑（要精细控制时）

```bash
bookforge info   https://blog.example.com              # 只看探测结果
bookforge group  https://blog.example.com --urls-out urls.txt   # 只看目录结构
bookforge fetch  https://blog.example.com --archive ./mybook    # 只抓
bookforge cover  --archive ./mybook --style editorial            # 只做封面
bookforge pack   --archive ./mybook --theme modern -o out.epub   # 只打包
bookforge reorder --archive ./mybook --order date-asc            # 重排顺序
bookforge translate prepare --archive ./mybook --workdir ./tr    # 翻译（可选）
```

### 6. 其它

```bash
bookforge doctor      # 环境体检：依赖、字体、适配器，附修复命令
bookforge adapters    # 列出站点适配器与自定义适配器搜索目录
bookforge themes      # 列出排版主题（classic / modern / magazine / academic）
```

---

## 目录结构长什么样

`--by category` 在 WordPress 站点的产物：

```
第一部 · 项目            ← 可点击的部页（显示篇数、字数）
  第一章 · 网络推广       ← 可点击的章页
    无极后端鱼塘营销：0成本获取100倍的推广效果
    通过QQ一天添加700个精准用户
    …
  第二章 · 电商运营
    …
第二部 · 资源
  …
```

* 每个「部」「章」都是**真实页面**，点进去能看到篇数与字数，不是点不动的空标题
* 某个「部」下面只有一个细分类型时（比如"随笔"），自动省掉章这一层
* 书脊顺序：`封面 → 扉页 → 版权页 → 目录 → 正文`（目录必须靠前，700 篇的书目录放书末等于没有目录）
* 同目录还会生成 `index.md` —— 按部/章分节的篇目清单，可以直接发给别人

---

## 分组规则（可复现）

WordPress 分类是两级结构，正好映射成「部 → 章」。归属遵循四条规则：

1. **一篇文章只进一个细分类型** —— 避免同一篇在多章重复出现
2. **优先选最深的分类** —— 父分类通常只是收纳容器（如"项目""资源"）
3. **深度相同时**按优先级裁决。这个顺序会**自动推导**：更深、文章数更少的分类优先
   （"更具体"打败"更泛"）；泛主题自然沉底。想人工干预就用 `--priority`
4. **直接挂在顶层分类上的文章**，进入该部下的「综合」小节

另外有条安全底线：**任何没分到组的文章都会被兜进「未分类」部**，
绝不会因为分组规则而凭空消失。

非 WordPress 站点用 `--by date` / `--by path`，逻辑同理，只是分组依据换成日期或路径。

---

## 站点适配器

适配器只负责一件事：**在某个站点的 HTML 里准确指出正文在哪**。
没命中适配器也能用（走通用启发式），命中则提取质量更高。

内置：`paulgraham`、`wujie1230`、`wordpress`（通用 WordPress）。

要给新站点写适配器，**不用改 Python** —— 丢一个 YAML 就行：

```bash
mkdir -p bookforge-adapters
cp $(python -c "import bookforge,os;print(os.path.dirname(bookforge.__file__))")/adapters/example.yaml \
   bookforge-adapters/myblog.yaml
# 改 hosts / content_xpath / remove / 元数据选择器
bookforge info https://myblog.com     # 确认命中
```

搜索位置（按优先级）：

1. `./bookforge-adapters/`（跟书籍项目放一起）
2. `./.bookforge/adapters/`
3. `~/.config/bookforge/adapters/`
4. `$BOOKFORGE_ADAPTERS` 指向的目录
5. 包内自带（内置示例）

完整字段说明见 **[docs/ADDING_A_SITE.md](docs/ADDING_A_SITE.md)**。

---

## 输出（归档包规范）

```
<归档包>/
├── content/001-xxx.md     正文（YAML frontmatter + Markdown，编号即书内顺序）
├── assets/                本地化后的图片（已压缩、去重）
├── cover/                 封面（cover.jpg / cover.png / cover-thumb.jpg / PROMPT.txt）
├── metadata/
│   ├── book.json          书籍级元数据
│   ├── groups.json        目录结构（决定 EPUB 的部/章划分）
│   └── book-uuid.txt      稳定书号（反复导出不变）
├── manifest.json          单一可信源（所有阶段围绕它读写）
├── index.md               篇目清单（人类可读，可直接分享）
├── .cache/                原始 HTML 缓存（改提取逻辑时用它秒回，**别删**）
└── reports/
    ├── summary.json       ← agent 只需要读这一份
    ├── quality-report.json
    ├── cover-report.json
    └── publish-report.json
```

任何阶段都只通过 `manifest.json` 读写，所以每一步都能单独重跑、断点续传。

---

## 常见问题

**图片没下下来？**
看 `reports/quality-report.json` 的 `image_failures` 和构建摘要里的提示。常见是
被 `robots.txt` 拒绝（用 `--no-robots` 强行下载，请自行确认合规），
或图片服务器有防盗链。下载失败的图片会保留原链接，书仍然合法，但离线会缺图。

**正文抓得不对？**
先用 `--max 5` 小跑，然后给这个站点写一个 YAML 适配器（见上）。
这比调通用启发式可靠得多。

**书名 / 作者没识别出来？**
会自动尝试从页面 meta 读。读不到就手动传：`--title "书名" --author "作者"`。

**封面能换吗？**
```bash
bookforge cover --archive <归档包> --style editorial   # 四种风格随便试
bookforge pack  --archive <归档包> --cover 我的封面.jpg -o out.epub
```
默认封面是程序生成的（不需要任何 AI 服务）。想用 AI 生成底图：
`bookforge cover` 会写出 `cover/PROMPT.txt`，拿去生图（提示词已强制 "no text"，
因为生图模型写文字必错），把结果存成 `cover/background.png`，再 `bookforge cover` 一次即可。

**要断点续抓吗？**
直接重跑同一条命令。已抓的文章按 URL 跳过，`.cache/` 里的 HTML 也还在。

**想改成按时间从早到晚？**
```bash
bookforge reorder --archive <归档包> --order date-asc
```

---

## 版权与合规

请只抓取你有权获取的内容，用于个人学习与备份。

* 默认**尊重 `robots.txt`**；`--no-robots` 是显式放弃该保护，请自行确认合法合规。
* 默认限速 `--delay 0.6`，遇 429/503 自动退避，不会打垮对方站点。
* 抓取并再次分发他人作品可能侵权 —— 自己看没问题，公开传播请先拿到授权。

---

## 开发

```bash
pip install -e ".[dev]"
pytest -q
```

架构说明见 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**，
多 agent 接入见 **[docs/AGENTS.md](docs/AGENTS.md)**。

## License

MIT
