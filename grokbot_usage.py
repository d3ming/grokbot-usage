#!/usr/bin/env python3
"""grokbot-usage: weekly usage report for Grok Bot agents, built from local agent data.

Usage:
  python3 grokbot_usage.py [--agent-data DIR] [--out-dir DIR] [--week last|this|latest|YYYY-MM-DD]
                           [--weeks N] [--as-of YYYY-MM-DD] [--since YYYY-MM-DD] [--tz ZONE] [--png]

For each agent folder under --agent-data/agents/ it reads audit.jsonl, profile.json and
automations/*/{automation,runs}.json read-only, copies store.db and conversation-blobs.db
(plus WAL/SHM files) into a temporary folder and opens only those copies, then writes one
self-contained HTML report (inline SVG + JS) and prints a weekly summary to stdout.

IMPORTANT: the local data has no token or cost fields. Every number is an ACTIVITY PROXY
(tool calls, browser navigations, computer-use sessions, stored conversation bytes), not
tokens and not dollars.
"""
import argparse, collections, datetime as dt, html, json, os, re, shutil, sqlite3, subprocess, sys, tempfile
from zoneinfo import ZoneInfo

DEFAULT_AGENT_DATA = os.environ.get('GROKBOT_AGENT_DATA', '~/agent-data')
DEFAULT_OUT_DIR = os.environ.get('GROKBOT_USAGE_OUT', './usage-report-out')
TZ = ZoneInfo('America/Los_Angeles')  # overridden by --tz
ROOT_ID = 'sand-live-conversation-root-v1__'.encode().hex()
STALE_AFTER_DAYS = 2

# step kinds inside a stored conversation step (protobuf field number -> label), learned from the data
STEP_KIND = {1: 'Shell', 8: 'Read', 9: 'TodoWrite', 15: 'MCP call', 18: 'WebSearch', 19: 'Task (subagent spawn)',
             30: 'Screenshot', 37: 'WebFetch', 42: 'Await', 44: 'Tool schema lookup', 48: 'Built-in tool call',
             55: 'SendToUser'}
# step kinds counted as "activity" from transcripts (the rest of the activity comes from audit.jsonl,
# which already logs shell, MCP, browser navigation and computer-use sessions)
CONTENT_ONLY_ACTIVITY = {'Read', 'WebSearch', 'WebFetch', 'Screenshot', 'Task (subagent spawn)'}

# ---------------------------------------------------------------- protobuf helpers
def _varint(b, i):
    r = s = 0
    while True:
        x = b[i]; i += 1; r |= (x & 0x7f) << s; s += 7
        if x < 0x80: return r, i

def pb(b):
    """Minimal schemaless protobuf parse -> list of (field, wiretype, value) or None."""
    i, out = 0, []
    try:
        while i < len(b):
            k, i = _varint(b, i); f, w = k >> 3, k & 7
            if f == 0: return None
            if w == 0: v, i = _varint(b, i)
            elif w == 1: v = b[i:i + 8]; i += 8
            elif w == 5: v = b[i:i + 4]; i += 4
            elif w == 2:
                l, i = _varint(b, i); v = b[i:i + l]; i += l
                if i > len(b): return None
            else: return None
            out.append((f, w, v))
        return out
    except IndexError:
        return None

def first(p, f, w=None):
    for ff, ww, v in p or []:
        if ff == f and (w is None or ww == w): return v
    return None

# ---------------------------------------------------------------- loading
def read_json(path, default):
    try:
        with open(path, encoding='utf8') as fh: return json.load(fh)
    except Exception:
        return default

def ms_of(iso):
    return int(dt.datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() * 1000)

def pt(ms):
    return dt.datetime.fromtimestamp(ms / 1000, TZ)

def copy_dbs(agent_dir, aid, copy_root):
    """Copy an agent's SQLite files (incl. WAL/SHM) so the live databases are never opened."""
    dst = os.path.join(copy_root, aid); os.makedirs(dst, exist_ok=True)
    copied = []
    for f in ('store.db', 'store.db-wal', 'store.db-shm', 'conversation-blobs.db',
              'conversation-blobs.db-wal', 'conversation-blobs.db-shm'):
        s = os.path.join(agent_dir, f)
        d = os.path.join(dst, f)
        if os.path.isfile(s):
            shutil.copy2(s, d); copied.append(f)
    return dst, copied

def clean_prompt(t):
    t = t or ''
    t = re.sub(r'^\[t\d+u\]\n', '', t)
    t = re.sub(r'\[Agents mentioned in this message.*?\]\n', '', t, flags=re.S)
    t = re.sub(r'<system_reminder>.*?(</system_reminder>|$)', '', t, flags=re.S)
    t = t.replace('[SAND_HIDDEN_PROMPT]', '')
    t = re.sub(r'<<SAND_AGENT_PROFILE_UPDATE:[^>]*>>', '(agent profile update)', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

def strip_reminders(raw):
    r = raw or ''
    while True:
        r2 = re.sub(r'^(\[SAND_HIDDEN_PROMPT\])*\s*<system_reminder>.*?</system_reminder>\s*', '', r, count=1, flags=re.S)
        if r2 == r: return r
        r = r2

ROUTINE_RE = re.compile(r'^\[A background task just completed\].{0,200}?Background task "\[routine\] "([^"]+)"', re.S)

def classify_prompt(raw):
    """Return (category, routine name) for a main-conversation turn from its stored starting prompt."""
    r = strip_reminders(raw)
    r1 = re.sub(r'^(\[SAND_HIDDEN_PROMPT\])+', '', r)
    if re.match(r'\[t\d+u\]', r) or r.startswith('[Answering your question') or r1.startswith('[The user submitted the form') \
            or r1.startswith('[The user reacted'):
        return 'User-asked', None
    m = ROUTINE_RE.match(r1)
    if m: return 'Routine / automation', m.group(1)
    if r1.startswith('[A background task just completed]'):
        return 'Background follow-up', None
    if r1.startswith('[agent]'):
        return 'Agent-to-agent', None
    return 'System / housekeeping', None

def new_turn(aid, tid):
    return dict(agent=aid, turn=tid, source=None, start=None, end=None, raw='', prompt='', category=None,
                routine=None, parent=None, sub_type=None, events=0, shell=0, mcp=0, nav=0, cu=0, cu_ms=0,
                cu_actions=0, cu_shots=0, mcp_ms=0, steps=0, model_steps=0, content_bytes=0,
                step_kinds=collections.Counter(), tools=collections.Counter(), spawned=[], note='')

def agent_name(aid, adir):
    """Display name, resolved at runtime from the agent's own profile.json (never hardcoded)."""
    prof = {}
    try:
        with open(os.path.join(adir, 'profile.json'), encoding='utf8') as fh: prof = json.load(fh)
    except Exception: pass
    name = (prof.get('name') or '').strip() if isinstance(prof.get('name'), str) else ''
    if aid.startswith('sand-subagent-'):
        return f"{name or 'Subagent'} ({aid[14:22]})"
    return name or f'agent {aid[:8]}'

def load_agent(aid, adir, turns, info, copy_root):
    a = info.setdefault(aid, dict(name=agent_name(aid, adir), ctx=None, automations=[], spawns=[], inbox=[],
                                  kv_requests=[], transcript_prompts={}, copied=[], last_ms=None))
    dst, a['copied'] = copy_dbs(adir, aid, copy_root)

    # ---- store.db copy: transcript user messages, request sources, automation inbox
    sp = os.path.join(dst, 'store.db')
    if os.path.exists(sp):
        con = sqlite3.connect(sp)
        try:
            kv = dict(con.execute('select key, value from kv'))
            try: a['kv_requests'] = json.loads(kv.get('requestIds', '[]'))
            except Exception: pass
            for (e,) in con.execute('select entry from transcript_entries order by seq'):
                j = json.loads(e)
                if j.get('timestampMs'): a['last_ms'] = max(a['last_ms'] or 0, j['timestampMs'])
                if j.get('kind') == 'message' and j.get('role') == 'user' and j.get('requestId'):
                    a['transcript_prompts'].setdefault(j['requestId'], (j.get('timestampMs'), j.get('content') or ''))
            a['inbox'] = [dict(id=i, text=t, attribution=at) for i, t, at in
                          con.execute('select id, text, attribution from automation_completion_inbox')]
        except sqlite3.Error as ex:
            print(f'  warning: {a["name"]}: store.db not fully readable ({ex})', file=sys.stderr)
        finally:
            con.close()

    # ---- automations (read-only JSON)
    adir_auto = os.path.join(adir, 'automations')
    if os.path.isdir(adir_auto):
        for n in sorted(os.listdir(adir_auto)):
            meta, runs = read_json(os.path.join(adir_auto, n, 'automation.json'), {}), read_json(os.path.join(adir_auto, n, 'runs.json'), [])
            a['automations'].append(dict(id=n, name=meta.get('name', n), schedule=meta.get('schedule'),
                                         enabled=meta.get('enabled'), lastRunAt=meta.get('lastRunAt'), runs=runs))

    # ---- conversation blobs copy: per-turn records hanging off the live root
    bp = os.path.join(dst, 'conversation-blobs.db')
    if os.path.exists(bp):
        con = sqlite3.connect(bp)
        try: blobs = dict(con.execute('select id, data from blobs'))
        except sqlite3.Error: blobs = {}
        finally: con.close()
        root = pb(blobs.get(ROOT_ID, b'')) or []
        for f, w, v in root:
            if f == 5 and w == 2:  # current context window snapshot (tokens used, window size)
                p = pb(v); a['ctx'] = (first(p, 1, 0), first(p, 2, 0))
            if f == 16 and w == 2:  # subagent registry
                p = pb(v); sid = (first(p, 1, 2) or b'').decode('utf8', 'replace')
                q = pb(first(p, 2, 2) or b'') or []
                ts = first(q, 2, 0); typ = ''
                t4 = first(q, 4, 2)
                if t4:
                    t3 = first(pb(t4), 3, 2)
                    if t3: typ = (first(pb(t3), 1, 2) or b'').decode('utf8', 'replace')
                a['spawns'].append(dict(sid=sid, ts=ts, type=typ, parent=None))
        sid_index = {s['sid']: s for s in a['spawns']}
        for f, w, v in root:
            if f != 8 or w != 2 or len(v) != 32: continue
            rec = pb(blobs.get(v.hex(), b''))
            body = pb(first(rec, 1, 2) or b'')
            if not body: continue
            rid = (first(body, 3, 2) or b'').decode('utf8', 'replace')
            if not rid: continue
            t = turns.setdefault((aid, rid), new_turn(aid, rid)); t['source'] = 'conversation'
            ph = first(body, 1, 2)
            if ph and ph.hex() in blobs:
                pp = pb(blobs[ph.hex()])
                raw = (first(pp, 1, 2) or b'').decode('utf8', 'replace')
                ts = first(pp, 25, 0)
                if ts: t['start'] = min(t['start'] or ts, ts)
                # merged records for one requestId: keep the real user message if any
                if not t['raw'] or classify_prompt(raw)[0] == 'User-asked': t['raw'] = raw
            for ff, ww, sv in body:
                if ff != 2 or ww != 2: continue
                d = blobs.get(sv.hex(), b'')
                t['steps'] += 1; t['content_bytes'] += len(d)
                s = pb(d) or []
                inner = pb(first(s, 2, 2) or b'')
                if inner is None or not first(s, 2, 2):
                    t['model_steps'] += 1  # assistant text / reasoning step
                    th = pb(first(s, 1, 2) or b'')
                    for k in (2, 3):
                        x = first(th, k, 0)
                        if x: t['start'] = min(t['start'] or x, x); t['end'] = max(t['end'] or x, x)
                    continue
                for k in (59, 60):
                    x = first(inner, k, 0)
                    if x: t['start'] = min(t['start'] or x, x); t['end'] = max(t['end'] or x, x)
                kinds = [x for x in inner if x[0] not in (57, 59, 60)]
                if not kinds: continue
                kf, kw, kv_ = kinds[0]
                label = STEP_KIND.get(kf, f'step type {kf}')
                t['step_kinds'][label] += 1
                if label in CONTENT_ONLY_ACTIVITY:
                    t['tools'][label] += 1; t['events'] += 1
                if label == 'Task (subagent spawn)':
                    res = pb(first(pb(kv_), 2, 2) or b'')
                    r1 = pb(first(res, 1, 2) or b'')
                    sid = (first(r1, 2, 2) or b'').decode('utf8', 'replace')
                    if sid in sid_index and sid_index[sid]['parent'] is None:
                        sid_index[sid]['parent'] = rid
                        t['spawned'].append(sid)
                    else:
                        for s2 in a['spawns']:
                            if s2['parent'] is None and s2['sid'].encode() in d:
                                s2['parent'] = rid; t['spawned'].append(s2['sid'])

    # ---- audit.jsonl (read-only)
    ap = os.path.join(adir, 'audit.jsonl')
    if os.path.exists(ap):
        with open(ap, encoding='utf8') as fh:
            for line in fh:
                line = line.strip()
                if not line: continue
                try: e = json.loads(line)
                except Exception: continue
                tid = e.get('turnId') or 'unknown'
                t = turns.setdefault((aid, tid), new_turn(aid, tid))
                if t['source'] is None: t['source'] = 'audit-only'
                try: ts = ms_of(e['ts'])
                except Exception: continue
                dur = e.get('durationMs') or 0
                a['last_ms'] = max(a['last_ms'] or 0, ts + dur)
                t['start'] = min(t['start'] or ts, ts); t['end'] = max(t['end'] or ts, ts + dur)
                typ = e.get('type'); t['events'] += 1
                if typ == 'shell_command':
                    t['shell'] += 1; t['tools']['Shell (' + (e.get('target') or '?') + ')'] += 1
                elif typ == 'mcp_tool_call':
                    t['mcp'] += 1; t['mcp_ms'] += dur
                    srv = e.get('serverIdentifier') or '?'; tn = e.get('toolName') or '?'
                    if tn.startswith(srv + '-'): tn = tn[len(srv) + 1:]
                    t['tools'][f'{srv} · {tn}'] += 1
                elif typ == 'browser_navigation':
                    t['nav'] += 1; t['tools']['Browser navigation'] += 1
                elif typ == 'computer_use_session':
                    t['cu'] += 1; t['cu_ms'] += dur; t['cu_actions'] += e.get('actionCount') or 0
                    t['cu_shots'] += e.get('screenshotCount') or 0; t['tools']['Computer-use session'] += 1
                else:
                    t['tools'][f'other: {typ}'] += 1

def attribute(turns, info):
    """Assign category / routine / parent to every turn."""
    for aid, a in info.items():
        runs = []
        for au in a['automations']:
            for r in au['runs']:
                runs.append((r.get('requestId'), r.get('startedAt'), r.get('finishedAt'), au['name']))
        spawns = sorted([s for s in a['spawns'] if s['ts']], key=lambda s: s['ts'])
        routine_done = []
        for (ag, tid), t in turns.items():
            if ag == aid and t['source'] == 'conversation':
                c, rn = classify_prompt(t['raw'])
                if rn and t['start']: routine_done.append((t['start'], rn))
        routine_done.sort()
        for (ag, tid), t in turns.items():
            if ag != aid: continue
            if t['source'] == 'conversation':
                tp = a['transcript_prompts'].get(tid)
                t['category'], t['routine'] = classify_prompt(t['raw'])
                if t['routine']: t['routine'] += ' (completion relay)'
                t['prompt'] = clean_prompt(tp[1] if (tp and t['category'] == 'User-asked' and re.match(r'\[t\d+u\]', t['raw'])) else strip_reminders(t['raw']))
                if tp and tp[0]: t['start'] = min(t['start'] or tp[0], tp[0])
                continue
            # audit-only turn: not in the stored main conversation
            hit = [r for r in runs if r[0] == tid]
            if hit:
                t['category'] = 'Routine / automation'; t['routine'] = hit[0][3]
                t['prompt'] = f"Scheduled run of routine '{hit[0][3]}'"; t['note'] = 'matched runs.json requestId'
                continue
            win = [r for r in runs if r[1] and r[2] and r[1] - 1000 <= (t['start'] or 0) <= r[2]]
            if win:
                t['category'] = 'Routine / automation'; t['routine'] = win[0][3]
                t['prompt'] = f"Helper work during routine run '{win[0][3]}'"
                t['note'] = 'inferred: started inside the routine run window'
                continue
            prev = [s for s in spawns if s['ts'] <= (t['start'] or 0) + 5000 and (t['start'] or 0) - s['ts'] <= 120000]
            if prev:
                s = prev[-1]; t['sub_type'] = s['type'] or 'subagent'
                if s['parent'] and (aid, s['parent']) in turns:
                    t['parent'] = (aid, s['parent'])
                t['note'] = f"inferred: subagent {s['sid'][14:22]} ({t['sub_type']}) spawned {round(((t['start'] or 0) - s['ts'])/1000)}s earlier"
                t['category'] = None  # inherits parent's later
                continue
            comp = [c for c in routine_done if 0 <= c[0] - (t['start'] or 0) <= 3600000]
            if comp:
                t['category'] = 'Routine / automation'; t['routine'] = comp[0][1]
                t['prompt'] = f"Scheduled run of routine '{comp[0][1]}'"
                t['note'] = 'matched: a "[routine]" completion for this routine arrived within 60 min'
                continue
            t['category'] = 'Background (unattributed)'
            t['prompt'] = '(background/subagent turn; no starting prompt stored locally)'
            t['note'] = 'not in the stored conversation and not linkable to a spawn or routine'
    # subagent turns inherit parent category/prompt
    for t in turns.values():
        if t['category'] is None:
            p = turns.get(t['parent']) if t['parent'] else None
            if p:
                t['category'] = p['category']; t['routine'] = p['routine']
                t['prompt'] = f"[{t['sub_type']} subagent] " + (p['prompt'] or '')
            else:
                t['category'] = 'Background (unattributed)'
                t['prompt'] = f"[{t['sub_type']} subagent] (parent turn not found)"

# ---------------------------------------------------------------- rendering helpers
AGENT_COLORS = ['#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f', '#edc948', '#b07aa1', '#ff9da7', '#9c755f', '#bab0ac']
CAT_COLORS = {'User-asked': '#4e79a7', 'Routine / automation': '#e15759', 'Background follow-up': '#f28e2b',
              'Agent-to-agent': '#76b7b2', 'System / housekeeping': '#bab0ac', 'Background (unattributed)': '#b07aa1'}
E = html.escape

def trunc(s, n=120):
    s = s or ''
    return s if len(s) <= n else s[:n - 1] + '…'

def fmt(x, nd=0):
    return f'{x:,.{nd}f}'

def stacked_svg(days, series, colors, title, unit, width=1000, height=320, xlabel='Date'):
    if not days: return '<p><i>No data in range.</i></p>'
    ml, mr, mt, mb = 60, 10, 20, 70
    W, H = width - ml - mr, height - mt - mb
    tot = [sum(series[k].get(d, 0) for k in series) for d in days]
    mx = max(tot) or 1
    bw = W / len(days)
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{E(title)}" style="font:11px sans-serif">']
    for i in range(5):
        v = mx * i / 4; y = mt + H - H * i / 4
        out.append(f'<line x1="{ml}" x2="{ml+W}" y1="{y:.1f}" y2="{y:.1f}" stroke="#ddd"/><text x="{ml-5}" y="{y+4:.1f}" text-anchor="end">{fmt(v, 0 if mx>=10 else 1)}</text>')
    for i, d in enumerate(days):
        x = ml + i * bw; y = mt + H
        for k in series:
            v = series[k].get(d, 0)
            if not v: continue
            h = H * v / mx; y -= h
            out.append(f'<rect x="{x+1:.1f}" y="{y:.1f}" width="{max(bw-2,1):.1f}" height="{h:.1f}" fill="{colors[k]}"><title>{E(d)} · {E(k)}: {fmt(v,1)} {E(unit)}</title></rect>')
        if len(days) <= 40 or i % 2 == 0:
            out.append(f'<text transform="translate({x+bw/2:.1f},{mt+H+8}) rotate(60)" font-size="10">{E(d[5:])}</text>')
    out.append(f'<text x="{ml}" y="{height-4}" font-size="11">{E(xlabel)} — y: {E(unit)}</text></svg>')
    return ''.join(out)

def hbar_svg(items, color_of, unit, width=1000, rowh=22):
    if not items: return '<p><i>No data.</i></p>'
    ml = 330; W = width - ml - 90; height = rowh * len(items) + 10
    mx = max(v for _, v in items) or 1
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" style="font:12px sans-serif">']
    for i, (k, v) in enumerate(items):
        y = 5 + i * rowh; w = W * v / mx
        out.append(f'<text x="{ml-6}" y="{y+15}" text-anchor="end">{E(trunc(k, 52))}</text>'
                   f'<rect x="{ml}" y="{y+3}" width="{max(w,1):.1f}" height="{rowh-6}" fill="{color_of(k)}"><title>{E(k)}: {fmt(v, 0 if v >= 10 else 1)} {E(unit)}</title></rect>'
                   f'<text x="{ml+w+5:.1f}" y="{y+15}">{fmt(v, 0 if v >= 10 else 1)}</text>')
    out.append('</svg>')
    return ''.join(out)

def legend(colors):
    return '<div class="legend">' + ''.join(f'<span><i style="background:{c}"></i>{E(k)}</span>' for k, c in colors.items()) + '</div>'

# ---------------------------------------------------------------- weekly helpers
OTHER_BG = ('Background follow-up', 'Background (unattributed)', 'Agent-to-agent', 'System / housekeeping')
BUCKETS = collections.OrderedDict([('User-asked', ('User-asked',)), ('Routine / automation', ('Routine / automation',)),
                                   ('Other background', OTHER_BG)])

def bucket_of(cat):
    for b, cats in BUCKETS.items():
        if cat in cats: return b
    return 'Other background'

def week_start(d):
    """Monday of the (Mon-Sun, local time zone) week containing date d."""
    return d - dt.timedelta(days=d.weekday())

def week_label(ws):
    we = ws + dt.timedelta(days=6)
    return f"{ws.strftime('%b %-d')}–{we.strftime('%b %-d' if we.month != ws.month else '%-d')}, {we.year}"

def delta(cur, prev, nd=0):
    """(absolute change text, percent change text)."""
    d = cur - prev
    a = ('+' if d > 0 else '−' if d < 0 else '±') + fmt(abs(d), nd)
    if prev:
        p = 100 * d / prev
        return a, ('+' if p > 0 else '−' if p < 0 else '±') + f'{abs(p):.0f}%'
    return a, ('new' if cur else '—')

def delta_cells(cur, prev, nd=0):
    a, p = delta(cur, prev, nd)
    cls = 'up' if cur > prev else 'down' if cur < prev else ''
    return f'<td class="n {cls}">{a}</td><td class="n {cls}">{p}</td>'

def resolve_week(spec, today, latest_day):
    if spec in (None, '', 'last'): return week_start(today) - dt.timedelta(days=7)
    if spec == 'this': return week_start(today)
    if spec == 'latest': return week_start(latest_day or today)
    try: return week_start(dt.date.fromisoformat(spec))
    except ValueError: raise SystemExit(f"--week: expected last, this, latest or YYYY-MM-DD, got {spec!r}")

# ---------------------------------------------------------------- main
def main():
    global TZ
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--agent-data', default=DEFAULT_AGENT_DATA,
                    help='Grok Bot agent-data folder that contains agents/ (default: $GROKBOT_AGENT_DATA or ~/agent-data)')
    ap.add_argument('--out-dir', default=DEFAULT_OUT_DIR,
                    help='where to write usage-report.html (default: $GROKBOT_USAGE_OUT or ./usage-report-out)')
    ap.add_argument('--week', default='last',
                    help="week to headline: 'last' (last complete Mon–Sun week, default), 'this' (week to date), "
                         "'latest' (week of the newest local data) or any YYYY-MM-DD inside the week")
    ap.add_argument('--weeks', type=int, default=8, help='number of weeks in the week-over-week table (default 8)')
    ap.add_argument('--as-of', help="treat this date (YYYY-MM-DD, end of day) as 'now' for 'last 7 days' and --week last")
    ap.add_argument('--since', help='detailed sections: only include turns starting on/after this date (YYYY-MM-DD)')
    ap.add_argument('--tz', default='America/Los_Angeles', help='IANA time zone for days and weeks (default America/Los_Angeles)')
    ap.add_argument('--png', action='store_true', help='also save a PNG screenshot of the report (needs Chrome/Chromium)')
    ap.add_argument('--keep-copies', action='store_true', help='keep the temporary database copies (prints their path)')
    args = ap.parse_args()
    TZ = ZoneInfo(args.tz)
    tzlabel = dt.datetime.now(TZ).strftime('%Z')

    src = os.path.join(os.path.expanduser(args.agent_data), 'agents')
    if not os.path.isdir(src):
        raise SystemExit(f'No agents/ folder under {args.agent_data!r}. Pass --agent-data or set GROKBOT_AGENT_DATA.')
    out_dir = os.path.abspath(os.path.expanduser(args.out_dir)); os.makedirs(out_dir, exist_ok=True)
    now = dt.datetime.now(TZ)
    if args.as_of:
        now = dt.datetime.combine(dt.date.fromisoformat(args.as_of), dt.time(23, 59, 59), TZ)
    now_ms = int(now.timestamp() * 1000)
    since_ms = None
    if args.since:
        since_ms = int(dt.datetime.fromisoformat(args.since).replace(tzinfo=TZ).timestamp() * 1000)

    copy_root = tempfile.mkdtemp(prefix='grokbot-usage-')
    turns, info = {}, {}
    try:
        for aid in sorted(os.listdir(src)):
            adir = os.path.join(src, aid)
            if os.path.isdir(adir) and os.path.exists(os.path.join(adir, 'profile.json')):
                load_agent(aid, adir, turns, info, copy_root)
    finally:
        if args.keep_copies: print('database copies kept in', copy_root, file=sys.stderr)
        else: shutil.rmtree(copy_root, ignore_errors=True)
    # disambiguate duplicate display names
    seen = collections.Counter(a['name'] for a in info.values())
    for aid, a in info.items():
        if seen[a['name']] > 1: a['name'] = f"{a['name']} ({aid[:6]})"
    attribute(turns, info)

    allturns = [t for t in turns.values() if t['start']]
    for t in allturns:
        t['day'] = pt(t['start']).strftime('%Y-%m-%d')
        t['dur_s'] = max(0, ((t['end'] or t['start']) - t['start']) / 1000)
        a = info[t['agent']]; a['last_ms'] = max(a['last_ms'] or 0, t['end'] or t['start'])
    data_min = min((t['start'] for t in allturns), default=None)
    data_max = max((t['end'] or t['start'] for t in allturns), default=None)
    T = [t for t in allturns if since_ms is None or t['start'] >= since_ms]
    name = lambda aid: info[aid]['name']

    # ---- tasks = top-level turn + attributed subagent turns (built over all turns)
    def build_tasks(turnlist):
        ids = {id(t) for t in turnlist}; tasks = {}
        for t in turnlist:
            key = t['parent'] if (t['parent'] and id(turns.get(t['parent'])) in ids) else (t['agent'], t['turn'])
            k = tasks.setdefault(key, dict(root=turns[key], turns=[], events=0, nav=0, cu=0, cu_ms=0, mcp=0, shell=0,
                                           content_bytes=0, model_steps=0, subturns=0, sub_types=collections.Counter()))
            k['turns'].append(t)
            for f in ('events', 'nav', 'cu', 'cu_ms', 'mcp', 'shell', 'content_bytes', 'model_steps'):
                k[f] += t[f]
            if key != (t['agent'], t['turn']): k['subturns'] += 1; k['sub_types'][t['sub_type'] or '?'] += 1
        for k in tasks.values(): k['start'] = min(t['start'] for t in k['turns'])
        return sorted(tasks.values(), key=lambda k: (k['events'], k['content_bytes']), reverse=True)
    all_tasks = build_tasks(allturns)
    task_list = build_tasks(T)

    # ---- weekly view (over all turns; --since only trims the detailed sections)
    today = now.date()
    latest_day = pt(data_max).date() if data_max else None
    sel_ws = resolve_week(args.week, today, latest_day)
    prev_ws = sel_ws - dt.timedelta(days=7)
    wk_of = lambda t: week_start(dt.date.fromisoformat(t['day']))
    weekly = collections.defaultdict(lambda: dict(act=0, turns=0, tasks=0, cu=0.0, kb=0.0, nav=0,
                                                  bucket=collections.Counter(), bot=collections.Counter()))
    for t in allturns:
        w = weekly[wk_of(t)]
        w['act'] += t['events']; w['turns'] += 1; w['cu'] += t['cu_ms'] / 60000; w['kb'] += t['content_bytes'] / 1024
        w['nav'] += t['nav']; w['bucket'][bucket_of(t['category'])] += t['events']; w['bot'][name(t['agent'])] += t['events']
    for k in all_tasks: weekly[week_start(pt(k['start']).date())]['tasks'] += 1
    first_ws = week_start(pt(data_min).date()) if data_min else sel_ws
    n_weeks = max(1, args.weeks)
    weeks = [sel_ws - dt.timedelta(days=7 * i) for i in range(n_weeks)][::-1]
    weeks = [w for w in weeks if w >= first_ws] or [sel_ws]
    cur, prv = weekly[sel_ws], weekly[prev_ws]
    week_tasks = [k for k in all_tasks if week_start(pt(k['start']).date()) == sel_ws]

    # rolling windows anchored at "now" (or --as-of)
    def window(lo, hi):
        sel = [t for t in allturns if lo < t['start'] <= hi]
        return sum(t['events'] for t in sel), len(sel)
    d7 = 7 * 86400000
    r_cur, r_prev = window(now_ms - d7, now_ms), window(now_ms - 2 * d7, now_ms - d7)

    stale_days = (now_ms - data_max) / 86400000 if data_max else None
    is_stale = stale_days is None or stale_days > STALE_AFTER_DAYS
    last_txt = pt(data_max).strftime(f'%b %-d %H:%M {tzlabel}') if data_max else 'never'

    def summary_line():
        if data_max and sel_ws > pt(data_max).date():
            lw = week_start(pt(data_max).date()); lc, lp = weekly[lw], weekly[lw - dt.timedelta(days=7)]
            a, p = delta(lc['act'], lp['act'])
            return (f"Week of {week_label(sel_ws)}: NO LOCAL DATA (local records end {last_txt}). "
                    f"Last week with data, {week_label(lw)} (partial): {fmt(lc['act'])} activity (proxy, not tokens), {a} ({p}) vs the week before. "
                    f"Last 7 days {fmt(r_cur[0])} vs prior 7 {fmt(r_prev[0])}.")
        a, p = delta(cur['act'], prv['act'])
        s = f"Week of {week_label(sel_ws)}{' (to date)' if sel_ws == week_start(today) else ''}: {fmt(cur['act'])} activity (proxy, not tokens) — {a} ({p}) vs prior week"
        if cur['act']:
            top_bot, tb = cur['bot'].most_common(1)[0]
            s += (f"; top bot {top_bot} ({100 * tb / cur['act']:.0f}%); user-asked {100 * cur['bucket']['User-asked'] / cur['act']:.0f}%"
                  f" / routine {100 * cur['bucket']['Routine / automation'] / cur['act']:.0f}%")
        s += f"; last 7 days {fmt(r_cur[0])} vs prior 7 {fmt(r_prev[0])}"
        if is_stale: s += f". WARNING: local data ends {last_txt}"
        return s + '.'
    summary = summary_line()

    # ---- aggregates for the detailed views (respect --since)
    agents_present = sorted({t['agent'] for t in T}, key=lambda a: -sum(x['events'] for x in T if x['agent'] == a))
    all_names = sorted({name(t['agent']) for t in allturns}, key=lambda n: -sum(w['bot'][n] for w in weekly.values()))
    acolors = {n: AGENT_COLORS[i % len(AGENT_COLORS)] for i, n in enumerate(all_names)}
    days = []
    if T:
        d0 = min(t['day'] for t in T); d1 = max(t['day'] for t in T)
        d = dt.date.fromisoformat(d0)
        while d.isoformat() <= d1: days.append(d.isoformat()); d += dt.timedelta(days=1)
    def series_by(metric, keyf, turnlist=T):
        s = collections.OrderedDict()
        for t in turnlist:
            s.setdefault(keyf(t), collections.Counter())[t['day']] += metric(t)
        return s
    metrics = collections.OrderedDict([
        ('activity', ('Activity count (tool calls + browser/computer actions)', 'activity', lambda t: t['events'])),
        ('kb', ('Stored conversation content (KB, main conversations only)', 'KB', lambda t: t['content_bytes'] / 1024)),
        ('cu', ('Browser / computer-use minutes', 'min', lambda t: t['cu_ms'] / 60000)),
        ('nav', ('Browser navigations', 'navigations', lambda t: t['nav'])),
    ])
    daily_svgs = {}
    for mk, (title, unit, fn) in metrics.items():
        s = series_by(fn, lambda t: name(t['agent']))
        s = collections.OrderedDict((name(a), s.get(name(a), {})) for a in agents_present)
        daily_svgs[mk] = stacked_svg(days, s, acolors, title, unit)
    # weekly stacked chart (by bot) over the table's weeks
    wk_keys = [w.isoformat() for w in weeks]
    wk_series = collections.OrderedDict((n, collections.Counter({w.isoformat(): weekly[w]['bot'][n] for w in weeks})) for n in all_names)
    weekly_svg = stacked_svg(wk_keys, wk_series, acolors, 'Weekly activity by bot', 'activity', height=260, xlabel='Week starting (Monday)')

    cats = collections.OrderedDict((c, collections.Counter()) for c in CAT_COLORS)
    for t in T:
        c = cats.setdefault(t['category'], collections.Counter())
        c['activity'] += t['events']; c['kb'] += t['content_bytes'] / 1024; c['cu'] += t['cu_ms'] / 60000; c['turns'] += 1
        c['nav'] += t['nav']
    tot_act = sum(c['activity'] for c in cats.values()) or 1
    tot_kb = sum(c['kb'] for c in cats.values()) or 1
    cat_daily = series_by(lambda t: t['events'], lambda t: t['category'])
    cat_daily = collections.OrderedDict((c, cat_daily.get(c, {})) for c in CAT_COLORS)
    routine_names = collections.Counter(); routine_turns = collections.Counter()
    for t in T:
        if t['category'] == 'Routine / automation':
            rn = (t['routine'] or 'Routine wake-up in main conversation').replace(' (completion relay)', '').replace(' (inferred)', '')
            routine_names[f"{name(t['agent'])} · {rn}"] += t['events']; routine_turns[f"{name(t['agent'])} · {rn}"] += 1

    tool_ct = collections.Counter(); conn_ct = collections.Counter()
    for t in T:
        for k, v in t['tools'].items():
            tool_ct[k] += v
            conn_ct[k.split(' · ')[0] if ' · ' in k else k] += v
    agent_tot = collections.Counter(); agent_kb = collections.Counter(); agent_turns = collections.Counter(); agent_cu = collections.Counter()
    for t in T:
        agent_tot[name(t['agent'])] += t['events']; agent_kb[name(t['agent'])] += t['content_bytes'] / 1024
        agent_turns[name(t['agent'])] += 1; agent_cu[name(t['agent'])] += t['cu_ms'] / 60000

    # ---- duplicates / surprises
    dup_prompts = []
    for aid, a in info.items():
        c = collections.defaultdict(list)
        for r in a['kv_requests'] if isinstance(a['kv_requests'], list) else []:
            if isinstance(r, dict) and r.get('prompt'): c[r['prompt'].strip()].append(r)
        for p, rs in c.items():
            rs2 = [r for r in rs if r.get('at') and (since_ms is None or r['at'] >= since_ms)]
            if len(rs2) > 1:
                dup_prompts.append((name(aid), p, len(rs2), sorted(set(r.get('source') or '?' for r in rs2)), min(r['at'] for r in rs2)))
    dup_prompts.sort(key=lambda x: -x[2])
    bursts = collections.defaultdict(lambda: dict(n=0, act=0, cu=0, types=collections.Counter(), parents=set()))
    for t in T:
        if t['source'] == 'audit-only' and t['sub_type']:
            b = bursts[(t['day'], name(t['agent']))]
            b['n'] += 1; b['act'] += t['events']; b['cu'] += t['cu_ms'] / 60000; b['types'][t['sub_type']] += 1
            if t['parent']: b['parents'].add(t['parent'])
    bursts = sorted(((k, v) for k, v in bursts.items() if v['n'] >= 4), key=lambda x: -x[1]['act'])
    auto_rows = []
    for aid, a in info.items():
        for au in a['automations']:
            runs = au['runs'] if isinstance(au['runs'], list) else []
            auto_rows.append((name(aid), au['name'], au['schedule'], au['enabled'], len(runs),
                              ', '.join(pt(r['startedAt']).strftime('%Y-%m-%d %H:%M') for r in runs if isinstance(r, dict) and r.get('startedAt'))))
        if a['inbox']:
            ib = collections.Counter(i['attribution'] for i in a['inbox'])
            for k2, v in ib.items():
                auto_rows.append((name(aid), f'{k2} (completion inbox only; no automations/ folder)', '?', '?', v, ''))

    # ---- HTML
    gen = dt.datetime.now(TZ).strftime(f'%Y-%m-%d %H:%M {tzlabel}')
    rng = f"{pt(data_min).strftime('%Y-%m-%d %H:%M')} → {pt(data_max).strftime('%Y-%m-%d %H:%M')} {tzlabel}" if data_min else 'n/a'
    user_act = cats['User-asked']['activity']; rout_act = cats['Routine / automation']['activity']
    bg_act = sum(cats[c]['activity'] for c in OTHER_BG)
    H = []
    H.append(f'''<!doctype html><html><head><meta charset="utf-8"><title>Grok Bot usage — week of {E(week_label(sel_ws))}</title>
<style>
body{{font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:24px auto;max-width:1100px;color:#222;padding:0 16px}}
h1{{margin:0 0 4px}} h2{{margin-top:34px;border-bottom:1px solid #ddd;padding-bottom:4px}}
.sub{{color:#666}} .kpis{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.kpi{{background:#f5f7fa;border:1px solid #e3e7ee;border-radius:8px;padding:10px 14px;min-width:150px}}
.kpi b{{display:block;font-size:22px}} .warn{{background:#fff8e1;border:1px solid #f0d58c;border-radius:8px;padding:10px 14px;margin-top:10px}}
.stale{{background:#fdecea;border:1px solid #f5b5ae;border-radius:8px;padding:10px 14px;margin-top:10px}}
.headline{{font-size:17px;background:#eef4fb;border-left:5px solid #4e79a7;padding:10px 14px;margin:14px 0}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border-bottom:1px solid #eee;padding:5px 6px;text-align:left;vertical-align:top}}
th{{background:#fafafa;cursor:pointer;position:sticky;top:0}} td.n,th.n{{text-align:right;white-space:nowrap}}
td.up{{color:#b03a2e}} td.down{{color:#1e7b34}} tr.sel td{{background:#eef4fb;font-weight:600}}
.legend span{{margin-right:14px;white-space:nowrap}} .legend i{{display:inline-block;width:11px;height:11px;margin-right:4px;vertical-align:-1px}}
.tabs button{{margin-right:6px;padding:4px 10px;border:1px solid #ccc;background:#fff;border-radius:5px;cursor:pointer}}
.tabs button.on{{background:#4e79a7;color:#fff;border-color:#4e79a7}} .muted{{color:#888;font-size:12px}}
.cols{{display:flex;gap:24px;flex-wrap:wrap}} .cols>div{{flex:1;min-width:320px}}
details summary{{cursor:pointer;color:#345}}
</style></head><body>
<h1>Grok Bot weekly usage</h1>
<div class="sub">Generated {gen} · local data found: {rng}{(' · detailed sections since ' + E(args.since)) if args.since else ''} · weeks run Monday–Sunday, {E(args.tz)}</div>
<div class="headline">{E(summary)}</div>''')
    if is_stale:
        H.append(f'<div class="stale"><b>Stale data:</b> the newest local record is from {E(last_txt)}'
                 f"{f' ({stale_days:.0f} days before this report)' if stale_days is not None else ''}. "
                 'Weeks after that show zero because nothing was recorded locally, not because the bots were idle. See “Data freshness” below.</div>')
    H.append('<div class="warn"><b>Proxy numbers, not tokens or dollars.</b> The local agent data stores no token or cost counts. '
             '“Activity” = tool calls, browser navigations and computer-use sessions from <code>audit.jsonl</code>, plus Read/WebSearch/WebFetch/Screenshot/Task steps in stored conversations. '
             'Browser and computer-use work is undercounted: a whole computer-use session counts as 1.</div>')
    # rolling + KPIs
    ra, rp = delta(r_cur[0], r_prev[0])
    H.append(f'''<div class="kpis">
<div class="kpi"><b>{fmt(cur["act"])}</b>activity, selected week<br><span class="muted">{delta(cur["act"], prv["act"])[0]} ({delta(cur["act"], prv["act"])[1]}) vs prior week</span></div>
<div class="kpi"><b>{fmt(r_cur[0])}</b>last 7 days<br><span class="muted">vs {fmt(r_prev[0])} prior 7 days: {ra} ({rp})</span></div>
<div class="kpi"><b>{fmt(cur["tasks"])}</b>tasks, selected week<br><span class="muted">prior week {fmt(prv["tasks"])}</span></div>
<div class="kpi"><b>{fmt(cur["cu"], 1)} min</b>computer use, selected week<br><span class="muted">prior week {fmt(prv["cu"], 1)} min</span></div>
</div>''')
    # week-over-week table
    H.append('<h2>Week over week</h2><table><tr><th>Week (Mon–Sun)</th><th class="n">Activity</th><th class="n">Δ</th><th class="n">Δ %</th>'
             '<th class="n">User-asked</th><th class="n">Routine</th><th class="n">Other bg</th><th class="n">Tasks</th><th class="n">CU min</th><th class="n">Conv KB</th></tr>')
    for w in weeks:
        cw, pw = weekly[w], weekly[w - dt.timedelta(days=7)]
        H.append(f'<tr class="{"sel" if w == sel_ws else ""}"><td>{E(week_label(w))}</td><td class="n">{fmt(cw["act"])}</td>{delta_cells(cw["act"], pw["act"])}'
                 + ''.join(f'<td class="n">{fmt(cw["bucket"][b])}</td>' for b in BUCKETS)
                 + f'<td class="n">{cw["tasks"]}</td><td class="n">{fmt(cw["cu"], 1)}</td><td class="n">{fmt(cw["kb"])}</td></tr>')
    H.append('</table><p class="muted">Highlighted row = selected week (<code>--week</code>). Δ compares each week with the week before it.</p>')
    H.append(legend({n: acolors[n] for n in all_names}) + weekly_svg)
    # selected week breakdowns
    def cmp_table(title, keys, cget, pget):
        rows = [f'<h3>{E(title)}</h3><table><tr><th></th><th class="n">This week</th><th class="n">Prior week</th><th class="n">Δ</th><th class="n">Δ %</th><th class="n">Share</th></tr>']
        tot = sum(cget(k) for k in keys) or 1
        for k in keys:
            c, p = cget(k), pget(k)
            if not c and not p: continue
            rows.append(f'<tr><td>{E(k)}</td><td class="n">{fmt(c)}</td><td class="n">{fmt(p)}</td>{delta_cells(c, p)}<td class="n">{100 * c / tot:.0f}%</td></tr>')
        if len(rows) == 1: rows.append('<tr><td colspan="6"><i>No activity in either week.</i></td></tr>')
        return ''.join(rows) + '</table>'
    bots = sorted(set(cur['bot']) | set(prv['bot']), key=lambda n: (-cur['bot'][n], -prv['bot'][n]))
    H.append(f'<h2>Week of {E(week_label(sel_ws))} vs {E(week_label(prev_ws))}</h2><div class="cols"><div>'
             + cmp_table('By bot', bots, lambda k: cur['bot'][k], lambda k: prv['bot'][k]) + '</div><div>'
             + cmp_table('Routine vs user-asked', list(BUCKETS), lambda k: cur['bucket'][k], lambda k: prv['bucket'][k]) + '</div></div>')
    # top tasks of the week
    def task_row(i, k):
        r = k['root']; when = pt(k['start'])
        end = max((t['end'] or t['start']) for t in k['turns'])
        sub = ', '.join(f'{v}× {s}' for s, v in k['sub_types'].items())
        return (f'<tr><td class="n">{i}</td><td>{when.strftime("%Y-%m-%d %H:%M")}</td><td>{E(name(r["agent"]))}</td><td>{E(r["category"] or "")}</td>'
                f'<td title="{E(r["prompt"] or "")}">{E(trunc(r["prompt"]))}</td><td class="n">{fmt(k["events"])}</td><td class="n">{k["mcp"]}</td><td class="n">{k["shell"]}</td>'
                f'<td class="n">{k["nav"]}</td><td class="n">{fmt(k["cu_ms"]/60000,1)}</td><td class="n">{fmt(k["content_bytes"]/1024)}</td>'
                f'<td class="n">{k["subturns"]}{(" (" + E(sub) + ")") if sub else ""}</td><td class="n">{fmt((end - k["start"])/60000,1)}</td></tr>')
    hdr = (f'<tr><th class="n">#</th><th>Started ({tzlabel})</th><th>Agent</th><th>Category</th><th>Starting prompt (≤120 chars)</th><th class="n">Activity</th>'
           '<th class="n">MCP</th><th class="n">Shell</th><th class="n">Browser nav</th><th class="n">CU min</th><th class="n">Conv KB</th><th class="n">Subagent turns</th><th class="n">Wall min</th></tr>')
    H.append(f'<h3>Top tasks, week of {E(week_label(sel_ws))}</h3>')
    H.append(('<table class="sortable">' + hdr + ''.join(task_row(i + 1, k) for i, k in enumerate(week_tasks[:10])) + '</table>')
             if week_tasks else '<p><i>No tasks recorded locally in this week.</i></p>')
    # data freshness
    H.append('<h2>Data freshness</h2><table><tr><th>Agent</th><th>Newest local record</th><th class="n">Days before report</th></tr>')
    for aid, a in sorted(info.items(), key=lambda x: -(x[1]['last_ms'] or 0)):
        lm = a['last_ms']
        H.append(f'<tr><td>{E(a["name"])}</td><td>{pt(lm).strftime(f"%Y-%m-%d %H:%M {tzlabel}") if lm else "no records"}</td>'
                 f'<td class="n">{fmt((now_ms - lm) / 86400000, 1) if lm else "—"}</td></tr>')
    H.append('</table><p class="muted">If every agent stops on the same date, the agents most likely moved to server-side storage and the box only keeps an old local snapshot. See the README section “Why local history can stop”.</p>')

    # ================= detailed views (from the original report) =================
    H.append('<h2>Detailed views</h2>')
    H.append(f'''<div class="kpis">
<div class="kpi"><b>{fmt(sum(t["events"] for t in T))}</b>activity count (all{' since ' + E(args.since) if args.since else ''})</div>
<div class="kpi"><b>{fmt(len(T))}</b>turns (incl. subagent/background)</div>
<div class="kpi"><b>{fmt(sum(t["content_bytes"] for t in T)/1024/1024,1)} MB</b>stored conversation content</div>
<div class="kpi"><b>{fmt(sum(t["cu_ms"] for t in T)/60000,1)} min</b>computer-use sessions</div>
<div class="kpi"><b>{fmt(100*user_act/tot_act,0)}% / {fmt(100*rout_act/tot_act,0)}% / {fmt(100*bg_act/tot_act,0)}%</b>user-asked / routine / other background</div>
</div>''')
    H.append('<h3>Daily usage, stacked by agent</h3><div class="tabs">')
    for i, (mk, (title, unit, fn)) in enumerate(metrics.items()):
        H.append(f'<button data-m="{mk}" class="{"on" if i == 0 else ""}">{E(title.split(" (")[0])}</button>')
    H.append('</div>' + legend({name(a): acolors[name(a)] for a in agents_present}))
    for i, mk in enumerate(metrics):
        H.append(f'<div class="chart" id="c_{mk}" style="display:{"block" if i == 0 else "none"}"><div class="muted">{E(metrics[mk][0])}</div>{daily_svgs[mk]}</div>')
    H.append('<h3>Per agent</h3><table><tr><th>Agent</th><th class="n">Turns</th><th class="n">Activity</th><th class="n">% of activity</th><th class="n">Conversation KB</th><th class="n">Computer-use min</th><th class="n">Current context fill (tokens, snapshot)</th></tr>')
    for a in agents_present:
        n = name(a); ctx = info[a]['ctx']
        H.append(f'<tr><td>{E(n)}</td><td class="n">{agent_turns[n]}</td><td class="n">{fmt(agent_tot[n])}</td><td class="n">{fmt(100*agent_tot[n]/tot_act,1)}%</td><td class="n">{fmt(agent_kb[n])}</td><td class="n">{fmt(agent_cu[n],1)}</td><td class="n">{(fmt(ctx[0] or 0) + " / " + fmt(ctx[1] or 0)) if ctx else "—"}</td></tr>')
    idle = [info[a]['name'] for a in info if a not in agents_present]
    H.append('</table>' + (f'<p class="muted">No recorded activity in range: {E(", ".join(idle))}.</p>' if idle else '')
             + '<p class="muted">“Current context fill” is the only real token figure stored: the size of each agent\'s live context window at snapshot time. It is not cumulative usage.</p>')
    H.append('<h3>Routine / background vs user-asked (daily)</h3>' + legend(CAT_COLORS))
    H.append(stacked_svg(days, cat_daily, CAT_COLORS, 'Activity by category', 'activity', height=260))
    H.append('<table><tr><th>Category</th><th class="n">Turns</th><th class="n">Activity</th><th class="n">% activity</th><th class="n">Conversation KB</th><th class="n">% KB</th><th class="n">Computer-use min</th></tr>')
    for c, v in cats.items():
        if not v['turns']: continue
        H.append(f'<tr><td><i style="display:inline-block;width:10px;height:10px;background:{CAT_COLORS.get(c,"#999")}"></i> {E(str(c))}</td><td class="n">{v["turns"]}</td><td class="n">{fmt(v["activity"])}</td><td class="n">{fmt(100*v["activity"]/tot_act,1)}%</td><td class="n">{fmt(v["kb"])}</td><td class="n">{fmt(100*v["kb"]/tot_kb,1)}%</td><td class="n">{fmt(v["cu"],1)}</td></tr>')
    H.append('</table><p class="muted">User-asked = turns started by a user chat message or form answer, plus the subagents those turns spawned. Routine = scheduled runs (matched to <code>runs.json</code> or inferred, see notes) and routine wake-ups. Background follow-up = the agent being woken when a background task finished. Agent-to-agent = a message from another bot. System/housekeeping = first-run, instruction/profile updates, unanswered-prompt nudges. “Other bg” in the weekly tables = these last four combined.</p>')
    if routine_names:
        H.append('<h3>Routines</h3><table><tr><th>Agent · routine</th><th class="n">Turns</th><th class="n">Activity</th></tr>' +
                 ''.join(f'<tr><td>{E(k)}</td><td class="n">{routine_turns[k]}</td><td class="n">{fmt(v)}</td></tr>' for k, v in routine_names.most_common()) + '</table>')
    H.append('<h3>Top 20 heaviest tasks (all weeks)</h3><p class="muted">A task = one conversation turn plus the subagent turns it spawned. Ranked by activity count. Click a column header to sort. Hover a prompt for the full text.</p>')
    H.append('<table class="sortable">' + hdr + ''.join(task_row(i + 1, k) for i, k in enumerate(task_list[:20])) + '</table>')
    H.append('<h3>Connectors and tool families</h3>')
    H.append(hbar_svg(conn_ct.most_common(20), lambda k: '#59a14f' if k.startswith('user-') else ('#e15759' if 'rowser' in k or 'omputer' in k or 'Screenshot' in k else '#4e79a7'), 'calls'))
    H.append('<h3>Top 25 individual tools</h3>' + hbar_svg(tool_ct.most_common(25), lambda k: '#59a14f' if k.startswith('user-') else '#4e79a7', 'calls'))
    H.append('<p class="muted">Green = MCP connectors. Red = browser/computer-use (usually the most expensive per step: each action round-trips screenshots). Blue = box shell and built-in tools. Built-in WebSearch/WebFetch/Read/Screenshot/Task counts exist only for main-conversation turns; subagent transcripts are not stored in the per-agent databases.</p>')
    H.append('<h3>Possible duplicate or wasted work</h3>')
    if auto_rows:
        H.append('<h4>Routines found</h4><table><tr><th>Agent</th><th>Routine</th><th>Schedule (cron)</th><th>Enabled</th><th class="n">Runs recorded</th><th>Run starts</th></tr>' +
                 ''.join(f'<tr><td>{E(str(a))}</td><td>{E(str(b))}</td><td>{E(str(c))}</td><td>{E(str(d))}</td><td class="n">{e}</td><td>{E(f)}</td></tr>' for a, b, c, d, e, f in auto_rows) + '</table>')
    if bursts:
        H.append('<h4>Days with many subagent runs (≥4)</h4><table><tr><th>Day</th><th>Agent</th><th class="n">Subagent turns</th><th>Types</th><th class="n">Distinct parent turns</th><th class="n">Activity</th><th class="n">CU min</th></tr>' +
                 ''.join(f'<tr><td>{E(d)}</td><td>{E(a)}</td><td class="n">{v["n"]}</td><td>{E(", ".join(f"{c}× {s}" for s, c in v["types"].most_common()))}</td><td class="n">{len(v["parents"])}</td><td class="n">{v["act"]}</td><td class="n">{fmt(v["cu"],1)}</td></tr>' for (d, a), v in bursts) +
                 '</table><p class="muted">Many subagents of the same type on one day usually means retries of the same job. Check the top-tasks table for the prompts.</p>')
    if dup_prompts:
        H.append('<h4>Prompts that triggered many requests</h4><table><tr><th>Agent</th><th>First</th><th>Prompt</th><th class="n">Requests</th><th>Request sources</th></tr>' +
                 ''.join(f'<tr><td>{E(a)}</td><td>{pt(at).strftime("%Y-%m-%d %H:%M")}</td><td>{E(trunc(p))}</td><td class="n">{n}</td><td>{E(", ".join(s))}</td></tr>' for a, p, n, s, at in dup_prompts[:15]) +
                 '</table><p class="muted">From each agent\'s recent request log (store.db kv <code>requestIds</code>). Sources like web-search/web-fetch/background-revival are follow-up requests inside the same task, not necessarily duplicates.</p>')
    H.append('<h3>All tasks</h3><details><summary>Show all ' + str(len(task_list)) + ' tasks (sortable)</summary><table class="sortable">' + hdr +
             ''.join(task_row(i + 1, k) for i, k in enumerate(task_list)) + '</table></details>')
    nconv = sum(1 for t in T if t['source'] == 'conversation'); naud = len(T) - nconv
    H.append(f'''<h2>How this is measured</h2>
<ul>
<li><b>Not tokens, not dollars.</b> The local data has no per-request token or cost counts. The one real token number is each agent's current context-window fill, a snapshot shown in the per-agent table.</li>
<li><b>Activity count</b> (headline metric) = every event in <code>audit.jsonl</code> (shell commands, MCP connector calls, browser navigations, computer-use sessions) + Read / WebSearch / WebFetch / Screenshot / Task steps found in stored main-conversation transcripts. Each counts as 1 regardless of size, so it understates long browser sessions; use the computer-use minutes for those.</li>
<li><b>Weeks</b> run Monday 00:00 to Sunday 24:00 in {E(args.tz)}. A task belongs to the week its first turn started in. “Last 7 days” is a rolling window ending at report time (or <code>--as-of</code>).</li>
<li><b>Conversation KB</b> = bytes of stored conversation steps per turn from <code>conversation-blobs.db</code>. Only each agent's main conversation is stored; subagent and routine runs have audit events but no stored content, so their KB is 0.</li>
<li><b>Turns</b>: {nconv} main-conversation turns (with stored prompt) and {naud} audit-only turns (subagents, routine runs, background work). Audit-only turns are linked to the turn that spawned them when a subagent spawn was recorded ≤120 s before (inferred), to a routine via <code>automations/*/runs.json</code> requestId or run window, or flagged “unattributed”.</li>
<li><b>Category</b> of a main turn comes from how its starting prompt is marked in storage (user message, form answer, background-task completion, agent message, routine status, system notice).</li>
<li>Source data is read read-only; SQLite databases are copied to a temporary folder before opening and the copies are deleted afterwards.</li>
</ul>
<script>
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{{document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
document.querySelectorAll('.chart').forEach(c=>c.style.display='none');document.getElementById('c_'+b.dataset.m).style.display='block';}});
document.querySelectorAll('table.sortable').forEach(tb=>{{tb.querySelectorAll('th').forEach((th,i)=>th.onclick=()=>{{
const rows=[...tb.querySelectorAll('tr')].slice(1);const asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
const v=r=>{{const t=r.children[i].innerText.replace(/,/g,'');const n=parseFloat(t);return isNaN(n)?t:n;}};
rows.sort((a,b)=>{{const x=v(a),y=v(b);return (x>y?1:x<y?-1:0)*(asc?1:-1);}});rows.forEach(r=>tb.appendChild(r));}});}});
</script></body></html>''')
    out_html = os.path.join(out_dir, 'usage-report.html')
    with open(out_html, 'w', encoding='utf8') as fh: fh.write(''.join(H))

    # ---- stdout: the 5-second version
    print(summary)
    print()
    print(f"{'Week (Mon–Sun)':22s} {'Activity':>9s} {'Δ':>8s} {'Δ %':>6s} {'User':>7s} {'Routine':>8s} {'Other':>7s} {'Tasks':>6s}")
    for w in weeks:
        cw, pw = weekly[w], weekly[w - dt.timedelta(days=7)]
        a, p = delta(cw['act'], pw['act'])
        print(f"{('> ' if w == sel_ws else '  ') + week_label(w):22s} {fmt(cw['act']):>9s} {a:>8s} {p:>6s} "
              f"{fmt(cw['bucket']['User-asked']):>7s} {fmt(cw['bucket']['Routine / automation']):>8s} {fmt(cw['bucket']['Other background']):>7s} {cw['tasks']:>6d}")
    print()
    print('wrote', out_html)

    if args.png:
        chrome = shutil.which('google-chrome') or shutil.which('chromium') or shutil.which('chromium-browser')
        if chrome:
            png = os.path.join(out_dir, 'usage-report.png')
            try:
                subprocess.run([chrome, '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
                                f'--screenshot={png}', '--window-size=1200,1600', 'file://' + out_html],
                               check=True, timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print('wrote', png)
            except Exception as ex:
                print('PNG skipped:', ex)
        else:
            print('PNG skipped: no chrome/chromium found')

if __name__ == '__main__':
    main()
