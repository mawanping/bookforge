# 示例

## 一条命令出书

```bash
# 先小跑 20 篇看效果（跨组抽样，几十秒）
bookforge build https://1230.la/ --max 20 -o 试读.epub

# 满意后全量
bookforge build https://1230.la/ -o 无极领域文集.epub
```

## 按主题分章（WordPress 站点）

```bash
bookforge build https://1230.la/ -o 无极领域文集.epub \
  --theme-order "xiangmu,free,jinrongheike,suibi" \
  --subtype-order "wltg,dianshang,zqxm,websrc,software,hack,jinrong" \
  --priority "jinrong,hack,dianshang,wltg,zqxm,websrc,software,xiangmu,free,jinrongheike,suibi"
```

不传这三个参数也行 —— 会自动推导出接近的结果。

## 按年份的文集（任何站点）

```bash
bookforge build https://blog.example.com/ -o 十年文集.epub --by date
```

## 给 agent / 脚本用

```bash
bookforge build https://blog.example.com/ -o book.epub --quiet --json
# stdout 只有 JSON；同时落盘 <归档包>/reports/summary.json
```
