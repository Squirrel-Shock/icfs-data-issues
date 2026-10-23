"""Walk each earth-station call sign's granted Schedule B history in grant order and flag radical
jumps between consecutive versions, and jumps that are undone by the next version.

    python walk.py [--db data/icfs.db] [--out reports/walk.csv]

Versions: granted (last_action GRA, not Pending) License, Modification, MFS Modification,
Registration and Renewal filings that carry Schedule B bands, ordered by grant date (a stale or
bulk-stamped grant_date is replaced by last_action_date, see freqs.grant_of), then filing date.
Between consecutive versions:
  relocated          every site moved more than SITE_MATCH_KM (no site in common)
  hemisphere_flip    a site's latitude or longitude changed sign but kept its magnitude
  spectrum_replaced  less than SPECTRUM_KEEP of the earlier spectrum survives
  eirp_jump          the highest EIRP moved more than EIRP_JUMP_DB
  dish_jump          the largest dish changed by more than DISH_RATIO times
  *_reverted         version n jumps and version n+1 jumps back (n-1 ~ n+1)
A blank hemisphere is read as N / W (US convention; sites.py reports the blank itself).
Each finding is attributed to the filing that is probably wrong: a coordinate outside its
declared state, a dish diameter that disagrees with its own gain, a band-unit typo in the earlier
filing.  When that cannot be told, both filings get a row.  Rows the verification judged benign
keep confidence low and a detail starting "likely benign: <why>".  Columns: the original nine,
then the standard repair columns.  Stdlib only.
"""
import argparse
import collections
import csv
import datetime
import math
import os
import re
import sqlite3

from freqs import TECHNICAL, bands_of, filing_date, grant_of, num, parse_date
from profiler import HERE, records

SITE_MATCH_KM = 1.0
SPECTRUM_KEEP = 0.25
EIRP_JUMP_DB = 10.0
DISH_RATIO = 3.0
TYPO_KM = 20.0
EXAMPLES = 6
BENIGN = 'likely benign: '
CLASS = {'relocated': 'Site moved between versions', 'relocated_reverted': 'Site moved between versions',
         'hemisphere_flip': 'Site moved between versions', 'spectrum_replaced': 'Spectrum replaced between versions',
         'spectrum_replaced_reverted': 'Spectrum replaced between versions', 'eirp_jump': 'EIRP or dish jump',
         'eirp_jump_reverted': 'EIRP or dish jump', 'dish_jump': 'EIRP or dish jump', 'dish_jump_reverted': 'EIRP or dish jump'}
COLUMNS = ['rule', 'call_sign', 'from_file', 'from_type', 'from_date', 'to_file', 'to_type', 'to_date', 'detail',
           'issue_class', 'confidence', 'subsystem', 'file_number', 'filing_type', 'state', 'before_file',
           'before_value', 'after_file', 'after_value', 'expected_value']
DMS = ('site_lat_deg', 'site_lat_min', 'site_lat_sec', 'site_lat_hemi',
       'site_long_deg', 'site_long_min', 'site_long_sec', 'site_long_hemi')


def degrees(d, m, s, hemi):
    d, m, s = num(d), num(m), num(s)
    if d is None:
        return None
    v = d + (m or 0) / 60.0 + (s or 0) / 3600.0
    return -v if (hemi or '').strip().upper()[:1] in ('S', 'W') else v


def km(a, b):
    p = math.pi / 180
    h = math.sin((b[0] - a[0]) * p / 2) ** 2 + math.cos(a[0] * p) * math.cos(b[0] * p) * math.sin((b[1] - a[1]) * p / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def implied_diameter(gain, ghz):
    return SPEED_OF_LIGHT / (math.pi * ghz * 1e9) * math.sqrt(10 ** (gain / 10.0) / 0.6)


SPEED_OF_LIGHT = 299792458.0


def version(r):
    """The comparable shape of one filing's Schedule B, or None without bands."""
    bands = bands_of(r)
    if not bands:
        return None
    sites, raw_sites, dish = [], [], None
    for sb in r.get('schedule_b') or []:
        for s in sb.get('site') or []:
            lat = degrees(s.get('site_lat_deg'), s.get('site_lat_min'), s.get('site_lat_sec'),
                          (s.get('site_lat_hemi') or '').strip() or 'N')
            lon = degrees(s.get('site_long_deg'), s.get('site_long_min'), s.get('site_long_sec'),
                          (s.get('site_long_hemi') or '').strip() or 'W')
            if lat is not None and lon is not None and (lat or lon):
                sites.append((lat, lon))
                raw_sites.append({'pt': (lat, lon), 'city': re.sub(r'[^A-Z]', '', (s.get('site_site_city') or '').upper()),
                                  'state': (s.get('site_site_state') or '').strip().upper(),
                                  'dms': tuple((s.get(k) or '').strip().upper() for k in DMS),
                                  'text': '%s %s, %s' % (' '.join((s.get(k) or '') for k in DMS),
                                                         (s.get('site_site_city') or '').strip(), s.get('site_site_state') or '')})
            for a in s.get('esaf') or []:
                d = num(a.get('esaf_diameter'))
                if d and d > 0 and (dish is None or d > dish['d']):
                    gains = [(num(o.get('ofb_gain')), num(o.get('ofb_gain_frequency')), num(o.get('ofb_frequency_lower')),
                              num(o.get('ofb_frequency_upper'))) for o in a.get('ofb') or []]
                    gains = [g for g in gains if g[0]]
                    g = max(gains, key=lambda x: x[0]) if gains else None
                    ghz = None
                    if g:
                        ghz = g[1] / 1000.0 if g[1] and g[1] > 500 else g[1]
                        if not ghz and g[2] and g[3]:
                            ghz = (g[2] + g[3]) / 2000.0
                    dish = {'d': d, 'gain': g[0] if g else None, 'ghz': ghz, 'major': num(a.get('esaf_diameter_major')),
                            'model': ('%s %s' % (a.get('esaf_manufacturer') or '', a.get('esaf_model') or '')).strip().upper()}
    eirps = [b['eirp'] for b in bands if b['eirp'] is not None]
    carriers = {}
    for b in bands:
        for e, v in b['carriers'].items():
            k = (round(b['key'][0]), round(b['key'][1]), b['key'][2], e)
            carriers[k] = max(carriers.get(k, v), v)
    return {'sites': sites, 'raw_sites': raw_sites,
            'spectrum': merge([(b['key'][0], b['key'][1]) for b in bands if b['key'][1] >= b['key'][0]]),
            'bands': [(b['key'][0], b['key'][1]) for b in bands], 'carriers': carriers,
            'eirp': max(eirps) if eirps else None, 'dish': dish['d'] if dish else None, 'dish_info': dish}


def merge(ranges):
    out = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def width(spec):
    return sum(hi - lo for lo, hi in spec)


def overlap(a, b):
    total, i, j = 0.0, 0, 0
    while i < len(a) and j < len(b):
        total += max(0.0, min(a[i][1], b[j][1]) - max(a[i][0], b[j][0]))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


def kept(prev, cur):
    """Share of prev's spectrum still present in cur (single-frequency bands count as 1 kHz)."""
    p = [(lo, hi if hi > lo else lo + 0.001) for lo, hi in prev]
    c = [(lo, hi if hi > lo else lo + 0.001) for lo, hi in cur]
    return overlap(p, c) / width(p) if width(p) else 1.0


def jumps(a, b):
    """{rule: detail} for radical differences from version a to version b."""
    out = {}
    if a['sites'] and b['sites']:
        nearest = [min(km(s, t) for t in a['sites']) for s in b['sites']]
        if min(nearest) > SITE_MATCH_KM:
            out['relocated'] = '%.1f km (closest pair)' % min(nearest)
        for s in b['sites']:
            for t in a['sites']:
                flipped = (abs(s[0] + t[0]) < 0.01 and abs(s[1] - t[1]) < 0.01 and abs(s[0]) > 0.01) or \
                          (abs(s[1] + t[1]) < 0.01 and abs(s[0] - t[0]) < 0.01 and abs(s[1]) > 0.01)
                if flipped:
                    out['hemisphere_flip'] = '(%.4f, %.4f) -> (%.4f, %.4f)' % (t + s)
    if a['spectrum'] and b['spectrum']:
        share = kept(a['spectrum'], b['spectrum'])
        if share < SPECTRUM_KEEP:
            out['spectrum_replaced'] = '%.0f%% kept: %s -> %s' % (100 * share, fmt_spec(a['spectrum']), fmt_spec(b['spectrum']))
    if a['eirp'] is not None and b['eirp'] is not None and abs(a['eirp'] - b['eirp']) > EIRP_JUMP_DB:
        out['eirp_jump'] = '%.1f -> %.1f dBW' % (a['eirp'], b['eirp'])
    if a['dish'] and b['dish'] and max(a['dish'], b['dish']) / min(a['dish'], b['dish']) > DISH_RATIO:
        out['dish_jump'] = '%.2f -> %.2f m' % (a['dish'], b['dish'])
    return out


def fmt_spec(spec):
    """Every range, no truncation (the workbook is a repair reference)."""
    return ', '.join('%g-%g' % (lo, hi) for lo, hi in spec)


def when(r):
    return grant_of(r) or datetime.date.min


# --- attribution helpers -------------------------------------------------------------------------

def outside_state(site):
    """True when the point lies outside its declared US state's box, None when that cannot be told."""
    from sites import BBOX, in_state  # sites imports this module
    if site['state'] not in BBOX:
        return None
    return not in_state(site['state'], *site['pt'])


def closest(a, b):
    best = None
    for s in b['raw_sites']:
        for t in a['raw_sites']:
            d = km(s['pt'], t['pt'])
            if best is None or d < best[0]:
                best = (d, t, s)
    return best


def shares_site(a, b):
    return bool(a['sites'] and b['sites']) and min(km(s, t) for s in a['sites'] for t in b['sites']) <= SITE_MATCH_KM


def typo_band(v):
    return any((lo > 0 and hi / lo > 1.5) or (hi - lo > 1000 and hi < 30000) for lo, hi in v['bands'])


def dish_fault(info):
    """Why a version's largest dish disagrees with its own data, or ''."""
    if not info:
        return ''
    d = info['d']
    if info['gain'] and info['ghz'] and info['ghz'] > 0:
        imp = implied_diameter(info['gain'], info['ghz'])
        if d / imp > 3 or d / imp < 1 / 3.0:
            return 'diameter %g m but its %.1f dBi gain at %g GHz implies ~%.2f m' % (d, info['gain'], info['ghz'], imp)
    if info['major'] and max(d, info['major']) / min(d, info['major']) > 3:
        return 'diameter %g m but esaf_diameter_major %g m' % (d, info['major'])
    return ''


def dish_expected(info, other=None):
    """The decimal-shifted diameter that best matches the implied (or the other version's) value."""
    target = implied_diameter(info['gain'], info['ghz']) if info['gain'] and info['ghz'] else (other or info['major'])
    if not target:
        return ''
    best = min((info['d'] * f for f in (0.01, 0.1, 10, 100)), key=lambda x: abs(math.log(x / target)))
    return '%g m' % best if abs(math.log(best / target)) < math.log(2) else ''


def norm_app(name):
    return re.sub(r'[^A-Z0-9]', '', (name or '').upper())[:12]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=os.path.join(HERE, 'data', 'icfs.db'))
    ap.add_argument('--out', default=os.path.join(HERE, 'reports', 'walk.csv'))
    a = ap.parse_args()
    conn = sqlite3.connect('file:%s?mode=ro' % a.db, uri=True)
    history = collections.defaultdict(list)
    for r in records(conn):
        if r.get('subsystem') == 'SES' and r.get('call_sign') and r.get('last_action') == 'GRA' \
                and r.get('filing_type') in TECHNICAL and not (r.get('state') or '').startswith('Pending'):
            v = version(r)
            if v:
                history[r['call_sign']].append({'when': when(r), 'fn': r.get('file_number'), 'type': r.get('filing_type'),
                                                'state': r.get('state') or '', 'filed': filing_date(r),
                                                'app': norm_app(r.get('applicant_name')), 'v': v})

    rows, counts, examples = [], collections.Counter(), collections.defaultdict(list)
    pairs = collections.Counter()
    seen = set()

    def add(rule, cs, prev, cur, detail, target=None, conf='medium', benign='', expected='', bv='', av=''):
        """One row; target is the filing to repair (default the later one, cur)."""
        target = target or cur
        if (rule, target['fn']) in seen:
            return
        seen.add((rule, target['fn']))
        counts[(rule, target['type'])] += 1
        if len(examples[(rule, target['type'])]) < EXAMPLES:
            examples[(rule, target['type'])].append(target['fn'])
        other = prev if target is cur else cur
        if not bv and rule.startswith('dish_jump') and other['v']['dish']:
            bv = '%g m' % other['v']['dish']
        rows.append([rule, cs, prev['fn'], prev['type'], str(prev['when']), cur['fn'], cur['type'], str(cur['when']),
                     (BENIGN + benign + '; ' if benign else '') + detail, CLASS[rule], 'low' if benign else conf, 'SES',
                     target['fn'], target['type'], target['state'], other['fn'], bv, target['fn'], av, expected])

    def site_text(v):
        return ' | '.join(s['text'] for s in v['raw_sites'])

    for cs, hist in history.items():
        hist.sort(key=lambda x: (x['when'], x['filed'] or datetime.date.min, x['fn']))
        steps = [jumps(hist[i - 1]['v'], hist[i]['v']) for i in range(1, len(hist))]
        for i, step in enumerate(steps, 1):
            prev, cur = hist[i - 1], hist[i]
            pv, cv = prev['v'], cur['v']
            pairs[cur['type']] += 1
            sibling = prev['when'] == cur['when'] or (prev['app'] and cur['app'] and prev['app'] != cur['app'])
            for rule, detail in step.items():
                if rule == 'relocated':
                    d, t, s = closest(pv, cv)
                    same_place = t['city'] and t['city'] == s['city'] and t['state'] == s['state']
                    one_diff = sum(x != y for x, y in zip(t['dms'], s['dms'])) == 1
                    bv, av = t['text'], s['text']
                    if d > TYPO_KM and (same_place or one_diff or cur['type'] == 'Renewal') and not sibling:
                        why = '; '.join(w for w, ok in (('same city/state', same_place), ('one DMS component differs', one_diff),
                                                        ('a Renewal cannot relocate', cur['type'] == 'Renewal')) if ok)
                        bad_p, bad_c = outside_state(t), outside_state(s)
                        if bad_c and not bad_p:
                            add(rule, cs, prev, cur, '%s; %s; later site outside its declared state' % (detail, why),
                                conf='high', expected=bv, bv=bv, av=av)
                        elif bad_p and not bad_c:
                            add(rule, cs, prev, cur, '%s; %s; earlier site outside its declared state: the earlier filing '
                                'holds the typo' % (detail, why), target=prev, conf='high', expected=av, bv=av, av=bv)
                        else:
                            add(rule, cs, prev, cur, '%s; %s; likely a coordinate typo in one of the two filings' % (detail, why),
                                bv=bv, av=av)
                            add(rule, cs, prev, cur, '%s; %s; likely a coordinate typo in one of the two filings (earlier '
                                'filing of the pair)' % (detail, why), target=prev, bv=av, av=bv)
                    else:
                        add(rule, cs, prev, cur, detail, bv=bv, av=av, benign=(
                            'same-day or different-applicant filing: sibling site of a network, not a version' if sibling else
                            'refinement of %.1f km' % d if d <= TYPO_KM else 'relocation granted by %s' % cur['type']))
                elif rule == 'hemisphere_flip':
                    pair = None
                    for s in cv['raw_sites']:
                        for t in pv['raw_sites']:
                            if (abs(s['pt'][0] + t['pt'][0]) < 0.01 and abs(s['pt'][1] - t['pt'][1]) < 0.01) or \
                                    (abs(s['pt'][1] + t['pt'][1]) < 0.01 and abs(s['pt'][0] - t['pt'][0]) < 0.01):
                                pair = (t, s)
                    if not pair:
                        continue
                    t, s = pair
                    bad_p, bad_c = outside_state(t), outside_state(s)
                    if bad_c and not bad_p:
                        add(rule, cs, prev, cur, detail + '; later filing has the wrong sign for its state', conf='high',
                            expected=t['text'], bv=t['text'], av=s['text'])
                    elif bad_p and not bad_c:
                        add(rule, cs, prev, cur, detail + '; earlier filing has the wrong sign for its state', target=prev,
                            conf='high', expected=s['text'], bv=s['text'], av=t['text'])
                    else:
                        add(rule, cs, prev, cur, detail + '; one of the two signs is wrong', bv=t['text'], av=s['text'])
                        add(rule, cs, prev, cur, detail + '; one of the two signs is wrong (earlier filing)', target=prev,
                            bv=s['text'], av=t['text'])
                elif rule == 'spectrum_replaced':
                    bv, av = fmt_spec(pv['spectrum']), fmt_spec(cv['spectrum'])
                    if typo_band(pv):
                        add(rule, cs, prev, cur, detail + '; the earlier filing has a band-unit typo band that inflates its '
                            'spectrum', target=prev, bv=av, av=bv)
                    elif cur['type'] == 'Renewal' and prev['filed'] and cur['filed'] and cur['filed'] > prev['filed'] \
                            and shares_site(pv, cv) and not typo_band(cv):
                        add(rule, cs, prev, cur, detail + '; a Renewal cannot change bands: compare with sibling filings '
                            'filed the same day (swapped Schedule B?)', expected=bv, bv=bv, av=av)
                    else:
                        add(rule, cs, prev, cur, detail, bv=bv, av=av, benign=(
                            'parallel or same-day filing' if sibling else 'Modification/License may change bands or list '
                            'only the changed ones'))
                elif rule == 'eirp_jump':
                    bv, av = '%.1f dBW' % pv['eirp'], '%.1f dBW' % cv['eirp']
                    if cur['type'] == 'Renewal' and cur['when'] != prev['when']:
                        add(rule, cs, prev, cur, detail + '; a Renewal should repeat the authorized carriers', bv=bv, av=av)
                    else:
                        add(rule, cs, prev, cur, detail, bv=bv, av=av, benign='%s may change EIRP (carrier/antenna added '
                            'or dropped)' % cur['type'])
                elif rule == 'dish_jump':
                    fp, fc = dish_fault(pv['dish_info']), dish_fault(cv['dish_info'])
                    bv, av = '%g m' % pv['dish'], '%g m' % cv['dish']
                    if not fp and not fc:
                        add(rule, cs, prev, cur, detail, bv=bv, av=av,
                            benign='both diameters agree with their own gain: equipment change')
                    if fc:
                        add(rule, cs, prev, cur, '%s; %s' % (detail, fc), conf='high' if not fp else 'medium',
                            expected=dish_expected(cv['dish_info'], pv['dish']), bv=bv, av=av)
                    if fp:
                        add(rule, cs, prev, cur, '%s; earlier filing: %s' % (detail, fp), target=prev,
                            conf='high' if not fc else 'medium', expected=dish_expected(pv['dish_info'], cv['dish']),
                            bv=av, av=bv)
                    for bad, info in ((prev, pv['dish_info']), (cur, cv['dish_info'])):
                        why = dish_fault(info)
                        for other in hist if why else ():  # the same bad value carried into other versions
                            if other is not bad and other['v']['dish'] == info['d'] and dish_fault(other['v']['dish_info']):
                                add(rule, cs, prev, cur, '%s; carries the same bad diameter as %s: %s' % (detail, bad['fn'], why),
                                    target=other, expected=dish_expected(other['v']['dish_info']), av='%g m' % info['d'])
            # undone by the next version: n-1 and n+1 agree although n differs from both
            if i < len(steps):
                nxt = hist[i + 1]
                nv = nxt['v']
                for rule in set(step) & set(steps[i]):
                    if rule in jumps(pv, nv):
                        continue
                    rr = rule + '_reverted'
                    base = 'reverted by %s (%s, granted %s)' % (nxt['fn'], nxt['type'], nxt['when'])
                    tied = len({prev['when'], cur['when'], nxt['when']}) < 3
                    if rule == 'relocated':
                        d, t, s = closest(pv, cv)
                        one_diff = sum(x != y for x, y in zip(t['dms'], s['dms'])) == 1
                        same_place = t['city'] and t['city'] == s['city'] and t['state'] == s['state']
                        pingpong = i + 1 < len(hist) - 1 and 'relocated' not in jumps(cv, hist[-1]['v'])
                        bv, av = site_text(pv), site_text(cv)
                        if tied or pingpong:
                            why = 'same grant date as a neighbour: order uncertain' if tied else \
                                'the middle matches the latest version: the reverting filing may be the stale one'
                            add(rr, cs, prev, cur, '%s; %s' % (base, why), conf='low', bv=bv, av=av)
                            add(rr, cs, cur, nxt, '%s; reverting filing of a %s' % (base, why), target=nxt, conf='low',
                                bv=av, av=site_text(nv))
                        elif one_diff or (d > 100 and same_place):
                            add(rr, cs, prev, cur, '%s; %s' % (base, 'one DMS component differs' if one_diff else
                                                               '%.0f km with city unchanged' % d),
                                conf='high', expected=t['text'], bv=t['text'], av=s['text'])
                        else:
                            add(rr, cs, prev, cur, base, bv=bv, av=av)
                    elif rule == 'spectrum_replaced':
                        bv, av = fmt_spec(pv['spectrum']), fmt_spec(cv['spectrum'])
                        if shares_site(cv, pv) and shares_site(cv, nv) and len({prev['when'], cur['when'], nxt['when']}) > 1:
                            add(rr, cs, prev, cur, base + '; Schedule B probably belongs to another station at the same site',
                                expected=bv, bv=bv, av=av)
                        else:
                            add(rr, cs, prev, cur, base, bv=bv, av=av,
                                benign='different sites or one same-day batch: parallel filings of a network call sign')
                    elif rule == 'eirp_jump':
                        hits = []
                        for k, e1 in cv['carriers'].items():
                            e0, e2 = pv['carriers'].get(k), nv['carriers'].get(k)
                            if e0 is not None and e2 is not None and abs(e1 - e0) > EIRP_JUMP_DB and abs(e1 - e2) > EIRP_JUMP_DB \
                                    and abs(e0 - e2) <= 1:
                                hits.append((k, e0, e1))
                        if hits and not tied and shares_site(pv, cv) and shares_site(cv, nv):
                            k, e0, e1 = hits[0]
                            add(rr, cs, prev, cur, '%s; carrier %s %g-%g %s at %.1f dBW vs %.1f before and after' % (
                                base, k[3], k[0], k[1], k[2], e1, e0), expected='%.1f dBW' % e0, bv='%.1f dBW' % e0,
                                av='%.1f dBW' % e1)
                        else:
                            add(rr, cs, prev, cur, base, bv='%.1f dBW' % pv['eirp'], av='%.1f dBW' % cv['eirp'],
                                benign='no single carrier jumps and returns (carrier set change, partial Schedule B or '
                                'same-day filings)')
                    elif rule == 'dish_jump':
                        mi, pi_, ni = cv['dish_info'], pv['dish_info'], nv['dish_info']
                        ref = next((x for x in (pi_, ni) if x and x['model'] and x['model'] == mi['model']), None)
                        ratio = ref and mi['d'] / ref['d']
                        pow10 = ratio and any(abs(ratio / f - 1) <= 0.02 for f in (10, 100, 0.1, 0.01))
                        bv, av = '%g m' % pv['dish'], '%g m' % cv['dish']
                        if pow10:
                            add(rr, cs, prev, cur, '%s; same antenna model %s, decimal slip' % (base, mi['model']),
                                conf='high', expected='%g m' % ref['d'], bv=bv, av=av)
                        else:
                            add(rr, cs, prev, cur, base, bv=bv, av=av,
                                benign='different antenna models: hub/VSAT components filed separately')

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)
    md = ['# Schedule B history walk (earth stations)', '',
          '%d call signs with granted Schedule B; %d consecutive pairs compared (%s).' % (
              len(history), sum(pairs.values()), ', '.join('%s %d' % kv for kv in pairs.most_common())), '',
          '| rule | flagged filing type | rows | examples |', '|---|---|---|---|']
    md += ['| %s | %s | %d | %s |' % (k[0], k[1], n, ', '.join(examples[k]))
           for k, n in sorted(counts.items(), key=lambda x: (x[0][0], -x[1]))]
    with open(os.path.splitext(a.out)[0] + '.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(md) + '\n')
    print('wrote %s (%d rows)' % (a.out, len(rows)))
    by = collections.Counter((x[0], x[10], x[8].startswith(BENIGN)) for x in rows)
    for (rule, conf, benign), n in sorted(by.items()):
        print('  %-28s %-6s %-6s %6d' % (rule, conf, 'benign' if benign else '', n))


if __name__ == '__main__':
    main()
