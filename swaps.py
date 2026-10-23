"""Find earth-station Schedule B data that was swapped or copied between call signs.

    python swaps.py [--db data/icfs.db] [--out reports/swaps.csv]

Each SES call sign's granted technical versions (walk.version, ordered by walk.when) are walked;
a step prev -> cur is suspect when cur differs radically from prev (walk.jumps non-empty, less
than half of prev's spectrum kept, or the site moved) AND cur is not the same station as prev.
"Same station" (~) = merged spectrum equal within 0.01 MHz and every site within 1 km of a site
of the other version.  For each suspect step of call sign A:
  mutual_swap     another call sign B has a step B.prev -> B.cur with A.cur ~ B.prev and
                  B.cur ~ A.prev, B's step radical too, granted within SWAP_DAYS of A.cur (the two descriptions traded)
  one_way_copy    no swap, but A.cur ~ the version B held at A.cur's grant date, and B did not
                  arrive there from A.prev (co-located stations moved together are not copies);
                  expected_value is blank: a real relocation onto a shared teleport, a corrected
                  typo and a copy look the same, so the filing has to be checked
The copy's source filing is not reported (verification: it was correct in every case); the
one_way_copy detail names it.  mutual_swap: high when the two filings are adjacent or filed the
same day AND max EIRP / dish traded too, else medium; same-day grants inside one chain are a
joint move (low, "likely benign").  one_way_copy: high/medium when the coordinates match the
partner's but the city differs, medium for a Renewal that changed city, low "likely benign"
when the previous and new filings name the same city (a local move or a corrected typo).  One row per flagged filing
and rule; the best partner (adjacent, same date, same applicant, nearest sequence, nearest
grant) is named and every other qualifying call sign is listed in detail.  Stdlib only.
"""
import argparse
import collections
import csv
import datetime
import os
import sqlite3

from freqs import TECHNICAL, filing_date
from profiler import FILE_NUMBER, HERE, records
from walk import SITE_MATCH_KM, jumps, kept, km, version, when

COPY_KEEP = 0.5
ADJACENT_SEQ = 10
SWAP_DAYS = 365
CELL = 20  # site grid cells per degree (~5 km), for the fingerprint index
COLUMNS = ['issue_class', 'rule', 'confidence', 'subsystem', 'call_sign', 'file_number', 'filing_type', 'state',
           'before_file', 'before_value', 'after_file', 'after_value', 'expected_value', 'detail']


def spec_key(v):
    # ponytail: rounding to 0.01 MHz can split two edges that straddle a rounding boundary; rare enough
    return tuple((round(lo, 2), round(hi, 2)) for lo, hi in v['spectrum'])


def same_sites(a, b):
    return bool(a and b) and all(min(km(s, t) for t in b) <= SITE_MATCH_KM for s in a) \
        and all(min(km(s, t) for t in a) <= SITE_MATCH_KM for s in b)


def same(a, b):
    return spec_key(a) == spec_key(b) and same_sites(a['sites'], b['sites'])


def radical(a, b):
    """Why b is a radical departure from a, or '' when it is not."""
    why = [k for k in jumps(a, b)]
    if a['spectrum'] and b['spectrum'] and kept(a['spectrum'], b['spectrum']) < COPY_KEEP and 'spectrum_replaced' not in why:
        why.append('spectrum_kept_%.0f%%' % (100 * kept(a['spectrum'], b['spectrum'])))
    return ', '.join(why) if why and not same(a, b) else ''


BENIGN = 'likely benign: '


def params(v):
    return (v.get('eirp'), v.get('dish'))


def cities(v):
    return {s['city'] for s in v.get('raw_sites', []) if s.get('city')}


def describe(v):
    sites = sorted({(round(lat, 4), round(lon, 4)) for lat, lon in v['sites']})
    return '%s MHz @ %s' % (', '.join('%g-%g' % (lo, hi) for lo, hi in v['spectrum']) or 'no spectrum',
                            '; '.join('%.4f,%.4f' % s for s in sites) or 'no site')


def cells(v):
    return {(int(lat * CELL // 1), int(lon * CELL // 1)) for lat, lon in v['sites']}


def near_cells(v):
    return {(a + i, b + j) for a, b in cells(v) for i in (-1, 0, 1) for j in (-1, 0, 1)}


def seq(fn):
    m = FILE_NUMBER.match(fn or '')
    return (m.group(3), int(m.group(4))) if m else (None, None)


def relation(a, b):
    """Score how closely filing b was filed to filing a (higher sorts first)."""
    (da, sa), (db, sb) = seq(a['file']), seq(b['file'])
    adjacent = da is not None and da == db and abs(sa - sb) <= ADJACENT_SEQ
    same_day = a['filed'] is not None and a['filed'] == b['filed']
    same_app = (bool(a['frn']) and a['frn'] == b['frn']) or (
        not (a['frn'] and b['frn']) and bool(a.get('name')) and a.get('name') == b.get('name'))
    gap = abs(sa - sb) if adjacent else 10 ** 9
    return (adjacent, same_day, same_app, -gap, -abs((a['when'] - b['when']).days)), \
        '%s%s%s' % ('adjacent file numbers (seq %+d); ' % (sb - sa) if adjacent else '',
                    'filed same day; ' if same_day else '', 'same applicant' if same_app else 'different applicant')


def find(history):
    """history: {call_sign: [entry sorted by (when, file)]}, entry = dict(file, type, state, frn, filed, when, v).
    Returns CSV rows (lists)."""
    index = collections.defaultdict(list)  # (spec_key, cell) -> [(call_sign, i)]
    for cs, hist in history.items():
        for i, e in enumerate(hist):
            if e['v']['sites']:
                for c in cells(e['v']):
                    index[(spec_key(e['v']), c)].append((cs, i))

    def candidates(v):
        k = spec_key(v)
        return {hit for c in near_cells(v) for hit in index.get((k, c), ())}

    rows, seen = [], set()

    def emit(rule, conf, cs, prev, cur, expected, detail):
        if (rule, cur['file']) in seen:
            return
        seen.add((rule, cur['file']))
        cls = 'Schedule B swapped between call signs' if rule == 'mutual_swap' else 'Schedule B copied from another call sign'
        rows.append([cls, rule, conf, 'SES', cs, cur['file'], cur['type'], cur['state'],
                     prev['file'] if prev else '', describe(prev['v']) if prev else '',
                     cur['file'], describe(cur['v']), expected, detail])

    for a_cs, hist in history.items():
        for i in range(1, len(hist)):
            prev, cur = hist[i - 1], hist[i]
            why = radical(prev['v'], cur['v'])
            if not why or not cur['v']['sites'] or not prev['v']['sites']:
                continue
            swaps, copies = [], []
            for b_cs, j in candidates(cur['v']):
                if b_cs == a_cs:
                    continue
                b_hist = history[b_cs]
                b = b_hist[j]
                if not same(b['v'], cur['v']):
                    continue
                if j + 1 < len(b_hist) and same(b_hist[j + 1]['v'], prev['v']) and radical(b['v'], b_hist[j + 1]['v']) \
                        and abs((b_hist[j + 1]['when'] - cur['when']).days) <= SWAP_DAYS:
                    swaps.append((relation(cur, b_hist[j + 1]), b_cs, b, b_hist[j + 1]))
                # the version B held when A.cur was granted
                held = [k for k, e in enumerate(b_hist) if e['when'] <= cur['when']]
                # skip joint changes: B came from the same description A came from (co-located stations moved together)
                if held and held[-1] == j and not (j and same(b_hist[j - 1]['v'], prev['v'])):
                    copies.append((relation(cur, b), b_cs, b))
            if swaps:
                swaps.sort(key=lambda s: s[0][0], reverse=True)
                (score, how), b_cs, b_prev, b_cur = swaps[0]
                others = sorted({s[1] for s in swaps[1:]} - {b_cs})
                detail = ("traded with %s: its %s (%s %s) now holds this call sign's previous description, and this filing "
                          'holds its previous one (%s); %s; %s -> %s: %s%s' % (
                              b_cs, b_cur['file'], b_cur['type'], b_cur['when'], b_prev['file'], how, prev['type'],
                              cur['type'], why, '; other qualifying partners: ' + ' '.join(others) if others else ''))
                if b_cur['when'] == b_prev['when'] or cur['when'] == prev['when']:
                    # a stale renewal granted the same day as a modification sorts after it: a joint move, not a swap
                    emit('mutual_swap', 'low', a_cs, prev, cur, '', BENIGN + 'same-day grants in one chain, order '
                         'uncertain: sister stations that moved together; ' + detail)
                else:
                    exact = params(cur['v']) == params(b_prev['v']) and params(b_cur['v']) == params(prev['v'])
                    conf = 'high' if (score[0] or score[1]) and exact else 'medium'
                    emit('mutual_swap', conf, a_cs, prev, cur, describe(b_cur['v']), detail + (
                        '; max EIRP and dish also traded' if exact else '; max EIRP or dish differ'))
            elif copies:
                copies.sort(key=lambda s: s[0][0], reverse=True)
                (score, how), b_cs, b = copies[0]
                others = sorted({s[1] for s in copies[1:]} - {b_cs})
                exact = params(cur['v']) == params(b['v'])
                cc, bc, pc = cities(cur['v']), cities(b['v']), cities(prev['v'])
                detail = ('matches %s %s (%s, granted %s), which %s held at this grant; %s; %s -> %s: %s; %s; '
                          'cities: previous %s, this %s, partner %s%s' % (
                              b_cs, b['file'], b['type'], b['when'], b_cs, how, prev['type'], cur['type'], why,
                              'max EIRP and dish also identical' if exact else 'max EIRP or dish differ',
                              '/'.join(sorted(pc)) or '?', '/'.join(sorted(cc)) or '?', '/'.join(sorted(bc)) or '?',
                              '; %d other call signs hold the same description: %s' % (len(others), ' '.join(others))
                              if others else ''))
                if cc and bc and not cc & bc:
                    emit('one_way_copy', 'high' if score[0] else 'medium', a_cs, prev, cur, describe(prev['v']),
                         "coordinates match %s's site but the city name differs: coordinates copied under another city; "
                         % b_cs + detail)
                elif cur['type'] == 'Renewal' and pc and cc and not pc & cc:
                    emit('one_way_copy', 'medium', a_cs, prev, cur, describe(prev['v']),
                         'a Renewal cannot relocate the station; ' + detail)
                elif pc and cc and pc & cc and not (exact and score[0]):
                    emit('one_way_copy', 'low', a_cs, prev, cur, '', BENIGN + 'same city before and after: a local move '
                         'or a corrected coordinate typo in the previous filing; ' + detail)
                else:
                    emit('one_way_copy', 'medium' if score[0] else 'low', a_cs, prev, cur, '',
                         'either a real move onto a shared site and band plan or a copy - check the filing; ' + detail)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=os.path.join(HERE, 'data', 'icfs.db'))
    ap.add_argument('--out', default=os.path.join(HERE, 'reports', 'swaps.csv'))
    a = ap.parse_args()
    conn = sqlite3.connect('file:%s?mode=ro' % a.db, uri=True)
    history = collections.defaultdict(list)
    for r in records(conn):
        if r.get('subsystem') == 'SES' and r.get('call_sign') and r.get('last_action') == 'GRA' \
                and r.get('filing_type') in TECHNICAL and not (r.get('state') or '').startswith('Pending'):
            v = version(r)
            if v:
                history[r['call_sign']].append({'file': r.get('file_number'), 'type': r.get('filing_type'),
                                                'state': r.get('state') or '', 'frn': r.get('applicant_frn') or '',
                                                'name': (r.get('applicant_name') or '').strip().upper(),
                                                'filed': filing_date(r), 'when': when(r), 'v': v})
    for hist in history.values():
        hist.sort(key=lambda e: (e['when'], e['file']))
    rows = find(history)
    rows.sort(key=lambda r: (r[1], {'high': 0, 'medium': 1, 'low': 2}[r[2]], r[4], r[5]))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)
    counts = collections.Counter((r[1], r[2]) for r in rows)
    print('wrote %s (%d rows)' % (a.out, len(rows)))
    for (rule, conf), n in sorted(counts.items()):
        print('  %-14s %-6s %d' % (rule, conf, n))


if __name__ == '__main__':
    main()
