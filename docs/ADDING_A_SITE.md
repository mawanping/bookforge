# 给新站点加适配器

适配器只负责一件事：**在某个站点的 HTML 里准确指出正文在哪、标题作者日期怎么读。**

没有适配器也能用（走通用启发式，会自动识别 `<article>` / `<main>` /
`.post-content` / `.entry-content` 这类常见容器）。但通用启发式不可能对所有站点都对，
所以**站点专属适配器是提高质量最有效的手段**。

写适配器**不需要改 Python**，丢一个 YAML 文件就行。

---

## 1. 放哪里

按优先级依次扫描：

| 位置 | 适合 |
|---|---|
| `./bookforge-adapters/*.yaml` | 跟某个书籍项目放一起 |
| `./.bookforge/adapters/*.yaml` | 同上，隐藏目录版 |
| `~/.config/bookforge/adapters/*.yaml` | 个人常用站点，跨项目复用 |
| `~/.bookforge/adapters/*.yaml` | 同上 |
| `$BOOKFORGE_ADAPTERS` 指向的目录或文件 | 脚本化 / CI |
| 包内 `bookforge/adapters/` | 内置示例（别改这里） |

同名适配器**先扫到的先赢**，所以项目级的会覆盖用户级的。

## 2. 最短的可用例子

```yaml
name: myblog
hosts: [myblog.com, www.myblog.com]
content_xpath:
  - "//div[contains(@class,'post-content')]"
```

就这三行已经能显著改善提取质量了。其余字段都是可选的。

## 3. 完整字段表

### 匹配（三者至少给一个，否则这个适配器会被跳过）

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 适配器名，`bookforge adapters` 里显示 |
| `hosts` | list | 域名列表，匹配该域及其子域 |
| `match_regex` | str | 直接对 URL 做正则匹配（`match` 同义） |
| `url_prefixes` | list | URL 前缀列表 |

### 正文定位

| 字段 | 默认 | 说明 |
|---|---|---|
| `content_xpath` | `[]` | **XPath 列表，按顺序尝试，第一个命中的作为正文容器** |
| `heading_offset` | `0` | 正文里的 `h*` 整体降级层数。站点自己把标题写成 `h2` 时保持 0；写成 `h1` 时设 1，避免和书名/章节层级打架 |
| `remove` | `[]` | 解析前删掉的噪声，XPath 列表，如 `[".sidebar", ".comments", "nav"]` |

**XPath 写法提示**（lxml 语法，不是 CSS 选择器）：

```yaml
content_xpath:
  - "//div[contains(@class,'entry-content')]"   # 类名包含
  - "//article"                                  # 标签
  - "//div[@id='main']"                          # id 精确
  - "//section[@class='post']//div[@class='body']"
```

### 元数据

| 字段 | 说明 |
|---|---|
| `title_selector` | XPath，读标题；读不到会退回 `<title>`（并自动剥掉「- 站名」） |
| `author_selector` | XPath，读作者 |
| `author_regex` | 正则在**整页文本**里找作者，取第 1 个捕获组。适合「作者：张三」这种 |
| `date_selector` | XPath，读日期 |
| `date_attr` | 优先从这个属性取（如 `datetime`），取不到再用节点文本 |
| `date_regex` | 自定义日期正则，默认认 `YYYY-MM-DD`、`YYYY/MM/DD`、`YYYY.MM.DD`、`YYYY-MM` |

### 文章发现

不写这些就走通用发现（从入口页抽同站链接，按"像文章"的程度排序）。
明确指定更可靠：

| 字段 | 说明 |
|---|---|
| `discover.feed` | RSS/Atom 地址（**最推荐**，顺序即发布时间序） |
| `discover.sitemap` | sitemap.xml 地址（会递归 sitemapindex） |
| `discover.list_page` | 列表页地址 |
| `entry_urls` | 直接给入口 URL 列表 |
| `link_include` | 只保留 URL 匹配该正则的链接 |
| `link_exclude` | 排除 URL 匹配该正则的链接 |

## 4. 怎么调试

```bash
mkdir -p bookforge-adapters
vim bookforge-adapters/myblog.yaml

# ① 确认适配器被加载、且命中了这个站
bookforge adapters
bookforge info https://myblog.com

# ② 小跑 5 篇看正文干不干净
bookforge build https://myblog.com --max 5 -o test.epub

# ③ 看质量报告里的提取策略：应当出现 adapter:myblog
python -c "import json;print(json.load(open('test-archive/reports/quality-report.json'))['extract_strategies'])"
```

`extract_strategies` 的值形如：

* `adapter:myblog` —— 适配器命中，最可靠（置信度 0.95）
* `class:entry-content` —— 通用启发式按类名命中
* `heuristic:density` —— 按文本密度猜的，**这种最需要人工抽查**

如果改完还是 `heuristic:*`，说明 `content_xpath` 没写对 ——
用浏览器 F12 在正文上「检查元素」，照着它的 tag/class 改。

## 5. 什么时候该写 Python 适配器

YAML 表达不了这些情况时，才去 `bookforge/sites.py` 写 `SiteAdapter` 子类：

* 需要在 DOM 上做**结构性改造**（比如把没有 `<tr>` 的空壳 `<table>` 拆掉）
* 标题藏在**图片的 alt 属性**里（像 paulgraham.com）
* 需要多步正则清洗正文
* 站点是**动态渲染**的，要对接它自己的 JSON API 而不是解析 HTML

Python 适配器的约定：

```python
from .sites import SiteAdapter, register

@register
class MyBlog(SiteAdapter):
    name = "myblog"
    content_xpath = ["//div[@class='post-content']"]
    heading_offset = 0

    def match(self, url: str) -> bool:
        return urllib.parse.urlparse(url).netloc.lower() in ("myblog.com",)

    def entry_urls(self, base_url: str) -> list[str]:
        return [base_url + "/feed"]

    def title_from(self, tree) -> str: ...
    def author_from(self, tree) -> str: ...
    def date_from(self, tree, text: str = "") -> str: ...
    def preprocess(self, tree) -> None: ...
```

**注意**：`title_from` / `author_from` / `date_from` 是在 `prune()` **之前**
被调用的（`prune` 会把 `<header>` 当导航噪声删掉，标题常常就在里面）。
所以这些方法能安全地读 header 里的内容。

## 6. 贡献回上游

写好的通用适配器欢迎提 PR —— 放到 `bookforge/adapters/` 下（YAML）
或 `bookforge/sites.py`（Python），并在 `CHANGELOG.md` 记一笔。
