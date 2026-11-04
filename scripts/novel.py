#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""网文 TXT 整本下载器。

子命令：
  catalog  取正版平台（番茄小说）官方目录 -> catalog.json
  probe    自动探测任意书站的章节结构 -> site.json
  fetch    按 site.json 逐章抓正文（并发/缓存/断点续跑）-> cache/
  merge    按 catalog.json 排序合并 -> txt（UTF-8 + GBK 双份）
  verify   单独跑质检

用法示例：
  python novel.py catalog --bookid 7366582677454998590 --out catalog.json
  python novel.py probe   --url https://www.ishuku.com/book/157451967786.html --out site.json
  python novel.py fetch   --site site.json --bookid 157451967786 --total 1152 --cache cache
  python novel.py merge   --catalog catalog.json --cache cache --title 书名 --author 作者 --out .
"""
import argparse, glob, json, os, re, sys, time, threading, random
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sites as S

FQ = "https://fanqienovel.com"


# ---------------------------------------------------------------- catalog

def cmd_catalog(args):
    """番茄小说官方目录 API —— 免签名，是唯一可靠的权威目录来源。"""
    url = f"{FQ}/api/reader/directory/detail?bookId={args.bookid}"
    raw = S.http_get("fanqienovel.com", f"/api/reader/directory/detail?bookId={args.bookid}",
                     referer=f"{FQ}/page/{args.bookid}")
    if raw is None:
        print("[!] 目录接口请求失败，尝试重试…")
        raw = S.http_get("fanqienovel.com", f"/api/reader/directory/detail?bookId={args.bookid}",
                         referer=f"{FQ}/page/{args.bookid}", tries=6)
    if raw is None:
        print("[X] 无法获取目录。可能原因：bookId 错误 / 作品不在番茄 / 需要换源", file=sys.stderr)
        return 2
    try:
        data = json.loads(raw)["data"]
    except Exception as e:
        print(f"[X] 目录解析失败: {e}\n原始响应前 300 字: {raw[:300]}", file=sys.stderr)
        return 2
    vols = data.get("chapterListWithVolume") or []
    out = {"book_id": args.bookid, "volumes": []}
    for v in vols:
        vname = v[0].get("volume_name", "") if v else ""
        chapters = []
        for c in v:
            chapters.append({
                "index": len(chapters) + 1,
                "title": c.get("title", ""),
                "item_id": c.get("itemId", ""),
                "order": c.get("realChapterOrder", ""),
                "locked": bool(c.get("isChapterLock") or c.get("needPay")),
            })
        out["volumes"].append({"name": vname, "chapters": chapters})
    total = sum(len(v["chapters"]) for v in out["volumes"])
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[OK] 目录已保存 -> {args.out}")
    print(f"     卷数 {len(out['volumes'])}  章节 {total}")
    for v in out["volumes"]:
        print(f"     【{v['name']}】{len(v['chapters'])} 章")
    return 0


# ---------------------------------------------------------------- probe

def cmd_probe(args):
    """探测未知书站结构：章节 URL 模式、正文容器、分页规则。"""
    url = args.url
    host, path = S.host_of(url), S.path_of(url)
    print(f"[*] 探测 {url}")
    raw = S.http_get(host, path, referer=f"https://{host}/")
    if raw is None:
        print("[X] 书页抓取失败", file=sys.stderr)
        return 2
    base = f"https://{host}"

    # 1) 章节链接样本 —— 按"数字段个数"归一化，避免把 book_id 当成章节号
    links = re.findall(r'href="(/[^"]*?\d+[^"]*?\.html)"', raw)
    links = [l for l in links if not re.search(r"/(author|list|rank|bookshelf|sitemap)", l)]
    if not links:
        print("[X] 书页里没找到章节链接", file=sys.stderr)
        return 2

    # 优先锚定书页 URL 里的 book_id（"相关推荐"链接各不相同，章节链接共享同一 book_id）
    page_ids = re.findall(r"\d+", path)
    anchored = [l for l in links if any(i in l for i in page_ids)]
    if anchored:
        links = anchored
        print(f"[1] 用书页 URL 中的 id 锚定，命中 {len(links)} 条章节链接")
    else:
        print(f"[1] 书页 URL 未含 id，改用数字集中度筛选，共 {len(links)} 条候选")

    pat = defaultdict(list)
    for l in links:
        pat[re.sub(r"\d+", "#", l)].append(l)
    # 选模式：样本里首段数字集中度最高（章节链接 100% 共享 book_id）
    def concentration(items):
        firsts = [re.findall(r"\d+", l)[0] for l in items if re.findall(r"\d+", l)]
        if not firsts:
            return 0, ""
        c = max(set(firsts), key=firsts.count)
        return firsts.count(c) / len(firsts), c
    scored = sorted(pat.items(), key=lambda kv: (concentration(kv[1])[0], len(kv[1])), reverse=True)
    best_pat, samples = scored[0]
    conc, _ = concentration(samples)
    print(f"    章节链接模式: {best_pat}  (样本 {len(samples)} 个, 集中度 {conc:.0%})")

    # 从样本推断 book_id 与章节号：首个数字段是 book_id，次个数字段是章节序号
    segs = [(l, re.findall(r"\d+", l)) for l in samples]
    segs = [(l, s) for l, s in segs if s]
    if not segs:
        print("[X] 链接中无数字段", file=sys.stderr)
        return 2
    firsts = [s[0] for _, s in segs]
    book_id = max(set(firsts), key=firsts.count)
    chap_samples = []
    for l, s in segs:
        if s[0] == book_id and len(s) >= 2:
            chap_samples.append((l, s[1]))
    if chap_samples:
        # 章节序号应随链接递增，取靠后的一段做样本
        sample = sorted(chap_samples, key=lambda x: int(x[1]))[-1][0]
        tmpl = sample.replace(book_id, "{book_id}", 1)
        # 只替换最后一个数字段为 {n}
        m2 = re.search(r"(\d+)(?!.*\d)", tmpl)
        if m2:
            tmpl = tmpl[:m2.start(1)] + "{n}" + tmpl[m2.end(1):]
    else:
        sample = samples[0]
        tmpl = sample.replace(book_id, "{book_id}", 1)
    print(f"    book_id={book_id}   章节 URL 模板={tmpl}")


    # 2) 抓一个章节页（取序号较大的样本，长章节分页更可能出现在后面）
    probe_n = "2"
    if chap_samples:
        probe_n = sorted(chap_samples, key=lambda x: int(x[1]))[-1][1]
    ch_path = tmpl.format(book_id=book_id, n=probe_n)
    craw = S.http_get(host, ch_path, referer=f"{base}{path}")
    if craw is None:
        print("[X] 章节页抓取失败", file=sys.stderr)
        return 2
    print(f"[2] 章节页: {ch_path}  ({len(craw)} 字节)")

    # 3) 正文容器
    tag, ps, cls = S.probe_content_block(craw)
    if tag is None:
        print("[X] 未探测到正文容器（可能正文由 JS 渲染，需换源）", file=sys.stderr)
        return 2
    print(f"[3] 正文容器: {re.sub(chr(10),' ',tag)[:80]}   class={cls}   <p> 段落数 {len(ps)}")

    # 4) 分页规则
    paged = None
    m = re.search(r'<a href="([^"]*?)"[^>]*>\s*(?:下一页|下页)', craw)
    if m and re.search(r"-\d+\.html", m.group(1)):
        # 分页链接 = 章节链接 + 一个额外的数字段（页号）
        plink = m.group(1)
        segs = re.findall(r"\d+", plink)
        page_num = segs[-1]
        tmpl2 = re.sub(r"(\d+)(\.html)$", r"{page}\2", plink)   # 末段数字 = 页号
        if probe_n in tmpl2:
            tmpl2 = tmpl2.replace(probe_n, "{n}")                 # 章节号 -> {n}
        tmpl2 = tmpl2.replace(book_id, "{book_id}")
        paged = tmpl2
        print(f"[4] 检测到长章节分页: {plink}  -> 模板 {paged}")
    else:
        print("[4] 未检测到分页（章节可能整章不分页）")


    # 5) 标题
    title = S.extract_title(craw)
    print(f"[5] 标题抽取: {title[:50]!r}")

    cfg = {
        "name": args.name or host.replace("www.", ""),
        "base": base,
        "list_url": path,
        "book_id": book_id,
        "chapter_url": tmpl,
        "content_class": cls,
        "content_regex": r'<div class="' + re.escape(cls or "content") + r'"[^>]*>(.*?)</div>' if cls else "",
        "title_regex": r"<h1[^>]*>(.*?)</h1>",
        "probe_sample": ch_path,
    }
    if paged:
        cfg["paged_url"] = paged
        cfg["next_paged_regex"] = r'<a href="([^"]+?)"[^>]*>\s*下一页'
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] 站点配置已保存 -> {args.out}")
    print(json.dumps(cfg, ensure_ascii=False, indent=1))
    print("\n[!] 请人工核对上面的章节 URL 模式与正文容器，再执行 fetch")
    return 0


# ---------------------------------------------------------------- fetch

class Fetcher:
    def __init__(self, cfg, book_id, cache_dir, workers=8):
        self.cfg = cfg
        self.book_id = book_id
        self.cache = cache_dir
        self.host = S.host_of(cfg["base"])
        self.workers = workers
        os.makedirs(cache_dir, exist_ok=True)
        self.ch_url = cfg.get("chapter_url", "/book/{book_id}-{n}.html")
        self.paged_url = cfg.get("paged_url")
        self.content_class = cfg.get("content_class")
        self.content_re = re.compile(cfg["content_regex"], re.S) if cfg.get("content_regex") else None
        self.title_re = re.compile(cfg.get("title_regex", r"<h1[^>]*>(.*?)</h1>"), re.S)
        self.next_re = re.compile(cfg.get("next_paged_regex", r""), re.S) if cfg.get("next_paged_regex") else None
        self.lock = threading.Lock()
        self.stats = {"ok": 0, "cached": 0, "fail": 0}
        self.failed = []

    def _path(self, tpl, **kw):
        return tpl.format(book_id=self.book_id, **kw)

    def _parse(self, raw):
        """提取正文段落。优先按 class 定位（配对扫描），回退到正则。"""
        body = None
        if self.content_class:
            body = S.extract_by_class(raw, self.content_class)
        if body is None and self.content_re:
            m = self.content_re.search(raw)
            if m:
                body = m.group(1)
        if body is None:
            return None, []
        paras = []
        for p in re.findall(r"<p[^>]*>(.*?)</p>", body, re.S):
            paras.extend(S.strip_html(p))
        tm = self.title_re.search(raw)
        title = ""
        if tm:
            t = S.strip_html(tm.group(1))
            title = t[0] if t else ""
        return title, paras

    def _next_page_path(self, raw, page):
        """返回同一章的下一页路径（None 表示没有）。"""
        if not self.next_re:
            return None
        m = self.next_re.search(raw)
        if not m:
            return None
        href = m.group(1)
        mm = re.search(r"-(\d+)\.html$", href)
        if not mm:
            return None
        nxt_page = int(mm.group(1))
        if nxt_page != page + 1:
            return None
        if href.startswith("/"):
            return href
        if self.paged_url:
            return self._path(self.paged_url, n=self._cur_n, page=nxt_page)
        return None

    def chapter(self, n):
        path = os.path.join(self.cache, "%d.json" % n)
        if os.path.exists(path):
            try:
                d = json.load(open(path, encoding="utf-8"))
                if d.get("done"):
                    return "cached"
            except Exception:
                pass
        p = self._path(self.ch_url, n=n)
        self._cur_n = n
        raw = S.http_get(self.host, p, referer=self.cfg["base"] + self.cfg.get("list_url", "/"))
        if raw is None:
            return "FAIL-fetch"
        title, paras = self._parse(raw)
        if title is None:
            return "FAIL-parse"
        page = 1
        # 长章节分页
        while True:
            npath = self._next_page_path(raw, page)
            if not npath:
                break
            raw2 = S.http_get(self.host, npath, referer=self.cfg["base"] + self.cfg.get("list_url", "/"))
            if raw2 is None:
                break
            _, p2 = self._parse(raw2)
            if not p2:
                break
            paras.extend(p2)
            page += 1
            raw = raw2
        rec = {"n": n, "title": title, "paras": paras, "pages": page, "done": True}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, path)
        return "ok p%d c%d" % (page, len(paras))

    def run(self, total):
        t0 = time.time()
        def work(n):
            r = self.chapter(n)
            with self.lock:
                if r == "cached":
                    self.stats["cached"] += 1
                elif r.startswith("ok"):
                    self.stats["ok"] += 1
                    done = self.stats["ok"] + self.stats["cached"]
                    if done % 25 == 0 or done == total:
                        el = time.time() - t0
                        print("  [%d/%d] ok=%d cached=%d fail=%d  %.0fs (%.1f 章/分)"
                              % (done, total, self.stats["ok"], self.stats["cached"],
                                 self.stats["fail"], el,
                                 (done - self.stats["cached"]) / max(el, 1) * 60), flush=True)
                else:
                    self.stats["fail"] += 1
                    self.failed.append((n, r))
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            list(ex.map(work, range(1, total + 1)))
        print("DONE ok=%d cached=%d failed=%d 耗时=%.0fs"
              % (self.stats["ok"], self.stats["cached"], self.stats["fail"], time.time() - t0))
        if self.failed:
            print("FAILED:", self.failed[:60])
            with open(os.path.join(self.cache, "_failed.json"), "w", encoding="utf-8") as f:
                json.dump(self.failed, f)
        return 0 if not self.failed else 1


def cmd_fetch(args):
    cfg = S.load_site(args.site)
    f = Fetcher(cfg, args.bookid, args.cache, args.workers)
    print("[*] 站点 %s  章节数 %d  并发 %d" % (cfg.get("name"), args.total, args.workers))
    return f.run(args.total)


# ---------------------------------------------------------------- merge

def norm(s):
    return re.sub(r"[\s　]", "", s or "")


def load_catalog(path):
    data = json.load(open(path, encoding="utf-8"))
    out = []
    for v in data.get("volumes", []):
        for c in v.get("chapters", []):
            out.append((v.get("name", ""), c.get("title", ""), c.get("item_id", "")))
    return out


def load_cache(cache):
    """缓存章节 -> {归一化标题: [章节记录...]}。只接受 <数字>.json 命名的章节文件。"""
    buckets = defaultdict(deque)
    for p in glob.glob(os.path.join(cache, "*.json")):
        base = os.path.basename(p)[:-5]
        if not base.isdigit():
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, dict) and d.get("done"):
            buckets[norm(d.get("title", ""))].append(d)
    return buckets


def cmd_merge(args):
    cat = load_catalog(args.catalog)
    buckets = load_cache(args.cache)
    ordered, missing = [], []
    for vol, title, item_id in cat:
        key = norm(title)
        if buckets.get(key):
            ordered.append((vol, title, buckets[key].popleft()))
        else:
            missing.append((vol, title))
    leftover = sum(len(q) for q in buckets.values())
    chars = sum(sum(len(x) for x in d["paras"]) for _, _, d in ordered)

    L = []
    L.append(f"《{args.title}》")
    if args.author:
        L.append(f"作者：{args.author}")
    L.append(f"状态：{'已完结' if args.done else '连载中'}")
    L.append(f"规模：全 {len(ordered)} 章，正文约 {chars/10000:.1f} 万字")
    L.append(f"来源：{args.source}")
    L.append("")
    L.append("=" * 66)
    L.append("目　录")
    L.append("=" * 66)
    cur = None
    for vol, title, d in ordered:
        if vol != cur:
            cur = vol
            L.append("")
            L.append(f"【{vol}】")
        L.append(f"  {title}")
    L.append("")
    L.append("=" * 66)
    L.append("正　文")
    L.append("=" * 66)
    for vol, title, d in ordered:
        L.append("")
        L.append("─" * 66)
        L.append(title)
        L.append("─" * 66)
        L.extend(d["paras"])
        L.append("")

    text = "\n".join(L)
    os.makedirs(args.out, exist_ok=True)
    u8 = os.path.join(args.out, f"{args.title}.txt")
    with open(u8, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(text)
    gbk = os.path.join(args.out, f"{args.title}-GBK.txt")
    with open(gbk, "w", encoding="gb18030", newline="\r\n") as f:
        f.write(text)

    print(f"[OK] 章节 {len(ordered)}  字数 {chars} ({chars/10000:.1f}万)")
    print(f"     缺失(目录有缓存无) {len(missing)}  多余(缓存有目录无) {leftover}")
    print(f"     UTF-8 -> {u8}  ({os.path.getsize(u8)/1024/1024:.2f} MB)")
    print(f"     GBK   -> {gbk}  ({os.path.getsize(gbk)/1024/1024:.2f} MB)")
    return 0


# ---------------------------------------------------------------- verify

def cmd_verify(args):
    cat = load_catalog(args.catalog)
    buckets = load_cache(args.cache)
    cats = [norm(t) for _, t, _ in cat]
    cache_keys = set(buckets)
    only_cat = [t for t in cats if t not in cache_keys]
    only_cache = [k for k in cache_keys if k not in set(cats)]
    chars = 0
    empties = []
    for k, q in buckets.items():
        for d in q:
            c = sum(len(x) for x in d["paras"])
            chars += c
            if c == 0:
                empties.append(d["n"])
    print(f"目录章节 {len(cat)}   缓存章节 {sum(len(q) for q in buckets.values())}")
    print(f"总字数 {chars} ({chars/10000:.1f}万)")
    print(f"目录有缓存无: {len(only_cat)} {only_cat[:8]}")
    print(f"缓存有目录无: {len(only_cache)} {list(only_cache)[:8]}")
    print(f"空章节: {empties}")
    # 站点 URL 序号 vs 标题章号：正常会有出入（站点会插页/跳号），不影响输出
    off = []
    for k, q in buckets.items():
        for d in q:
            m = re.match(r"^第\s*(\d+)\s*章", d.get("title", ""))
            if m and int(m.group(1)) != d["n"]:
                off.append((d["n"], int(m.group(1))))
    print(f"提示: 站点URL序号≠标题章号 的章节 {len(off)} 处 —— 这是站点自身跳号/插页，"
          f"输出顺序以目录标题为准，不影响正确性")
    ok = not only_cat and not only_cache and not empties
    print("\n质检结论:", "PASS（章节集合与目录完全一致，无空章）" if ok else "需人工复核")
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="网文 TXT 整本下载器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("catalog", help="取番茄官方目录")
    p.add_argument("--bookid", required=True)
    p.add_argument("--out", default="catalog.json")
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("probe", help="自动探测书站结构")
    p.add_argument("--url", required=True)
    p.add_argument("--out", default="site.json")
    p.add_argument("--name", default=None)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("fetch", help="逐章抓正文")
    p.add_argument("--site", default="site.json")
    p.add_argument("--bookid", required=True)
    p.add_argument("--total", type=int, required=True)
    p.add_argument("--cache", default="cache")
    p.add_argument("--workers", type=int, default=8)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("merge", help="合并为 TXT")
    p.add_argument("--catalog", default="catalog.json")
    p.add_argument("--cache", default="cache")
    p.add_argument("--title", required=True)
    p.add_argument("--author", default="")
    p.add_argument("--source", default="番茄小说（正版首发）· 爱书库全文转载")
    p.add_argument("--out", default=".")
    p.add_argument("--done", action="store_true", default=True)
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("verify", help="质检")
    p.add_argument("--catalog", default="catalog.json")
    p.add_argument("--cache", default="cache")
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
