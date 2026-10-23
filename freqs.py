"""Check earth-station Schedule B frequency data, within each filing and across each call
sign's history.  Reads data/icfs.db (see harvest.py).

    python freqs.py [--db data/icfs.db] [--out reports/frequencies.csv]

Within a filing: band edges, units, transmit direction against well-known band plans,
antenna gain against dish size (at the gain's own frequency), per-carrier EIRP against the
antenna maximum, emission designators, broken site/antenna links, duplicate bands.
Emission findings are one row per (filing, rule, designator) with every location listed;
gain findings one row per antenna; the rest one row per distinct band/site.
Across a call sign: a Renewal cannot change technical parameters, so it is compared with the
authorization in force when it was filed: the latest granted License / Modification / MFS
Modification / Registration granted before the renewal's filing date (an earlier Renewal only
when no such filing exists).  before_file / before_value name that baseline.
Rows the verification judged not to need repair keep confidence low and a detail starting
"likely benign: <why>".  Columns: the original eight, then issue_class, confidence, subsystem,
before_file, before_value, after_file, after_value, expected_value.  Stdlib only.
"""
import argparse
import collections
import csv
import datetime
import math
import os
import re
import sqlite3

from profiler import FILE_NUMBER, HERE, parse_date, records

SPEED_OF_LIGHT = 299792458.0
DISH_EFFICIENCY = 0.65
GAIN_SLACK_DB = 10.0     # catches unit slips (cm for m, GHz for MHz), not design choices
MIN_DISH_M = 0.3         # smaller 'diameters' are flat panels and omnis: no textbook gain
PARAM_SLACK_DB = 0.5
EXAMPLES = 6
# (lower MHz, upper MHz, direction) for bands with a single allowed direction for FSS earth stations
BAND_PLAN = [(3700, 4200, 'Space-to-Earth'), (5925, 6425, 'Earth-to-Space'), (10700, 12750, 'Space-to-Earth'),
             (13750, 14500, 'Earth-to-Space'), (17800, 20200, 'Space-to-Earth'), (27500, 31000, 'Earth-to-Space')]
MSS_FEEDER = (19300, 19700)  # legitimate uplinks inside the 17.8-20.2 GHz downlink plan
TECHNICAL = ('License', 'Modification', 'MFS Modification', 'Registration of a receive-only earth station', 'Renewal')
NO_TECH_CHANGE = ('Renewal',)  # assignments and transfers carry no Schedule B (6 of 8,513 do)
# ITU emission designator: necessary bandwidth (3 digits + H/K/M/G in any of the 4 places), then
# modulation, nature of signal, type of information, optional 4th/5th characters (RR Appendix 1)
BW = r'(\d{3}[HKMG]|\d{2}[HKMG]\d|\d[HKMG]\d{2}|[HKMG]\d{3})'
EMISSION = re.compile(r'^%s[A-Z][0-9X][A-Z]([A-HJ-NWX][CFNTWX]?)?$' % BW)
LEGACY_EMISSION = re.compile(r'^\d+(\.\d+)?[A-Z]\d[A-Z]?$')  # pre-1982 style: kHz then class, e.g. 36000F9
NO_BANDWIDTH = ('N0N', 'NON')
BANDWIDTH_UNIT = {'H': 1e-6, 'K': 1e-3, 'M': 1.0, 'G': 1e3}
SUMMARY = re.compile(r'^\s*([\d.]+)\s*MHz\s*-\s*([\d.]+)\s*MHz')
PILOT = re.compile(r'cw|beacon|unmod|carrier|continuous|calib|track|pilot|tone|test|telemetry|ranging|afc|csm|monitor|iot', re.I)
BENIGN = 'likely benign: '
NOT_GRANTED = ('DIS', 'DDA', 'RNA', 'WTD', 'D/A', 'DEN', 'DFG', 'DNV')
C_BAND_ORDER = datetime.date(2020, 4, 1)

CLASS = {
    'band_lower_above_upper': 'SES band edges', 'band_out_of_range_units': 'SES band edges',
    'band_stub_zero_edges': 'SES band edges', 'band_edge_missing': 'SES band edges',
    'schedule_b_empty_placeholder': 'SES band edges', 'duplicate_band_on_antenna': 'SES band edges',
    'summary_text_disagrees': 'SES band edges', 'direction_against_band_plan': 'SES band direction',
    'emission_legacy_format': 'Emission designators', 'emission_trailing_punctuation': 'Emission designators',
    'emission_letter_o_for_zero': 'Emission designators', 'emission_lowercase': 'Emission designators',
    'emission_designator_malformed': 'Emission designators', 'emission_without_bandwidth': 'Emission designators',
    'emission_wider_than_band': 'Antenna parameters', 'gain_vs_dish_size': 'Antenna parameters',
    'gain_missing': 'Antenna parameters', 'carrier_eirp_above_antenna_max': 'Antenna parameters',
    'site_without_antenna': 'Missing technical data', 'antenna_without_band': 'Missing technical data',
    'no_schedule_b_on_renewal': 'Missing technical data', 'carrier_linked_to_other_antenna': 'Missing technical data',
    'bands_added': 'Renewal vs baseline', 'bands_dropped': 'Renewal vs baseline', 'emissions_changed': 'Renewal vs baseline',
    'eirp_changed': 'Renewal vs baseline', 'gain_changed': 'Renewal vs baseline', 'antenna_set_changed': 'Renewal vs baseline',
    'polarization_changed': 'Renewal vs baseline',
}
CONF = {  # default (verified) confidence per rule
    'band_lower_above_upper': 'high', 'band_out_of_range_units': 'high', 'band_stub_zero_edges': 'medium',
    'band_edge_missing': 'medium', 'schedule_b_empty_placeholder': 'low', 'duplicate_band_on_antenna': 'medium',
    'summary_text_disagrees': 'medium', 'direction_against_band_plan': 'high', 'emission_legacy_format': 'low',
    'emission_trailing_punctuation': 'medium', 'emission_letter_o_for_zero': 'high', 'emission_lowercase': 'medium',
    'emission_designator_malformed': 'medium', 'emission_without_bandwidth': 'low', 'emission_wider_than_band': 'low',
    'gain_vs_dish_size': 'medium', 'gain_missing': 'medium', 'carrier_eirp_above_antenna_max': 'high',
    'site_without_antenna': 'medium', 'antenna_without_band': 'high', 'no_schedule_b_on_renewal': 'medium',
    'carrier_linked_to_other_antenna': 'medium', 'bands_added': 'medium', 'bands_dropped': 'medium',
    'emissions_changed': 'medium', 'eirp_changed': 'medium', 'gain_changed': 'medium', 'antenna_set_changed': 'low',
    'polarization_changed': 'medium',
}
GROUPED = ('emission_legacy_format', 'emission_trailing_punctuation', 'emission_letter_o_for_zero', 'emission_lowercase',
           'emission_designator_malformed', 'emission_without_bandwidth')
COLUMNS = ['scope', 'rule', 'file_number', 'call_sign', 'filing_type', 'state', 'last_action', 'detail',
           'issue_class', 'confidence', 'subsystem', 'before_file', 'before_value', 'after_file', 'after_value',
           'expected_value']


def num(v):
    try:
        return float(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def fmt_bw(mhz):
    """Necessary bandwidth in MHz as the 4-character designator part (36 -> 36M0, 0.2 -> 200K)."""
    for unit, scale in (('G', 1e3), ('M', 1.0), ('K', 1e-3), ('H', 1e-6)):
        v = mhz / scale
        if v >= 1 or unit == 'H':
            break
    if v >= 999.5:
        return None
    t = ('%.2f' if v < 10 else '%.1f' if v < 100 else '%.0f') % v
    return t.replace('.', unit) if '.' in t else t + unit


def legacy_tail(em):
    """(kHz, class tail) of a kHz-first designator, else None."""
    m = re.match(r'^(\d+(?:\.\d+)?)([A-Z][0-9X][A-Z]?.*)$', em)
    return (m.group(1), m.group(2)) if m else None


def missing_unit(em):
    """A modern designator whose bandwidth unit letter was dropped (1250G7W, 200F1D, 0036000F9)."""
    lt = legacy_tail(em)
    if not lt:
        return False
    khz, tail = lt
    return khz.startswith('0') or tail[0] == 'G' or bool(
        re.match(r'^[A-Z][0-9][A-Z]$', tail) and (tail[0] not in 'AF' or tail[2] in 'DWEXN'))


def normalize_emission(em):
    """The well-formed designator em most likely meant, or '' when it cannot be derived."""
    s = re.sub(r'\s+', '', (em or '').upper())
    s = re.sub(r'^[^0-9A-Z]+', '', s)
    s = re.sub(r'[-.,;]+$', '', s)
    if not s:
        return ''
    # 4-character bandwidth first: the lazy split alone reads 2K4OG1D as 2 + K4O... and never fixes the O
    cands = []
    for pat in (r'^([0-9HKMGO]{4})([A-Z][0-9OX][A-Z].*)$', r'^([0-9HKMGO.]+?)([A-Z][0-9OX][A-Z].*)$'):
        m = re.match(pat, s)
        if m:
            bw, cls = m.group(1).replace('O', '0'), m.group(2)
            cands.append(bw + cls[0] + cls[1].replace('O', '0') + cls[2:])
    if cands:
        s = next((c for c in cands if EMISSION.match(c)), cands[-1])
    if s in ('NON', 'N0N'):
        return 'N0N'
    if EMISSION.match(s):
        return s
    lt = legacy_tail(s)
    if lt and missing_unit(s):
        b = fmt_bw(float(lt[0]) * 1e-3)
        if b and EMISSION.match(b + lt[1]):
            return b + lt[1]
    m = re.match(r'^(\d*)([HKMG])(\d*)([A-Z][0-9X][A-Z].*)$', s)
    if m and (m.group(1) or m.group(3)):
        v = float('%s.%s' % (m.group(1) or '0', m.group(3) or '0')) * BANDWIDTH_UNIT[m.group(2)]
        b = fmt_bw(v) if v > 0 else None
        if b and EMISSION.match(b + m.group(4)):
            return b + m.group(4)
    return ''


def emission_problem(em):
    """None for a well-formed designator, else the rule name for what is wrong with it."""
    if not em or EMISSION.match(em):
        return None
    if LEGACY_EMISSION.match(em):
        return 'emission_designator_malformed' if missing_unit(em) else 'emission_legacy_format'
    if em.upper() in NO_BANDWIDTH:
        return 'emission_without_bandwidth'
    if EMISSION.match(em.rstrip('-.,; ')):
        return 'emission_trailing_punctuation'
    up = em.upper()
    if 'O' in up and EMISSION.match(up.replace('O', '0')[:4] + up[4:].replace('NO', 'N0')):
        return 'emission_letter_o_for_zero'
    if EMISSION.match(up):
        return 'emission_lowercase'
    return 'emission_designator_malformed'


def norm_em(e):
    """Designator for comparisons: punctuation, case and O/0 ignored; N0N carriers dropped (None)."""
    s = normalize_emission(e) or (e or '').strip().strip(' -.').upper()
    if not s or re.fullmatch(r'(\w{4})?N0N|NON|0H00N0N', s):
        return None
    return s


def is_legacy(e):
    return bool(LEGACY_EMISSION.match(e)) or not any(c in BANDWIDTH_UNIT for c in e[:4])


def _bandwidth_mhz(em):
    unit = next(c for c in em[:4] if c in BANDWIDTH_UNIT)
    return float(em[:4].replace(unit, '.')) * BANDWIDTH_UNIT[unit]


def _last_digit_mhz(em):
    unit = next(c for c in em[:4] if c in BANDWIDTH_UNIT)
    after = em[:4].split(unit)[1]
    return 10 ** -len(after) * BANDWIDTH_UNIT[unit]


def norm_pol(b):
    out = set()
    for k in ('ofb_polarization_code', 'ofb_polarization_code_2', 'ofb_polarization_code_3'):
        v = (b.get(k) or '').strip()
        if not v or v == 'Elliptic, Left':  # never with polarization_other text: a decoding artifact
            continue
        out.add('Horizontal and Vertical' if v == 'Linear' else v)
    return out


def merged_width_containing(lo, hi, spans):
    """Width of the merged block (touching or overlapping spans) that contains lo..hi."""
    block = [lo, hi]
    for a, b in sorted(spans):
        if a <= block[1] + 0.001 and b >= block[0] - 0.001:
            block = [min(block[0], a), max(block[1], b)]
    return block[1] - block[0]


def bands_of(r, find=None):
    """Flatten a filing's Schedule B into band dicts, reporting within-filing problems via
    find(rule, where, value='', expected='', conf=None, benign='', note='')."""
    find = find or (lambda *a, **k: None)
    out = []
    ftype = r.get('filing_type') or ''
    action, state = r.get('last_action') or '', r.get('state') or ''
    fdate = filing_date(r)
    sites = [s for sb in r.get('schedule_b') or [] for s in sb.get('site') or []]
    ids_with_antennas = {s.get('site_site_id') or '' for s in sites if s.get('esaf')}
    for site in sites:
        sid = site.get('site_site_id') or ''
        esafs = site.get('esaf') or []
        blank_site = not sid and not any((site.get(k) or '').strip() for k in ('site_lat_deg', 'site_long_deg', 'site_site_city'))
        if not esafs and ftype not in ('Assignment', 'Transfer of Control', 'Extension', 'Special Temporary Authority'):
            where = 'site %s' % sid
            if action in NOT_GRANTED or state.startswith('Pending') or ftype == 'Miscellaneous Filing':
                find('site_without_antenna', where, benign='application not granted (or miscellaneous filing): nothing to restore')
            elif sid in ids_with_antennas:
                find('site_without_antenna', where, note='duplicate_site_stub: the same site id has antennas in this filing; delete the stub')
            elif action in ('GRA', 'SUR', 'CON', 'RVC') or state == 'Current':
                find('site_without_antenna', where, note='missing_antenna_data')
            else:
                find('site_without_antenna', where, conf='low')
        for n, ant in enumerate(esafs, 1):
            aid = (ant.get('esaf_antenna_id') or '').strip()
            awhere = 'site %s antenna %s (#%d)' % (sid, aid, n)
            diameter = num(ant.get('esaf_diameter'))
            max_eirp = num(ant.get('esaf_max_output_eirp'))
            power = num(ant.get('esaf_max_input_power'))
            ofbs = ant.get('ofb') or []
            if not ofbs:
                twin = any((o.get('esaf_antenna_id') or '').strip() == aid and o.get('ofb') for o in esafs if o is not ant)
                exp = '3700-4200 MHz receive (verify)' if ftype.startswith('Registration') else ''
                if 'TEST' in (r.get('applicant_name') or '').upper():
                    find('antenna_without_band', awhere, expected=exp, benign='test account filing')
                elif twin:
                    find('antenna_without_band', awhere, expected=exp, conf='low',
                         note='a twin antenna with the same id carries bands: give each antenna a unique id')
                elif state == 'Closed' and action in NOT_GRANTED:
                    find('antenna_without_band', awhere, expected=exp,
                         benign='never granted (%s): nothing to restore' % action)
                elif (state == 'Closed' and action not in ('GRA', 'CON')) or (fdate and fdate.year < 1998):
                    find('antenna_without_band', awhere, expected=exp, conf='low',
                         note='legacy (pre-1998) filing or one closed without a grant (%s)' % (action or 'no action'))
                else:
                    find('antenna_without_band', awhere, expected=exp)
            parsed, seen, gain_done = [], set(), False
            for b in ofbs:
                lo, hi = num(b.get('ofb_frequency_lower')), num(b.get('ofb_frequency_upper'))
                mode = b.get('ofb_trans_mode') or ''
                raw = '%s-%s' % (b.get('ofb_frequency_lower'), b.get('ofb_frequency_upper'))
                where = '%s %s' % (awhere, raw)
                if lo is None or hi is None:
                    if blank_site and not aid and not (b.get('ofb_frequency_lower') or b.get('ofb_frequency_upper')):
                        find('schedule_b_empty_placeholder', 'Schedule B', value='blank site/antenna/band',
                             benign='empty form shell: drop it or ignore it downstream; technical data is on the parent license')
                    else:
                        find('band_edge_missing', where, value=raw)
                    continue
                emissions = [(p.get('ofbp_emission') or '').strip() for p in b.get('ofbp') or []]
                if lo > hi:
                    if hi < lo / 5:
                        k = 1
                        while hi * 10 ** k <= lo:
                            k += 1
                        exp = '%g-%g (truncated upper edge? check)' % (lo, hi * 10 ** k)
                    else:
                        exp = '%g-%g' % (hi, lo)
                    find('band_lower_above_upper', where, value=raw, expected=exp)
                if lo == 0 and hi == 0:
                    if any(emissions) or len(ofbs) == 1:
                        find('band_stub_zero_edges', where, value=raw, note='0-0 band stub with carriers (or the antenna\'s only band): real edges lost')
                    else:
                        find('band_stub_zero_edges', where, value=raw, benign='empty 0-0 stub beside the antenna\'s real bands')
                elif lo <= 0 or hi <= 30 or hi > 300000:
                    exp = '%g-%g' % (lo * 1000, hi * 1000) if 0 < lo < 100 and 0 < hi < 100 else ''
                    find('band_out_of_range_units', where, value=raw, expected=exp)
                key = (round(lo, 3), round(hi, 3), mode)
                dup = key + (frozenset(norm_pol(b)), b.get('ofb_polarization_code') or '',
                             frozenset(norm_em(e) or e.upper() for e in emissions))
                if dup in seen:
                    find('duplicate_band_on_antenna', where, value=raw, expected='delete the repeated row')
                seen.add(dup)
                m = SUMMARY.match(b.get('ofb_summary') or '')
                if m and (abs(float(m.group(1)) - lo) > 0.001 or abs(float(m.group(2)) - hi) > 0.001):
                    find('summary_text_disagrees', where, value=b.get('ofb_summary'), expected='%g MHz - %g MHz' % (lo, hi))
                if lo <= hi:
                    for plo, phi, pdir in BAND_PLAN:
                        if plo <= lo and hi <= phi and mode and mode != pdir:
                            if mode.startswith('Earth-to-Space') and MSS_FEEDER[0] <= lo and hi <= MSS_FEEDER[1]:
                                continue
                            find('direction_against_band_plan', where, value=mode, expected=pdir,
                                 conf='medium' if hi - lo < 5 else 'high',
                                 note='narrow (<5 MHz) carrier, possibly TT&C: confirm' if hi - lo < 5 else '')
                gain = num(b.get('ofb_gain'))
                gfreq = num(b.get('ofb_gain_frequency'))
                if gfreq and gfreq > 500:
                    gfreq /= 1000.0
                ghz = gfreq or (lo + hi) / 2000.0
                if not gain_done and gain == 0 and diameter and diameter >= MIN_DISH_M:
                    gain_done = True
                    find('gain_missing', awhere, value='ofb_gain=0', note='0 used as "not provided"')
                elif (not gain_done and gain and diameter and diameter >= MIN_DISH_M and lo >= 2000 and hi >= 30
                      and not (gfreq and gfreq < 2) and ghz > 0):
                    expect = 10 * math.log10(DISH_EFFICIENCY * (math.pi * diameter * ghz * 1e9 / SPEED_OF_LIGHT) ** 2)
                    if abs(gain - expect) > GAIN_SLACK_DB:
                        gain_done = True
                        find('gain_vs_dish_size', awhere, value='gain %.1f dBi, diameter %g m' % (gain, diameter),
                             note='at %g GHz a %.2f m dish expects ~%.1f dBi (65%% efficiency)' % (ghz, diameter, expect))
                eirps = [num(p.get('ofbp_eirp')) for p in b.get('ofbp') or []]
                eirps = [e for e in eirps if e is not None]
                if eirps and max_eirp is not None and max_eirp > 0 and round(max(eirps), 2) > round(max_eirp + PARAM_SLACK_DB, 2):
                    top = max(eirps)
                    implied = 10 * math.log10(power) + gain if power and power > 0 and gain is not None else None
                    val = 'carrier %.2f dBW > antenna max %.2f dBW' % (top, max_eirp)
                    imp = '; implied 10log10(%g W) + %.1f dBi = %.1f dBW' % (power, gain, implied) if implied is not None else ''
                    if mode.startswith('Space-to-Earth'):
                        find('carrier_eirp_above_antenna_max', where, value=val, expected='blank the EIRP', conf='low',
                             note='EIRP on a receive (Space-to-Earth) band' + imp)
                    elif implied is not None and abs(top - implied) <= 1.5:
                        find('carrier_eirp_above_antenna_max', where, value=val,
                             expected='esaf_max_output_eirp=%.1f' % implied,
                             conf='low' if top - max_eirp <= 1.0 else None, note='antenna maximum is the wrong field' + imp)
                    elif implied is not None and abs(max_eirp - implied) <= 1.5 and top > implied + 1.5:
                        find('carrier_eirp_above_antenna_max', where, value=val,
                             conf='low' if top - max_eirp <= 1.0 else None,
                             note='carrier ofbp_eirp (or the input power) is the wrong field' + imp)
                    else:
                        find('carrier_eirp_above_antenna_max', where, value=val,
                             conf='low' if top - max_eirp <= 1.0 else None, note='both differ from the implied value' + imp)
                for p in b.get('ofbp') or []:
                    em = (p.get('ofbp_emission') or '').strip()
                    kind = emission_problem(em)
                    if not kind:
                        continue
                    exp = normalize_emission(em)
                    if kind == 'emission_legacy_format':
                        find(kind, where, value=em, benign='pre-1982 kHz-style designator, historically accurate')
                    elif kind == 'emission_trailing_punctuation':
                        base = em.rstrip('-.,; ')
                        if em.endswith('--') and em[:-2] == base:
                            find(kind, where, value=em, expected=base, benign='ITU placeholder dashes for unused 4th/5th characters')
                        elif em == base + '-':
                            find(kind, where, value=em, expected=base, conf='medium')
                        else:
                            find(kind, where, value=em, expected=base, conf='high')
                    elif kind == 'emission_without_bandwidth':
                        mod = p.get('ofbp_modulation') or ''
                        exp = 'N0N' if em.upper() == 'NON' else ''
                        if not mod.strip() or PILOT.search(mod):
                            find(kind, where, value=em, expected=exp, note='modulation %r' % mod,
                                 benign='unmodulated pilot/beacon/test carrier: N0N is the right class')
                        else:
                            find(kind, where, value=em, expected=exp, conf='medium',
                                 note='modulation %r describes traffic: check the real designator' % mod)
                    elif kind == 'emission_designator_malformed' and em.upper() in ('N/A', 'NA', '0') and ftype.startswith('Registration'):
                        find(kind, where, value=em, expected='(blank)', benign='receive-only registration: no carrier')
                    else:
                        find(kind, where, value=em, expected=exp)
                for p in b.get('ofbp') or []:
                    if (p.get('ofbp_antenna') or aid).strip() != aid or (p.get('ofbp_site') or '') not in ('', b.get('ofb_site') or ''):
                        find('carrier_linked_to_other_antenna', where, value=raw)
                        break
                carriers = {}
                for p in b.get('ofbp') or []:
                    e, v = norm_em(p.get('ofbp_emission')), num(p.get('ofbp_eirp'))
                    if e and v is not None:
                        carriers[e] = max(carriers.get(e, v), v)
                parsed.append((lo, hi, mode, emissions, where))
                out.append({'key': key, 'gain': gain, 'eirp': max(eirps) if eirps else None,
                            'emissions': frozenset(e for e in emissions if e),
                            'norm_em': frozenset(x for x in (norm_em(e) for e in emissions) if x),
                            'carriers': carriers, 'diam': diameter, 'pols': norm_pol(b),
                            'pol': b.get('ofb_polarization_code') or ''})
            wider(parsed, find)
    return out


def wider(parsed, find):
    """emission_wider_than_band on one antenna's bands, with touching segments merged."""
    for lo, hi, mode, emissions, where in parsed:
        if lo >= hi or hi < 300:
            continue  # point frequencies are carrier centres; inverted / MHz-unit bands have their own rules
        width = hi - lo
        valid = [e for e in emissions if e and EMISSION.match(e)]
        fits = [e for e in valid if _bandwidth_mhz(e) <= width + _last_digit_mhz(e)]
        for em in sorted(set(valid)):
            bw = _bandwidth_mhz(em)
            if bw - width <= _last_digit_mhz(em):
                continue  # within the designator's 3-significant-digit rounding
            val = '%s (%g MHz) in %g MHz' % (em, bw, width)
            spans = [(a, b) for a, b, m, _, _ in parsed if m == mode and a <= b]
            if bw >= 1000:
                find('emission_wider_than_band', where, value=val, expected=normalize_emission(em[:4].replace('G', 'M') + em[4:]) or '',
                     conf='high', note='gigahertz bandwidth: G probably typed for M or K')
            elif merged_width_containing(lo, hi, spans) + _last_digit_mhz(em) >= bw:
                find('emission_wider_than_band', where, value=val, benign='carrier crosses into a touching segment of the same antenna')
            elif any(em in e2 and b - a + _last_digit_mhz(em) >= bw for a, b, m, e2, _ in parsed if m == mode and (a, b) != (lo, hi)):
                find('emission_wider_than_band', where, value=val,
                     benign='carrier list repeated on a short segment; the carrier sits on a wider band of the antenna')
            elif fits:
                find('emission_wider_than_band', where, value=val, conf='low', note='other carriers on the band fit')
            else:
                find('emission_wider_than_band', where, value=val, conf='medium',
                     note='no listed carrier fits: designator or band edge mistyped')


def summarize(bands):
    """{band key: merged parameters} so two filings compare band by band, ignoring site/antenna numbering."""
    out = {}
    for b in bands:
        m = out.setdefault(b['key'], {'gain': None, 'eirp': None, 'emissions': set(), 'norm_em': set(),
                                      'carriers': {}, 'diam': set(), 'pol': set()})
        for p in ('gain', 'eirp'):
            if b[p] is not None:
                m[p] = b[p] if m[p] is None else max(m[p], b[p])
        m['emissions'] |= b['emissions']
        m['norm_em'] |= b['norm_em']
        for e, v in b['carriers'].items():
            m['carriers'][e] = max(m['carriers'].get(e, v), v)
        if b['diam']:
            m['diam'].add(round(b['diam'], 1))
        m['pol'] |= b['pols']
    return out


def fmt(key):
    return '%g-%g %s' % (key[0], key[1], {'Space-to-Earth': 'S-E', 'Earth-to-Space': 'E-S'}.get(key[2], key[2]))


def fmt_set(keys):
    return ', '.join(fmt(k) for k in sorted(keys)) or '(none)'


def bad_band(k):
    lo, hi, mode = k
    return lo > hi or hi <= 30 or any(plo <= lo and hi <= phi and mode and mode != pdir for plo, phi, pdir in BAND_PLAN)


def compare(prev, cur, find, ctx):
    """Renewal (cur) against its baseline (prev).  ctx: dict(renewal_date, ses_type, amended, base_file)."""
    added, dropped = set(cur) - set(prev), set(prev) - set(cur)
    whole = {'before_value': fmt_set(prev), 'after_value': fmt_set(cur)}
    cband = ctx['renewal_date'] and ctx['renewal_date'] >= C_BAND_ORDER
    amended = ctx['amended']
    if dropped:
        detail = 'dropped ' + fmt_set(dropped)
        moved = [k for k in dropped if any(c[:2] == k[:2] and c[2] != k[2] for c in cur)
                 or any(c[2] == k[2] and c[0] <= k[0] and k[1] <= c[1] for c in cur) or bad_band(k)]
        receive = any(k[2].startswith('Space-to-Earth') for k in cur)
        if cband and all(3700 <= k[0] and k[1] <= 4200 and k[2] == 'Space-to-Earth' for k in dropped) \
                and any(k[:2] == (4000, 4200) and k[2] == 'Space-to-Earth' for k in cur):
            find('bands_dropped', detail, benign='2020 C-band transition (3700-4000 MHz cleared)', **whole)
        elif moved and len(moved) == len(dropped):
            find('bands_dropped', detail + '; the renewal corrects the baseline (direction, units, edge or split)',
                 retarget=True, conf='medium', **whole)
        elif amended:
            find('bands_dropped', detail + '; granted Amendment %s between baseline and renewal' % amended,
                 benign='an amendment changed the station before the renewal', **whole)
        elif 'Transmit/Receive' in ctx['ses_type'] and not receive:
            find('bands_dropped', detail + '; Transmit/Receive station renewed with no Space-to-Earth band',
                 conf='high', expected='restore ' + fmt_set(dropped), **whole)
        else:
            find('bands_dropped', detail, expected='restore ' + fmt_set(dropped), **whole)
    if added:
        detail = 'added ' + fmt_set(added)
        allowed = {(4000, 4200, 'Space-to-Earth'), (3625, 3700, 'Space-to-Earth')}
        notation = dropped and all(any(d[2] == a[2] and abs(d[0] - a[0]) <= 1 and abs(d[1] - a[1]) <= 1 for d in dropped)
                                   for a in added)
        flipped = [a for a in added if any(d[:2] == a[:2] or (abs(d[0] * 1000 - a[0]) < 1 and abs(d[1] * 1000 - a[1]) < 1)
                                         for d in dropped)]
        if ctx['renewal_date'] and ctx['renewal_date'].year >= 2020 and added <= allowed \
                and all(3625 <= d[0] and d[1] <= 4200 and d[2] == 'Space-to-Earth' for d in dropped):
            find('bands_added', detail, benign='2020 C-band transition', **whole)
        elif notation:
            find('bands_added', detail, benign='notation only: edges re-written within 1 MHz', **whole)
        elif not dropped and len(added) == 1 and next(iter(added)) in ((3700, 4200, 'Space-to-Earth'), (11700, 12200, 'Space-to-Earth')):
            find('bands_added', detail, benign='legacy license listed only the transmit band', **whole)
        elif flipped and len(flipped) == len(added):
            find('bands_added', detail + '; same band as the baseline apart from direction or x1000: the baseline is wrong',
                 retarget=True, **whole)
        elif amended:
            find('bands_added', detail + '; granted Amendment %s between baseline and renewal' % amended,
                 benign='an amendment changed the station before the renewal', **whole)
        else:
            find('bands_added', detail, expected='remove ' + fmt_set(added) + ' unless authorized', **whole)
    for k in sorted(set(cur) & set(prev)):
        a, b = prev[k], cur[k]
        if a['gain'] is not None and b['gain'] is not None and abs(a['gain'] - b['gain']) > PARAM_SLACK_DB:
            val = {'before_value': '%s gain %.1f dBi dishes %s' % (fmt(k), a['gain'], sorted(a['diam'])),
                   'after_value': '%s gain %.1f dBi dishes %s' % (fmt(k), b['gain'], sorted(b['diam']))}
            if a['diam'] != b['diam']:
                find('antenna_set_changed', '%s antennas %s -> %s' % (fmt(k), sorted(a['diam']), sorted(b['diam'])), **val)
            else:
                find('gain_changed', '%s %.1f -> %.1f dBi' % (fmt(k), a['gain'], b['gain']), expected='%.1f' % a['gain'], **val)
        diffs = ['%s %.1f -> %.1f' % (e, a['carriers'][e], b['carriers'][e]) for e in sorted(set(a['carriers']) & set(b['carriers']))
                 if abs(a['carriers'][e] - b['carriers'][e]) > PARAM_SLACK_DB]
        if diffs:
            val = {'before_value': '%s %s' % (fmt(k), ', '.join('%s %.1f' % (e, a['carriers'][e]) for e in sorted(a['carriers']))),
                   'after_value': '%s %s' % (fmt(k), ', '.join('%s %.1f' % (e, b['carriers'][e]) for e in sorted(b['carriers'])))}
            deltas = [abs(a['carriers'][e] - b['carriers'][e]) for e in set(a['carriers']) & set(b['carriers'])]
            lower = all(b['carriers'][e] <= a['carriers'][e] for e in set(a['carriers']) & set(b['carriers']))
            year = ctx['renewal_date'].year if ctx['renewal_date'] else 0
            if year and year < 2000 and max(deltas) < 1:
                find('eirp_changed', '%s carrier EIRP %s' % (fmt(k), '; '.join(diffs)), benign='legacy re-entry rounding', **val)
            elif year >= 2017 and lower:
                find('eirp_changed', '%s carrier EIRP %s; declared reduction: check the grant' % (fmt(k), '; '.join(diffs)), **val)
            else:
                find('eirp_changed', '%s carrier EIRP %s' % (fmt(k), '; '.join(diffs)), **val)
        ea, eb = a['norm_em'], b['norm_em']
        if ea and eb and ea != eb:
            val = {'before_value': '%s %s' % (fmt(k), ','.join(sorted(a['emissions']))),
                   'after_value': '%s %s' % (fmt(k), ','.join(sorted(b['emissions'])))}
            detail = '%s %s -> %s' % (fmt(k), ','.join(sorted(ea)), ','.join(sorted(eb)))
            la, lb = all(is_legacy(e) for e in ea), all(is_legacy(e) for e in eb)
            if la != lb or (any(is_legacy(e) for e in ea) != any(is_legacy(e) for e in eb)):
                find('emissions_changed', detail, benign='legacy-to-ITU designator recoding of the same carriers', **val)
            elif eb < ea:
                find('emissions_changed', detail + '; carriers dropped', conf='low', **val)
            else:
                find('emissions_changed', detail, expected=','.join(sorted(ea)), **val)
        if a['pol'] and b['pol'] and a['pol'] != b['pol']:
            find('polarization_changed', '%s %s -> %s' % (fmt(k), '/'.join(sorted(a['pol'])), '/'.join(sorted(b['pol']))),
                 before_value='/'.join(sorted(a['pol'])), after_value='/'.join(sorted(b['pol'])),
                 expected='/'.join(sorted(a['pol'])))


def filing_date(r):
    m = FILE_NUMBER.match(r.get('file_number') or '')
    if m:
        try:
            return datetime.date(int(m.group(3)[:4]), int(m.group(3)[4:6]), int(m.group(3)[6:]))
        except ValueError:
            pass
    return parse_date(r.get('date_filed')) or parse_date(r.get('grant_date'))


def grant_of(r):
    """Grant date, with a stale or bulk-stamped grant_date replaced by last_action_date."""
    gd, ld, fd = parse_date(r.get('grant_date')), parse_date(r.get('last_action_date')), filing_date(r)
    if gd and fd and gd < fd - datetime.timedelta(days=30):
        gd = ld if ld and ld >= fd else None
    if gd and ld and gd > ld + datetime.timedelta(days=1):
        gd = ld
    return gd or ld or fd


def baseline_of(hist, i):
    """Index of the authorization in force when hist[i] (a renewal) was filed, or None."""
    filed = hist[i]['filed'] or hist[i]['grant']
    cands = [j for j, h in enumerate(hist) if j != i and h['summary'] is not None and h['type'] in TECHNICAL
             and h['grant'] and filed and h['grant'] < filed]
    plain = [j for j in cands if hist[j]['type'] != 'Renewal']
    pick = plain or cands
    return max(pick, key=lambda j: (hist[j]['grant'], hist[j]['filed'] or datetime.date.min, hist[j]['fn'])) if pick else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=os.path.join(HERE, 'data', 'icfs.db'))
    ap.add_argument('--out', default=os.path.join(HERE, 'reports', 'frequencies.csv'))
    a = ap.parse_args()
    conn = sqlite3.connect('file:%s?mode=ro' % a.db, uri=True)
    counts, examples = collections.Counter(), collections.defaultdict(list)
    has_sb = collections.Counter()
    by_type = collections.Counter()
    history = collections.defaultdict(list)
    with_antennas = collections.defaultdict(list)  # call sign -> [(date, file)] of filings that list antennas
    rows = []

    def emit(r, scope, rule, detail, conf=None, benign='', expected='', before_file='', before_value='',
             after_value='', file_rec=None):
        f = file_rec or r
        conf = 'low' if benign else (conf or CONF[rule])
        detail = (BENIGN + benign + '; ' if benign else '') + detail
        key = (scope, rule, f.get('filing_type') or '')
        counts[key] += 1
        if len(examples[key]) < EXAMPLES:
            examples[key].append(f.get('file_number'))
        rows.append([scope, rule, f.get('file_number'), f.get('call_sign'), f.get('filing_type'), f.get('state'),
                     f.get('last_action'), detail, CLASS[rule], conf, f.get('subsystem') or 'SES', before_file,
                     before_value, f.get('file_number'), after_value, expected])

    def within(r):
        grouped = collections.OrderedDict()

        def find(rule, where, value='', expected='', conf=None, benign='', note=''):
            gk = (rule, value) if rule in GROUPED else (rule, where) if rule in ('gain_vs_dish_size', 'gain_missing') \
                else (rule, where, value)
            g = grouped.setdefault(gk, {'rule': rule, 'value': value, 'expected': expected, 'conf': conf,
                                        'benign': benign, 'note': note, 'where': []})
            if where not in g['where']:
                g['where'].append(where)
        bands = bands_of(r, find)
        for g in grouped.values():
            detail = '; '.join(x for x in (g['note'], 'at ' + ' | '.join(g['where'])) if x)
            if g['rule'] in GROUPED:
                detail = 'emission %s; %s' % (g['value'], detail)
            emit(r, 'within_filing', g['rule'], detail, g['conf'], g['benign'], g['expected'], after_value=g['value'])
        return bands

    for r in records(conn):
        if r.get('subsystem') != 'SES':
            continue
        by_type[r.get('filing_type') or ''] += 1
        bands = within(r)
        if bands:
            has_sb[r.get('filing_type') or ''] += 1
        if r.get('call_sign') and any(s.get('esaf') for sb in r.get('schedule_b') or [] for s in sb.get('site') or []):
            with_antennas[r['call_sign']].append((filing_date(r) or datetime.date.min, r.get('file_number')))
        if r.get('call_sign') and r.get('last_action') == 'GRA' and not (r.get('state') or '').startswith('Pending'):
            history[r['call_sign']].append({'fn': r.get('file_number'), 'type': r.get('filing_type') or '',
                                            'filed': filing_date(r), 'grant': grant_of(r), 'rec': {
                                                k: r.get(k) for k in ('file_number', 'call_sign', 'filing_type', 'state',
                                                                      'last_action', 'subsystem', 'ses_type_of_earth_station')},
                                            'summary': summarize(bands) if bands else None})

    # nearest other filing of the call sign that lists antennas, for site_without_antenna
    for row in rows:
        if row[1] == 'site_without_antenna' and not row[7].startswith(BENIGN) and row[3]:
            mine = filing_date({'file_number': row[2]}) or datetime.date.min
            others = [x for x in with_antennas.get(row[3], []) if x[1] != row[2]]
            if others:
                near = min(others, key=lambda x: abs((x[0] - mine).days))
                row[15] = 'antenna data as in %s' % near[1]
                row[11] = near[1]

    compared = collections.Counter()
    for cs, hist in history.items():
        hist.sort(key=lambda h: (h['grant'] or datetime.date.min, h['filed'] or datetime.date.min, h['fn']))
        for i, h in enumerate(hist):
            if h['type'] not in NO_TECH_CHANGE:
                continue
            j = baseline_of(hist, i)
            if j is None:
                continue
            base = hist[j]
            r = h['rec']
            if h['summary'] is None:
                legacy = h['filed'] and h['filed'].year < 1983
                late = h['grant'] and h['grant'] >= datetime.date(2025, 1, 1)
                emit(r, 'history', 'no_schedule_b_on_renewal', 'call sign %s; baseline %s %s' % (cs, base['type'], base['fn']),
                     benign='new-ICFS (2025+) renewal record with an empty Schedule B shell' if late else
                     'legacy 1978-82 record' if legacy else '', expected='Schedule B as in %s' % base['fn'],
                     before_file=base['fn'], before_value=fmt_set(base['summary']), after_value='(no Schedule B bands)')
                continue
            compared[h['type']] += 1
            amended = next((x['fn'] for x in hist if x['type'] == 'Amendment' and x['grant'] and base['grant']
                            and base['grant'] < x['grant'] and (h['filed'] is None or x['grant'] <= h['filed'])), '')

            def find(rule, detail, conf=None, benign='', expected='', before_value='', after_value='', retarget=False,
                     r=r, base=base, h=h):
                if retarget:  # the baseline holds the error: flag it, the renewal is the other version
                    emit(r, 'history', rule, 'baseline %s %s is the suspect; renewal %s: %s' % (
                        base['type'], base['fn'], h['fn'], detail), conf, benign, expected, h['fn'], after_value,
                        before_value, file_rec=base['rec'])
                else:
                    emit(r, 'history', rule, 'vs baseline %s %s (granted %s): %s' % (
                        base['type'], base['fn'], base['grant'], detail), conf, benign, expected, base['fn'],
                        before_value, after_value)
            compare(base['summary'], h['summary'], find, {
                'renewal_date': h['grant'], 'ses_type': r.get('ses_type_of_earth_station') or '', 'amended': amended})

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)
    md = ['# Earth-station Schedule B frequency checks', '',
          '## Schedule B presence by filing type', '',
          '| filing type | filings | with Schedule B bands |', '|---|---|---|']
    md += ['| %s | %d | %d |' % (t, n, has_sb[t]) for t, n in by_type.most_common()]
    md += ['', 'Granted renewals compared with the baseline in force when filed: %s' % dict(compared), '',
           '## Findings', '', '| scope | rule | filing type | count | examples |', '|---|---|---|---|---|']
    md += ['| %s | %s | %s | %d | %s |' % (k[0], k[1], k[2], n, ', '.join(examples[k]))
           for k, n in sorted(counts.items(), key=lambda x: (x[0][0], -x[1]))]
    with open(os.path.splitext(a.out)[0] + '.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(md) + '\n')
    print('wrote %s (%d rows)' % (a.out, len(rows)))
    by = collections.Counter((x[1], x[9], x[7].startswith(BENIGN)) for x in rows)
    for (rule, conf, benign), n in sorted(by.items()):
        print('  %-34s %-6s %-6s %7d' % (rule, conf, 'benign' if benign else '', n))


if __name__ == '__main__':
    main()
