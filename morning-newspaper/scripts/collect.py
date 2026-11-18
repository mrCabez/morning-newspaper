#!/usr/bin/env python3
"""Automatic news-gathering for the morning-newspaper skill.

In one pass, without spending model tokens, it fetches:
  - candidate stories per section (Google News RSS searches + direct RSS feeds from config.json),
    already filtered by date window, with stories from recent editions removed, taking turns
    between the searches so one busy search cannot crowd out the others;
  - today's weather forecast (wttr.in, with Open-Meteo as a fallback);
  - "On this day" candidates (Wikipedia, in the reader's language when available).

It writes everything to WORK/collected.json (used later by assemble.py) and prints a compact
summary with a short ID per story (a1, b3, ...; h1... for history), which is what the model reads.
Every run also appends its command and output to WORK/run.log (for debug reports; the model does not read it).

Standard library only. Only performs read-only GET requests to the hosts listed in the config
and to wttr.in, api.open-meteo.com and *.wikipedia.org. Only writes inside WORK.

With --from-results FILE it touches no network at all: it takes the web search results the model
gathered (when the sources are blocked, see "No network" in SKILL.md) and applies the same filters,
ranking, flags and IDs, so the rest of the edition works exactly as after a normal collection.

Usage:
  python3 collect.py --config config.json --work WORK_DIR [--index index.json] [--date YYYY-MM-DD]
                     [--from-results results.json]
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

UA = "morning-newspaper/1.10.0 (+github.com/oantuneshenriqueo/morning-newspaper; personal newspaper; RSS reader)"
TIMEOUT = 12
MAX_BYTES = 5_000_000
# Stories are only compared against the latest editions: a headline from months ago must not
# block today's news on a recurring subject (same number assemble.py keeps titles/links for).
DEDUP_EDITIONS = 14


# "Did you know?" rotates through one theme per day so the box does not keep landing on the same
# famous trivia. Readers can replace this list with "did_you_know_themes" in config.json.
DYK_THEMES = [
    "origin of a programming language", "how a piece of everyday hardware works inside",
    "history of a file format or protocol", "origin of a computing term (not 'bug')",
    "a curious record in computing", "early video game history", "how the internet's infrastructure works",
    "history of a famous algorithm", "cryptography and codes through history", "space missions and their computers",
    "history of a keyboard key or symbol", "how a common app feature was invented", "storage media history",
    "a programming convention and where it came from", "history of an operating system",
    "how GPS, Wi-Fi or Bluetooth works", "computing in your country's history", "open-source software history",
    "a surprising fact about electricity or batteries", "the science behind a screen technology",
    "history of artificial intelligence before 2000", "a famous software failure and its lesson",
    "units and numbers in computing (bytes, hertz, binary)", "the history of email or the web",
    "an invention that came from a game or a toy", "how data compression works",
    "history of the mouse, touchscreen or another input device", "a curiosity about databases",
    "the origin of a tech company's name", "how undersea cables or satellites carry data",
]

# English Wikipedia articles per theme, read through the cached page-summary endpoint when the
# search API refuses (it answers 429 to shared cloud addresses far more often than page summaries
# do). Themes without seeds (and the reader's own themes) only use the search.
DYK_SEEDS = {
    'origin of a programming language': ['Python (programming language)', 'C (programming language)', 'Lua (programming language)', 'Ruby (programming language)', 'COBOL', 'Go (programming language)', 'Java (programming language)', 'BASIC'],
    'how a piece of everyday hardware works inside': ['Hard disk drive', 'Solid-state drive', 'Lithium-ion battery', 'Inkjet printing', 'Laser printing', 'Microwave oven', 'Quartz clock', 'Optical mouse'],
    'history of a file format or protocol': ['JPEG', 'GIF', 'PDF', 'MP3', 'ZIP (file format)', 'HTTP', 'Domain Name System', 'Simple Mail Transfer Protocol'],
    "origin of a computing term (not 'bug')": ['HTTP cookie', 'Computer virus', 'Spooling', 'Booting', 'Pixel', 'Wiki', 'Firewall (computing)', 'Patch (computing)'],
    'a curious record in computing': ['Largest known prime number', 'ENIAC', "Moore's law", 'Year 2038 problem', 'Colossus computer', 'Manchester Baby'],
    'early video game history': ['Tennis for Two', 'Spacewar!', 'Pong', 'Magnavox Odyssey', 'Computer Space', 'Colossal Cave Adventure', 'Atari 2600', 'OXO (video game)'],
    "how the internet's infrastructure works": ['Internet exchange point', 'Border Gateway Protocol', 'Root name server', 'Content delivery network', 'IPv6', 'Packet switching'],
    'history of a famous algorithm': ["Dijkstra's algorithm", 'Quicksort', 'Fast Fourier transform', 'PageRank', 'RSA cryptosystem', 'Euclidean algorithm'],
    'cryptography and codes through history': ['Enigma machine', 'Caesar cipher', 'Vigenère cipher', 'Code talker', 'Zimmermann Telegram', 'One-time pad', 'Voynich manuscript'],
    'space missions and their computers': ['Voyager 1', 'Mars Pathfinder', 'Ariane flight V88', 'Mars Climate Orbiter', 'Hubble Space Telescope', 'Ingenuity (helicopter)'],
    'history of a keyboard key or symbol': ['At sign', 'Ampersand', 'Number sign', 'Esc key', 'Tab key', 'Interrobang', 'Backslash', 'Pilcrow'],
    'how a common app feature was invented': ['Hyperlink', 'Cut, copy, and paste', 'Undo', 'Emoji', 'Autocomplete', 'Pull-to-refresh', 'Scroll wheel'],
    'storage media history': ['Floppy disk', 'Punched card', 'Magnetic-tape data storage', 'Compact disc', 'Zip drive', 'USB flash drive', 'Drum memory'],
    'a programming convention and where it came from': ['Camel case', 'Indentation style', 'Magic number (programming)', 'Off-by-one error', 'Foobar', 'Zero-based numbering'],
    'history of an operating system': ['MS-DOS', 'Linux', 'Classic Mac OS', 'BeOS', 'OS/2', 'Android (operating system)', 'Multics'],
    'how GPS, Wi-Fi or Bluetooth works': ['Global Positioning System', 'Wi-Fi', 'Bluetooth', 'Harald Bluetooth', 'Hedy Lamarr', 'Frequency-hopping spread spectrum'],
    'open-source software history': ['GNU Project', 'Debian', 'Free Software Foundation', 'GNU General Public License', 'Apache HTTP Server', 'Mozilla', 'Linux kernel', 'Wikipedia'],
    'a surprising fact about electricity or batteries': ['Baghdad Battery', 'Voltaic pile', 'War of the currents', 'Leyden jar', 'Electric eel', 'Static electricity'],
    'the science behind a screen technology': ['Cathode-ray tube', 'Liquid-crystal display', 'OLED', 'Plasma display', 'Electronic paper', 'Quantum dot display'],
    'history of artificial intelligence before 2000': ['ELIZA', 'Dartmouth workshop', 'Perceptron', 'Deep Blue (chess computer)', 'SHRDLU', 'AI winter', 'Shakey the robot'],
    'a famous software failure and its lesson': ['Therac-25', 'Year 2000 problem', 'Pentium FDIV bug', 'Morris worm', 'Northeast blackout of 2003', 'Patriot missile'],
    'units and numbers in computing (bytes, hertz, binary)': ['Byte', 'Nibble', 'Hertz', 'Binary number', 'Binary prefix', "Two's complement"],
    'the history of email or the web': ['Mosaic (web browser)', 'Usenet', 'Emoticon', 'Netscape Navigator', 'Webmail', 'Browser wars'],
    'an invention that came from a game or a toy': ['Silly Putty', 'Play-Doh', 'Super Soaker', 'Slinky', "Rubik's Cube", 'Graphics processing unit'],
    'how data compression works': ['Huffman coding', 'Lempel–Ziv–Welch', 'LZ77 and LZ78', 'Run-length encoding', 'Discrete cosine transform', 'Arithmetic coding'],
    'history of the mouse, touchscreen or another input device': ['Trackball', 'Touchscreen', 'Light pen', 'Graphics tablet', 'Joystick', 'Stylus (computing)'],
    'a curiosity about databases': ['Relational database', 'SQL', 'Edgar F. Codd', 'IBM System R', 'B-tree', 'Oracle Database'],
    "the origin of a tech company's name": ['Googol', 'Amazon (company)', 'Nintendo', 'Samsung', 'Yahoo', 'Adobe Inc.', 'Sony', 'Lenovo'],
    'how undersea cables or satellites carry data': ['Submarine communications cable', 'Transatlantic telegraph cable', 'Communications satellite', 'Geostationary orbit', 'Telstar', 'Starlink'],
}


def link_id(url):
    """Short fingerprint of a link (same as assemble.py): single-page states keep these, not URLs."""
    return hashlib.sha1(str(url).encode("utf-8")).hexdigest()[:12]


def dyk_theme(cfg, day):
    themes = cfg.get("did_you_know_themes") or DYK_THEMES
    return themes[day.toordinal() % len(themes)]


# Words that describe a theme but find nothing on their own ("how ... works", "history of a ...").
THEME_FILLER = {"a", "an", "the", "of", "its", "and", "or", "how", "works", "work", "inside", "history", "origin",
                "famous", "common", "curious", "surprising", "fact", "about", "behind", "your", "country", "s",
                "came", "from", "where", "it", "what", "in", "before", "their", "lesson", "through", "piece",
                "everyday", "an", "that", "came", "invented", "feature", "record", "science", "curiosity", "name",
                "names", "carry", "and", "its", "convention", "early", "surprising", "technology"}


def theme_query(theme):
    kept = [w for w in re.findall(r"[\w-]+", theme) if w.lower() not in THEME_FILLER and not w.isdigit()]
    return " ".join(kept) or theme


def wiki_lang(cfg):
    return re.sub(r"[^a-z]", "", (cfg.get("wikipedia_lang") or (cfg.get("language") or "en").split("-")[0]).lower()) or "en"


def collect_did_you_know(cfg, day, used):
    """'Did you know?' candidates (k1, k2…): Wikipedia articles on today's theme, with their summary.
    Like "On this day", Wikipedia counts as confirmation, so the model needs no search or page of
    its own. Order: a search on the reader's Wikipedia, tried once (its text needs no translation;
    the search API answers 429 to shared cloud addresses, so a refusal is not waited out); then the
    theme's seed articles (English Wikipedia's cached page summaries, which answer far more often);
    then a patient search on the English Wikipedia."""
    themes = cfg.get("did_you_know_themes") or DYK_THEMES
    theme = dyk_theme(cfg, day)
    used_n = [u for u in used if u]
    cands, errors = [], []
    query = theme_query(theme)
    offset = (day.toordinal() // len(themes)) % 3 * 4  # the next time the theme comes back, other articles
    seeds = DYK_SEEDS.get(theme) or []
    start = (day.toordinal() // len(themes)) % len(seeds) if seeds else 0
    reader = wiki_lang(cfg)
    steps = [("search", reader, 1), ("seeds", "en", 0)]
    if reader != "en" or not seeds:
        steps.append(("search", "en", 3))
    for kind, lang, tries in steps:
        if len(cands) >= 2:
            break
        known = {c["title"] for c in cands}
        if kind == "seeds":
            titles = [t for t in seeds[start:] + seeds[:start] if t not in known][:4]
        else:
            q = urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": query, "srlimit": 4,
                                        "sroffset": offset, "srnamespace": 0, "format": "json"})
            try:
                hits = json.loads(fetch(f"https://{lang}.wikipedia.org/w/api.php?{q}", tries=tries)).get("query", {}).get("search", [])
            except Exception as e:
                errors.append(e)
                continue
            titles = [h["title"] for h in hits if h.get("title") and h["title"] not in known
                      and not re.match(LIST_ARTICLE, h["title"], re.I)]
        cands += wiki_summaries(lang, titles, used_n, want=3 - len(cands))
    if not cands and errors:
        raise errors[-1]  # reported in FAILURES with its HTTP code
    return [{"id": f"k{i}", **c} for i, c in enumerate(cands[:3], 1)]


# Lists, comparisons and indexes tell no single fact ("List of free and open-source software packages").
LIST_ARTICLE = (r"(list|lists|comparison|index|outline|timeline|glossary) of\b|(lista|listas|comparação|"
                          r"comparación|índice|cronologia|cronología|glossário|glosario) d")


def wiki_summaries(lang, titles, used_n, want=3):
    """Page summaries of these articles, one at a time (Wikipedia answers 429 to bursts), skipping
    disambiguations, short extracts and facts already printed; stops once it has `want`."""
    out = []
    for title in titles:
        if len(out) >= want:
            break
        path = urllib.parse.quote(title.replace(" ", "_"), safe="")
        page = _safe(lambda: json.loads(fetch(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{path}", tries=3)))
        if not page or page.get("type") == "disambiguation":
            continue
        text = clean(page.get("extract") or "", 480)
        url = ((page.get("content_urls") or {}).get("desktop") or {}).get("page", "")
        if len(text) < 150 or not url.startswith("https://"):
            continue
        name = page.get("title") or title
        if any(same_story(name, u) or same_story(text, u) for u in used_n):
            continue
        out.append({"title": name, "text": text, "url": url, "lang": lang})
    return out


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def tz_of(cfg):
    name = cfg.get("timezone") or "UTC"
    if ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return dt.timezone.utc


def fetch(url, tries=2):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    for n in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read(MAX_BYTES)
        except urllib.error.HTTPError as e:
            if n + 1 >= tries or e.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(retry_wait(e, n))


def retry_wait(err, n):
    """Seconds before the next try: the server's Retry-After when it gives one (capped at 10 s, so
    the edition never stalls), else 2 s, 4 s, 6 s…"""
    try:
        return min(10.0, max(1.0, float((err.headers or {}).get("Retry-After"))))
    except (TypeError, ValueError, AttributeError):
        return 2 + 2 * n


def words(txt):
    txt = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", txt).split()


def clean(txt, limit=220):
    txt = html.unescape(re.sub(r"<[^>]+>", " ", txt or ""))
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt if len(txt) <= limit else txt[: limit - 1].rsplit(" ", 1)[0] + "…"


# Words that date a title but say nothing about which fact it is: two release-date stories for
# different games ("X early access date 9 November 2026", "Y release date: November 16, 2026")
# must not count as one story because both name the month, the year and the word "date".
DATE_WORDS = {"date", "release", "lancamento", "lanzamiento", "hoje", "today", "hoy",
              "ontem", "yesterday", "ayer", "amanha", "tomorrow", "manana", "domingo", "segunda", "terca",
              "quarta", "quinta", "sexta", "sabado", "sunday", "monday", "tuesday", "wednesday", "thursday",
              "friday", "saturday", "lunes", "martes", "miercoles", "jueves", "viernes"}


def _story_words(txt):
    months = set(words(" ".join(list(MONTHS) + list(MONTHS_READER)))) | {m.lower() for m in MONTHS_EN}
    return {p for p in words(txt) if len(p) > 3 and not p.isdigit() and p not in months and p not in DATE_WORDS}


def same_story(a, b):
    """Two titles tell the same story if they share most of their meaningful words (dates, months
    and years left out: they are shared by unrelated stories of the same week)."""
    pa = _story_words(a)
    pb = _story_words(b)
    if not pa or not pb:
        return False
    return len(pa & pb) / min(len(pa), len(pb)) >= 0.6


def adds_to(summary, title):
    """True if the summary carries facts beyond its title (at least 3 meaningful words the title
    does not have): only a summary that merely repeats the title is dropped."""
    return len(_story_words(summary) - _story_words(title)) >= 3


# Names a country goes by in different titles ("EUA", "Estados Unidos", "US"): folded into one
# token before comparing, so the same fact told with a different name is still recognized.
# Full names and unambiguous short forms match in any case:
ALIASES = [(r"\bestados unidos\b|\bunited states\b|\beeuu\b|\bee uu\b|\beua\b", " euaxx "),
           (r"\breino unido\b|\bunited kingdom\b|\buk\b", " ukxxx "),
           (r"\buniao europeia\b|\bunion europea\b|\beuropean union\b", " uexxx ")]
# Short forms that are also everyday words count only in capitals, and never in an all-caps title:
# "usa" (verb in Portuguese and Spanish), "us" (English pronoun), "eu" (Portuguese pronoun), "uê".
# "US$" is the dollar, not the country. In Spanish, "EU" is how Mexican outlets write Estados Unidos.
CAPS_US = r"\bU\.?S\.?(?:A\.?)?(?![\w.$])"
CAPS_EU = r"\bEU\b(?!\$)"
CAPS_UE = r"\bUE\b"
LANG = None  # reader's language (set in main); only changes what "EU" means


def all_caps(txt):
    letters = [ch for ch in txt or "" if ch.isalpha()]
    return len(letters) >= 8 and sum(ch.isupper() for ch in letters) / len(letters) > 0.8


# Words every headline uses, which say nothing about which fact it is.
COMMON = {"para", "apos", "sobre", "como", "mais", "deixa", "anuncia", "lanca", "novo", "nova", "novos", "novas",
          "pode", "podem", "sera", "desde", "durante", "contra", "segundo", "entre", "veja", "after", "with",
          "from", "over", "says", "launches", "announces", "will", "could", "tras", "tiene", "nuevo", "nueva",
          "anuncio", "despues", "este", "esta", "isso", "this", "that", "your", "seus", "suas"}


def stems(txt):
    """Meaningful words cut to their first 5 letters, so "paralisam" and "paralisação" match."""
    txt = txt or ""
    if not all_caps(txt):
        txt = re.sub(CAPS_US, " eua ", txt)
        txt = re.sub(CAPS_EU, " eua " if (LANG or "").startswith("es") else " uniao europeia ", txt)
        txt = re.sub(CAPS_UE, " uniao europeia ", txt)
    n = " " + " ".join(words(txt)) + " "
    for pat, rep in ALIASES:
        n = re.sub(pat, rep, n)
    return {w[:5] for w in n.split() if (len(w) > 3 and w not in COMMON) or w.isdigit()}


# Coined names that point to one fact: letters, a digit, letters again ("Java4Shell", "Log4j") or
# a CVE id. Two titles that share one tell the same story even with no other word in common.
COINED = r"\b(?:[A-Za-z]+\d+[A-Za-z][A-Za-z0-9]*|CVE-\d{4}-\d{4,})\b"


def coined_names(txt):
    return {m.lower() for m in re.findall(COINED, txt or "") if len(m) >= 5}


def related(a, b):
    """Looser than same_story: two titles that may tell the same fact in different words.
    Only a hint for the model (printed as ≈a1); nothing is dropped because of it. A shared coined
    name is enough; bare numbers ("4.5", "2") never count as shared words."""
    if coined_names(a) & coined_names(b):
        return True
    pa = {w for w in stems(a) if not w.isdigit()}
    pb = {w for w in stems(b) if not w.isdigit()}
    if not pa or not pb:
        return False
    shared = pa & pb
    return len(shared) >= 2 and len(shared) / min(len(pa), len(pb)) >= 0.4


def printed_before(title, past_titles, city=""):
    """Looser repeat check against recent editions: the same fact told by another outlet with other
    words ("Médico é alvo de denúncia após carregar carro elétrico…" after "Médico é filmado
    carregando o BYD…"). The reader's city is left out of the comparison: two different local
    stories both name it. Only a hint (printed?), nothing is dropped because of it."""
    city_stems = stems(city) if city else set()
    mine = stems(title) - city_stems
    for t in past_titles:
        other = stems(t) - city_stems
        shared = mine & other
        if mine and other and len(shared) >= 2 and len(shared) / min(len(mine), len(other)) >= 0.4:
            return True
    return False


def seen_earlier(title, older):
    """Date (MM-DD) of an older story, from the same searches, that tells the same fact: a small
    outlet's "Google launches X" today when bigger ones covered X days ago. Stricter than related()
    (3 shared words), since ongoing subjects ("Tesouro Direto taxas") come back every day. Only a
    hint (earlier?), nothing is dropped because of it."""
    mine = stems(title)
    if len(mine) < 3:
        return None
    for old_title, when in older:
        other = stems(old_title)
        shared = mine & other
        if len(shared) >= 3 and len(shared) / min(len(mine), len(other)) >= 0.5:
            return when
    return None


def outlet_key(name):
    return " ".join(words(name))


def junk_title(title, outlet):
    """Empty titles, or titles that are only the outlet's name ("- Jornal Panorama")."""
    t = (title or "").strip(" -–—|·:")
    tw = words(t)
    return not tw or set(tw) <= set(words(outlet)) or len(tw) < 2


MONTHS = {  # (lowercase names, accents kept) → month number; English needs a capital (not "May")
    **{m: i for i, m in enumerate(["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
                                   "setembro", "outubro", "novembro", "dezembro"], 1)},
    **{m: i for i, m in enumerate(["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
                                   "septiembre", "octubre", "noviembre", "diciembre"], 1)},
}
MONTHS_EN = {m: i for i, m in enumerate(["January", "February", "March", "April", "", "June", "July", "August",
                                         "September", "October", "November", "December"], 1) if m}
MONTHS_READER = {}  # config.labels.months (other languages), matched exactly as written: "mars" ≠ "Mars"


def set_reader_months(cfg):
    names = ((cfg.get("labels") or {}).get("months")) or []
    MONTHS_READER.clear()
    if isinstance(names, list) and len(names) == 12:
        MONTHS_READER.update({n.strip(): i for i, n in enumerate(names, 1)
                              if isinstance(n, str) and re.fullmatch(r"\w+", n.strip())})


def other_month(title, day):
    """True if the title names a past month that is not this one or the last ("IPCA fecha junho" in
    November): the story is probably old or republished even though its timestamp is recent.
    Upcoming months are fine ("presidency from January 2027", "Black Friday in December"): a month
    with a later year is never flagged, and without a year the next three months count as upcoming."""
    tokens = re.findall(r"\w+", title or "")
    for i, tok in enumerate(tokens):
        m = MONTHS.get(tok.lower()) or MONTHS_EN.get(tok) or MONTHS_READER.get(tok)
        if not m:
            continue
        # "outubro de 2024", "octobre 2024", "ottobre del 2024": a year in the next two words
        after = [t for t in tokens[i + 1:i + 3] if re.fullmatch(r"(19|20)\d\d", t)][:1]
        year = int(after[0]) if after else None
        if year is not None:
            back = (day.year - year) * 12 + day.month - m
        else:
            back = (day.month - m) % 12
            if back >= 9:  # one to three months ahead
                continue
        if back >= 2:
            return True
    return False


# Scheduled decisions and figures (rate decisions, inflation, jobs, GDP): outlets republish them
# for days, and Google News shows the republication date. Readers can add terms with
# "recurring_terms" in config.json. Whole words, compared without accents.
RECURRING = ["copom", "selic", "ipca", "inpc", "igp m", "caged", "pnad", "fomc", "fed", "federal reserve",
             "banxico", "ecb", "bce", "interest rate", "interest rates", "taxa de juros", "tasa de interes",
             "inflation", "inflacao", "inflacion", "cpi", "payroll", "jobs report", "pib", "gdp",
             # rate moves named without "rate of" ("Alta de juros nos EUA faz dólar disparar")
             "juros", "tasas de interes", "tipos de interes", "rate hike", "rate hikes", "rate cut", "rate cuts",
             "rate decision"]


def recurring(title, extra=()):
    n = f" {' '.join(words(title))} "
    return any(f" {p} " in n for p in phrases(RECURRING + list(extra)))


def names_city(text, city):
    c = " ".join(words(city))
    return bool(c) and f" {c} " in f" {' '.join(words(text))} "


def phrases(lst):
    """Reader's "avoid" list, normalized (lowercase, no accents) for whole-word matching."""
    return [" ".join(words(p)) for p in lst or [] if words(str(p))]


def keyword_hit(text, keywords):
    """True if the text carries one of the section's keywords. Each keyword word matches any word
    that starts with its first five letters, so "simulação" also finds "simulador" and "simuladores";
    shorter words must match whole ("sim" never matches "simples")."""
    have = words(text)
    for kw in keywords:
        parts = words(kw)
        if parts and all(any(h.startswith(w[:5]) if len(w) >= 5 else h == w for h in have) for w in parts):
            return True
    return False


def avoided(text, avoid):
    """True if any avoided word or phrase appears in the text as whole words
    ("war" matches "the war ended", never "software")."""
    n = f" {' '.join(words(text))} "
    return any(f" {p} " in n for p in avoid)


def past_editions(index, day):
    """What recent editions already printed: (previous editions newest first, links, titles)."""
    editions = index.get("editions") or index.get("edicoes") or []
    previous = sorted((e for e in editions if (e.get("date") or e.get("data")) != day.isoformat()),
                      key=lambda e: e.get("date") or e.get("data") or "", reverse=True)
    recent = previous[:DEDUP_EDITIONS]
    links = {l for e in recent for l in [*(e.get("links") or []), *(e.get("link_ids") or [])]}
    titles = [t for e in recent
              for t in [e.get("headline") or e.get("manchete") or "", *(e.get("titles") or e.get("titulos") or []),
                        *(e.get("source_titles") or [])] if t]
    return previous, links, titles


# ---------------------------------------------------------------- news

def google_news_url(query, days, loc):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{query} when:{days}d", "hl": loc.get("hl", "en-US"), "gl": loc.get("gl", "US"),
         "ceid": loc.get("ceid", "US:en")})


def _local(tag):
    """Tag name without its XML namespace ("{namespace}entry" -> "entry")."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el, *names):
    c = _child(el, *names)
    return (c.text or "").strip() if c is not None and c.text else ""


def _date(txt):
    """RFC 822 (RSS pubDate) or ISO 8601 (Atom, dc:date)."""
    if not txt:
        return None
    try:
        when = email.utils.parsedate_to_datetime(txt)
    except Exception:
        try:
            when = dt.datetime.fromisoformat(txt.strip().replace("Z", "+00:00"))
        except Exception:
            return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def _link(el):
    for c in el:
        if _local(c.tag) != "link":
            continue
        if c.get("href"):  # Atom: <link href="..." rel="alternate"/>
            if c.get("rel", "alternate") == "alternate":
                return c.get("href").strip()
        elif c.text:
            return c.text.strip()
    return ""


def read_rss(url):
    """Reads RSS 2.0, RSS 1.0 (RDF) and Atom feeds. A document with no items at all, or whose items
    carry no date, raises: the caller reports it in FAILURES instead of silently printing nothing."""
    root = ET.fromstring(fetch(url))
    entries = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    if not entries and _local(root.tag) not in ("rss", "RDF", "feed"):
        raise ValueError("not a feed")
    items, undated = [], 0
    for it in entries:
        title = clean(_text(it, "title"), 300)
        link = _link(it)
        link = re.sub(r"^http:(?=//)", "https:", link)  # the paper only links https
        src = _child(it, "source")
        outlet = ""
        if src is not None:
            outlet = (src.text or "").strip() or _text(src, "title")
        if outlet and title.endswith(" - " + outlet):  # Google News appends " - Outlet"
            title = title[: -len(" - " + outlet)]
        if not outlet:
            outlet = urllib.parse.urlparse(link).netloc.replace("www.", "")
        title = re.sub(r"\s+[-–—|]\s*$", "", title).strip()  # a trailing " -" left by the outlet
        if junk_title(title, outlet):
            continue
        when = _date(_text(it, "pubDate") or _text(it, "date") or _text(it, "published") or _text(it, "updated"))
        if when is None:
            undated += 1
        summary = clean(_text(it, "description") or _text(it, "summary") or _text(it, "content"), 160)
        if summary and same_story(summary, title):
            summary = ""  # Google News descriptions usually repeat the title
        if title and link.startswith("https://"):
            items.append({"title": title, "outlet": outlet, "url": link, "date": when, "summary": summary})
    if items and undated == len(items):
        raise ValueError("items without dates")
    return items


def prefixes(sections):
    letters = [c for c in "abcdefgijlmnopqrstuvwxyz"]  # 'h' (history) and 'k' (did you know) are reserved
    return {s["id"]: letters[i % len(letters)] for i, s in enumerate(sections)}


def collect_news(cfg, window_h, now, used_links, used_titles, tz):
    days = max(1, round(window_h / 24))
    loc = cfg.get("news_locale") or {}
    tasks = []
    for sec in cfg["sections"]:
        for q in sec.get("queries", []):
            tasks.append((sec["id"], google_news_url(q, days, loc)))
        for f in sec.get("feeds", []):
            if str(f).startswith("https://"):
                tasks.append((sec["id"], f))
    results = [[] for _ in tasks]
    failures = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        fut = {ex.submit(read_rss, u): i for i, (_, u) in enumerate(tasks)}
        for f in cf.as_completed(fut):
            sid, u = tasks[fut[f]]
            try:
                results[fut[f]] = f.result()
            except Exception as e:
                failures.append(f"{sid}: {urllib.parse.urlparse(u).netloc} ({type(e).__name__} {getattr(e, 'code', '')})".replace(" )", ")"))
    return rank_news(cfg, [sid for sid, _ in tasks], results, window_h, now, used_links, used_titles, tz), failures


# Why candidates were left out, per section, so a missing story never has to be guessed:
# {section: {reason: [count, [titles]]}}. Filled by rank_news, printed by main.
DROPPED = {}


def drop(sid, reason, title=None):
    entry = DROPPED.setdefault(sid, {}).setdefault(reason, [0, []])
    entry[0] += 1
    if title and len(entry[1]) < 3:
        entry[1].append(clean(title, 50))


def dropped_lines():
    out = []
    for sid, reasons in DROPPED.items():
        parts = []
        for reason, (n, titles) in reasons.items():
            parts.append(f"{n} {reason}" + (f" ({' / '.join(titles)})" if titles else ""))
        out.append(f"{sid}: " + "; ".join(parts))
    return out


def rank_news(cfg, task_sections, results, window_h, now, used_links, used_titles, tz):
    """Filters and ranks candidates. results[i] holds the stories of search/feed i, which belongs to
    section task_sections[i]. A story whose date is unknown (only possible with --from-results) is
    kept after the dated ones and flagged "date?" so the model confirms it."""
    DROPPED.clear()
    cutoff = now - dt.timedelta(hours=window_h)
    tasks = [(sid, None) for sid in task_sections]
    undated_last = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    pre = prefixes(cfg["sections"])
    out = {}
    # Stories the searches returned from before the window, oldest first: a candidate that tells
    # one of these facts is probably a republication (flagged "earlier? MM-DD").
    older = sorted(((it["title"], it["date"]) for res in results for it in res
                    if it.get("date") is not None and it["date"] < cutoff), key=lambda x: x[1])
    older = [(t, d.astimezone(tz).strftime("%m-%d")) for t, d in older[-400:]]
    # Groups of stories that may tell the same fact, across sections. A story joins a group only if
    # it looks like every story already in it, and is marked with the group's first ID: no chains
    # (b8≈b7≈b6) that would lump different facts together.
    groups = []
    for sec in cfg["sections"]:
        sid = sec["id"]
        avoid = phrases(list(cfg.get("avoid") or []) + list(sec.get("avoid") or []))
        # Outlets the reader never wants (e.g. another country's sites that share the language).
        blocked_outlets = {outlet_key(o) for o in list(cfg.get("avoid_outlets") or []) + list(sec.get("avoid_outlets") or [])}
        blocked_outlets.discard("")
        # One queue per search/feed, newest first, already cleared of old, repeated and avoided stories.
        queues = []
        for i, (task_sid, _) in enumerate(tasks):
            if task_sid != sid:
                continue
            ok = []
            for it in results[i]:
                if not (it["date"] is None and it.get("undated_ok") or it["date"] is not None and it["date"] >= cutoff):
                    drop(sid, "outside the window")
                elif it["url"] in used_links or link_id(it["url"]) in used_links:
                    drop(sid, "link already printed", it["title"])
                elif blocked_outlets and outlet_key(it.get("outlet", "")) in blocked_outlets:
                    drop(sid, "avoid_outlets list", it["title"])
                elif avoided(f"{it['title']} {it['summary']}", avoid):
                    drop(sid, "avoid list", it["title"])
                elif any(same_story(it["title"], t) for t in used_titles):
                    drop(sid, "same story as a recent edition", it["title"])
                else:
                    ok.append(it)
            queues.append(sorted(ok, key=lambda x: x["date"] or undated_last, reverse=True))
        # The section's keywords (optional): stories that carry one fill the list first, so a
        # search that also brings unrelated games or topics cannot crowd out the ones that fit.
        keywords = [k for k in sec.get("keywords") or [] if words(str(k))]
        fits = (lambda it: keyword_hit(f"{it['title']} {it['summary']}", keywords)) if keywords else (lambda it: True)
        rounds = [[[it for it in q if fits(it)] for q in queues]]
        if keywords:
            rounds.append([[it for it in q if not fits(it)] for q in queues])
        # Take turns: the newest story of each search, then the second of each, and so on.
        kept, limit = [], sec.get("max_candidates", 10)
        for round_queues in rounds:
            depth = 0
            while len(kept) < limit and any(depth < len(q) for q in round_queues):
                for q in round_queues:
                    if depth < len(q) and len(kept) < limit:
                        it = q[depth]
                        if not any(it["url"] == k["url"] or same_story(it["title"], k["title"]) for k in kept):
                            kept.append(it)
                        else:
                            drop(sid, "same story as another candidate", it["title"])
                depth += 1
        kept.sort(key=lambda x: x["date"] or undated_last, reverse=True)
        if keywords:
            kept.sort(key=lambda x: not fits(x))
        # Local sections of a small town fill up with the region: the stories that name the city
        # itself come first, so a busy neighbouring town cannot push them out of sight.
        city = (cfg.get("city") or {}).get("name", "")
        if city and any(names_city(q, city) for q in sec.get("queries", [])):
            kept.sort(key=lambda x: not names_city(x["title"], city))
        items = []
        for i, it in enumerate(kept, 1):
            when = ("" if it["date"] is None else it["date"].astimezone(tz).strftime("%Y-%m-%d")
                    if it.get("day_only") else it["date"].astimezone(tz).strftime("%Y-%m-%d %H:%M"))
            n = {"id": f"{pre[sid]}{i}", "title": it["title"], "outlet": it["outlet"], "url": it["url"],
                 "date": when, "summary": it["summary"]}
            flags = [] if it["date"] is not None else ["date?"]
            group = next((g for g in groups if all(related(it["title"], m) for m in g["titles"])), None)
            if group:
                group["titles"].append(it["title"])
                n["same_as"] = group["id"]
                flags.append(f"≈{group['id']}")
            else:
                groups.append({"id": n["id"], "titles": [it["title"]]})
            if used_titles and printed_before(it["title"], used_titles, city):
                flags.append("printed?")
            earlier = seen_earlier(it["title"], older) if it["date"] is not None else None
            if earlier:
                flags.append(f"earlier? {earlier}")
            if other_month(it["title"], now.date()):
                flags.append("old month?")
            elif recurring(it["title"], cfg.get("recurring_terms") or []):
                flags.append("recurring?")
            if city and names_city(it["title"], city):
                flags.append("city")
            if keywords and not fits(it):
                flags.append("fit?")
            if flags:
                n["flags"] = flags
            items.append(n)
        out[sid] = items
    return out


# ---------------------------------------------------------------- results gathered by the model

def result_date(txt, tz, now):
    """A date given with a web result: "2026-11-14" (the whole day counts), an ISO date-time or an
    RFC 822 date. Returns (datetime or None, day_only)."""
    txt = str(txt or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", txt):
        try:
            d = dt.date.fromisoformat(txt)
        except ValueError:
            return None, False
        return min(dt.datetime(d.year, d.month, d.day, 23, 59, tzinfo=tz), now), True
    when = _date(txt)
    if when is not None and txt and "T" in txt and not re.search(r"(Z|[+-]\d\d:?\d\d)$", txt):
        when = when.replace(tzinfo=tz)  # a local time with no offset is the reader's
    return when, False


def from_results(path, cfg, tz, now):
    """Reads the results file (format in references/edition-format.md, "No network") and returns
    (task_sections, results, weather, history events, problems)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        data = {"news": data}
    section_ids = [s["id"] for s in cfg["sections"]]
    problems, order, queues = [], [], {}
    for i, r in enumerate(data.get("news") or []):
        if not isinstance(r, dict):
            problems.append(f"news[{i}]: not an object")
            continue
        sid = r.get("section")
        if sid not in section_ids:
            problems.append(f"news[{i}]: section {sid!r} is not in config.json")
            continue
        url = re.sub(r"^http:(?=//)", "https:", str(r.get("url") or "").strip())
        outlet = str(r.get("outlet") or "").strip() or urllib.parse.urlparse(url).netloc.replace("www.", "")
        title = re.sub(r"\s+[-–—|]\s*$", "", clean(r.get("title") or "", 300)).strip()
        if outlet and title.endswith(" - " + outlet):
            title = title[: -len(" - " + outlet)]
        if not url.startswith("https://") or junk_title(title, outlet):
            problems.append(f"news[{i}]: needs a title and an https url")
            continue
        when, day_only = result_date(r.get("date"), tz, now)
        summary = clean(r.get("summary") or "", 160)
        if summary and not adds_to(summary, title):
            summary = ""
        key = (sid, str(r.get("query") or ""))
        if key not in queues:
            queues[key] = []
            order.append(key)
        queues[key].append({"title": title, "outlet": outlet, "url": url, "date": when, "day_only": day_only,
                            "summary": summary, "undated_ok": True})
    weather = data.get("weather")
    if isinstance(weather, dict) and isinstance(weather.get("max"), (int, float)) \
            and isinstance(weather.get("min"), (int, float)):
        w = {"max": round(weather["max"]), "min": round(weather["min"]),
             "condition": str(weather.get("condition") or "").strip(),
             "rain_chance": weather.get("rain_chance"),
             "icon": weather.get("icon") or icon_from_text(str(weather.get("condition") or "")),
             "condition_lang": (cfg.get("language") or "en").split("-")[0]}
        if str(weather.get("url") or "").startswith("https://"):
            w["url"] = weather["url"]
        if weather.get("source"):
            w["source"] = str(weather["source"])[:60]
        weather = w
    else:
        if weather:
            problems.append("weather: needs numeric max and min")
        weather = None
    events = [h for h in data.get("history") or [] if isinstance(h, dict) and h.get("text")]
    return [k[0] for k in order], [queues[k] for k in order], weather, events, problems


# ---------------------------------------------------------------- weather

def icon_from_text(t):
    t = " ".join(words(t))
    if any(k in t for k in ("thunder", "trovoad", "tempestade", "tormenta")):
        return "storm"
    # Whole words: "neve" must not catch "nevoeiro" (fog).
    if re.search(r"\b(snow\w*|sleet|hail|ice pellets|freezing (drizzle|rain)|frost|blizzard|neve|nevasca|nevando"
                 r"|granizo|geada|nieve|nevada|helada|aguanieve|chuva congelada)\b", t):
        return "snow"
    if any(k in t for k in ("rain", "drizzle", "shower", "chuv", "garoa", "lluvia", "llovizna")):
        return "rain"
    if any(k in t for k in ("partly", "parcial", "poucas nuvens", "parcialmente")):
        return "sun-cloud"
    if any(k in t for k in ("cloud", "overcast", "fog", "mist", "nubl", "encoberto", "nuvens", "nevoeiro",
                            "neblina", "nublado", "niebla")):
        return "cloud"
    return "sun"


WMO = {0: ("Clear sky", "sun"), 1: ("Mainly clear", "sun-cloud"), 2: ("Partly cloudy", "sun-cloud"),
       3: ("Overcast", "cloud"), 45: ("Fog", "cloud"), 48: ("Fog", "cloud"), 51: ("Drizzle", "rain"),
       53: ("Drizzle", "rain"), 55: ("Heavy drizzle", "rain"), 61: ("Light rain", "rain"), 63: ("Rain", "rain"),
       65: ("Heavy rain", "rain"), 56: ("Freezing drizzle", "snow"), 57: ("Freezing drizzle", "snow"),
       66: ("Freezing rain", "snow"), 67: ("Freezing rain", "snow"), 71: ("Light snow", "snow"),
       73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow grains", "snow"),
       85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"), 80: ("Rain showers", "rain"), 81: ("Rain showers", "rain"),
       82: ("Heavy showers", "rain"), 95: ("Thunderstorms", "storm"), 96: ("Thunderstorms with hail", "storm"),
       99: ("Thunderstorms with hail", "storm")}


def weather_wttr(city, lang):
    # Coordinates when known: a name alone picks the wrong town when several share it.
    if "latitude" in city and "longitude" in city:
        name = f"{float(city['latitude']):.4f},{float(city['longitude']):.4f}"
    else:
        name = urllib.parse.quote(city["name"].replace(" ", "+"), safe="+")
    d = json.loads(fetch(f"https://wttr.in/{name}?format=j1&lang={lang}"))
    today = d["weather"][0]
    noon = today["hourly"][min(4, len(today["hourly"]) - 1)]
    cond = (noon.get(f"lang_{lang}") or [{}])[0].get("value") or noon["weatherDesc"][0]["value"]
    rain = max(int(h.get("chanceofrain", 0)) for h in today["hourly"])
    return {"max": int(today["maxtempC"]), "min": int(today["mintempC"]), "condition": cond.strip().capitalize(),
            "rain_chance": rain, "icon": icon_from_text(cond), "url": "https://wttr.in/" + name,
            "condition_lang": lang if noon.get(f"lang_{lang}") else "en"}


def weather_open_meteo(city, tzname):
    q = urllib.parse.urlencode({"latitude": city["latitude"], "longitude": city["longitude"],
                                "daily": "temperature_2m_max,temperature_2m_min,weathercode,precipitation_probability_max",
                                "timezone": tzname, "forecast_days": 1})
    d = json.loads(fetch("https://api.open-meteo.com/v1/forecast?" + q))["daily"]
    cond, ic = WMO.get(int(d["weathercode"][0]), ("Variable", "sun-cloud"))
    return {"max": round(d["temperature_2m_max"][0]), "min": round(d["temperature_2m_min"][0]), "condition": cond,
            "rain_chance": d.get("precipitation_probability_max", [None])[0], "icon": ic,
            "url": "https://open-meteo.com/", "condition_lang": "en"}


def collect_weather(cfg):
    city = cfg.get("city") or {}
    if not city.get("name"):
        return None, []  # no city configured (e.g. sample edition): not a failure
    lang = (cfg.get("language") or "en").split("-")[0]
    errors = []
    try:
        return weather_wttr(city, lang), errors
    except Exception as e:
        errors.append(f"weather: wttr.in ({type(e).__name__})")
    if "latitude" in city and "longitude" in city:
        try:
            return weather_open_meteo(city, cfg.get("timezone") or "UTC"), errors
        except Exception as e:
            errors.append(f"weather: open-meteo ({type(e).__name__})")
    return None, errors


# ---------------------------------------------------------------- on this day

def collect_history(day, cfg, used):
    lang = wiki_lang(cfg)
    url = f"https://{lang}.wikipedia.org/api/rest_v1/feed/onthisday/events/{day:%m}/{day:%d}"
    d = json.loads(fetch(url))
    events = []
    for ev in d.get("events", []):
        page = (ev.get("pages") or [{}])[0]
        events.append({"year": ev.get("year"), "text": ev.get("text", ""),
                       "url": ((page.get("content_urls") or {}).get("desktop") or {}).get("page", "")})
    return rank_history(events, cfg, used)


def rank_history(events, cfg, used):
    """"On this day" candidates (h1, h2…): the reader's avoid list and used extras removed, the
    ones matching history_keywords first. Events: {"year", "text", "url"}."""
    pref = [" ".join(words(p)) for p in cfg.get("history_keywords", [])]
    used_n = [" ".join(words(u)) for u in used if u]
    avoid = phrases(cfg.get("avoid"))  # nothing is filtered unless the reader asked for it
    cands = []
    for ev in events:
        txt = clean(ev.get("text", ""), 260)
        n = " ".join(words(txt))
        if not txt or avoided(txt, avoid):
            continue
        if any(u and (n in u or u in n or same_story(n, u)) for u in used_n):
            continue
        link = str(ev.get("url") or "")
        if not link.startswith("https://"):
            link = ""
        try:
            year = int(ev.get("year"))
        except (TypeError, ValueError):
            year = None
        c = {"year": year, "text": txt, "url": link, "score": sum(1 for p in pref if p and p in n)}
        if re.search(r"\d{7,}", txt):
            c["check_numbers"] = True  # a flattened exponent or formula (2^1398269−1 → "21398269−1")
        cands.append(c)
    cands.sort(key=lambda c: (-c["score"], -(c["year"] or 0)))
    return [{"id": f"h{i}", **{k: v for k, v in c.items() if k != "score"}} for i, c in enumerate(cands[:6], 1)]


# ---------------------------------------------------------------- network probe

def network_blocked(cfg, day):
    """One quick request to each kind of source, in parallel, before the full collection. If none
    answers at all (Claude's network access does not reach them), the collection stops at once
    instead of waiting for a dozen requests to fail."""
    loc = cfg.get("news_locale") or {}
    urls = [google_news_url((q or "news"), 1, loc) for q in
            [next((q for sec in cfg["sections"] for q in sec.get("queries", [])), "news")]]
    urls += [f for sec in cfg["sections"] for f in sec.get("feeds", []) if str(f).startswith("https://")][:1]
    lang = wiki_lang(cfg)
    urls.append(f"https://{lang}.wikipedia.org/api/rest_v1/feed/onthisday/events/{day:%m}/{day:%d}")
    city = cfg.get("city") or {}
    if "latitude" in city and "longitude" in city:
        urls.append(f"https://wttr.in/{float(city['latitude']):.4f},{float(city['longitude']):.4f}?format=j1")
        urls.append("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
            {"latitude": city["latitude"], "longitude": city["longitude"], "daily": "weathercode", "forecast_days": 1}))

    def answers(url):
        try:
            fetch(url, tries=1)
            return True
        except urllib.error.HTTPError as e:
            return e.code not in (403, 407)  # the site answered (even 404/429): the network works
        except Exception:
            return False

    with cf.ThreadPoolExecutor(max_workers=len(urls)) as ex:
        return not any(ex.map(answers, urls))


# ---------------------------------------------------------------- main

class _Tee:
    """Everything printed also goes to WORK/run.log, written by the script itself: a debug report can
    quote the real output later, and the model spends no tokens on it (it never reads the log)."""

    def __init__(self, out, log):
        self.out, self.log = out, log

    def write(self, txt):
        self.out.write(txt)
        try:
            self.log.write(txt)
        except (OSError, ValueError):
            pass
        return len(txt)

    def flush(self):
        self.out.flush()
        try:
            self.log.flush()
        except (OSError, ValueError):
            pass


def start_log(work, script):
    """Appends this run's command, time and output to WORK/run.log. Never blocks the edition."""
    try:
        os.makedirs(work, exist_ok=True)
        log = open(os.path.join(work, "run.log"), "a", encoding="utf-8", buffering=1)
        now = dt.datetime.now(dt.timezone.utc).astimezone()
        log.write(f"\n===== {script} · {now.isoformat(timespec='seconds')} · "
                  f"{' '.join(sys.argv[1:])}\n")
        sys.stdout = _Tee(sys.stdout, log)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="reader's config.json")
    ap.add_argument("--work", required=True, help="working folder for this edition")
    ap.add_argument("--index", help="current editions/index.json from the newsstand, if any")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in the reader's timezone)")
    ap.add_argument("--from-results", metavar="FILE",
                    help="no network: filter and rank the web search results in FILE instead of fetching")
    a = ap.parse_args()
    start_log(a.work, "collect.py")

    # Check the working folder before spending a minute on the network.
    try:
        os.makedirs(a.work, exist_ok=True)
        with open(os.path.join(a.work, "collected.json"), "a", encoding="utf-8"):
            pass
    except OSError as e:
        print(f"ERROR: cannot write to the working folder {a.work} ({type(e).__name__}). "
              "Use a temporary folder (mktemp -d) as WORK; if none can be written, tell the reader.")
        return 2
    with open(a.config, encoding="utf-8") as f:
        cfg = json.load(f)
    global LANG
    LANG = str(cfg.get("language") or "").lower()
    set_reader_months(cfg)
    tz = tz_of(cfg)
    now = dt.datetime.now(tz)
    day = dt.date.fromisoformat(a.date) if a.date else now.date()

    index = {"editions": []}
    if a.index and os.path.exists(a.index):
        with open(a.index, encoding="utf-8") as f:
            index = json.load(f)
    editions = index.get("editions") or index.get("edicoes") or []
    previous, used_links, used_titles = past_editions(index, day)
    # Extras of every edition, today's too: a reissue must not repeat the morning's
    # "On this day" or "Did you know?" (its stories may come back; its extras should not).
    extras = [(e.get("extras") or {}) for e in sorted(editions, key=lambda e: e.get("date") or e.get("data") or "",
                                                       reverse=True)]
    used_history = [x.get("history") or x.get("historia") or "" for x in extras]
    used_didyouknow = [x.get("did_you_know") or x.get("voce_sabia") or "" for x in extras]

    weekend = day.weekday() >= 5
    window = 72 if day.weekday() in (0, 5, 6) else 48
    number = max([e.get("number") or e.get("numero") or 0 for e in previous] + [0]) + 1
    reissue = next((e.get("number") or e.get("numero") for e in editions
                    if (e.get("date") or e.get("data")) == day.isoformat()), None)

    problems, did_you_know = [], []
    if a.from_results:
        try:
            task_sections, results, weather, events, problems = from_results(a.from_results, cfg, tz, now)
        except (OSError, ValueError) as e:
            print(f"ERROR: could not read {a.from_results} ({type(e).__name__}: {str(e)[:120]}). "
                  "Fix the results file (format in references/edition-format.md) and run again.")
            return 1
        news = rank_news(cfg, task_sections, results, window, now, used_links, used_titles, tz)
        history = rank_history(events, cfg, used_history)
        failures = []
    elif network_blocked(cfg, day):
        news, weather, history, failures = {s["id"]: [] for s in cfg["sections"]}, None, [], ["network: no source answered"]
    else:
        news, failures = collect_news(cfg, window, now, used_links, used_titles, tz)
        weather, wf = collect_weather(cfg)
        failures += wf
        try:
            history = collect_history(day, cfg, used_history)
        except Exception as e:
            history = []
            failures.append(f"history: wikipedia ({type(e).__name__} {getattr(e, 'code', '')})".replace(" )", ")"))
        if history:  # Wikipedia answers: the "Did you know?" candidates come from it too
            try:
                did_you_know = collect_did_you_know(cfg, day, used_didyouknow)
            except Exception as e:
                failures.append(f"did_you_know: wikipedia ({type(e).__name__} {getattr(e, 'code', '')})".replace(" )", ")"))

    # Nothing at all could be reached (Claude's network access is off or limited): say it in one
    # line so the model switches to the no-network mode instead of reading a dozen failures.
    blocked = not a.from_results and bool(failures) and not any(news.values()) and weather is None and not history
    out = {"date": day.isoformat(), "number": reissue or number, "reissue": bool(reissue), "weekend": weekend,
           "window_hours": window, "weather": weather, "news": news, "history": history,
           "did_you_know_theme": dyk_theme(cfg, day), "did_you_know": did_you_know,
           "did_you_know_used": [s for s in used_didyouknow if s][:30], "failures": failures,
           "network_blocked": blocked, "from_results": bool(a.from_results),
           "generated_at": now.isoformat(timespec="minutes")}
    with open(os.path.join(a.work, "collected.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    # Compact summary for the model (no long URLs: assemble.py resolves them by ID)
    print(f"# {day.isoformat()} ({day.strftime('%A')}) · issue {out['number']}{' (reissue)' if reissue else ''}"
          f"{' · WEEKEND' if weekend else ''} · window {window}h · language {cfg.get('language', 'en')}"
          f"{' · FROM WEB RESULTS' if a.from_results else ''}")
    for p in problems:
        print("SKIPPED:", p)
    if weather:
        rain = f" · rain {weather['rain_chance']}%" if weather.get("rain_chance") is not None else ""
        print(f"WEATHER: {weather['max']}°/{weather['min']}° {weather['condition']} [{weather['condition_lang']}]"
              f"{rain} · icon {weather['icon']}")
    elif not (cfg.get("city") or {}).get("name"):
        print("WEATHER: no city configured (leave the weather out)")
    else:
        print("WEATHER: unavailable")
    if any(n.get("flags") for lst in news.values() for n in lst):
        print("FLAGS: ≈a1 = maybe the same fact as a1 (keep one) · old month? = the title names another"
              " month (likely old news) · recurring? = a scheduled decision or figure (rates, inflation…):"
              " the date shown may be a republication, confirm the event date before using it"
              " · city = names the reader's city · date? = no date was given: confirm it is inside the window"
              " before using it · printed? = may be a fact a recent edition already printed: leave it out"
              " unless it brings something new · earlier? 11-13 = the same fact came out on that date,"
              " before the window: probably a republication, leave it out unless it brings something new"
              " · fit? = carries none of the section's keywords: use it only if the title or summary shows it fits")
    all_news = [n for lst in news.values() for n in lst]
    if all_news:
        with_summary = sum(1 for n in all_news if n["summary"])
        print(f"SUMMARIES: {with_summary} of {len(all_news)} candidates have one (shown after —); the others"
              " carry only their title (Google News search feeds send no summary), so their text only restates it")
    for sec in cfg["sections"]:
        print(f"\n[{sec['id']}] {sec['title']}")
        for n in news.get(sec["id"], []):
            line = f"{n['id']} | {n['date'][5:] or '?'} | {n['outlet']} | {n['title']}"
            if n["summary"]:
                line += f" — {n['summary']}"
            if n.get("flags"):
                line += f" [{', '.join(n['flags'])}]"
            print(line)
        if not news.get(sec["id"]):
            failed = any(f.startswith(f"{sec['id']}:") for f in failures)
            print("(not collected: no source answered)" if blocked else
                  "(source failed: see FAILURES)" if failed else "(nothing new)")
    print("\n[history]")
    for h in history:
        print(f"{h['id']} | {h['year']} | {h['text']}"
              + (" [check the numbers on the linked page]" if h.get("check_numbers") else ""))
    print(f"\n[did_you_know] today's theme: {out['did_you_know_theme']}")
    for d in did_you_know:
        print(f"{d['id']} | {d['title']}{'' if d['lang'] == wiki_lang(cfg) else ' [' + d['lang'] + ']'} | {d['text']}")
    if out["did_you_know_used"]:
        print("[did_you_know already used] " + " / ".join(out["did_you_know_used"][:15]))
    if DROPPED:
        print("\nDROPPED (left out by the script, not by you):")
        for line in dropped_lines():
            print("  " + line)
    if blocked:
        print("\nNETWORK BLOCKED: no source could be reached. Follow 'No network' in SKILL.md.")
    elif failures:
        print("\nFAILURES: " + "; ".join(failures))
    return 0


if __name__ == "__main__":
    rc = main()
    if isinstance(sys.stdout, _Tee):
        print(f"[exit {rc}]")
    sys.exit(rc)
