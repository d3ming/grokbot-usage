#!/usr/bin/env python3
"""Generate a SYNTHETIC agent-data folder for trying grokbot-usage without real data.

Everything it writes is fake: made-up bot names, random IDs, and generic tool names.
Only profile.json and audit.jsonl are produced (no SQLite databases), so the report shows
audit-only turns, which land in "Other background" / "Background (unattributed)".

  python3 examples/make_synthetic_data.py /tmp/fake-agent-data
  python3 grokbot_usage.py --agent-data /tmp/fake-agent-data --out-dir /tmp/fake-report --week latest
"""
import datetime as dt, json, os, random, sys, uuid

FAKE_BOTS = ['Example Bot A', 'Example Bot B', 'Example Bot C']
EVENT_TYPES = [('shell_command', {'target': 'box'}),
               ('mcp_tool_call', {'serverIdentifier': 'user-ExampleMail', 'toolName': 'search'}),
               ('mcp_tool_call', {'serverIdentifier': 'user-ExampleDocs', 'toolName': 'read'}),
               ('browser_navigation', {}),
               ('computer_use_session', {'durationMs': 90000, 'actionCount': 12, 'screenshotCount': 6})]

def main(root, weeks=6, seed=7):
    rnd = random.Random(seed)
    end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    for i, name in enumerate(FAKE_BOTS):
        aid = str(uuid.UUID(int=rnd.getrandbits(128)))
        d = os.path.join(root, 'agents', aid); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'profile.json'), 'w') as fh:
            json.dump({'name': name, 'title': 'Synthetic example'}, fh)
        with open(os.path.join(d, 'audit.jsonl'), 'w') as fh:
            for day in range(weeks * 7):
                for _ in range(rnd.randint(0, 3 - i)):  # a few turns per day, fewer for later bots
                    turn = str(uuid.UUID(int=rnd.getrandbits(128)))
                    t = end - dt.timedelta(days=day, minutes=rnd.randint(0, 1200))
                    for _ in range(rnd.randint(1, 15)):
                        typ, extra = rnd.choice(EVENT_TYPES)
                        t += dt.timedelta(seconds=rnd.randint(5, 120))
                        e = {'ts': t.isoformat().replace('+00:00', 'Z'), 'type': typ, 'turnId': turn,
                             'durationMs': rnd.randint(100, 5000)}
                        e.update(extra)
                        fh.write(json.dumps(e) + '\n')
    print('wrote synthetic agent data to', root)

if __name__ == '__main__':
    if len(sys.argv) != 2: raise SystemExit(__doc__)
    main(sys.argv[1])
