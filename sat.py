"""Space-station (SAT) Schedule S checks -> one CSV row per affected filing per issue.

    python sat.py [--db data/icfs.db] [--out reports/sat.csv]

Schedule S lives in r['s1'] (a list; one entry per satellite/sub-system, usually one):
s2 frequency bands, s3 GSO orbit (longitude + hemisphere), s4/s5 receive/transmit beams to Earth,
s6/s7 space-to-space receive/transmit beams, s8 gain-contour files.  The API carries no NGSO
orbit parameters (apogee, perigee, inclination), so those checks cannot be made here.

Within a filing (every SAT filing with Schedule S, any state): band edges, units, direction,
duplicates, beams outside the s2 bands, lifetime, orbit type, GSO longitude/hemisphere/eccentricity.
Across each call sign's granted versions (grant date, then file date) with Schedule S:
GSO location jumps outside a Modification, spectrum replaced, network name changes, and
A->B->A reversions (the middle filing is the suspect).  Modifications and Amendments often carry
only the changed part of Schedule S, so spectrum/name/location changes into them are not reported.
HF edges (3-30 MHz) are legitimate (sounders, amateur links).  Rows the verification judged benign
keep confidence low and a detail starting "likely benign: <why>".  Stdlib only.
"""
import argparse
import collections
import csv
import datetime
import os
import re
import sqlite3

from freqs import filing_date, grant_of, num
from profiler import HERE, parse_date, records
from walk import kept, merge

MIN_MHZ, MAX_MHZ = 3.0, 300000.0  # HF (3-30 MHz) space-station bands exist
MAX_LIFETIME = 50.0
JUMP_DEG = 1.0
SPECTRUM_KEEP = 0.25
RELOCATING = ('Modification', 'Modification to PDR/PPL', 'Amendment')
PARTIAL = RELOCATING  # Schedule S lists only what changes
BENIGN = 'likely benign: '
LON_TEXT = re.compile(r'(\d+(?:\.\d+)?)\s*(?:deg\w*\.?\s*)?(W\.?\s*L\b\.?|E\.?\s*L\b\.?|WEST\b|EAST\b|WL\b|EL\b)', re.I)
DIRECTIONS = {'Earth-to-Space (Receive)', 'Space-to-Earth (Transmit)',
              'Space-to-Space (Receive)', 'Space-to-Space (Transmit)'}
BEAM_DIRECTION = {'s4': 'Earth-to-Space (Receive)', 's5': 'Space-to-Earth (Transmit)',
                  's6': 'Space-to-Space (Receive)', 's7': 'Space-to-Space (Transmit)'}
ORBIT_TYPES = ('Geostationary (GSO)', 'Non-Geostationary (NGSO)')
LABEL = re.compile(r'^\s*([\d.]+)\s*-\s*([\d.]+)\s*$')
COLUMNS = ['issue_class', 'rule', 'confidence', 'subsystem', 'call_sign', 'file_number', 'filing_type', 'state',
           'before_file', 'before_value', 'after_file', 'after_value', 'expected_value', 'detail']

# rule: (issue_class, default confidence, one-line description)
RULES = {
    'band_edge_missing': ('band', 'high', 's2 band lower or upper limit blank or non-numeric'),
    'band_lower_above_upper': ('band', 'high', 's2 band lower limit above upper limit (expected = swapped)'),
    'band_unit_slip': ('band', 'low', 's2 edge outside 3 MHz-300 GHz and x1000 or /1000 lands inside (GHz/kHz entered as MHz)'),
    'band_out_of_range': ('band', 'medium', 's2 edge outside 3 MHz-300 GHz with no obvious unit fix'),
    'band_label_disagrees': ('band', 'high', 's2_frequency_band_mhz preset disagrees with the entered limits'),
    'band_duplicate': ('band', 'medium', 'identical s2 band (edges, direction, service) entered more than once'),
    'band_direction_invalid': ('direction', 'high', 's2 direction blank or not E-S / S-E / space-to-space'),
    'beam_edge_invalid': ('beam', 'medium', 's4-s7 beam frequency blank, non-numeric or lower above upper'),
    'beam_outside_bands': ('beam', 'high', 's4-s7 beam frequency range not covered by any s2 band'),
    'beam_direction_mismatch': ('beam', 'low', 'beam range is in s2 only under a different direction'),
    'lifetime_missing': ('lifetime', 'low', 's1 estimated lifetime blank'),
    'lifetime_implausible': ('lifetime', 'high', 's1 estimated lifetime non-numeric, <= 0 or > 50 years'),
    'orbit_type_invalid': ('orbit', 'low', 's1 orbit type blank or not GSO/NGSO'),
    'gso_longitude_missing': ('orbit', 'low', 'GSO filing with no s3 orbital longitude (expected = nearest granted version of the call sign)'),
    'gso_longitude_out_of_range': ('orbit', 'high', 's3 longitude outside 0..180 (with hemisphere) / -180..180'),
    'gso_hemisphere_missing': ('orbit', 'medium', 's3 longitude given (non-zero, not 180) but hemisphere blank'),
    'gso_eccentricity_implausible': ('orbit', 'medium', 's3 eccentricity outside 0..0.1 (GSO is ~0)'),
    'gso_location_jump': ('history', 'low', 'GSO longitude moved > 1 deg between granted versions, not by a Modification'),
    'spectrum_replaced': ('history', 'low', '< 25% of the previous granted spectrum kept'),
    'network_name_changed': ('history', 'low', 's1 network name changed between granted versions'),
    'gso_location_reverted': ('history', 'low', 'A->B->A longitude: the middle granted version is suspect'),
    'spectrum_reverted': ('history', 'low', 'A->B->A spectrum: the middle granted version is suspect'),
    'network_name_reverted': ('history', 'low', 'A->B->A network name: the middle granted version is suspect'),
}


def g(v):
    return '%g' % v


def unit_fix(v):
    """The MHz value v probably meant, if v is out of range and x1000 or /1000 lands in range."""
    if v < MIN_MHZ and MIN_MHZ <= v * 1000 <= MAX_MHZ:
        return v * 1000
    if v > MAX_MHZ and MIN_MHZ <= v / 1000 <= MAX_MHZ:
        return v / 1000
    return None


def norm_name(n):
    return re.sub(r'[^A-Z0-9]', '', re.sub(r'\bAND\b', '', (n or '').upper()))


def signed_lon(s3):
    lon = num(s3.get('s3_orbital_longitude'))
    if lon is None:
        return None
    return -lon if (s3.get('s3_hemisphere') or '').strip().upper().startswith('W') else lon


def covered(lo, hi, spec):
    return any(a <= lo + 1e-6 and hi <= b + 1e-6 for a, b in spec)


def check(r, find):
    """Within-filing Schedule S checks; find(rule, after_value, expected_value, detail[, confidence])."""
    s1s = r.get('s1') or []
    lifetimes = [(x.get('s1_estimated_lifetime_of_satellite') or '').strip() for x in s1s]
    lifetimes = [v for v in lifetimes if v]
    fn = r.get('file_number') or ''
    fdate = filing_date(r)
    code = fn.split('-')[1] if fn.count('-') >= 3 else ''
    for n, s in enumerate(s1s, 1):
        tag = 's1[%d] ' % n if len(s1s) > 1 else ''
        bands, seen = [], set()
        for i, b in enumerate(s.get('s2') or [], 1):
            raw_lo, raw_hi = b.get('s2_lower_frequency_limit'), b.get('s2_upper_frequency_limit')
            lo, hi = num(raw_lo), num(raw_hi)
            d = (b.get('s2_direction_of_transmission') or '').strip()
            where = '%ss2 band %d %s-%s MHz %s' % (tag, i, raw_lo, raw_hi, d or '(no direction)')
            val = '%s-%s' % (raw_lo, raw_hi)
            if d not in DIRECTIONS:
                find('band_direction_invalid', d, '', where)
            if lo is None or hi is None:
                find('band_edge_missing', val, '', where)
                continue
            out = [v for v in (lo, hi) if not MIN_MHZ <= v <= MAX_MHZ]
            if out:
                fixes = [unit_fix(v) for v in (lo, hi)]
                if all(fixes[k] or MIN_MHZ <= v <= MAX_MHZ for k, v in enumerate((lo, hi))):
                    find('band_unit_slip', val, '%s-%s' % tuple(g(fixes[k] or v) for k, v in enumerate((lo, hi))), where)
                else:
                    find('band_out_of_range', val, '', where)
            if lo > hi:
                find('band_lower_above_upper', val, '%s-%s' % (g(hi), g(lo)), where)
            m = LABEL.match(b.get('s2_frequency_band_mhz') or '')
            if m and (abs(float(m.group(1)) - lo) > 0.001 or abs(float(m.group(2)) - hi) > 0.001):
                find('band_label_disagrees', 'preset "%s"' % b.get('s2_frequency_band_mhz'),
                     'preset "%s - %s" (or Other); keep the limits' % (g(lo), g(hi)),
                     '%s; the entered limits are usually right and the preset is stale' % where)
            key = (lo, hi, d, b.get('s2_type_of_service') or '', b.get('s2_non_conforming_indicator') or '')
            if key in seen:
                find('band_duplicate', val, '', where)
            seen.add(key)
            bands.append((min(lo, hi), max(lo, hi), d))
        if bands:
            every = merge([(lo, hi) for lo, hi, _ in bands])
            for sec, want in BEAM_DIRECTION.items():
                same = merge([(lo, hi) for lo, hi, d in bands if d == want])
                for b in s.get(sec) or []:
                    raw_lo, raw_hi = b.get(sec + '_lower_frequency'), b.get(sec + '_upper_frequency')
                    lo, hi = num(raw_lo), num(raw_hi)
                    where = '%s%s beam %s %s-%s MHz' % (tag, sec, b.get(sec + '_beam_name') or '?', raw_lo, raw_hi)
                    val = '%s-%s' % (raw_lo, raw_hi)
                    if lo is None or hi is None or lo > hi:
                        find('beam_edge_invalid', val, '', where)
                        continue
                    if not covered(lo, hi, every):
                        find('beam_outside_bands', val, '', '%s; s2 bands %s' % (where, fmt(every)))
                    elif not covered(lo, hi, same):
                        find('beam_direction_mismatch', val, want, '%s; no %s s2 band covers it' % (where, want))
        life = (s.get('s1_estimated_lifetime_of_satellite') or '').strip()
        lv = num(life)
        if not life:
            real = fdate and fdate.year >= 2017 and code not in ('MOD', 'MPL') and s.get('s2')
            if lifetimes:
                find('lifetime_missing', '', lifetimes[0], tag + 'estimated lifetime blank; another s1 entry of this '
                     'filing gives %s years' % lifetimes[0], 'medium')
            elif real:
                find('lifetime_missing', '', '', tag + 'estimated lifetime blank on a 2017+ new-authority filing with '
                     'bands: take the design lifetime from the narrative', 'medium')
            else:
                find('lifetime_missing', '', '', BENIGN + ('legacy migrated Schedule S stub' if not fdate or fdate.year < 2017
                     else 'modification or stub without bands: the field does not apply') + '; ' + tag +
                     'estimated lifetime blank')
        elif lv is None or lv <= 0 or lv > MAX_LIFETIME:
            find('lifetime_implausible', life, '', tag + 'estimated lifetime %r years' % life)
        ot = s.get('s1_orbit_type') or ''
        s3s = s.get('s3') or []
        content = s.get('s2') or any(s.get(k) for k in ('s4', 's5', 's6', 's7')) or any(
            (v or '').strip() for x in s3s for v in x.values() if isinstance(v, str))
        if ot not in ORBIT_TYPES:
            detail = '%sorbit type %r; %d s3 entries' % (tag, ot, len(s3s))
            if 'ngso' in ot.lower():
                find('orbit_type_invalid', ot, 'Non-Geostationary (NGSO)', detail + '; raw internal enum leaked; confirm '
                     'against 312_class_of_station', 'medium')
            elif ot or content:
                find('orbit_type_invalid', ot, 'Geostationary (GSO)' if any(signed_lon(x) is not None for x in s3s) else '',
                     detail, 'medium')
            else:
                find('orbit_type_invalid', ot, '', BENIGN + 'stub Schedule S on a notification/legacy filing; ' + detail)
        if ot == 'Geostationary (GSO)' and not any((x.get('s3_orbital_longitude') or '').strip() for x in s3s):
            m = LON_TEXT.search(s.get('s1_description') or '')
            exp = '%s %s' % (m.group(1), 'West' if m.group(2).upper().startswith('W') else 'East') if m else ''
            detail = tag + 'GSO Schedule S left as an empty stub (no orbital longitude)%s' % (
                '; s1_description names %s' % m.group(0) if m else '')
            if r.get('filing_type') in PARTIAL:
                find('gso_longitude_missing', '', exp, BENIGN + 'administrative modification: carry the longitude from '
                     'the underlying authorization; ' + detail)
            else:
                find('gso_longitude_missing', '', exp, detail + '; backfill from the Schedule S attachment or description',
                     'medium')
        for x in s3s:
            raw, hemi = (x.get('s3_orbital_longitude') or '').strip(), (x.get('s3_hemisphere') or '').strip()
            lon = num(raw)
            if raw and (lon is None or (hemi and not 0 <= lon <= 180) or (not hemi and not -180 <= lon <= 180)):
                exp = ''
                if lon is not None and 180 < lon < 360 and hemi.upper().startswith('E'):
                    exp = '%s West' % g(360 - lon)  # 0-360 east convention
                find('gso_longitude_out_of_range', '%s %s' % (raw, hemi), exp, tag + 's3 longitude %s %s' % (raw, hemi))
            elif lon is not None and not hemi and lon not in (0, 180) and lon > 0:
                find('gso_hemisphere_missing', raw, '', tag + 's3 longitude %s with no hemisphere' % raw)
            e = num(x.get('s3_orbital_eccentricity'))
            if (x.get('s3_orbital_eccentricity') or '').strip() and (e is None or not 0 <= e <= 0.1):
                find('gso_eccentricity_implausible', x.get('s3_orbital_eccentricity'), '', tag + 's3 eccentricity')


def fmt(spec):
    return ', '.join('%g-%g' % (lo, hi) for lo, hi in spec)


def version(r):
    """Comparable shape of a filing's Schedule S, or None without one."""
    s1s = r.get('s1') or []
    if not s1s:
        return None
    names = sorted({(s.get('s1_satellite_network_name') or '').strip() for s in s1s} - {''})
    lons = sorted({round(l, 2) for s in s1s for x in s.get('s3') or [] for l in [signed_lon(x)] if l is not None})
    spec = merge([(min(lo, hi), max(lo, hi)) for s in s1s for b in s.get('s2') or []
                  for lo, hi in [(num(b.get('s2_lower_frequency_limit')), num(b.get('s2_upper_frequency_limit')))]
                  if lo is not None and hi is not None])
    return {'names': names, 'lons': lons, 'spectrum': [(lo, hi if hi > lo else lo + 0.001) for lo, hi in spec]}


def lon_jump(a, b):
    """Smallest move from any a longitude to the nearest b longitude, if both have any."""
    if not a['lons'] or not b['lons']:
        return None
    return max(min(min(abs(x - y), 360 - abs(x - y)) for x in a['lons']) for y in b['lons'])


def steps(a, b):
    """{rule: (before_value, after_value, detail)} between consecutive granted versions."""
    out = {}
    j = lon_jump(a, b)
    if j is not None and j > JUMP_DEG:
        out['gso_location'] = (lonfmt(a), lonfmt(b), '%.2f deg' % j)
    if a['spectrum'] and b['spectrum']:
        share = kept(a['spectrum'], b['spectrum'])
        if share < SPECTRUM_KEEP:
            out['spectrum'] = (fmt(a['spectrum']), fmt(b['spectrum']), '%.0f%% kept' % (100 * share))
    na, nb = {norm_name(n) for n in a['names']}, {norm_name(n) for n in b['names']}
    if na and nb and na != nb:
        out['network_name'] = ('; '.join(a['names']), '; '.join(b['names']), '')
    return out


def lonfmt(v):
    return '; '.join('%g%s' % (abs(l), 'W' if l < 0 else 'E') for l in v['lons'])


def filing_date_of(fn):
    return str(filing_date({'file_number': fn}) or datetime.date.min)


def when(r):
    """Sort key: grant date (stale/bulk-stamped values replaced by last_action_date), filing date, file."""
    return (str(grant_of(r) or '9999'), str(filing_date(r) or datetime.date.min), r.get('file_number') or '')


def granted_before_filed(r):
    gd, fd = parse_date(r.get('grant_date')), filing_date(r)
    return bool(gd and fd and gd < fd)


def alias(a, b):
    """Two network names that are probably the same network: ITU USASAT-* vs commercial, or one inside the other."""
    na, nb = {norm_name(x) for x in a}, {norm_name(x) for x in b}
    if any(x.startswith('USASAT') for x in na) != any(x.startswith('USASAT') for x in nb):
        return True
    return any(x and y and (x in y or y in x) for x in na for y in nb)


def history_rows(hist):
    """hist: [(record, version)] sorted by when(); yields (rule, before_rec, after_rec, bv, av, expected, detail)."""
    st = [steps(hist[i - 1][1], hist[i][1]) for i in range(1, len(hist))]
    for i, step in enumerate(st, 1):
        prev, cur = hist[i - 1][0], hist[i][0]
        pv, cv = hist[i - 1][1], hist[i][1]
        same_day = when(prev)[0] == when(cur)[0]
        for kind, (bv, av, detail) in step.items():
            if kind == 'gso_location' and (cur.get('filing_type') in RELOCATING or same_day):
                continue  # relocation by modification/amendment, or a parent and its amendment granted together
            if kind == 'spectrum' and (cur.get('filing_type') in PARTIAL or same_day or
                                       (pv['names'] and cv['names'] and not alias(pv['names'], cv['names'])
                                        and {norm_name(n) for n in pv['names']} != {norm_name(n) for n in cv['names']})):
                continue  # partial Schedule S, granted together, or a different fleet under one call sign
            if kind == 'network_name' and (alias(pv['names'], cv['names']) or granted_before_filed(cur)):
                continue  # ITU vs commercial name, generation suffix, or an unreliable grant date
            rule = {'gso_location': 'gso_location_jump', 'spectrum': 'spectrum_replaced',
                    'network_name': 'network_name_changed'}[kind]
            yield rule, prev, cur, bv, av, '', detail
        if i < len(st):
            nxt, nv = hist[i + 1][0], hist[i + 1][1]
            for kind in set(step) & set(st[i]):
                if kind in steps(pv, nv):
                    continue
                if kind == 'gso_location' and cur.get('filing_type') in RELOCATING:
                    continue  # moved by one granted modification and back by another
                if kind == 'spectrum' and cur.get('filing_type') in PARTIAL and (
                        kept(pv['spectrum'], cv['spectrum']) == 0 or re.search(r'\badd', cur.get('s1') and
                                                                           cur['s1'][0].get('s1_description') or '', re.I)):
                    continue  # additive modification: its Schedule S is a delta
                if kind == 'network_name' and (same_day or when(cur)[0] == when(nxt)[0] or alias(pv['names'], cv['names'])
                                               or any(granted_before_filed(x) for x in (prev, cur, nxt))):
                    continue
                bv, av, detail = step[kind]
                yield (kind + '_reverted', prev, cur, bv, av, bv,
                       '%s; reverted by %s (%s, granted %s)' % (detail, nxt.get('file_number'),
                                                               nxt.get('filing_type'), nxt.get('grant_date')))


def row(rule, r, before=None, bv='', after_file='', av='', expected='', detail='', conf=None):
    cls, default, _ = RULES[rule]
    conf = 'low' if detail.startswith(BENIGN) else conf or default
    return [cls, rule, conf, r.get('subsystem'), r.get('call_sign') or '', r.get('file_number'), r.get('filing_type'),
            r.get('state'), before.get('file_number') if before else '', bv, after_file, av, expected, detail]


def audit(recs):
    rows, history = [], collections.defaultdict(list)
    for r in recs:
        if r.get('subsystem') != 'SAT' or not r.get('s1'):
            continue
        fn = r.get('file_number')
        check(r, lambda rule, av, exp, detail, conf=None, r=r, fn=fn: rows.append(row(rule, r, None, '', fn, av, exp, detail, conf)))
        if r.get('last_action') == 'GRA':
            v = version(r)
            key = r.get('call_sign') or ('name:' + '|'.join(norm_name(n) for n in v['names']) if v['names'] else '')
            if key:
                history[key].append((r, v))
    for key, hist in history.items():
        hist.sort(key=lambda x: when(x[0]))
        known = [(when(r), r.get('file_number'), lonfmt(v)) for r, v in hist if v['lons']]
        for x in rows:
            if x[1] == 'gso_longitude_missing' and x[4] and key == x[4] and known and not x[12] and not x[13].startswith(BENIGN):
                # the latest granted version filed no later than this one, else the earliest
                mine = filing_date_of(x[5])
                prior = [k for k in known if k[0][1] <= mine] or known[:1]
                x[12] = prior[-1][2]
                x[13] += '; expected from %s' % prior[-1][1]
        for rule, prev, cur, bv, av, exp, detail in history_rows(hist):
            rows.append(row(rule, cur, prev, bv, cur.get('file_number'), av, exp,
                            '%s (%s, granted %s) -> %s (%s, granted %s); %s' % (
                                prev.get('file_number'), prev.get('filing_type'), prev.get('grant_date'),
                                cur.get('file_number'), cur.get('filing_type'), cur.get('grant_date'), detail)))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=os.path.join(HERE, 'data', 'icfs.db'))
    ap.add_argument('--out', default=os.path.join(HERE, 'reports', 'sat.csv'))
    a = ap.parse_args()
    conn = sqlite3.connect('file:%s?mode=ro' % a.db, uri=True)
    rows = audit(records(conn))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)
    counts = collections.Counter(x[1] for x in rows)
    for rule in RULES:
        print('%-30s %6d  %s  %s' % (rule, counts[rule], RULES[rule][1], RULES[rule][2]))
    print('wrote %s (%d rows, %d filings)' % (a.out, len(rows), len({x[5] for x in rows})))


if __name__ == '__main__':
    main()
