#!/usr/bin/env python3
"""Finds direct RSS feeds for a newspaper's sections, so more stories arrive with their own
summary (Google News search feeds send none).

It looks at the outlets that brought the most candidates to each section in today's collection
(WORK/collected.json), finds each outlet's own feed (the <link rel="alternate"> of its home page,
else /feed/, /rss, /feed.xml, /rss.xml) and checks it: enough recent items, most with a summary
and, outside the local section, some carrying the section's keywords. It prints what it found; only
with --write, after the reader said yes, does it add the feeds to config.json.

Read-only GET requests to the outlets' own sites; writes only the config file it was given.

Usage:
  python3 feeds.py --config WORK/config.json --collected WORK/collected.json [--write]
"""
import argparse
import collections
import concurrent.futures as cf
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402  (same folder: its fetch, feed reader and keyword match)

PATHS = ["/feed/", "/rss", "/feed.xml", "/rss.xml", "/rss/"]
# Aggregators and portals whose "site" is not one outlet's newsroom.
SKIP_SITES = {"news.google.com", "msn.com", "yahoo.com", "flipboard.com", "newsbreak.com", "youtube.com"}
PER_SECTION = 3
MIN_ITEMS = 5


def site_of(n):
    site = n.get("site") or ""
    if not site:
        url = n.get("url") or ""
        if "news.google.com" in url:
            return ""
        p = urllib.parse.urlparse(url)
        site = f"{p.scheme}://{p.netloc}" if p.scheme == "https" and p.netloc else ""
    host = urllib.parse.urlparse(site).netloc.lower().removeprefix("www.")
    return "" if not host or any(host == s or host.endswith("." + s) for s in SKIP_SITES) else site.rstrip("/")


def discover(site):
    """Feed URLs to try for a site: the ones its home page declares first, then the usual paths."""
    found = []
    try:
        home = collect.fetch(site + "/", tries=1).decode("utf-8", "ignore")
        for tag in re.findall(r"<link\b[^>]*>", home[:300_000], re.I):
            if re.search(r"application/(rss|atom)\+xml", tag, re.I):
                href = re.search(r'href\s*=\s*["\']([^"\']+)', tag, re.I)
                if href:
                    found.append(urllib.parse.urljoin(site + "/", href.group(1)))
    except Exception:
        pass
    found += [site + p for p in PATHS]
    out = []
    for u in found:
        u = re.sub(r"^http:(?=//)", "https:", u)
        if u.startswith("https://") and u not in out and "comments" not in u:
            out.append(u)
    return out[:6]


def check(url, keywords):
    """(items, with summary, with a keyword) of a feed, or None when it is not a usable feed."""
    try:
        items = collect.read_rss(url)
    except Exception:
        return None
    if len(items) < MIN_ITEMS:
        return None
    summed = sum(1 for it in items if it["summary"])
    fit = sum(1 for it in items if keywords and collect.keyword_hit(f"{it['title']} {it['summary']}", keywords))
    return len(items), summed, fit


def first_good(site, keywords, local):
    """The site's first feed that is worth adding, as (url, items, with summary, with a keyword)."""
    for url in discover(site):
        res = check(url, keywords)
        if not res:
            continue
        n, summed, fit = res
        # Most items must carry a summary (the point of a direct feed) and, outside the local
        # section, the feed must bring stories of the section, not the whole paper.
        if summed * 2 >= n and (local or not keywords or fit * 5 >= n):
            return url, n, summed, fit
        return None
    return None


def is_regional(n):
    """A local candidate that names the city or a place of its region (collect.py's flags)."""
    return any(f == "city" or f.startswith(("city (", "near ")) for f in n.get("flags") or [])


def suggest(cfg, collected):
    """{section id: [(feed url, items, with summary, with a keyword)]}"""
    city = (cfg.get("city") or {}).get("name", "")
    jobs = []
    for sec in cfg["sections"]:
        have = {urllib.parse.urlparse(f).netloc.lower().removeprefix("www.") for f in sec.get("feeds") or []}
        local = bool(city) and any(collect.names_city(q, city) for q in sec.get("queries") or [])
        cands = (collected.get("news") or {}).get(sec["id"]) or []
        if local:  # an outlet of another town only counts with stories of this region
            cands = [n for n in cands if is_regional(n)]
        counts = collections.Counter(site_of(n) for n in cands)
        counts.pop("", None)
        keywords = [] if local else [k for k in sec.get("keywords") or [] if collect.words(str(k))]
        sites = [x for x, _ in counts.most_common()
                 if urllib.parse.urlparse(x).netloc.lower().removeprefix("www.") not in have][:PER_SECTION + 2]
        jobs += [(sec["id"], site, keywords, local) for site in sites]
    out = {s["id"]: [] for s in cfg["sections"]}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda j: first_good(j[1], j[2], j[3]), jobs))
    for (sid, *_), res in zip(jobs, results):
        if res and len(out[sid]) < PER_SECTION:
            out[sid].append(res)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--collected", required=True, help="WORK/collected.json of a collection run")
    ap.add_argument("--write", action="store_true", help="add the feeds found to config.json (only with the reader's yes)")
    a = ap.parse_args()
    with open(a.config, encoding="utf-8") as f:
        cfg = json.load(f)
    with open(a.collected, encoding="utf-8") as f:
        collected = json.load(f)
    found = suggest(cfg, collected)
    titles = {s["id"]: s["title"] for s in cfg["sections"]}
    any_found = False
    for sid, picks in found.items():
        for url, n, summed, fit in picks:
            any_found = True
            print(f"FEED: {titles[sid]} · {url} · {n} items, {summed} with a summary"
                  + (f", {fit} with the section's keywords" if fit else ""))
    if not any_found:
        print("FEEDS: no direct feed with summaries found for today's outlets.")
        return 0
    if a.write:
        for sec in cfg["sections"]:
            feeds = list(sec.get("feeds") or [])
            feeds += [u for u, *_ in found.get(sec["id"], []) if u not in feeds]
            sec["feeds"] = feeds
        with open(a.config, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        with open(os.path.join(os.path.dirname(os.path.abspath(a.config)), "config.changed"), "w") as f:
            f.write("feeds")
        print("Saved in", a.config, "(assemble.py publishes it with today's edition)")
    else:
        print("Nothing changed. Ask the reader; on a yes, run again with --write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
