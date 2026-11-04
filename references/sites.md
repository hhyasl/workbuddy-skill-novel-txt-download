# 数据源与站点适配细节

## 一、番茄小说（目录源，正版首发平台）

### 作品页 → bookId

作品页 URL 里就是 bookId：

```
https://fanqienovel.com/page/7366582677454998590
                                  ^^^^^^^^^^^^^^^^ bookId
```

### 官方目录接口（免签名，可靠）

```
GET https://fanqienovel.com/api/reader/directory/detail?bookId=<bookId>
Referer: https://fanqienovel.com/page/<bookId>
```

返回结构：

```json
{
  "data": {
    "volumeNameList": ["第一卷：潜龙勿用", "..."],
    "chapterListWithVolume": [
      [ {"itemId":"7367041042832425534","title":"第1章 只办三件事！",
         "volume_name":"第一卷：潜龙勿用","realChapterOrder":"1",
         "isChapterLock":false,"needPay":0}, ... ]
    ]
  }
}
```

- `chapterListWithVolume` 是**二维数组**：外层按卷，内层是该卷的章节列表
- `itemId` 是该章在番茄的唯一 ID（不是章节序号）
- `needPay` / `isChapterLock` 标记付费章节，**不要抓**
- `title` 有时是 `"第1章 XXX"`，有时是 `"新年贺岁篇！"`、`"感谢XXX"`（作者致谢章）—— 全部原样保留

### 不可用的接口（别浪费时间）

| 接口 | 结果 |
|---|---|
| `/api/reader/full?itemId=..&chapterId=..` | HTTP 200 但 **0 字节**（需 a_bogus/msToken 签名） |
| `/api/reader/directory/detail?itemId=..` | 返回 `{"message":"缺少bookId"}` —— 参数名是 `bookId` 不是 `itemId` |
| `/api/author/search/search_book/v1?query=..` | HTTP 200 但 **0 字节**（搜索 API 不可用） |
| `/search/<书名>` | 返回 HTML，但搜索结果是**客户端渲染**，页面里没有 bookId |
| `/reader/<bookId>/<chapterId>` | 404，路径格式错 |
| `/reader/<chapterItemId>` | 200 但**只有标题没有正文**（正文走 JS + 签名接口） |

> 结论：番茄只能用来拿**目录**，正文必须走第三方转载站。

## 二、爱书库 ishuku（正文源）

```
书页   https://www.ishuku.com/book/{book_id}.html
章节   https://www.ishuku.com/book/{book_id}-{n}.html
分页   https://www.ishuku.com/book/{book_id}-{n}-{k}.html   (k 从 2 开始)
目录   https://www.ishuku.com/chapter/{book_id}.html   (只列前 100 章，分页是 JS 控制的)
```

| 元素 | 结构 |
|---|---|
| 正文容器 | `<div class="content">` 内是一堆 `<p>` 段 |
| 章节标题 | `<h1>`（不是 `<title>`，后者带书名后缀） |
| 分页链接 | 底部 `<a href="/book/{id}-{n}-{k}.html" class="btnGray">下一页</a>` |
| 推荐书单 | `<div class="txt font16">`，`<p>` 很多但每段极短 —— 会污染正文探测，需排除 |

**踩过的坑**：

1. `keep-alive` 复用连接会被服务端挂起（每请求卡满 timeout）→ 必须 `Connection: close` + 每请求新建连接
2. 高频请求被 reset，curl 返回 `000`（不是 403）→ 8 并发约 30~60 章/分钟
3. 书页只列**最近 40 章**（不是全部）→ 章节总数要用番茄目录
4. 书页里的"相关推荐"链接和章节链接格式相似 → 用**书页 URL 的 id 锚定** + 首段数字集中度筛选才能区分
5. 站点 URL 序号与章节号**不一一对应**（实测某书 660 号 URL 是"第560章"，末尾还插了 7 章作者致谢）→ 必须按目录标题配对

## 三、其他候选站（探测记录）

均为同类书站，结构相似但反爬强度不同，可作为备用正文源：

| 站点 | 直连状态 | 备注 |
|---|---|---|
| `www.369shuba.com` / `.net` | 000 / 404 | 需 WebFetch 通道或换 IP |
| `69shuba.tw` | 403 | 有 Cloudflare 类验证，WebFetch 也会卡在验证页 |
| `www.wfxs.tw` | 403 | 同上 |
| `www.ishuku.com` | 200 ✅ | **当前可用，已完整验证** |

换源时的做法：拿书页 URL 跑 `probe`，人工核对 `chapter_url` 模板和 `content_class` 两个字段即可，其余逻辑通用。

## 四、通用探测规则（`probe` 子命令的推断逻辑）

### 章节 URL 模板

1. 收集书页里所有 `/…数字….html` 形式的链接（排除 `/author/`、`/list-`、`/rank`、`/sitemap`）
2. 用**书页 URL 里的数字段**锚定筛选（章节链接共享同一 book_id；推荐书链接各不相同）
3. 按 `\d+` → `#` 归一化分组，选**首段数字集中度最高**的组
4. 首个数字段 = `book_id`，次个数字段 = 章节序号 → 生成 `{book_id}` / `{n}` 模板

### 正文容器

1. 扫描所有 `<div>` 并做嵌套配对（不能用正则截断 `</div>`，嵌套会截错）
2. 取每个 div 内的 `<p>`，统计总文本长度与段落平均长度
3. 排除"推荐书单"型容器：**段落多且平均长度 < 18 字符**
4. 保留文本量 ≥ 最大值 90% 的候选（内外层容器文本量相同）
5. 优先选 class 名**精确**命中 `content`/`txt`/`chapter`/`text`/`neirong` 的容器；其次子串命中；最后取文本量最大者

> 注意 `strip_html()` 返回的是**列表**，算长度要用 `len("".join(...))`，否则会把段落行数当字数，导致正文被误判成书单。

### 分页规则

1. 找正文后面的"下一页"锚点
2. 链接形如 `…-{章节号}-{页号}.html` → 页号是**最后一个数字段**
3. 生成模板：页号 → `{page}`，章节号 → `{n}`，book_id → `{book_id}`

> 坑：正则 `<a href="([^"]*?)-(\d+)\.html">` 会把 href 截断在页号之前。**改为捕获完整 href**，再从 href 末尾解析页号。
