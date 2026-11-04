#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""低速补抓 fetch 阶段失败的章节。

用法：
    python refetch.py --site site.json --bookid 157451967786 --cache cache

策略：单线程、逐章、章间长间隔 —— 绕开限流。失败的章节再额外重试两轮，
每轮间隔递增。复用 novel.py 的 Fetcher，保证标题与解析结果和正常抓取完全一致
（merge 按标题配对，标题为空会导致章节配不上）。
全部搞定后退出码 0，仍有失败则退出码 1。
"""
import argparse, json, os, random, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from novel import Fetcher
import sites as S


def main():
    ap = argparse.ArgumentParser(description="低速补抓失败章节")
    ap.add_argument("--site", default="site.json")
    ap.add_argument("--bookid", required=True)
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--gap", type=float, default=6.0, help="章间基础间隔秒数")
    args = ap.parse_args()

    cfg = S.load_site(args.site)
    fail_file = os.path.join(args.cache, "_failed.json")
    if not os.path.exists(fail_file):
        print("[!] 没有 _failed.json，无需补抓")
        return 0
    failed = json.load(open(fail_file, encoding="utf-8"))
    nums = sorted({n for n, _ in failed})
    print(f"[*] 待补抓 {len(nums)} 章: {nums}")

    f = Fetcher(cfg, args.bookid, args.cache, workers=1)
    still = []
    for i, n in enumerate(nums):
        r = f.chapter(n)
        if not (r.startswith("ok") or r == "cached"):
            for attempt in range(2):
                time.sleep(args.gap * (attempt + 1) + random.random() * 3)
                r = f.chapter(n)
                if r.startswith("ok") or r == "cached":
                    break
        state = "OK" if (r.startswith("ok") or r == "cached") else "仍失败"
        print(f"  [{i+1}/{len(nums)}] n={n} -> {state} ({r})")
        if state == "仍失败":
            still.append(n)
        time.sleep(args.gap + random.random() * 4)

    print(f"\n补抓完成: 成功 {len(nums)-len(still)} / {len(nums)}")
    if still:
        print("仍失败:", still)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
