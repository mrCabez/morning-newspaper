# No network

Read when the collection printed `NETWORK BLOCKED`: Claude's network access does not reach the sources (some apps let the shell reach only a list of hosts). Gather with the web tools, which still work, and let the script do the filtering. Tell the reader nothing about it.

1. One web search per section (two for the section with the most `count`), built from its `queries` plus the current month and year.
2. Weather: the app's weather tool if there is one (it has no page: give `source`, no `url`), else one web search. "On this day": fetch the Wikipedia page for today's date in `wikipedia_lang` (`https://en.wikipedia.org/wiki/November_15`, `https://pt.wikipedia.org/wiki/15_de_novembro`, `https://es.wikipedia.org/wiki/15_de_noviembre`) and take 6–10 events from its events section.
3. Write every result to `WORK/results.json` (format below): title, link, outlet, the date when the result shows one, a short snippet. Leave nothing out on your own: the script applies the date window, the repeat checks and the `avoid` list.
4. Run `python3 SKILL/scripts/collect.py --config WORK/config.json --work WORK --index WORK/index.json --from-results WORK/results.json`. It prints the same summary as a normal collection, with IDs: from here on use `ref` as usual. A story flagged `date?` came without a date: open it (or search its title) to confirm it is inside the window before using it.

## The results file

Plain data, one entry per result, in the reader's language as found:

```json
{
  "news": [
    {"section": "tech", "query": "the search you ran", "title": "...", "url": "https://...", "outlet": "Name",
     "date": "2026-11-14", "summary": "the result's snippet"}
  ],
  "weather": {"max": 29, "min": 19, "condition": "...", "rain_chance": 47, "source": "app weather tool"},
  "history": [{"year": 1889, "text": "...", "url": "https://pt.wikipedia.org/wiki/15_de_novembro"}]
}
```

- `section` is a section `id` from config.json; `query` groups results so the script takes turns between searches.
- `date`: only when the result shows it (`YYYY-MM-DD`, or a full date-time). No date: leave it out; the story is kept, flagged `date?`, and must be confirmed before use. Never guess a date.
- `weather` and `history` are optional. `weather.url` only if the numbers came from that page.
- The script drops what is outside the date window, repeated from the last 14 editions or on the `avoid` list, and prints IDs (`a1`, `h1`…) as usual.
