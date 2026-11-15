# No network

Read when the collection printed `NETWORK BLOCKED`: Claude's network access does not reach the sources (some apps let the shell reach only a list of hosts). Gather with the web tools, which still work, and let the script do the filtering. Tell the reader nothing about it.

1. **All in one block of calls**, in parallel: one web search per section (two for the section with the most `count`), built from its `queries` plus the current month and year; the weather (the app's weather tool if there is one, else one search); and one search for today's date in history in the reader's language ("15 de novembro na história", "November 15 in history"). Never open a Wikipedia date page: it is very long.
2. Weather from the app's tool has no page: give `source`, no `url`. History: take the dated events from the results' snippets, each with the link of the result it came from; use an event only from Wikipedia or an encyclopedia when one is among the results. "Did you know?" has no candidates in this mode: `references/extras.md` (its search and page limits apply).
3. Write **every** result to `WORK/results.json` (format below): title, link, outlet, the date when the result shows one, and the snippet as the result shows it. Leave nothing out on your own, even a result that looks old or repeated: the script applies the date window, the repeat checks and the `avoid` list, and lists what it dropped and why (`DROPPED`).
   - **The snippet is the result's own text, never the search tool's written answer** (some search tools add a summary of all results in their own words; it mixes sources and is not a source). No snippet shown: leave `summary` empty.
4. Run `python3 SKILL/scripts/collect.py --config WORK/config.json --work WORK --index WORK/index.json --from-results WORK/results.json`. It prints the same summary as a normal collection, with IDs: from here on use `ref` as usual. A story flagged `date?` came without a date: confirm it is inside the window before using it, with one search of its title (not by opening the page), and only for stories you would print. Send these confirmations together with the headline's extra research, in one block. Not confirmed: leave the story out. Nothing beyond this: no second round of section searches.

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
- `date`: only when the result shows it as the publication date (`YYYY-MM-DD`, or a full date-time). Not a date read from the URL, the title or the page header: leave it out; the story is kept, flagged `date?`, and must be confirmed before use. Never guess a date.
- `weather` and `history` are optional. `weather.url` only if the numbers came from that page.
- The script drops what is outside the date window, repeated from the last 14 editions or on the `avoid` list, and prints IDs (`a1`, `h1`…) as usual.
