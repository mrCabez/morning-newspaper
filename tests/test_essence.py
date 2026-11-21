"""Stories with substance (edition No. 010): the story's own page (enrich.py), photo captions,
summaries winning over bare titles, the "In brief" column, ECHO, nearby places (cities.py) and
direct feeds (feeds.py). Fully offline."""
import base64
import copy
import datetime as dt
import email.utils
import importlib.util
import json
import sys

import pytest

from conftest import SCRIPTS
from test_assemble import ok, refused

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def load(name):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def enrich():
    return load("enrich")


@pytest.fixture(scope="module")
def cities():
    return load("cities")


def feed(*items, site="https://outlet.example"):
    """RSS 2.0; each item: (title, link, hours_ago, outlet, description)."""
    body = []
    for title, link, ago, outlet, desc in items:
        pub = email.utils.format_datetime(NOW - dt.timedelta(hours=ago))
        src = f"<source url='{site}'>{outlet}</source>" if outlet else ""
        body.append(f"<item><title>{title}</title><link>{link}</link><pubDate>{pub}</pubDate>{src}"
                    f"<description><![CDATA[{desc}]]></description></item>")
    return f"<?xml version='1.0'?><rss version='2.0'><channel>{''.join(body)}</channel></rss>"


PAGE = """<html><head>
<meta name="description" content="Rádio Exemplo FM">
<meta property="article:published_time" content="2026-11-12T10:00:00-03:00">
</head><body><nav><p>Home · Polícia · Esportes · Cidade · Contato · Anuncie aqui conosco</p></nav>
<article><h1>Motorista fica ferido após caminhão cair de ponte</h1>
<p>Um caminhão carregado de leite caiu de um pontilhão em uma estrada rural de Salto, na manhã desta quinta-feira (12).</p>
<p>Segundo informações, o motorista, de 50 anos, teria perdido o controle da direção ao passar pela ponte.</p>
<p>Leia também: outras notícias da região que você não pode perder hoje no nosso portal</p>
</article><footer><p>Todos os direitos reservados à Rádio Exemplo FM e seus parceiros comerciais</p></footer></body></html>"""


# ---------------------------------------------------------------- enrich.py

def test_lead_takes_the_story_paragraphs_not_the_furniture(enrich, collect_module):
    text, when = enrich.lead(PAGE, "Motorista fica ferido após caminhão cair de ponte",
                             collect_module._story_words, generic=["Rádio Exemplo FM"])
    assert text.startswith("Um caminhão carregado de leite")
    assert "teria perdido o controle" in text  # the hedge travels with the fact
    assert "Leia também" not in text and "direitos" not in text and "Anuncie" not in text
    assert len(text) <= enrich.MAX_CHARS
    assert when == dt.datetime(2026, 11, 12, 13, 0, tzinfo=UTC)


def test_lead_is_empty_when_the_page_only_repeats_the_title(enrich, collect_module):
    page = "<html><head><meta name='description' content='Site Name'></head><body><p>x</p></body></html>"
    assert enrich.lead(page, "Some title here", collect_module._story_words)[0] == ""


def test_lead_uses_json_ld_and_og_description(enrich, collect_module):
    page = ('<html><head><meta property="og:description" content="The open model beats larger models on a '
            'coding test, its lab says, and ships under a permissive license next week."></head><body></body></html>')
    text, _ = enrich.lead(page, "Open model tops coding test", collect_module._story_words)
    assert "permissive license" in text


def test_inline_tags_do_not_split_words(enrich):
    assert enrich._plain("caminhão de <b>lei</b>te , na <a href='x'>AMG-3005</a> .") == "caminhão de leite, na AMG-3005."


def test_cut_ends_at_a_sentence(enrich):
    text = "First sentence is here. " * 30
    out = enrich.cut(text)
    assert out.endswith(".") and len(out) <= enrich.MAX_CHARS


def test_old_google_news_ids_decode_offline(enrich):
    token = base64.urlsafe_b64encode(b"\x08\x13\x22\x1fhttps://outlet.example/story-1\xd2\x01\x00").decode().rstrip("=")
    assert enrich.gnews_offline(f"https://news.google.com/rss/articles/{token}?oc=5") == "https://outlet.example/story-1"
    assert enrich.gnews_offline("https://outlet.example/x") is None


def test_google_news_lookup_reads_the_outlet_address(enrich):
    calls = []

    def get(url, limit=None):
        calls.append(url)
        return b'<c-wiz data-n-a-id="ABC" data-n-a-ts="1795186405" data-n-a-sg="SIG"></c-wiz>'

    def post(url, body):
        assert "garturlreq" in body and "SIG" in body and "1795186405" in body
        return b')]}\'\n[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"https://outlet.example/real\\",1]",null]]'

    assert enrich.gnews_lookup("https://news.google.com/rss/articles/ABC?oc=5", get, post) == "https://outlet.example/real"
    assert calls == ["https://news.google.com/rss/articles/ABC"]


def test_enrich_one_prefers_the_outlets_search_feed(enrich, collect_module):
    item = {"title": "Motorista fica ferido após caminhão cair de ponte", "outlet": "Rádio Exemplo FM",
            "url": "https://news.google.com/rss/articles/AU_yqLxyz?oc=5", "site": "https://outlet.example"}
    asked = []

    def read_feed(url):
        asked.append(url)
        return [{"title": item["title"], "url": "https://outlet.example/noticia/1", "summary": ""}]

    def get(url, limit=None):
        assert url == "https://outlet.example/noticia/1"
        return PAGE.encode()

    def post(*a):
        raise AssertionError("the lookup is not needed when the outlet's feed answers")

    res = enrich.enrich_one(item, {"get": get, "post": post, "read_feed": read_feed,
                                   "story_words": collect_module._story_words,
                                   "same_story": collect_module.same_story})
    assert res["page_url"] == "https://outlet.example/noticia/1"
    assert res["summary"].startswith("Um caminhão")
    assert "feed=rss2" in asked[0] and "s=" in asked[0]


def test_enrich_news_fills_summaries_and_flags_old_pages(collect_module, monkeypatch, config):
    config["sections"] = config["sections"][:1]
    news = {"tech": [{"id": "a1", "title": "T one", "outlet": "O", "url": "https://news.google.com/rss/articles/X",
                      "date": "", "summary": ""},
                     {"id": "a2", "title": "T two", "outlet": "O", "url": "https://o.example/2", "date": "",
                      "summary": "has one already"},
                     {"id": "a3", "title": "T three", "outlet": "O", "url": "https://o.example/3", "date": "",
                      "summary": "", "flags": ["printed?"]}]}
    seen = []

    def fake(item, deps):
        seen.append(item["id"])
        return {"summary": "What the page says.", "page_url": "https://o.example/1",
                "published": NOW - dt.timedelta(days=5)}

    monkeypatch.setattr(collect_module.enrich, "enrich_one", fake)
    done = collect_module.enrich_news(config, news, NOW - dt.timedelta(hours=48), UTC)
    a1 = news["tech"][0]
    assert done == 1 and seen == ["a1"]  # never a story with a summary or one likely left out
    assert a1["summary"] == "What the page says." and a1["summary_from"] == "page"
    assert a1["url"] == "https://o.example/1" and a1["alt_url"].startswith("https://news.google.com/")
    assert any(f.startswith("old page? ") for f in a1["flags"])


# ---------------------------------------------------------------- feeds: captions and summaries

G1 = ('<img src="https://img.example/x.jpg" /><br />    Sam Altman e Dario Amodei\n'
      'Reuters/Manuel Orbegozo; Reuters/Bhawika Chhabra\nA estreia da Anthropic na bolsa marcou um novo capítulo.')


def test_photo_caption_is_not_a_summary(collect_module):
    assert collect_module.without_caption(G1) == "A estreia da Anthropic na bolsa marcou um novo capítulo."
    only = '<img src="https://img.example/x.jpg" /><br /> Um homem nos escombros.\nREUTERS/Thomas Peter'
    assert collect_module.without_caption(only) == ""
    assert collect_module.without_caption("Plain description, no photo.") == "Plain description, no photo."


def test_feed_keeps_the_outlet_site_and_drops_captions(collect_module, offline):
    offline["feed.example"] = feed(("Rivalidade entre CEOs vira símbolo da corrida", "https://o.example/1", 1, "g1", G1))
    [it] = collect_module.read_rss("https://feed.example/rss")
    assert it["site"] == "https://outlet.example"
    assert it["summary"].startswith("A estreia da Anthropic")


def test_the_same_fact_with_a_summary_wins(collect_module, offline, config):
    config["sections"] = [dict(config["sections"][0], queries=["one", "two"], keywords=[])]
    offline["q=one"] = feed(("Tesouro Direto suspende negociação de títulos hoje", "https://a.example/1", 1, "A", ""))
    offline["q=two"] = feed(("Tesouro Direto suspende negociação de títulos hoje à tarde", "https://b.example/2", 2, "B",
                             "As negociações foram suspensas durante a tarde em meio à volatilidade dos juros."))
    news, _ = collect_module.collect_news(config, 48, NOW, set(), [], UTC)
    [only] = news["tech"]
    assert only["outlet"] == "B" and only["summary"]


# ---------------------------------------------------------------- cities.py

REGION = {"source": "ibge", "city": "Itu", "radius_km": 60, "state": "SP", "built": "2026-11-20",
          "places": [["Vila Nova", 0, "Itu"], ["Salto", 9], ["Porto Feliz", 20], ["Feliz", 55]]}


def test_places_in_prefers_the_longest_name_and_needs_a_capital(cities):
    m = cities.matcher(REGION, "Itu")
    assert cities.places_in("Ponte cai em Porto Feliz", m)[0][0] == "Porto Feliz"
    assert cities.places_in("Caminhão cai de ponte em Salto", m)[0][:2] == ("Salto", 9)
    assert cities.places_in("Loja vende sapato de salto alto", m) == []
    assert cities.places_in("Chuva em Vila Nova", m)[0][2] == "Itu"


@pytest.mark.parametrize("title, uf", [
    ("Obras em Salto - Agora RN", "RN"),
    ("Acidente em Salto (RN)", "RN"),
    ("Chuva forte no Pará deixa feridos", "PA"),
    ("Faculdade expande atuação para mais nove cidades de SP", ""),
    ("Feira em Salto, SP", ""),
])
def test_other_state(cities, title, uf):
    assert cities.other_state(title, cities.matcher(REGION)) == uf


def test_a_town_named_after_a_state_is_not_that_state(cities):
    assert cities.other_state("Pará de Minas recebe feira", cities.matcher(dict(REGION, state="MG"))) == ""


def test_centroid_of_a_square(cities):
    lat, lon = cities.centroid({"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]})
    assert abs(lat - 1) < 1e-9 and abs(lon - 1) < 1e-9


def test_compact_keeps_nearest_one_per_name_without_population_cut(cities):
    found = {"source": "ibge", "state": "MG", "places": [
        {"name": "Porto Feliz", "km": 20, "uf": "SP"}, {"name": "Vila Pequena", "km": 5, "uf": "SP"},
        {"name": "Porto Feliz", "km": 50, "uf": "SP"}, {"name": "Vila Nova", "km": 0, "seat": "Itu", "uf": "SP"},
        {"name": "Divisa", "km": 30, "uf": "MG"}, {"name": "Itu", "km": 0, "uf": "SP"}]}
    found["state"] = "SP"
    r = cities.compact(found, 60, "Itu")
    assert r["places"] == [["Vila Nova", 0, "Itu"], ["Vila Pequena", 5], ["Porto Feliz", 20], ["Divisa", 30, "", "MG"]]


def test_needs_build(cities):
    cfg = {"city": {"name": "Itu"}}
    assert cities.needs_build(cfg)
    cfg["region"] = dict(REGION)
    assert not cities.needs_build(cfg)
    cfg["city"]["name"] = "Salto"
    assert cities.needs_build(cfg)  # moved: the list belongs to another city
    cfg["city"]["name"] = "Itu"
    cfg["region"] = dict(REGION, source="model", built="2026-10-01")
    assert cities.needs_build(cfg, dt.date(2026, 11, 20))  # the model's list is replaced by real data


def test_build_falls_back_to_geonames_then_reports(cities, monkeypatch):
    cfg = {"city": {"name": "X", "latitude": -19.8, "longitude": -44.6}, "news_locale": {"gl": "BR"}}

    def ibge_down(cfg, radius):
        raise OSError("IBGE down")

    monkeypatch.setitem(cities.PROVIDERS, "BR", [ibge_down])
    monkeypatch.setattr(cities, "geonames", lambda cfg, radius, cc: {
        "source": "geonames", "state": "15", "places": [{"name": "Y", "km": 10, "uf": "15"}]})
    region, notes = cities.build(cfg)
    assert region["source"] == "geonames" and region["places"] == [["Y", 10]]
    assert notes and notes[0].startswith("ibge_down: OSError")
    monkeypatch.setattr(cities, "geonames", lambda *a: (_ for _ in ()).throw(OSError("down")))
    region, notes = cities.build(cfg)
    assert region is None and len(notes) == 2


def test_list_from_the_model_is_checked(cities, tmp_path):
    path = tmp_path / "region.json"
    path.write_text(json.dumps({"radius_km": 60, "places": [["Salto", 9], ["Longe", 300], ["bad"],
                                                             ["Vila Nova", 0, "Itu"]]}), encoding="utf-8")
    region, problems = cities.from_model(path, {"city": {"name": "Itu"}})
    assert region["source"] == "model"
    assert [p[0] for p in region["places"]] == ["Vila Nova", "Salto"]
    assert len(problems) == 2


def test_local_candidates_get_region_flags_and_sort_by_distance(collect_module, offline, config, monkeypatch):
    config["city"]["name"] = "Itu"
    config["sections"] = [dict(config["sections"][1], queries=['"Itu" SP'])]
    monkeypatch.setattr(collect_module, "REGION", collect_module.cities.matcher(REGION, "Itu"))
    offline["news.google.com"] = feed(
        ("Feliz inaugura praça", "https://n.example/1", 1, "A", ""),
        ("Obras em Salto atrasam", "https://n.example/2", 2, "Agora RN", ""),
        ("Ponte cai em Porto Feliz", "https://n.example/3", 3, "B", ""),
        ("Bar é assaltado no interior", "https://n.example/4", 4, "C", ""),
        ("Chuva alaga ruas de Vila Nova", "https://n.example/5", 5, "D", ""))
    news, _ = collect_module.collect_news(config, 48, NOW, set(), [], UTC)
    flags = {n["title"]: n.get("flags", []) for n in news["local"]}
    assert flags["Ponte cai em Porto Feliz"] == ["near Porto Feliz 20km"]
    assert flags["Feliz inaugura praça"] == ["near Feliz 55km"]
    assert flags["Obras em Salto atrasam"] == ["other state? RN"]
    assert flags["Bar é assaltado no interior"] == ["where?"]
    assert flags["Chuva alaga ruas de Vila Nova"] == ["city (Vila Nova)"]
    order = [n["title"] for n in news["local"]]
    assert order.index("Chuva alaga ruas de Vila Nova") < order.index("Ponte cai em Porto Feliz") \
        < order.index("Feliz inaugura praça") < order.index("Bar é assaltado no interior")


# ---------------------------------------------------------------- assemble.py: briefs and ECHO

def test_title_only_story_becomes_a_brief(assembler):
    assembler.edition["sections"]["tech"].append({"ref": "a3", "title": "Password manager reports a breach"})
    proc = ok(assembler.run())
    assert "BRIEFS: a3" in proc.stdout
    page = assembler.page()
    assert 'class="breves"' in page and "In brief" in page
    assert '<p class="breve">Password manager reports a breach <a href="https://sec.example.com/a3"' in page


def test_title_only_story_with_text_still_becomes_a_brief(assembler):
    assembler.edition["sections"]["tech"].append({"ref": "a3", "title": "Password manager reports a breach",
                                                  "text": "A password manager reported a breach."})
    ok(assembler.run())
    assert "A password manager reported a breach." not in assembler.page()


def test_brief_does_not_take_a_section_slot(assembler):
    assembler.config["sections"][0]["count"] = {"weekday": [1, 1], "weekend": [1, 1]}
    assembler.edition["sections"]["tech"].append({"ref": "a3", "title": "Password manager reports a breach"})
    assert "above its count" not in ok(assembler.run()).stdout


def test_headline_is_never_a_brief(assembler):
    assembler.edition["headline"]["brief"] = True
    refused(assembler.run())


def test_echo_when_the_text_restates_the_title(assembler):
    story = assembler.edition["sections"]["local"][0]
    story["text"] = "Main Street gets new asphalt, the city says."
    proc = ok(assembler.run())
    assert "ECHO: b1:" in proc.stdout
    story["text"] = "Work on the asphalt starts next week, the city says."
    assert "ECHO:" not in ok(assembler.run()).stdout


def test_no_briefs_box_without_briefs(assembler):
    ok(assembler.run())
    assert 'class="breves"' not in assembler.page()


def test_link_ids_keep_the_google_news_link_a_page_replaced(assembler):
    a2 = next(n for n in assembler.collected["news"]["tech"] if n["id"] == "a2")
    a2["alt_url"] = "https://news.google.com/rss/articles/OLD"
    ok(assembler.run())
    from test_assemble import link_id
    assert link_id("https://news.google.com/rss/articles/OLD") in assembler.new_index()["editions"][0]["link_ids"]


def test_config_changed_by_the_collection_is_published(assembler):
    assembler.index = {"founded": "2026-11-01", "editions": []}
    assert "config.json ->" not in ok(assembler.run()).stdout
    assembler.collected["config_changed"] = True
    assert "config.json ->" in ok(assembler.run()).stdout


# ---------------------------------------------------------------- feeds.py

def test_feeds_suggests_feeds_with_summaries(collect_module, monkeypatch, config):
    feeds = load("feeds")
    good = feed(*[(f"Falha de segurança {i} em software", f"https://o.example/{i}", 1, None,
                   "Um texto com resumo de verdade sobre a falha.") for i in range(6)])
    bare = feed(*[(f"Notícia {i}", f"https://p.example/{i}", 1, None, "") for i in range(6)])

    def fake_fetch(url, tries=2):
        if url == "https://o.example/feed/":
            return good.encode()
        if url == "https://o.example/":
            return b'<link rel="alternate" type="application/rss+xml" href="/feed/">'
        if url.startswith("https://p.example/feed"):
            return bare.encode()
        raise OSError("no")

    monkeypatch.setattr(feeds.collect, "fetch", fake_fetch)
    config["sections"] = [dict(config["sections"][0], keywords=["software", "segurança"], feeds=[])]
    collected = {"news": {"tech": [{"url": "https://news.google.com/rss/articles/1", "site": "https://o.example"},
                                   {"url": "https://news.google.com/rss/articles/2", "site": "https://o.example"},
                                   {"url": "https://p.example/x", "site": ""}]}}
    found = feeds.suggest(config, collected, now=NOW)
    assert [u for u, *_ in found["tech"]] == ["https://o.example/feed/"]


# ---------------------------------------------------------------- review of v1.13.0 (CodeRabbit, PR #9)

def test_reissue_with_a_replaced_link_is_not_a_weaker_edition(assembler):
    a2 = next(n for n in assembler.collected["news"]["tech"] if n["id"] == "a2")
    a2["alt_url"] = "https://news.google.com/rss/articles/OLD"
    ok(assembler.run())
    index = assembler.new_index()
    entry = index["editions"][0]
    assert entry["stories"] < len(entry["link_ids"])  # two fingerprints for one story
    proc = ok(assembler.run(index=index))
    assert "KEEP:" not in proc.stdout


def test_reissue_with_fewer_stories_than_stored_still_asks(assembler):
    ok(assembler.run())
    index = assembler.new_index()
    index["editions"][0]["stories"] += 5
    proc = assembler.run(index=index)
    assert proc.returncode == 3 and "KEEP:" in proc.stdout


def test_title_only_story_repeating_its_collected_link_is_a_brief(assembler):
    a3 = next(n for n in assembler.collected["news"]["tech"] if n["id"] == "a3")
    assembler.edition["sections"]["tech"].append({"ref": "a3", "title": "Password manager reports a breach",
                                                  "url": a3["url"], "outlet": a3["outlet"]})
    assert "BRIEFS: a3" in ok(assembler.run()).stdout


def test_a_page_with_no_paragraph_about_the_story_gives_no_summary(enrich, collect_module):
    page = ("<html><body><p>Quality reporting takes time and resources. Create an account to continue "
            "reading this article on our website.</p></body></html>")
    text, _ = enrich.lead(page, "City council approves school budget", collect_module._story_words)
    assert text == ""


def test_the_description_still_serves_when_no_paragraph_is_about_the_story(enrich, collect_module):
    page = ('<html><head><meta name="description" content="Vereadores aprovaram ontem o orçamento das escolas '
            'municipais para 2027, com reforço para merenda e transporte."></head><body><p>Quality reporting '
            'takes time and resources. Create an account to continue reading this article.</p></body></html>')
    text, _ = enrich.lead(page, "Câmara aprova orçamento das escolas", collect_module._story_words)
    assert text.startswith("Vereadores aprovaram")


def test_site_search_needs_the_same_title_not_the_same_subject(enrich, collect_module):
    def read_feed(url):
        return [{"title": "City council approves hospital expansion", "url": "https://o.example/h", "summary": ""}]
    assert enrich.site_search("https://o.example", "City council approves school budget", read_feed,
                              collect_module.same_story) is None
    assert enrich.same_title("Motorista fica ferido após caminhão cair de ponte",
                             "Motorista fica ferido após caminhão cair de ponte em Salto")
    assert not enrich.same_title("City council approves school budget", "City council approves hospital expansion")


def test_an_old_page_is_flagged_even_without_a_summary(enrich, collect_module):
    page = ('<html><head><meta property="article:published_time" content="2020-01-01T00:00:00Z"></head>'
            '<body><p>City council approves school budget</p></body></html>')
    item = {"title": "City council approves school budget", "outlet": "O", "url": "https://o.example/1"}
    res = enrich.enrich_one(item, {"get": lambda url, limit=None: page.encode(), "post": None, "read_feed": None,
                                   "story_words": collect_module._story_words,
                                   "same_story": collect_module.same_story})
    assert "summary" not in res and res["published"].year == 2020


def test_a_town_holding_a_state_name_is_not_that_state(cities):
    region = dict(REGION, places=REGION["places"] + [["Espírito Santo do Pinhal", 58]])
    m = cities.matcher(region, "Itu")
    assert cities.other_state("Feira agita Espírito Santo do Pinhal neste fim de semana", m) == ""
    assert cities.other_state("Chuva forte no Espírito Santo deixa feridos", m) == "ES"
    assert cities.other_state("Acidente em Espírito Santo do Pinhal (RN)", m) == "RN"


def test_summary_read_from_the_page_goes_through_the_avoid_list(collect_module, config):
    config["avoid"] = ["eleição"]
    sid = config["sections"][0]["id"]
    news = {sid: [{"title": "Prefeito visita obra", "summary": "Na véspera da eleição, o prefeito...",
                   "summary_from": "page"},
                  {"title": "Ponte é reaberta", "summary": "A ponte foi reaberta ontem.", "summary_from": "page"},
                  {"title": "Feira na praça", "summary": "Fala da eleição, mas veio do feed."}]}
    collect_module.drop_avoided_pages(config, news)
    assert [n["title"] for n in news[sid]] == ["Ponte é reaberta", "Feira na praça"]


def test_region_saved_leaves_a_marker_and_a_whole_config(collect_module, config, tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    config["region"] = {"places": [["Salto", 9]]}
    collect_module.save_config(config, str(path), str(tmp_path))
    assert json.loads(path.read_text())["region"]["places"] == [["Salto", 9]]
    assert (tmp_path / "config.changed").exists() and not (tmp_path / "config.json.tmp").exists()


def test_feeds_keeps_looking_after_a_bad_feed_and_ignores_old_items(monkeypatch):
    feeds = load("feeds")
    fresh = feed(*[(f"Falha {i}", f"https://o.example/{i}", 1, None, "Um resumo de verdade.") for i in range(6)])
    stale = feed(*[(f"Falha {i}", f"https://o.example/{i}", 24 * 400, None, "Um resumo de verdade.")
                   for i in range(6)])
    bare = feed(*[(f"Falha {i}", f"https://o.example/{i}", 1, None, "") for i in range(6)])
    pages = {"https://o.example/a": bare, "https://o.example/b": fresh}
    monkeypatch.setattr(feeds, "discover", lambda site: list(pages))
    monkeypatch.setattr(feeds.collect, "fetch", lambda url, tries=2: pages[url].encode())
    assert feeds.first_good("https://o.example", [], True, now=NOW)[0] == "https://o.example/b"
    pages["https://o.example/b"] = stale
    assert feeds.first_good("https://o.example", [], True, now=NOW) is None
