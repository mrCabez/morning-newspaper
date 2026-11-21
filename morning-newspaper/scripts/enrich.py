"""Reads the opening of a candidate story's own page, so a story that came with only its title
(Google News search feeds send no summary) gets the facts its outlet wrote, at no token cost.

Used by collect.py for the first candidates of each section; it never runs on its own during an
edition. Every request is a read: GET for pages and feeds, plus, for a Google News link, the one
lookup Google News itself makes to turn its redirect into the outlet's address. Nothing here
writes anywhere: collect.py stores what it returns in WORK/collected.json.

Order for each story:
1. a direct link (from an RSS feed) is opened as it is;
2. a Google News link: first the outlet's own site search feed (WordPress sites answer
   "?s=<words>&feed=rss2"), which gives the real address and an excerpt in one small request;
   then Google News' own lookup, which gives the real address;
3. the page is read: the description it declares (og:description, JSON-LD) and its first
   paragraphs. A page that says nothing beyond the title leaves the story as it was.
"""

import base64
import datetime as dt
import html
import json
import re
import urllib.parse
import urllib.request

MAX_CHARS = 380        # what the model reads per story: the lead, never the article
PAGE_BYTES = 1_500_000  # article pages; Google News' redirect page is read whole (its token is at the end)

# Paragraphs that are page furniture, not the story (cookie banners, "read more", credits, ads).
BOILER = (
    r"(cookie|newsletter|assine|assinante|inscreva|leia (tamb[eé]m|mais)|veja (tamb[eé]m|mais)|siga[- ]nos|"
    r"clique aqui|whatsapp|telegram|todos os direitos|copyright|©|pol[ií]tica de privacidade|"
    r"read (more|also)|sign up|subscribe|all rights reserved|privacy policy|follow us|"
    r"lea (tambi[eé]n|m[aá]s)|suscr[ií]bete|s[ií]guenos|publicidade|advertisement|publicidad|"
    r"^foto:|^imagem:|^image:|^cr[eé]dito|/divulga[cç][aã]o|/reprodu[cç][aã]o)")
# Blocks whose paragraphs are never the story.
SKIP_BLOCKS = r"<(script|style|noscript|nav|footer|header|aside|form|figure|figcaption)\b.*?</\1\s*>"


def _attr(tag, name):
    m = re.search(rf'\b{name}\s*=\s*("([^"]*)"|\'([^\']*)\')', tag, re.I)
    return html.unescape(m.group(2) if m.group(2) is not None else m.group(3)) if m else ""


INLINE = r"</?(?:a|b|i|em|strong|span|u|small|sup|sub|abbr|cite|mark|font)\b[^>]*>"


def _plain(fragment):
    txt = re.sub(INLINE, "", fragment or "", flags=re.I)
    txt = html.unescape(re.sub(r"<[^>]+>", " ", txt))
    txt = re.sub(r"\s+", " ", txt).strip()
    return re.sub(r"\s+([,.;:!?)])", r"\1", txt)


def meta(page):
    """{"description": ..., "published": ...} declared by the page (Open Graph, meta, JSON-LD, <time>)."""
    out = {}
    for tag in re.findall(r"<meta\b[^>]*>", page[:400_000], re.I):
        key = (_attr(tag, "property") or _attr(tag, "name") or _attr(tag, "itemprop")).lower()
        val = _attr(tag, "content").strip()
        if not val:
            continue
        if key in ("og:description", "twitter:description", "description") and len(val) > len(out.get("description", "")):
            out["description"] = val
        elif key in ("article:published_time", "datepublished", "date", "dc.date.issued", "pubdate") \
                and "published" not in out:
            out["published"] = val
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                stack.extend(v for v in d.values() if isinstance(v, (list, dict)))
                if str(d.get("@type", "")).lower().endswith(("article", "newsarticle", "reportagenewsarticle")) \
                        or "articleBody" in d:
                    if d.get("datePublished") and "published" not in out:
                        out["published"] = str(d["datePublished"])
                    if isinstance(d.get("articleBody"), str) and len(d["articleBody"]) > 80:
                        out["body"] = _plain(d["articleBody"])[:2000]
                    if isinstance(d.get("description"), str) and len(d["description"]) > len(out.get("description", "")):
                        out["description"] = d["description"].strip()
    if "published" not in out:
        m = re.search(r'<time\b[^>]*\bdatetime="([^"]+)"', page, re.I)
        if m:
            out["published"] = m.group(1)
    return out


def paragraphs(page):
    """The page's text paragraphs, in order, without the furniture around the story."""
    body = page
    m = re.search(r"<article\b.*?</article\s*>", page, re.S | re.I)
    if m and len(_plain(m.group(0))) > 200:
        body = m.group(0)
    body = re.sub(SKIP_BLOCKS, " ", body, flags=re.S | re.I)
    out = []
    for p in re.findall(r"<p\b[^>]*>(.*?)</p\s*>", body, re.S | re.I):
        links = len(re.findall(r"<a\b", p, re.I))
        txt = _plain(p)
        if len(txt) < 60 or re.search(BOILER, txt, re.I) or links > 2:
            continue
        out.append(txt)
    return out


def published(value):
    """A page date as an aware datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", value)
        if not m:
            return None
        when = dt.datetime.fromisoformat(m.group(1))
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def lead(page, title, story_words, generic=()):
    """(text, published) for one story page. The text is the page's own description or its first
    paragraphs, whichever tells more than the title, cut at a sentence near MAX_CHARS. "" when the
    page says nothing beyond its title (a site name as description, a paywall stub)."""
    info = meta(page)
    title_w = story_words(title)
    banned = {w for g in generic for w in story_words(g)}

    def adds(txt):
        w = story_words(txt) - banned
        return len(w - title_w) >= 4

    paras = paragraphs(page)
    # Start at the first paragraph that is about the story (shares a word with its title).
    # None of them is: no paragraph is used (a paywall or cookie notice is not the story).
    start = next((i for i, p in enumerate(paras[:8]) if story_words(p) & title_w), None)
    body = " ".join(paras[start:start + 4]) if start is not None else ("" if paras else info.get("body", ""))
    desc = info.get("description", "")
    text = ""
    for cand in (body, desc, info.get("body", "")):
        cand = _plain(cand)
        if cand and adds(cand):
            text = cand
            break
    return cut(text), published(info.get("published"))


def cut(text, limit=MAX_CHARS):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    end = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if end >= limit // 2:
        return head[:end + 1]
    return head.rsplit(" ", 1)[0].rstrip(",;:") + "…"


# ---------------------------------------------------------------- Google News links

GNEWS_HOST = "https://news.google.com/"
GNEWS = "^" + re.escape(GNEWS_HOST) + r"(?:rss/)?articles/([A-Za-z0-9_-]+)"


def is_gnews(url):
    return bool(re.match(GNEWS, url or ""))


def gnews_offline(url):
    """Older Google News ids carry the address inside them (base64). Newer ones do not: None."""
    m = re.match(GNEWS, url or "")
    if not m:
        return None
    token = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return None
    found = re.search(rb"https?://[\x21-\x7e]+", raw)
    if not found or b"AU_yqL" in raw[:12]:
        return None
    return found.group(0).decode("ascii", "ignore").split("\x00")[0]


def gnews_lookup(url, get, post):
    """Google News' own lookup (the call its page makes when a reader clicks): page token and
    signature from the redirect page, then one request that answers with the outlet's address."""
    m = re.match(GNEWS, url or "")
    if not m:
        return None
    aid = m.group(1)
    page = get(f"https://news.google.com/rss/articles/{aid}", limit=None).decode("utf-8", "ignore")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sg and ts):
        return None
    inner = (f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{aid}",{int(ts.group(1))},"{sg.group(1)}"]')
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner]]]))
    answer = post("https://news.google.com/_/DotsSplashUi/data/batchexecute", body).decode("utf-8", "ignore")
    found = re.search(r'garturlres\\",\\"(https?://[^"\\]+)', answer)
    return found.group(1) if found else None


def site_search(site, title, read_feed, same_story):
    """The outlet's own search feed (WordPress: ?s=…&feed=rss2): its item for this title, or None."""
    if not str(site or "").startswith("https://"):
        return None
    words = [w for w in re.findall(r"\w+", title) if len(w) > 3][:6]
    if len(words) < 2:
        return None
    url = site.rstrip("/") + "/?" + urllib.parse.urlencode({"s": " ".join(words), "feed": "rss2"})
    for it in read_feed(url)[:10]:
        if same_title(it["title"], title) and str(it.get("url", "")).startswith("https://"):
            return it
    return None


def same_title(a, b):
    """The same story's title, not just the same subject: the shorter title's words are all in the
    longer one, but for one ("City council approves school budget" is not "City council approves
    hospital expansion"), and its numbers are the same. Replacing a story's address needs this certainty; when unsure, Google
    News' own lookup is used instead."""
    na, nb = (set(re.findall(r"\d+(?:[.,]\d+)*", t)) for t in (a, b))
    if na and nb and na != nb:
        return False  # "cuts 10 jobs" is not "cuts 20 jobs"
    wa, wb = (set(w.lower() for w in re.findall(r"\w{3,}", t)) | n for t, n in ((a, na), (b, nb)))
    if len(wa) > len(wb):
        wa, wb = wb, wa
    return len(wa) >= 3 and len(wa - wb) <= (1 if len(wa) >= 6 else 0)


def enrich_one(item, deps):
    """What the story's page adds: {"summary", "page_url", "published"} or {} when nothing.
    deps: get(url, limit), post(url, body), read_feed(url), story_words(txt), same_story(a, b)."""
    title = item["title"]
    url = item["url"]
    feed_summary = ""
    if is_gnews(url):
        direct = gnews_offline(url)
        if not direct and item.get("site"):
            try:
                hit = site_search(item["site"], title, deps["read_feed"], deps["same_story"])
            except Exception:
                hit = None
            if hit:
                direct = hit["url"]
                feed_summary = hit.get("summary") or ""
        if not direct:
            direct = gnews_lookup(url, deps["get"], deps["post"])
        if not direct:
            return {}
        url = direct
    if not url.startswith("https://"):
        url = re.sub(r"^http:", "https:", url)
    text, when = "", None
    try:
        page = deps["get"](url, limit=PAGE_BYTES).decode("utf-8", "ignore")
        text, when = lead(page, title, deps["story_words"], generic=[item.get("outlet", "")])
    except Exception:
        page = ""
    if not text and feed_summary and len(deps["story_words"](feed_summary) - deps["story_words"](title)) >= 4:
        text = cut(feed_summary)
    out = {}
    if url != item["url"]:
        out["page_url"] = url
    if when:
        out["published"] = when  # an old page is flagged even when it adds nothing to the title
    if text:
        out["summary"], out["page_url"] = text, url
    return out
