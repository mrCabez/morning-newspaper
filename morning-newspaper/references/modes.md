# Modes and missing tools

Read in step 0 when a tool is missing, or when the reader asks only for the newspaper, a test or a copy to show other people.

## What the skill needs, and what happens without it

Only the shell, web search and a folder the scripts can write to are required; everything else degrades on its own:

| Missing tool | What happens |
| --- | --- |
| Artifact tools (list, read, publish) | **Only the newspaper**, no newsstand (below). Never publish anywhere public to make up for it. |
| Publishing several files to one artifact | **Single page** (`references/archive.md`): each edition replaces the previous one in the same private page, which carries the reader's settings and recent stories, so nothing is lost between runs. |
| Shell network access to the sources | The collection prints `NETWORK BLOCKED` (`references/no-network.md`). Some apps let the shell reach only a list of hosts; the script needs `news.google.com`, `wttr.in`, `api.open-meteo.com`, `<lang>.wikipedia.org` and the hosts of any `feeds`. |
| Google Calendar | Use the phone's or the app's own calendar tool if the session has one (same listing, today and tomorrow). With neither, the agenda box stays and says that Google Calendar is not connected and that connecting it syncs the agenda. Connected but failing twice: the box says it could not be read, and why. |
| Gmail | The classifieds box stays and says that Gmail is not connected (or, if it failed twice, that it could not be read, and why). |
| Connector registry | In setup, name the missing connection; there is no button to show. |
| Multiple-choice question tool | Setup asks in one numbered message (`references/setup.md`). |
| Scheduled-task tool | No daily schedule: say so in one line if the reader asks for one, and that they can ask for the paper whenever they like. Never offer "schedule my newspaper" in this session. |
| Playwright with Chromium | No screenshot check. Install nothing. |

## Only the newspaper (no newsstand)

The edition is made as a single page, with no newsstand, when:
- the reader asks only for the newspaper, a test, "don't publish" or "don't add it to the newsstand", or a copy to show other people; or
- the newsstand cannot be created or reached: this session has no artifact tools, or publishing fails for good (`references/archive.md`).

How:
- **To show other people** (colleagues, friends, a post): the agenda and the emails are personal, so the reader decides. In an interactive session ask one short question before collecting, with "Leave them out (Recommended)" first and "Include them" second. No answer, or a scheduled run: leave them out. Never decide it silently in either direction.
- If there is a newsstand, still find it (step 1) and read its config and index, **read-only**, so the issue number and the repeat checks are right. Publish nothing and create nothing.
- Without a newsstand and without a config: interactive → the quick start of `references/setup.md` (keep the config in WORK); scheduled → a sample edition (`references/sample.md`).
- In step 5 add `--share` (copy to show) or `--standalone` (everything else). The page has no newsstand button, says at the top what was left out (`--share`), and no index is written.
- Step 6 is skipped. Steps 7 (sentence-by-sentence check) and the `COVERAGE:` line still apply. In step 8 send the file the script printed and **say in one sentence that no newsstand was created and why**: the reader asked for it; this app has no way to keep a newsstand (no artifact tools); or publishing failed (say the error in plain words).

## Newsstand not found

When the slug or link given in the task or the request still cannot be read after a second try (`references/archive.md`). The reader's settings live in the newsstand's `config.json`, so what comes out depends on whether a copy exists elsewhere:
- **A config in the task prompt or the request:** save it as `WORK/config.json` and make the full edition.
- **No config anywhere:** a minimal edition, never a sample or a guess at their interests. Write `WORK/config.json` with only `language` (the language the task prompt or the request is written in, or the one it names) and `timezone` (the session's), and `"sections": []`. The collection then brings no news and no weather, only "On this day" and "Did you know?"; read the agenda and Gmail as usual. In `edition.json` leave `headline` and `sections` out.
- No index exists, so this is No. 001 and the repeat checks see nothing: that is expected.
- In step 5 add `--newsstand-missing` (it implies `--standalone`): the page says at the top that the newsstand was not found and, without a config, that there are no news sections. Skip step 6; steps 7 and 8 apply. In step 8 send the file and add one **Missing** line saying the newsstand given in the task was not found, so the edition was not added to it (and, without a config, why there is no news). Recreating the newsstand is the reader's decision: never do it in a scheduled run.

## No folder to write in

If the working folder (SKILL.md, "Edition flow") cannot be created (read-only), use the session's temporary folder (`mktemp -d`) as WORK. If no folder can be written at all, there can be no edition: tell the reader in one sentence that the newspaper could not be printed because this session cannot save files, and stop.
