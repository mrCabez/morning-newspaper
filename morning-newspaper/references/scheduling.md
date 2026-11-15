# Scheduling

When the reader asks for a daily schedule, or chose a delivery time during setup (`references/setup.md`). Never create one they did not ask for:
- **No config yet?** Do not create the task first. Run `references/setup.md` (the quick start is fine), publish edition No. 001 so the reader sees what they will get, and only then create the schedule. A schedule created before setup would wake up to a sample edition instead of their paper.
- Use the session's scheduled-task tool (if there is none, see `references/modes.md`). Run a few minutes before the time they want the paper ready, in their timezone (`CRON_TZ=<config.timezone> 43 6 * * *` for a 7:00 paper).
- The task prompt must be self-contained, written in the reader's language, and include the newsstand slug, e.g.: "Run the morning-newspaper skill and publish today's edition to the newsstand (slug: SLUG). Scheduled run: ask nothing, decide and go on."
- After creating it, say in one sentence which approval mode it got. If runs will ask for approval, explain that nobody is there at dawn to approve, so the edition would stall, and point to `references/permissions.md`.
- Never create, change or delete scheduled tasks during a scheduled run.
