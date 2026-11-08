# novel-txt-download

把一部中文网络小说抓成一份 **完整、章节不乱序、可离线阅读** 的 TXT 文件。

核心难点不在"下载"，而在三件事：

1. **拿到权威目录** —— 决定章节顺序和完整性（抓取站的 URL 序号往往是乱的）
2. **逐章稳定抓取** —— 长章节分页、限流、断网续跑
3. **按目录顺序合并并校验** —— 抓取站的 URL 序号 ≠ 章节号，直接按序号输出会乱序

纯 Python 3 标准库，**无需 pip 安装任何依赖**。

---

## 效果

以《不是哥们你在大乾玩带货》（1152 章 / 433 万字）实测：

| 指标 | 结果 |
|---|---|
| 章节完整性 | 1152 / 1152，标题集合与官方目录 **完全一致**（缺失 0、多余 0） |
| 空章节 | 0 |
| 乱码 / HTML 残留 | 0 |
| 抓取耗时 | 约 20~30 分钟（8 并发） |
| 产物 | UTF-8 版 12.77 MB + GBK 版 8.67 MB，内容逐字节一致 |

---

## 安装

### 作为 WorkBuddy Skill

```bash
git clone https://github.com/rhernandez7183/novel-txt-download.git \
    ~/.workbuddy/skills/novel-txt-download
```

之后在 WorkBuddy 里说「帮我下载《XX》的 txt」即可自动触发。

### 作为独立脚本

```bash
git clone https://github.com/rhernandez7183/novel-txt-download.git
cd novel-txt-download
python scripts/novel.py --help
```

---

## 使用

### 第 1 步：拿官方目录

番茄小说的作品页 URL 里就是 bookId：

```
https://fanqienovel.com/page/7366582677454998590
                                  ^^^^^^^^^^^^^^^^ bookId
```

```bash
python scripts/novel.py catalog --bookid 7366582677454998590 --out catalog.json
```

走番茄官方目录接口（免签名），返回**分卷结构 + 每章标题 + 章节 itemId**，这是后续排序与校验的基准。章数从输出里读。

> 番茄正文接口 `/api/reader/full` 拿不到正文（需 `a_bogus` 签名，返回 0 字节），`/reader/<itemId>` 页也只有标题没有正文。**正文必须走第三方转载站，目录走番茄官方接口。**

### 第 2 步：自动探测书站结构

```bash
python scripts/novel.py probe \
    --url "https://www.ishuku.com/book/157451967786.html" --out site.json
```

探测器自动推断章节 URL 模板、站点 book_id、正文容器 class、长章节分页规则、标题选择器：

```
[1] 用书页 URL 中的 id 锚定，命中 45 条章节链接
[2] 章节页: /book/157451967786-1152.html
[3] 正文容器: <div class="content">   class=content   <p> 段落数 54
[4] 检测到长章节分页: ...-1152-2.html  -> 模板 /book/{book_id}-{n}-{page}.html
```

**必须人工核对**输出的 `chapter_url` 模板与 `content_class` 两项。

### 第 3 步：抓正文

```bash
python scripts/novel.py fetch --site site.json --bookid 157451967786 \
    --total 1152 --cache cache --workers 8
```

- 逐章写 JSON 缓存（`cache/<n>.json`），**中断后重跑自动跳过已完成的章节**
- 长章节自动跟随"下一页"翻页合并
- 失败章节记入 `cache/_failed.json`

失败章节单独低速重试（单线程、章间长间隔，绕开限流）：

```bash
python scripts/refetch.py --site site.json --bookid 157451967786 --cache cache --gap 6
```

### 第 4 步：质检

```bash
python scripts/novel.py verify --catalog catalog.json --cache cache
```

**必须达到 PASS 才算完成** —— PASS 的含义是：缓存章节集合与官方目录集合**完全一致**、无空章节。

### 第 5 步：合并输出

```bash
python scripts/novel.py merge --catalog catalog.json --cache cache \
    --title "书名" --author "作者" --out .
```

按**目录标题**配对、按**目录顺序**输出，生成两个文件：

| 文件 | 编码 | 大小 | 用途 |
|---|---|---|---|
| `<书名>.txt` | UTF-8 | 大 | 电脑、现代手机、脚本处理 |
| `<书名>-GBK.txt` | GB18030 | 小约 1/3 | 老手机、老阅读器 |

两版内容完全一致，只是字节排布不同（GBK 中文 2 字节，UTF-8 3 字节）。

---

## 踩过的坑（都在 references/ 里）

| 坑 | 症状 | 解法 |
|---|---|---|
| 番茄正文接口要签名 | HTTP 200 但 0 字节 | 目录走官方接口，正文走转载站 |
| keep-alive 连接复用 | 每个请求卡满 timeout | `Connection: close`，每请求新建 TCP 连接 |
| 高频请求被 reset | curl 返回 `000`（不是 403） | 重试 + 退避；8 并发约 30~60 章/分钟 |
| 站点 URL 序号 ≠ 章节号 | 第 500 章突然变成"第 560 章" | 按官方目录标题配对排序，绝不按 URL 序号排 |
| `strip_html` 返回列表 | 把正文误判成"推荐书单" | 算长度要用 `len("".join(...))` |
| 分页正则截断 href | 长章节只拿到一半 | 捕获完整 href，再从末尾解析页号 |
| `load_cache` 误读 `_failed.json` | 章节凭空消失 | 只接受 `<数字>.json` 命名的章节文件 |

更多细节见 [`references/sites.md`](references/sites.md)（站点结构、番茄接口清单、探测规则）和 [`references/troubleshooting.md`](references/troubleshooting.md)（状态码对照、编码、乱序、缺章处置）。

---

## 目录结构

```
novel-txt-download/
├── SKILL.md                     # WorkBuddy Skill 入口（工作流 + 硬性约束）
├── README.md
├── LICENSE
├── scripts/
│   ├── novel.py                 # 主工具（catalog/probe/fetch/merge/verify）
│   ├── sites.py                 # 站点适配层 + 通用探测器
│   └── refetch.py               # 失败章节低速补抓
└── references/
    ├── sites.md
    └── troubleshooting.md
```

---

## 使用边界

- 仅用于**正版免费作品**的个人离线阅读（番茄小说等平台的免费连载作品）
- 只抓取公开可访问的页面，不绕过付费墙、不抓取盗版专有内容
- 付费章节（`needPay` / `isPaidPublication` / `isPaidStory`）会被跳过。注意 `isChapterLock` **不是**付费标记，免费书也会大片为 `true`

## License

MIT
