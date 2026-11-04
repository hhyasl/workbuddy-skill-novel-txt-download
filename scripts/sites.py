# -*- coding: utf-8 -*-
"""网文抓取站点适配层：内置适配器 + 通用自动探测。

站点配置（site.json）字段说明见 references/sites.md
"""
import re, json, html, ssl, time, random, threading
import http.client

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------- HTTP

def http_get(host, path, referer=None, timeout=20, tries=4, extra_headers=None):
    """每次请求新建连接。

    经验：这类书站对 keep-alive 连接复用不友好，服务端会挂起连接直到超时，
    表现为每个请求都卡满 timeout。必须禁用连接复用。
    失败返回 None（调用方负责退避重试）。
    """
    for i in range(tries):
        c = None
        try:
            c = http.client.HTTPSConnection(host, 443, context=CTX, timeout=timeout)
            c.connect()
            headers = {
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Connection": "close",
            }
            if referer:
                headers["Referer"] = referer
            if extra_headers:
                headers.update(extra_headers)
            c.request("GET", path, headers=headers)
            r = c.getresponse()
            raw = r.read()
            if r.status != 200 or len(raw) < 1200:
                raise IOError("status=%s len=%d" % (r.status, len(raw)))
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("gb18030", "ignore")
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(0.4 * (i + 1) + random.random() * 0.25)
        finally:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
    return None


def host_of(url):
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else url


def path_of(url):
    m = re.match(r"https?://[^/]+(/.*)?$", url)
    return (m.group(1) or "/") if m else "/"


# ---------------------------------------------------------------- 解析

def strip_html(fragment):
    t = re.sub(r"<br\s*/?>", "\n", fragment)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t).replace("\u3000", " ")
    return [ln.strip() for ln in t.split("\n") if ln.strip()]


def extract_title(raw):
    m = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.S)
    if m:
        t = strip_html(m.group(1))
        if t:
            return t[0]
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S)
    if m:
        t = strip_html(m.group(1))
        if t:
            return re.sub(r"[_《》].*$", "", t[0]).strip() or t[0]
    return ""


def _div_blocks(raw):
    """取出所有 div 开标签及其配对结束位置。"""
    out = []
    for m in re.finditer(r"<div\b[^>]*>", raw):
        start = m.end()
        depth = 1
        pos = start
        while depth > 0:
            nxt_open = re.compile(r"<div\b[^>]*>").search(raw, pos)
            nxt_close = re.compile(r"</div>").search(raw, pos)
            if not nxt_close:
                break
            if nxt_open and nxt_open.start() < nxt_close.start():
                depth += 1
                pos = nxt_open.end()
            else:
                depth -= 1
                pos = nxt_close.end()
                if depth == 0:
                    out.append((m.group(0), raw[start:nxt_close.start()]))
                    break
    return out


def extract_by_class(raw, class_name):
    """按 class 精确定位 div 内部内容（配对扫描，避免正则截断嵌套结构）。"""
    pat = re.compile(r'<div\b[^>]*class="([^"]*\b' + re.escape(class_name) + r'\b[^"]*)"[^>]*>', re.I)
    for m in pat.finditer(raw):
        depth = 1
        pos = m.end()
        while depth > 0:
            nxt_open = re.compile(r"<div\b[^>]*>").search(raw, pos)
            nxt_close = re.compile(r"</div>").search(raw, pos)
            if not nxt_close:
                return None
            if nxt_open and nxt_open.start() < nxt_close.start():
                depth += 1
                pos = nxt_open.end()
            else:
                depth -= 1
                pos = nxt_close.end()
                if depth == 0:
                    return raw[m.end():nxt_close.start()]
    return None


def class_names(tag):
    m = re.search(r'class="([^"]*)"', tag)
    return m.group(1).split() if m else []


def probe_content_block(raw):
    """自动探测正文容器。

    优先返回 class 含 content/txt/chapter/read/zw 的容器，
    其次按 <p> 总文本量最大的容器。
    """
    cands = []
    for tag, body in _div_blocks(raw):
        ps = re.findall(r"<p[^>]*>(.*?)</p>", body, re.S)
        if not ps:
            continue
        chunks = ["".join(strip_html(p)) for p in ps]
        text = "".join(chunks)
        if len(text) < 400:
            continue
        avg = len(text) / len(ps)
        # 排除"推荐书单"型容器：段落多、每段都很短（书名列表）
        if len(ps) > 8 and avg < 18:
            continue
        cands.append((len(text), len(ps), tag, ps))
    if not cands:
        return None, None, None
    maxlen = max(c[0] for c in cands)
    # 只保留文本量接近最大的那批（外层容器与内层容器文本量相同）
    top = [c for c in cands if c[0] >= maxlen * 0.9]
    # 第一轮：class 名精确命中（最内层的正文容器）
    exact = ("content", "txt", "chapter", "text", "neirong", "readtxt", "article", "bookcontent")
    for c in sorted(top, key=lambda c: -c[0]):
        cls = class_names(c[2])
        low = [x.lower() for x in cls]
        if any(x in exact for x in low):
            return c[2], c[3], cls[0]
    # 第二轮：class 名子串命中
    prefer = ("content", "txt", "chapter", "read", "zw", "text", "neirong")
    for c in sorted(top, key=lambda c: -c[0]):
        cls = class_names(c[2])
        if any(p in " ".join(cls).lower() for p in prefer):
            return c[2], c[3], cls[0]
    best = max(top, key=lambda c: c[0])
    cls = class_names(best[2])
    return best[2], best[3], (cls[0] if cls else None)


# ---------------------------------------------------------------- 内置适配器

ISHUKU = {
    "name": "ishuku",
    "base": "https://www.ishuku.com",
    "list_url": "/book/{book_id}.html",
    "chapter_url": "/book/{book_id}-{n}.html",
    "paged_url": "/book/{book_id}-{n}-{page}.html",
    "content_regex": r'<div class="content">(.*?)<div class="btnEW"',
    "title_regex": r"<h1[^>]*>(.*?)</h1>",
    "next_paged_regex": r'<a href="(/book/\d+-\d+-(\d+)\.html)"[^>]*>\s*下一页',
    "note": "正文容器 <div class=\"content\">，长章节分页形如 -2/-3/-4",
}


def builtin_sites():
    return {"ishuku": dict(ISHUKU)}


def load_site(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if cfg.get("name") == "ishuku":
        base = builtin_sites()["ishuku"]
        base.update({k: v for k, v in cfg.items() if v})
        return base
    return cfg
