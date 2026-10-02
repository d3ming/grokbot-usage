# grokbot-usage

A weekly usage report for Grok Bot agents. It reads the agent data stored on a Grok Bot box and writes
one self-contained HTML page plus a short text summary. The summary is meant to be read in about five
seconds, so it works as a scheduled weekly report.

> [!WARNING]
> **The numbers are an activity proxy. They are not tokens and not cost.**
> The local agent data has no token or cost fields. The headline metric, **activity**, counts tool calls,
> MCP connector calls, browser navigations and computer-use sessions. Each one counts as 1, whatever it
> cost. Use it to compare weeks and bots with each other, not to estimate a bill.

## Known limits

- **Activity, not tokens or dollars** (see above). The only real token figure stored locally is each
  agent's current context-window fill, which is a snapshot and not cumulative usage.
- **Local history stops on Sep 17, 2026.** On the box this was built on, every agent's local records
  (`audit.jsonl`, `store.db`, `conversation-blobs.db`) end on Sep 17, 2026 at 11:17 PT. Weeks after that
  show zero because nothing was recorded locally, not because the bots were idle. The report marks this
  with a red "Stale data" banner and a "Data freshness" table. See
  [Why local history can stop](#why-local-history-can-stop).
- **Browser work is undercounted.** One computer-use session counts as 1 activity, even when it ran
  dozens of screenshot-and-click steps. The report also shows computer-use minutes separately.
  Subagent transcripts are not stored in the per-agent databases, so subagent and routine runs only show
  their audit events.
- Turn categories and parent links for background work are partly **inferred** from timing (for example,
  a subagent spawn recorded 120 s or less before an audit-only turn). The "notes" field says when a link
  was inferred.

## What the report shows

1. **One summary line**: the selected week's activity and its change from the prior week, the top bot,
   the user-asked/routine split, and "last 7 days vs prior 7 days".
2. **Week over week**: one row per Mon–Sun week, showing activity, absolute change, % change,
   user-asked / routine / other-background activity, tasks, computer-use minutes and stored conversation KB.
3. **Selected week vs prior week**, by bot and by routine-triggered vs user-asked work, plus the
   **top tasks of the week**. A task is one conversation turn plus the subagent turns it spawned.
4. **Data freshness**: the newest local record for each agent.
5. **Detailed views**: daily charts by agent, per-agent totals, categories, routines, top tasks of all
   time, tool and connector breakdowns, possible duplicate work, and a sortable list of all tasks.

The same summary line and week table are printed to stdout.

## Run it

Requires Python 3.9+ and nothing else (standard library only).

```bash
python3 grokbot_usage.py                       # last complete week, agent data in ~/agent-data
python3 grokbot_usage.py --week this           # current week to date
python3 grokbot_usage.py --week latest         # the week with the newest local data
python3 grokbot_usage.py --week 2026-09-10     # the week containing that date
python3 grokbot_usage.py --agent-data /path/to/agent-data --out-dir ~/usage-reports --weeks 12
```

| Flag | Default | Meaning |
|---|---|---|
| `--agent-data DIR` | `$GROKBOT_AGENT_DATA` or `~/agent-data` | folder that contains `agents/<agent-id>/` |
| `--out-dir DIR` | `$GROKBOT_USAGE_OUT` or `./usage-report-out` | where `usage-report.html` goes |
| `--week SPEC` | `last` | `last`, `this`, `latest` or `YYYY-MM-DD` |
| `--weeks N` | `8` | rows in the week-over-week table |
| `--as-of YYYY-MM-DD` | today | treat that day as "now" for `last` and for "last 7 days" |
| `--since YYYY-MM-DD` | none | limit the detailed sections to turns on or after that date |
| `--tz ZONE` | `America/Los_Angeles` | IANA time zone used for days and weeks |
| `--png` | off | also save a PNG screenshot (needs Chrome or Chromium) |
| `--keep-copies` | off | keep the temporary database copies for debugging |

Write the output **outside this repository** or into `usage-report-out/`, which is git-ignored. Reports
contain your prompts and bot names.

### Try it without real data

```bash
python3 examples/make_synthetic_data.py /tmp/fake-agent-data
python3 grokbot_usage.py --agent-data /tmp/fake-agent-data --out-dir /tmp/fake-report --week this
```

The generator writes **synthetic example data**: fake bot names ("Example Bot A/B/C"), random IDs and
generic tool names.

## What it reads

For each `agents/<id>/` folder that has a `profile.json`:

| File | Used for |
|---|---|
| `profile.json` | the bot's display name (resolved at run time; no names are hardcoded) |
| `audit.jsonl` | shell, MCP, browser-navigation and computer-use events per turn |
| `store.db` (+ `-wal`/`-shm`) | transcript user messages, the recent request log, the automation inbox |
| `conversation-blobs.db` | stored main-conversation steps (step kinds, sizes, timestamps, subagent spawns) |
| `automations/*/automation.json`, `runs.json` | routine names, schedules and run windows |

Everything is read-only. The SQLite files are copied to a temporary folder, opened there, and deleted
afterwards, so the live databases are never opened.

## Why local history can stop

On the box this tool was developed on, local records end on Sep 17, 2026 even though the bots kept
working. Findings:

- All agents stop on the same day. The newest `audit.jsonl` event, `store.db` transcript row and
  request-log entry all end on Sep 17. Agents that were idle earlier stop earlier.
- New turns are not appended locally. Commands run on Oct 2 did not show up in `audit.jsonl`, and
  the file's mtime did not change.
- The files were restored from a snapshot. Every file under `agent-data/` has the same mtime (the time
  the box was re-provisioned on Oct 1), and the host log says
  `box-store sync enabled (snapshot-out only)`.
- The agents now run server-side. Each `profile.json` has a server-side harness and server ID. The
  host opens most `store.db` files read-only and logs `server-agent-proxy: server transcript tail ...`.
  So conversations and audit events now live in the backend, and the box only keeps the old local copy.
- No fresher local copy exists. `agent-transcripts/*.jsonl` and `agents/audit-outbox.json` also end
  on Sep 17, and the `/tmp` logs have no per-turn data.

**Effect:** with only local data, the weekly report can't cover weeks after Sep 17. The fix has to come
from a server-side source, such as a usage or audit export from the Grok Bot or Cursor backend (see
"Next steps"). The report flags stale data instead of showing a silent zero.

## Next steps

- Add a server-side data source for history after Sep 17 (a usage or audit export, or the team
  usage-events API if the account has one) and merge it by turn or request ID.
- Import real token and cost data. The Cursor dashboard's usage CSV export and the Cloud Agents API's
  per-run token usage both carry input, output and cache tokens. Join them to turns by timestamp, the
  way `cursortrack` joins billing rows.
- Attribute browser work properly: count computer-use actions and screenshots, not sessions.
- Run it on a schedule (for example, Monday mornings) and post the summary line.

## Privacy

This repository contains code only. Never commit generated reports, screenshots, databases or audit logs.
`.gitignore` blocks `*.html`, `*.png`, `*.db`, `*.jsonl`, `data/`, `scratch/` and the output folders.

## License

MIT. See [LICENSE](LICENSE).
