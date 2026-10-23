"""Cross-check earth-station points of communication (SES schedule_b[].site[].poc[]) against the
space-station (SAT) records, one row per affected earth-station filing per issue.

    python poc.py [--db data/icfs.db] [--out reports/poc.csv]

What the data looks like (measured 2026-10-22 over 108,211 SES filings, 144,514 poc entries):
  * poc_orbit_location is free text: 'GSO' (60,887 - nearly all 'Permitted Space Station List'),
    blank (41,370 - almost all 1977-1990 licenses), 'NGSO' (3,201), and '<deg> W.L.' / '<deg> E.L.'
    in many spellings ('111.1  W.L', '85 WL', '99.9<?> W.L.', '101 DEG. WL').  East longitudes are
    often written 0-360 ('304.5 E.L.' = 55.5 W), which is the FCC convention, not an error.
    Legacy rows were cut at 10 characters ('116.8 DE') and some carry other columns' text
    ('ALASKA', 'KS30', 'TDRS-171').
  * SAT records: sat_satellite_name and call_sign on every filing; the GSO orbital location only in
    Schedule S s1[].s3[] (s3_orbital_longitude + s3_hemisphere) on ~335 filings / 140 call signs,
    so the location checks cover only satellites filed on Schedule S.

Matching a poc entry to a SAT call sign, in order (first unambiguous hit wins):
  call_sign  a token in poc_satellite / poc_common_name / poc_itu_name equals a SAT call sign
             ('PANAMSAT-1R (S2368) Deorbited' -> S2368, common name 'KS30' -> KS30)
  name       a whole field, upper-cased alphanumerics only, equals a SAT sat_satellite_name or
             s1_satellite_network_name that belongs to exactly one call sign
  fuzzy      the same after dropping parentheses and status notes ('(Deorbited)', 'Retired ...')
A hit on two different call signs is ambiguous and not used.  The permitted list is never matched.

Rules (row confidence for SAT comparisons follows the match: call_sign high, name medium, fuzzy low):
  poc_orbit_unparseable      orbit text is neither a category (GSO/NGSO/...) nor one longitude
  poc_orbit_no_hemisphere    a bare number: E or W cannot be told
  poc_orbit_out_of_range     W.L. over 180, or over 360 either way (expected: the SAT location,
                             else a W.L. value read as 360 - v E.L.)
  poc_orbit_blank            a named satellite (not the permitted list) with no orbit location
  poc_orbit_hemisphere_flip  matches the SAT location with the opposite sign
  poc_orbit_mismatch         more than ORBIT_TOL_DEG from every location the SAT record held,
                             as granted (non-STA) on or before the earth-station grant date
  poc_list_empty             granted earth station with an Earth-to-Space band but no poc at all
  poc_sat_surrendered        the satellite's authorization was surrendered/revoked (SUR/RVC)
                             before the earth-station grant and never granted again (low:
                             foreign-licensed and replacement satellites keep names)
  poc_sat_expired            every granted operating term (not STA, not 'Launch Authority')
                             ended before the earth-station grant and no SAT filing for that
                             call sign is Current
  poc_names_retired_sat      a Current earth station whose poc text itself says the satellite is
                             deorbited / retired / surrendered / not to be used
Rows the verification judged benign keep confidence low and a detail starting "likely benign:".
Benign poc_orbit_blank and poc_names_retired_sat rows are collapsed to one row per filing (every
satellite named in after_value).  W.L. values 180-360 are the legacy 0-360 picklist notation
(191 W.L. = 169 E.L.) and are read, not flagged; ocean regions (AOR/POR/IOR), NGSO altitudes and
list values are categories.  poc_orbit_hemisphere_flip blames the SAT Schedule S when an earlier
grant of that satellite had the earth station's hemisphere and the flagged SAT filing does not
describe a relocation.  Stdlib only.
"""
import argparse
import collections
import csv
import datetime
import json
import os
import re
import sqlite3
import zlib

from profiler import HERE, parse_date
from freqs import filing_date
from walk import when

ORBIT_TOL_DEG = 0.5
NAME_FIELDS = ('poc_satellite', 'poc_common_name', 'poc_itu_name')
PERMITTED = 'PERMITTEDSPACESTATIONLIST'
CATEGORIES = {'GSO', 'NGSO', 'POLARNGSO', 'LEOP', 'VARIOUS', 'GSOVARIOUSLOCATIONS', 'NA', 'AOR', 'POR', 'IOR',
              'AORE', 'AORW', 'SYSTEM', 'ALSAT'}
CATEGORY_TEXT = re.compile(r'\bKM\b|LOW EAR|\bSYSTEM\b|\bALSAT\b|\b(AOR|POR|IOR)\b')
BENIGN = 'likely benign: '
GROUP = re.compile(r'^(ALL |ECHOSTARDISH|INMARSAT (POR|AOR|IOR)|PANAMSAT (ATLANTIC|PACIFIC|INDIAN))|OCEAN', re.I)
STRONG_RETIRED = re.compile(r'DE-?ORBIT|RETIRED|GRAVEYARD|LOST IN', re.I)
LON_TOKEN = re.compile(r'(\d+(?:\.\d+)?)\s*(?:DEG\w*\.?\s*)?(W\.?\s?L\.?|E\.?\s?L\.?|WEST|EAST)\b', re.I)
HEMI_TAIL = re.compile(r'(W\.?\s?L\.?|E\.?\s?L\.?|EAST|WEST)\s*$', re.I)
BULK = ('2025-06-22', '2025-06-23', '2025-08-25')
NOT_GRANTED = ('DIS', 'DEN', 'WTD', 'DDA', 'D/A')
WEST, EAST = {'W', 'WL', 'WEST'}, {'E', 'EL', 'EAST'}
STATUS_NOTE = re.compile(r'\(.*?\)|\b(DE-?ORBIT\w*|RETIRED|SURRENDER\w*|DECOMMISSION\w*|RELINQUISH\w*|GRAVEYARD|LOST|MOVED TO|DO NOT USE|TT&C ONLY)\b.*', re.I)
RETIRED = re.compile(r'DE-?ORBIT|RETIRED|SURRENDER|DECOMMISSION|RELINQUISH|GRAVEYARD|LOST IN|DO NOT USE|END OF LIFE', re.I)
COLUMNS = ['issue_class', 'rule', 'confidence', 'subsystem', 'call_sign', 'file_number', 'filing_type', 'state',
           'before_file', 'before_value', 'after_file', 'after_value', 'expected_value', 'detail']
CLASS = {'poc_orbit_unparseable': 'POC orbit format', 'poc_orbit_no_hemisphere': 'POC orbit format',
         'poc_orbit_out_of_range': 'POC orbit format', 'poc_orbit_blank': 'POC orbit format',
         'poc_orbit_hemisphere_flip': 'POC orbit vs SAT', 'poc_orbit_mismatch': 'POC orbit vs SAT',
         'poc_list_empty': 'POC missing', 'poc_sat_surrendered': 'POC satellite status',
         'poc_sat_expired': 'POC satellite status', 'poc_names_retired_sat': 'POC satellite status'}
MATCH_CONFIDENCE = {'call_sign': 'high', 'name': 'medium', 'fuzzy': 'low'}


def norm(s):
    return re.sub(r'[^A-Z0-9]', '', (s or '').upper())


def parse_orbit(text):
    """(kind, degrees) with kind blank|category|list|ok|no_hemisphere|out_of_range|bad; ok degrees in -180..180."""
    t = (text or '').upper().replace('�', ' ').replace('°', ' ').strip()
    if not t:
        return 'blank', None
    if t.startswith('GSO-'):
        t = t[4:]
    if norm(t) in CATEGORIES or CATEGORY_TEXT.search(t):
        return 'category', None
    nums = re.findall(r'\d+(?:\.\d+)?', t)
    if len(nums) > 1 and not re.search(r'[A-Z]{3}', re.sub(r'DEG\w*|W\.?\s*L|E\.?\s*L|TO|AND|WEST|EAST', '', t)):
        return 'list', None  # '101E /119W', '72, 77 DEG': several satellites, not compared
    if len(nums) != 1:
        return 'bad', None
    rest = norm(re.sub(r'^(R\s+)?AT\s+', '', t.replace(nums[0], ' ', 1)).replace('DEGREES', '').replace('DEGREE', '')
                .replace('DEGRE', '').replace('DEGR', '').replace('DEG', '').replace('DE', ''))
    v = float(nums[0])
    if rest in WEST:
        return ('out_of_range', -v) if v > 360 else ('ok', 360 - v) if v > 180 else ('ok', -v)
    if rest in EAST:
        return ('out_of_range', v) if v > 360 else ('ok', v - 360 if v > 180 else v)
    if rest == '':
        return ('out_of_range', v) if v > 360 else ('no_hemisphere', v)
    return 'bad', None


def fmt_lon(v):
    return '%g %s' % (abs(v), 'W.L.' if v < 0 else 'E.L.')


def gap(a, b):
    return abs((a - b + 180) % 360 - 180)


def sat_index(sats):
    """Per SAT call sign: names, granted GSO locations by date, surrender/revoke events, terms."""
    by_cs = collections.defaultdict(lambda: {'names': set(), 'locs': [], 'ends': [], 'grants': [], 'terms': [],
                                             'current': False, 'ngso': False, 'last': (datetime.date.min, ''), 'meta': {}})
    for r in sats:
        cs = (r.get('call_sign') or '').strip().upper()
        if not cs:
            continue
        e = by_cs[cs]
        fn, action = r.get('file_number') or '', r.get('last_action') or ''
        sta = (r.get('filing_type') or '').startswith('Special Temporary')
        granted = parse_date(r.get('grant_date')) if action == 'GRA' else None
        any_grant = parse_date(r.get('grant_date')) if action not in NOT_GRANTED and action else None
        for n in [r.get('sat_satellite_name')] + [s.get('s1_satellite_network_name') for s in r.get('s1') or []]:
            if norm(n):
                e['names'].add(n.strip())
        e['ngso'] |= any('NGSO' in (s.get('s1_orbit_type') or '') for s in r.get('s1') or [])
        e['meta'][fn] = {'subsystem': 'SAT', 'call_sign': r.get('call_sign') or '', 'file_number': fn,
                         'filing_type': r.get('filing_type') or '', 'state': r.get('state') or '',
                         'desc': ' '.join(s.get('s1_description') or '' for s in r.get('s1') or [])}
        if r.get('state') == 'Current':
            e['current'] = True  # whatever the action code (DFG, D/G...): the authorization is in force
        if any_grant and not sta:
            term = parse_date(r.get('term_end'))
            if term and (r.get('filing_type') or '') != 'Launch Authority':
                e['terms'].append((term, fn))  # term extensions are often recorded under RTS/DFG/D/G/SUR
            e['last'] = max(e['last'], (any_grant, r.get('filing_type') or ''))
        if granted:
            if not sta:
                e['grants'].append((granted, fn))
                locs = []
                for s in r.get('s1') or []:
                    for o in s.get('s3') or []:
                        try:
                            v = float(o.get('s3_orbital_longitude'))
                        except (TypeError, ValueError):
                            continue
                        locs.append(-v if (o.get('s3_hemisphere') or '').upper().startswith('W') else v)
                if locs:
                    e['locs'].append((granted, fn, locs))
        if action in ('SUR', 'RVC'):
            d = parse_date(r.get('last_action_date')) or parse_date(r.get('grant_date'))
            if d:
                e['ends'].append((d, fn, action))
    by_name, by_fuzzy = collections.defaultdict(set), collections.defaultdict(set)
    for cs, e in by_cs.items():
        for n in e['names'] | {cs}:
            by_name[norm(n)].add(cs)
            by_fuzzy[norm(STATUS_NOTE.sub('', n))].add(cs)
    return {'cs': dict(by_cs), 'norm_cs': {norm(cs): cs for cs in by_cs}, 'name': by_name, 'fuzzy': by_fuzzy}


def match(p, idx):
    """(call sign, how) or (None, why)."""
    if norm(p.get('poc_satellite')) == PERMITTED or norm(p.get('poc_common_name')) == 'PERMITTEDLIST':
        return None, 'permitted_list'
    hits = set()
    for f in NAME_FIELDS:
        text = (p.get(f) or '').upper()
        for tok in re.findall(r'[A-Z0-9]+(?:-[A-Z0-9]+)?(?:/\d+)*', text):
            head, *more = tok.split('/')
            prefix = re.match(r'[A-Z-]*', head).group(0)
            for t in [head] + [prefix + m for m in more]:
                if len(norm(t)) >= 4 and re.search(r'\d', t) and re.search(r'[A-Z]', t) and norm(t) in idx['norm_cs']:
                    hits.add(idx['norm_cs'][norm(t)])
    if len(hits) == 1:
        return hits.pop(), 'call_sign'
    if hits:
        return None, 'ambiguous'
    for how, table, key in (('name', idx['name'], norm), ('fuzzy', idx['fuzzy'], lambda s: norm(STATUS_NOTE.sub('', s or '')))):
        found = set()
        for f in NAME_FIELDS:
            k = key(p.get(f))
            if len(k) >= 3:
                found |= table.get(k, set())
        if len(found) == 1:
            return found.pop(), how
        if found:
            return None, 'ambiguous'
    return None, 'unmatched'


def location_at(e, date):
    """The (file, locations) of the latest non-STA grant on or before date (same-day grants combined), or None."""
    held = [x for x in e['locs'] if x[0] <= date]
    if not held:
        return None
    top = max(x[0] for x in held)
    same = sorted(x for x in held if x[0] == top)
    return same[-1][1], sorted({l for x in same for l in x[2]})


def es_date(r):
    """Earth-station date: grant date, but filing date when grant_date is a migration stamp."""
    if (r.get('grant_date') or '')[:10] in BULK:
        return filing_date(r) or when(r)
    return when(r)


def transmits(r):
    return any((o.get('ofb_trans_mode') or '').startswith('Earth-to-Space')
               for sb in r.get('schedule_b') or [] for s in sb.get('site') or []
               for a in s.get('esaf') or [] for o in a.get('ofb') or [])


def check(r, idx, ctx=None):
    """Yield (rule, confidence, before_file, before_value, after_value, expected, detail, target) for one SES
    filing; target is None (this filing) or the SAT filing's meta when the SAT record is the one to repair.
    ctx: {'poc_cs': call signs with a poc on some granted filing, 'latest': {call sign: latest granted file}}."""
    ctx = ctx or {}
    fn, cs_es, state = r.get('file_number') or '', r.get('call_sign') or '', r.get('state') or ''
    pocs = [p for sb in r.get('schedule_b') or [] for s in sb.get('site') or [] for p in s.get('poc') or []]
    if not pocs:
        if r.get('last_action') == 'GRA' and transmits(r):
            detail = 'granted with an Earth-to-Space band, no point of communication on any site'
            if not (r.get('grant_date') or '').strip():
                return  # GRA without a grant date: still pending, not granted
            elsewhere = cs_es in ctx.get('poc_cs', set())
            if state == 'Current' and not elsewhere:
                yield 'poc_list_empty', 'high', '', '', '', '', detail + '; no filing of the call sign lists one', None
            elif (state != 'Current' and elsewhere) or r.get('filing_type') in ('Amendment', 'Special Temporary Authority'):
                yield 'poc_list_empty', 'low', '', '', '', '', BENIGN + ('superseded history: another granted filing of '
                    'the call sign lists points of communication' if elsewhere and state != 'Current' else
                    'partial filing (amendment/STA)') + '; ' + detail, None
            else:
                yield 'poc_list_empty', 'medium', '', '', '', '', detail, None
        return
    date = es_date(r)
    fdate = filing_date(r)
    permitted = any(norm(p.get('poc_satellite')) == PERMITTED for p in pocs)
    named = [p for p in pocs if norm(p.get('poc_satellite')) != PERMITTED]
    retired = [p for p in named if RETIRED.search(' '.join(p.get(f) or '' for f in NAME_FIELDS))]
    if retired and state == 'Current':
        names = '; '.join(sorted({(p.get('poc_satellite') or p.get('poc_common_name') or '').strip() for p in retired}))
        latest = ctx.get('latest', {}).get(cs_es, fn) == fn
        strong = any(STRONG_RETIRED.search(' '.join(p.get(f) or '' for f in NAME_FIELDS)) for p in retired)
        if len(retired) == len(named) and not permitted and latest and strong:
            yield ('poc_names_retired_sat', 'medium', '', '', names, '', 'every listed satellite is marked gone and the '
                   'filing has no Permitted List entry: refer for a points-of-communication modification', None)
        else:
            why = ('not the latest granted filing of the call sign' if not latest else
                   'the filing also lists the Permitted Space Station List' if permitted else
                   'other usable satellites are listed' if len(retired) < len(named) else
                   'the label says surrendered / do not use, not that the satellite left orbit')
            yield ('poc_names_retired_sat', 'low', '', '', names, '', BENIGN + why + '; FCC picklist labels are '
                   'applied after the fact', None)
    for p in pocs:
        name = p.get('poc_satellite') or p.get('poc_itu_name') or p.get('poc_common_name') or ''
        raw = (p.get('poc_orbit_location') or '').replace('\ufffd', '?').strip()
        after = '%s @ %s' % (name, raw or '(blank)')
        text = ' '.join(p.get(f) or '' for f in NAME_FIELDS)
        kind, v = parse_orbit(raw)
        cs, how = match(p, idx)
        e = idx['cs'].get(cs) if cs else None
        held = location_at(e, date) if e else None
        conf = MATCH_CONFIDENCE.get(how, 'low')
        tag = 'poc site %s; %s' % (p.get('poc_site') or '?', 'matched %s by %s' % (cs, how) if cs else how)
        before_file, before_value, expected = '', '', ''
        if held:
            before_file, before_value = held[0], ' / '.join(fmt_lon(x) for x in held[1])
            expected = before_value
        if kind == 'blank':
            if how == 'permitted_list':
                continue
            ok_name = len(norm(name)) >= 3
            real = (ok_name and (state == 'Current' or state.startswith('Pending')) and fdate and fdate.year >= 1991
                    and not GROUP.search(name.strip()) and not (e and e['ngso']))
            if real:
                yield 'poc_orbit_blank', 'medium', before_file, before_value, after, expected or ('NGSO' if e and e['ngso'] else ''), \
                    tag + '; Current/pending filing names one satellite with no orbit location', None
            else:
                why = ('empty or junk poc name' if not ok_name else 'group/region placeholder' if GROUP.search(name.strip())
                       else 'NGSO satellite' if e and e['ngso'] else 'closed pre-1991 legacy license' if fdate and fdate.year < 1991
                       else 'closed filing')
                yield 'poc_orbit_blank', 'low', '', '', name.strip() or '(no name)', '', BENIGN + why, None
        elif kind == 'bad':
            src = p.get('poc_satellite') or p.get('poc_itu_name') or ''
            m = LON_TOKEN.search(src)
            if src.strip().startswith(('@', 'R, ')) or m:
                exp = ('%s %s' % (m.group(1), 'W.L.' if m.group(2).upper().startswith('W') else 'E.L.')) if m else ''
                yield 'poc_orbit_unparseable', 'medium', before_file, before_value, after, exp or expected, \
                    tag + '; field shift: the longitude sits in the name field; restore the name from poc_common_name ' \
                    '(%s)' % (p.get('poc_common_name') or '?'), None
            else:
                yield 'poc_orbit_unparseable', 'low', before_file, before_value, after, expected, BENIGN + \
                    'satellite name or service area copied into the orbit field (legacy conversion): the orbit is ' \
                    'missing; ' + tag, None
        elif kind == 'no_hemisphere':
            tail = next((HEMI_TAIL.search(p.get(f) or '') for f in ('poc_satellite', 'poc_itu_name')
                         if HEMI_TAIL.search(p.get(f) or '')), None)
            if tail:
                hemi = 'W.L.' if tail.group(1).upper().startswith('W') else 'E.L.'
                yield 'poc_orbit_no_hemisphere', 'high', before_file, before_value, after, '%g %s' % (v, hemi), \
                    tag + '; legacy field split: the hemisphere is in the name field; restore the name from ' \
                    'poc_common_name (%s)' % (p.get('poc_common_name') or '?'), None
            elif v > 180:
                yield 'poc_orbit_no_hemisphere', 'medium', before_file, before_value, after, expected, \
                    tag + '; legacy 0-360 value with no hemisphere: ambiguous', None
            else:
                yield 'poc_orbit_no_hemisphere', 'high', before_file, before_value, after, expected, tag + (
                    '; expected from the SAT grant is a hint only (it may be out of date)' if expected else
                    '; add E.L./W.L. from the satellite\'s known position'), None
        elif kind == 'out_of_range':
            yield 'poc_orbit_out_of_range', 'high', before_file, before_value, after, expected, tag, None
        elif kind == 'ok' and held:
            if min(gap(v, x) for x in held[1]) > ORBIT_TOL_DEG:
                flip = min(gap(-v, x) for x in held[1]) <= ORBIT_TOL_DEG
                detail = '%s; poc reads %s, SAT granted %s as of %s' % (tag, fmt_lon(v), before_value, date)
                if flip:
                    earlier = [x for x in e['locs'] if x[1] != held[0] and min(gap(v, l) for l in x[2]) <= ORBIT_TOL_DEG]
                    meta = e['meta'].get(held[0], {})
                    if earlier and not re.search(r'relocat|move|drift', meta.get('desc', ''), re.I):
                        yield ('poc_orbit_hemisphere_flip', 'medium', earlier[-1][1], ' / '.join(fmt_lon(l) for l in earlier[-1][2]),
                               before_value, fmt_lon(v), 'the SAT Schedule S hemisphere is the suspect: earlier grant %s '
                               'and earth station %s give %s, and %s describes no relocation' % (
                                   earlier[-1][1], fn, fmt_lon(v), held[0]), meta)
                        continue
                    yield 'poc_orbit_hemisphere_flip', conf, before_file, before_value, after, expected, detail, None
                    continue
                ever = [l for x in e['locs'] for l in x[2]]
                if RETIRED.search(text) or re.search(r'\bOLD\b|EXPIRED', text, re.I):
                    yield 'poc_orbit_mismatch', 'low', before_file, before_value, after, '', BENIGN + 'the poc is labelled '\
                        'retired/old: historical slot kept on the list; ' + detail, None
                elif (r.get('filing_type') or '').startswith('Special Temporary'):
                    yield 'poc_orbit_mismatch', 'low', before_file, before_value, after, '', BENIGN + 'STA pointing at an '\
                        'interim or transition slot; ' + detail, None
                elif min(gap(v, l) for l in ever) <= ORBIT_TOL_DEG:
                    yield 'poc_orbit_mismatch', 'low', before_file, before_value, after, '', BENIGN + 'the satellite held '\
                        'this slot at another time: it moved later (or earlier); ' + detail, None
                else:
                    yield 'poc_orbit_mismatch', 'medium', before_file, before_value, after, '', detail + '; review: ICFS '\
                        'SAT data often lacks the pre-2017 slot, so this may be the earlier operating location', None
        if e and date != datetime.date.min:
            ends = [x for x in e['ends'] if x[0] < date]
            if ends:
                end = max(ends)
                if not any(g[0] > end[0] for g in e['grants']):
                    detail = '%s; earth station dated %s' % (tag, date)
                    why = ('the poc text already marks the satellite as gone' if RETIRED.search(text) else
                           'federal TDRS satellite: the FCC commercial authorization ended, not the satellite'
                           if cs.startswith('TDRS') else 'bulk administrative PDR/PPL surrender of 2020-07-15'
                           if str(end[0]) == '2020-07-15' else '')
                    if why:
                        yield 'poc_sat_surrendered', 'low', end[1], '%s %s' % (end[2], end[0]), after, '', BENIGN + why + '; ' + detail, None
                    else:
                        live = state == 'Current' or state.startswith('Pending')
                        yield ('poc_sat_surrendered', 'medium' if live else 'low', end[1], '%s %s' % (end[2], end[0]),
                               after, 'remove or replace the poc' if live else '', detail + (
                                   '' if live else '; closed filing: historical only'), None)
                    continue
            if e['terms'] and not e['current']:
                last = max(e['terms'])
                if last[0] < date:
                    detail = '%s; earth station dated %s' % (tag, date)
                    live = state == 'Current' and r.get('last_action') == 'GRA'
                    foreign = 'Declaratory' in e['last'][1] or 'PDR' in e['last'][1]
                    if live and not cs.startswith('TDRS') and not foreign and not RETIRED.search(text):
                        yield ('poc_sat_expired', 'medium', last[1], 'term_end %s' % last[0], after,
                               'remove or annotate the stale poc at the next modification/renewal', detail, None)
                    else:
                        why = ('the poc text already marks the satellite as gone' if RETIRED.search(text) else
                               'federal TDRS satellite' if cs.startswith('TDRS') else
                               'foreign market-access satellite: its PDR/PPL term does not show its status' if foreign
                               else 'earth-station filing is not a Current grant (superseded, dismissed or pending)')
                        yield 'poc_sat_expired', 'low', last[1], 'term_end %s' % last[0], after, '', BENIGN + why + '; ' + detail, None


def scan(ses, idx, ctx=None):
    seen = set()
    for r in ses:
        fn = r.get('file_number') or ''
        collapsed = collections.OrderedDict()
        for rule, conf, bf, bv, av, exp, detail, target in check(r, idx, ctx):
            t = target or {'subsystem': r.get('subsystem') or 'SES', 'call_sign': r.get('call_sign') or '',
                           'file_number': fn, 'filing_type': r.get('filing_type') or '', 'state': r.get('state') or ''}
            if rule in ('poc_orbit_blank', 'poc_names_retired_sat') and detail.startswith(BENIGN):
                c = collapsed.setdefault((rule, detail), {'conf': conf, 'names': [], 't': t})
                for n in av.split('; '):
                    if n not in c['names']:
                        c['names'].append(n)
                continue
            key = (rule, t['file_number']) if target else (rule, fn, bf, av)
            if key in seen:
                continue  # the same satellite listed on several sites of one filing
            seen.add(key)
            yield {'issue_class': CLASS[rule], 'rule': rule, 'confidence': conf, 'subsystem': t['subsystem'],
                   'call_sign': t['call_sign'], 'file_number': t['file_number'], 'filing_type': t['filing_type'],
                   'state': t['state'], 'before_file': bf, 'before_value': bv, 'after_file': t['file_number'],
                   'after_value': av, 'expected_value': exp, 'detail': detail}
        for (rule, detail), c in collapsed.items():
            t = c['t']
            yield {'issue_class': CLASS[rule], 'rule': rule, 'confidence': c['conf'], 'subsystem': t['subsystem'],
                   'call_sign': t['call_sign'], 'file_number': t['file_number'], 'filing_type': t['filing_type'],
                   'state': t['state'], 'before_file': '', 'before_value': '', 'after_file': t['file_number'],
                   'after_value': '; '.join(c['names']), 'expected_value': '',
                   'detail': detail + ' (%d poc entr%s in this filing)' % (len(c['names']), 'y' if len(c['names']) == 1 else 'ies')}


def context(ses):
    """Call signs with a poc on some granted filing, and each call sign's latest granted filing."""
    poc_cs, latest = set(), {}
    for r in ses:
        cs = r.get('call_sign') or ''
        if not cs or r.get('last_action') not in ('GRA', 'CON'):
            continue
        if any(s.get('poc') for sb in r.get('schedule_b') or [] for s in sb.get('site') or []):
            poc_cs.add(cs)
        k = (when(r), r.get('file_number') or '')
        if cs not in latest or k > latest[cs][0]:
            latest[cs] = (k, r.get('file_number') or '')
    return {'poc_cs': poc_cs, 'latest': {cs: v[1] for cs, v in latest.items()}}


def load(conn, subsystem):
    for (raw,) in conn.execute('SELECT raw FROM filings WHERE subsystem = ?', (subsystem,)):
        yield json.loads(zlib.decompress(raw))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--db', default=os.path.join(HERE, 'data', 'icfs.db'))
    ap.add_argument('--out', default=os.path.join(HERE, 'reports', 'poc.csv'))
    args = ap.parse_args()
    conn = sqlite3.connect('file:%s?mode=ro' % args.db, uri=True)
    idx = sat_index(load(conn, 'SAT'))
    counts = collections.Counter()
    with open(args.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, COLUMNS)
        w.writeheader()
        for row in scan(load(conn, 'SES'), idx, context(load(conn, 'SES'))):
            w.writerow(row)
            counts[row['rule'], row['confidence']] += 1
    for (rule, conf), n in sorted(counts.items()):
        print('%-28s %-7s %7d' % (rule, conf, n))
    print('total %d rows -> %s' % (sum(counts.values()), args.out))


if __name__ == '__main__':
    main()
