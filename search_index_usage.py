#!/usr/bin/env python3
"""Usage from search-index.db (chat text only). Measures are message counts and
character counts. They are NOT tokens and NOT cost."""
import argparse, collections, glob, json, os, re, sqlite3, sys

ROUTINE = re.compile(r"^\s*(\[routine\]|\[automation\])", re.I)
WAKE = re.compile(r"^\s*(\[?GROK_BOT_HIDDEN_PROMPT\]?|\[agent\]|\[inbound\]|Cain here\b|Cain:|"
                  r"(Research )?task from Dong\b|Dong (said|asks|approved|decided|renamed|wants|via)\b|"
                  r"[^\n]{0,40}\bfrom Dong\b|[^\n]{0,40}\(Dong,|Bellatrix re-check|A message just arrived from another)", re.I)


def classify(text):
    if ROUTINE.search(text or ""):
        return "routine"
    if WAKE.search(text or ""):
        return "agent"
    return "user"


def open_ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def agent_names(agent_data):
    names = {}
    for p in glob.glob(os.path.join(agent_data, "agents", "*", "profile.json")):
        aid = os.path.basename(os.path.dirname(p))
        try:
            names[aid] = json.load(open(p)).get("name") or aid[:8]
        except (OSError, ValueError):
            names[aid] = aid[:8]
    return names


def transcript_ids(agent_data, aid):
    db = os.path.join(agent_data, "agents", aid, "store.db")
    if not os.path.exists(db):
        return set()
    try:
        c = open_ro(db)
        return {r[0] for r in c.execute("select id from transcript_entries")}
    except sqlite3.Error:
        return set()


def load_turns(db, start_ms=0, end_ms=None, skip_ids=None):
    """Turn = one user message plus the assistant messages after it, per agent."""
    c = open_ro(db)
    q = "select agent_id, entry_id, role, timestamp_ms, body from messages where timestamp_ms >= ?"
    args = [start_ms]
    if end_ms is not None:
        q += " and timestamp_ms < ?"
        args.append(end_ms)
    q += " order by agent_id, timestamp_ms, coalesce(seq, 0), id"
    turns, cur, seen = [], {}, set()
    for aid, eid, role, ts, body in c.execute(q, args):
        if (aid, eid) in seen or (skip_ids and eid in skip_ids.get(aid, ())):
            continue
        seen.add((aid, eid))
        if role == "user" or aid not in cur:
            t = {"agent_id": aid, "start_ms": ts, "end_ms": ts, "opening": body if role == "user" else "",
                 "kind": classify(body) if role == "user" else "orphan", "assistant_msgs": 0,
                 "assistant_chars": 0, "user_chars": len(body) if role == "user" else 0}
            cur[aid] = t
            turns.append(t)
        if role == "assistant":
            t = cur[aid]
            t["assistant_msgs"] += 1
            t["assistant_chars"] += len(body)
            t["end_ms"] = ts
    return turns


def routine_runs(agent_data, start_ms=0, end_ms=None):
    out = collections.Counter()
    for p in glob.glob(os.path.join(agent_data, "agents", "*", "automations", "*", "runs.json")):
        try:
            runs = json.load(open(p))
        except (OSError, ValueError):
            continue
        name = os.path.basename(os.path.dirname(p))
        for r in runs:
            s = r.get("startedAt") or 0
            if s >= start_ms and (end_ms is None or s < end_ms):
                out[(name, r.get("status"))] += 1
    return out


def summarize(turns, names):
    by = collections.defaultdict(lambda: collections.Counter())
    for t in turns:
        b = by[names.get(t["agent_id"], t["agent_id"][:8])]
        b["turns"] += 1
        b["turns_" + t["kind"]] += 1
        b["assistant_msgs"] += t["assistant_msgs"]
        b["assistant_chars"] += t["assistant_chars"]
    return by


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent-data", default=os.environ.get("GROKBOT_AGENT_DATA", os.path.expanduser("~/agent-data")))
    ap.add_argument("--db")
    ap.add_argument("--start-ms", type=int, default=0)
    ap.add_argument("--end-ms", type=int)
    ap.add_argument("--dedupe-transcripts", action="store_true")
    ap.add_argument("--json", action="store_true", help="per-turn JSON without message bodies")
    a = ap.parse_args(argv)
    db = a.db or os.path.join(a.agent_data, "search-index.db")
    names = agent_names(a.agent_data)
    skip = {aid: transcript_ids(a.agent_data, aid) for aid in names} if a.dedupe_transcripts else None
    turns = load_turns(db, a.start_ms, a.end_ms, skip)
    if a.json:
        for t in turns:
            print(json.dumps({k: v for k, v in t.items() if k != "opening"}))
        return 0
    print("Measures: messages (activity) and chars (proxy). NOT tokens.")
    for name, b in sorted(summarize(turns, names).items(), key=lambda x: -x[1]["assistant_chars"]):
        print(f"{name:12} turns={b['turns']:4} user={b['turns_user']:3} agent={b['turns_agent']:3} "
              f"routine={b['turns_routine']:3} asst_msgs={b['assistant_msgs']:4} asst_chars={b['assistant_chars']}")
    for (n, s), v in sorted(routine_runs(a.agent_data, a.start_ms, a.end_ms).items()):
        print(f"routine {n} status={s}: {v} runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
