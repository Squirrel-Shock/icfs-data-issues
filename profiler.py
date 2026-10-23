"""Profile the harvested ICFS filings: what is filled, what values occur, which dates are
implausible, and where fields disagree.  Read-only over data/icfs.db (see harvest.py).

    python profiler.py [--db data/icfs.db] [--out reports/profile.md]

The report is a survey, not a verdict: every count says how often something LOOKS wrong,
with example file numbers so a person can check.  Stdlib only.
"""
import argparse
import collections
import datetime
import json
import os
import re
import sqlite3
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))


def snapshot_date(db=os.path.join(HERE, 'data', 'icfs.db')):
    """The harvest date stored in data/icfs.db, so date rules do not drift with the run date; else today."""
    try:
        conn = sqlite3.connect('file:%s?mode=ro' % db, uri=True)
        (v,) = conn.execute("SELECT value FROM meta WHERE key = 'harvested_at'").fetchone()
        conn.close()
        return datetime.date.fromisoformat(v[:10])
    except Exception:
        return datetime.date.today()


TODAY = snapshot_date()
EARLIEST = datetime.date(1960, 1, 1)
FILE_NUMBER = re.compile(r'^([A-Z0-9]+)-([A-Z0-9/]+)-(\d{8})-(\d+)$')
DATE_FIELDS = ('date_filed', 'grant_date', 'term_begin', 'term_end', 'last_action_date',
               'accepted_pn_date', 'action_pn_date')
CLOSED = ('closed', 'dismissed', 'withdrawn')
EXAMPLES = 5
DISTINCT_CAP = 60


def parse_date(v):
    if not isinstance(v, str) or len(v) < 10:
        return None
    try:
        return datetime.date.fromisoformat(v[:10])
    except ValueError:
        return None


def mask(v):
    return re.sub(r'\d', '9', re.sub(r'[A-Za-z]', 'A', v))


class Ex(dict):
    """Counter that also keeps a few example file numbers per key."""
    def __init__(self):
        super().__init__()
        self.n = collections.Counter()

    def add(self, key, fn):
        self.n[key] += 1
        examples = self.setdefault(key, [])
        if len(examples) < EXAMPLES:
            examples.append(fn)


def records(conn):
    for (raw,) in conn.execute('SELECT raw FROM filings'):
        yield json.loads(zlib.decompress(raw))


def blank(v):
    return v is None or v == ''


def scan(conn):
    s = {
        'total': collections.Counter(),
        'type': collections.Counter(), 'state': collections.Counter(), 'action': collections.Counter(),
        'fields': collections.defaultdict(lambda: collections.defaultdict(collections.Counter)),
        'lists': collections.defaultdict(collections.Counter),
        'dates': collections.defaultdict(Ex), 'date_values': collections.defaultdict(collections.Counter),
        'fn': Ex(), 'order': Ex(), 'cs_mask': collections.defaultdict(collections.Counter),
        'grant_no_term': collections.Counter(), 'current_old': Ex(),
        'by_cs': collections.defaultdict(list),
    }
    for r in records(conn):
        sub = r.get('subsystem') or '?'
        fn = r.get('file_number') or '?'
        state = r.get('state') or ''
        action = r.get('last_action') or ''
        s['total'][sub] += 1
        s['type'][(sub, r.get('filing_type') or '')] += 1
        s['state'][(sub, state)] += 1
        s['action'][(sub, action)] += 1
        for k, v in r.items():
            if isinstance(v, list):
                s['lists'][(sub, k)]['present'] += 1
                s['lists'][(sub, k)]['non_empty'] += bool(v)
            elif not isinstance(v, dict):
                c = s['fields'][sub][k]
                if blank(v):
                    continue
                c['__filled__'] += 1
                key = str(v)[:60]
                if key in c or len(c) < DISTINCT_CAP:
                    c[key] += 1
                else:
                    c['__many__'] += 1

        d = {k: parse_date(r.get(k)) for k in DATE_FIELDS}
        for k in DATE_FIELDS:
            v = r.get(k)
            if blank(v):
                continue
            limit = TODAY + datetime.timedelta(days=366 * (30 if k == 'term_end' else 1))
            if d[k] is None:
                s['dates'][sub].add('%s unparseable' % k, fn)
                s['date_values'][k][str(v)[:19]] += 1
            elif d[k] < EARLIEST:
                s['dates'][sub].add('%s before 1960' % k, fn)
                s['date_values'][k][str(v)[:10]] += 1
            elif d[k] > limit:
                s['dates'][sub].add('%s too far in future' % k, fn)
                s['date_values'][k][str(v)[:10]] += 1

        m = FILE_NUMBER.match(fn)
        fn_date = None
        if not m:
            s['fn'].add('%s %s' % (sub, mask(fn)), fn)
        else:
            if m.group(1) != sub:
                s['fn'].add('prefix %s in subsystem %s' % (m.group(1), sub), fn)
            try:
                ymd = m.group(3)
                fn_date = datetime.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
            except ValueError:
                s['fn'].add('%s impossible date in file number' % sub, fn)

        o = s['order']
        day = datetime.timedelta(days=1)
        if fn_date and d['date_filed'] and abs((d['date_filed'] - fn_date).days) > 3:
            o.add('%s date_filed differs from file-number date by >3 days' % sub, fn)
        filed = d['date_filed'] if d['date_filed'] and EARLIEST <= d['date_filed'] <= TODAY else fn_date
        if filed and d['grant_date'] and d['grant_date'] < filed - day:
            o.add('%s grant_date before filing' % sub, fn)
        if d['term_begin'] and d['term_end'] and d['term_end'] < d['term_begin']:
            o.add('%s term_end before term_begin' % sub, fn)
        if d['grant_date'] and d['term_end'] and d['term_end'] < d['grant_date']:
            o.add('%s term_end before grant_date' % sub, fn)
        if d['last_action_date'] and filed and d['last_action_date'] < filed - day:
            o.add('%s last_action_date before filing' % sub, fn)
        if d['grant_date']:
            o.add('%s has grant_date; last_action=%s state=%s' % (sub, action or '(blank)', state or '(blank)'), fn)

        if d['grant_date'] and blank(r.get('term_end')):
            s['grant_no_term'][(sub, r.get('filing_type') or '', d['grant_date'].year // 5 * 5)] += 1
        start = d['term_begin'] or d['grant_date'] or filed
        if state.lower() not in CLOSED and start and start.year < TODAY.year - 20 \
                and not (d['term_end'] and d['term_end'] >= TODAY):
            s['current_old'].add('%s state=%s last_action=%s' % (sub, state or '(blank)', action or '(blank)'), fn)

        cs = r.get('call_sign') or ''
        s['cs_mask'][sub][mask(cs) if cs else '(blank)'] += 1
        if cs:
            s['by_cs'][(sub, cs)].append({
                'date': fn_date or filed, 'fn': fn, 'type': r.get('filing_type') or '', 'state': state,
                'action': action, 'frn': r.get('applicant_frn') or '',
                'name': (r.get('applicant_name') or '').strip().upper(), 'term_end': d['term_end']})
    return s


def chains(s):
    out = {'len': collections.Counter(), 'frn': Ex(), 'name': Ex(), 'after_end': Ex(), 'first': Ex(),
           'live_expired': Ex()}
    for (sub, cs), rows in s['by_cs'].items():
        rows.sort(key=lambda x: (x['date'] or datetime.date.min, x['fn']))
        n = len(rows)
        out['len'][(sub, '1' if n == 1 else '2-5' if n <= 5 else '6-20' if n <= 20 else '>20')] += 1
        if len({x['frn'] for x in rows if x['frn']}) > 1:
            out['frn'].add(sub, cs)
        if len({x['name'] for x in rows if x['name']}) > 1:
            out['name'].add(sub, cs)
        # the first filing type per call sign: shows which histories start mid-stream
        out['first'].add('%s %s' % (sub, rows[0]['type'] or '(blank)'), cs)
        cancel = next((i for i, x in enumerate(rows)
                       if 'cancel' in x['type'].lower() or 'surrender' in x['type'].lower()), None)
        if cancel is not None and cancel < n - 1:
            out['after_end'].add('%s filings after a cancel/surrender filing' % sub, cs)
        ends = [x['term_end'] for x in rows if x['term_end']]
        if ends and max(ends) < TODAY and any(x['state'].lower() not in CLOSED for x in rows):
            out['live_expired'].add(sub, cs)
    return out


def table(rows, head):
    lines = ['| ' + ' | '.join(head) + ' |', '|' + '---|' * len(head)]
    lines += ['| ' + ' | '.join(str(c).replace('|', '/') for c in r) + ' |' for r in rows]
    return '\n'.join(lines)


def ex_table(ex, head='what', top=40):
    return table([(k, n, ', '.join(ex[k])) for k, n in ex.n.most_common(top)], (head, 'count', 'examples'))


def report(conn, s, c):
    meta = dict(conn.execute('SELECT key, value FROM meta'))
    tot = dict(conn.execute('SELECT subsystem, total_records FROM totals'))
    md = ['# ICFS data profile', '',
          'Harvested %s from the FCC ICFS Public API; profiled %s.  API total_records %s; stored %d.' % (
              meta.get('harvested_at'), TODAY, meta.get('api_total_records'), sum(s['total'].values())),
          '', 'Contact keys stripped at harvest: %s' % (meta.get('dropped_keys') or '[]'), '',
          '## 1. Filings per subsystem', '',
          table([(k, v, tot.get(k, '')) for k, v in s['total'].most_common()], ('subsystem', 'stored', 'API total')),
          '', '## 2. Filing type, state, last action', '']
    for key, title in (('type', 'filing_type'), ('state', 'state'), ('action', 'last_action')):
        md += ['### %s' % title, '',
               table([(a, b or '(blank)', n) for (a, b), n in sorted(s[key].items())], ('subsystem', title, 'count')),
               '']
    md += ['## 3. Scalar field fill rates', '',
           'Fill = share of filings with a non-blank value.  Values = up to 6 most common; "(more)" means the'
           ' field has over %d distinct values.' % DISTINCT_CAP, '']
    for sub in sorted(s['fields']):
        rows = []
        for k, c2 in sorted(s['fields'][sub].items()):
            vals = [(v, n) for v, n in c2.most_common() if not v.startswith('__')]
            more = ' (more)' if c2['__many__'] else ''
            rows.append((k, '%.0f%%' % (100.0 * c2['__filled__'] / s['total'][sub]),
                         '; '.join('%s x%d' % (v, n) for v, n in vals[:6]) + more))
        md += ['### %s (%d filings)' % (sub, s['total'][sub]), '', table(rows, ('field', 'fill', 'values')), '']
    md += ['## 4. Nested sections present / non-empty', '',
           table([(a, b, c2['present'], c2['non_empty']) for (a, b), c2 in sorted(s['lists'].items())],
                 ('subsystem', 'section', 'present', 'non-empty')),
           '', '## 5. Implausible dates', '']
    for sub in sorted(s['dates']):
        md += ['### %s' % sub, '', ex_table(s['dates'][sub]), '']
    md += ['### Most common out-of-range values', '']
    md += ['- %s: %s' % (k, ', '.join('%s x%d' % x for x in c2.most_common(8)))
           for k, c2 in sorted(s['date_values'].items())]
    md += ['', '## 6. File numbers that break the SUB-TYPE-YYYYMMDD-NNNNN pattern', '', ex_table(s['fn'], 'pattern'),
           '', '## 7. Within-filing consistency', '',
           'Includes a census of last_action/state for every filing with a grant_date, so odd pairs stand out.', '',
           ex_table(s['order'], 'check', 80),
           '', '## 8. Granted with no term_end (subsystem, filing type, grant 5-year bucket)', '',
           table([(a, b, y, n) for (a, b, y), n in sorted(s['grant_no_term'].items())],
                 ('subsystem', 'filing_type', 'granted from', 'count')),
           '', '## 9. Not closed, started more than 20 years ago, no term_end in the future', '',
           ex_table(s['current_old'], 'state', 60),
           '', '## 10. Call signs', '', '### Shape (letters A, digits 9)', '']
    md += ['- %s: %s' % (sub, ', '.join('%s x%d' % x for x in s['cs_mask'][sub].most_common(8)))
           for sub in sorted(s['cs_mask'])]
    md += ['', '### Filings per call sign', '',
           table([(a, b, n) for (a, b), n in sorted(c['len'].items())], ('subsystem', 'filings', 'call signs')),
           '', '### Call-sign history checks', '',
           table([('more than one applicant FRN', k, c['frn'].n[k], ', '.join(c['frn'][k])) for k in sorted(c['frn'].n)]
                 + [('more than one applicant name', k, c['name'].n[k], ', '.join(c['name'][k]))
                    for k in sorted(c['name'].n)]
                 + [('latest term_end is past but some filing is not closed', k, c['live_expired'].n[k],
                     ', '.join(c['live_expired'][k])) for k in sorted(c['live_expired'].n)],
                 ('check', 'subsystem', 'call signs', 'examples')),
           '', ex_table(c['after_end'], 'check'),
           '', '### First filing type in each call-sign history', '', ex_table(c['first'], 'first filing', 60), '']
    return '\n'.join(md) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=os.path.join(HERE, 'data', 'icfs.db'))
    ap.add_argument('--out', default=os.path.join(HERE, 'reports', 'profile.md'))
    a = ap.parse_args()
    conn = sqlite3.connect('file:%s?mode=ro' % a.db, uri=True)
    s = scan(conn)
    text = report(conn, s, chains(s))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w', encoding='utf-8') as f:
        f.write(text)
    print('wrote %s (%d filings)' % (a.out, sum(s['total'].values())))


if __name__ == '__main__':
    main()
