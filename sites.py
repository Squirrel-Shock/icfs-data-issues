"""Location and identity checks over every ICFS filing: one CSV row per affected filing per issue.

    python sites.py [--db data/icfs.db] [--out reports/sites.csv]

Locations (only SES Schedule B sites carry coordinates; no other subsystem does):
  coords_missing / coords_zero        site with blank or all-zero coordinates
  coords_hemisphere_missing           degrees present but hemisphere blank or 'X'
  coords_minutes_seconds_60           minutes or seconds >= 60 (always exactly 60 in practice)
  coords_out_of_range                 latitude > 90 or longitude > 180
  longitude_hemisphere / latitude_hemisphere   sign error: the sign-flipped point lies in the
                                      declared state (high) or the sign cannot be right for it (medium)
  coords_outside_state                point outside the declared state's bounding box, not a sign slip
  state_code_unknown                  site_site_state is not a US state/territory code
  elevation_implausible               elevation < -100 m or > 5,000 m
  elevation_feet_suspected            ~3.28 x the elevation another filing gives the same site
Identity (every subsystem, per (subsystem, call sign), granted filings in filing-date order):
  frn_and_name_changed / frn_changed / name_changed   applicant changes between consecutive
                                      granted filings with no granted Assignment/Transfer between
  call_sign_stray_characters, call_sign_letter_o, call_sign_placeholder,
  call_sign_foreign_format, call_sign_unusual_shape   shape checks against the measured norm
  call_sign_multi_subsystem           the same call sign in two subsystems
  frn_shared_placeholder              every filing carrying an FRN used by 10+ distinct applicant names
Expected values come from the same call sign's latest granted technical filing for the same site
when one exists ("sibling"); otherwise from the rule itself (sign flip, 60 carried) or blank.
Rows the verification judged not to need repair (blanket/mobile sites, placeholders on transfer
filings, legacy call-sign shapes, licensees with several FRNs...) keep confidence low and a detail
starting "likely benign: <why>".  Identity changes get one row on the filing to repair (the later
one unless the earlier one is shown to be the odd one out).
Stdlib only; contact details are never read or written.
"""
import argparse
import collections
import csv
import datetime
import os
import re
import sqlite3

from freqs import TECHNICAL, filing_date, num
from profiler import HERE, records
from walk import km

COLUMNS = ['issue_class', 'rule', 'confidence', 'subsystem', 'call_sign', 'file_number', 'filing_type', 'state',
           'before_file', 'before_value', 'after_file', 'after_value', 'expected_value', 'detail']
SLACK = 0.3
FEET = 3.28084
FEET_BAND = (3.18, 3.38)
MIN_SIBLING_ELEV_M = 3.0
GRANTED = ('GRA', 'CON')
TRANSFERS = ('Assignment', 'Transfer of Control')
NON_TECH = TRANSFERS + ('Special Temporary Authority', 'Extension', 'Amendment', 'AFS - Amendment',
                        'Miscellaneous Filing', 'Temporary Authority')

# (lat_min, lat_max, lon_min, lon_max), west negative; Alaska as two boxes either side of 180.
BBOX = {
    'AL': [(30.14, 35.01, -88.47, -84.89)], 'AK': [(51.2, 71.4, -180.0, -129.98), (51.2, 53.0, 172.4, 180.0)],
    'AZ': [(31.33, 37.00, -114.82, -109.04)], 'AR': [(33.00, 36.50, -94.62, -89.64)],
    'CA': [(32.53, 42.01, -124.48, -114.13)], 'CO': [(36.99, 41.00, -109.06, -102.04)],
    'CT': [(40.95, 42.05, -73.73, -71.79)], 'DE': [(38.45, 39.84, -75.79, -75.05)],
    'DC': [(38.79, 39.00, -77.12, -76.91)], 'FL': [(24.40, 31.00, -87.63, -80.03)],
    'GA': [(30.36, 35.00, -85.61, -80.84)], 'HI': [(18.91, 28.40, -178.33, -154.81)],
    'ID': [(41.99, 49.00, -117.24, -111.04)], 'IL': [(36.97, 42.51, -91.51, -87.02)],
    'IN': [(37.77, 41.76, -88.10, -84.78)], 'IA': [(40.38, 43.50, -96.64, -90.14)],
    'KS': [(36.99, 40.00, -102.05, -94.59)], 'KY': [(36.50, 39.15, -89.57, -81.96)],
    'LA': [(28.93, 33.02, -94.04, -88.82)], 'ME': [(42.98, 47.46, -71.08, -66.95)],
    'MD': [(37.91, 39.72, -79.49, -75.05)], 'MA': [(41.24, 42.89, -73.51, -69.93)],
    'MI': [(41.70, 48.31, -90.42, -82.12)], 'MN': [(43.50, 49.38, -97.24, -89.49)],
    'MS': [(30.17, 35.00, -91.66, -88.10)], 'MO': [(35.99, 40.61, -95.77, -89.10)],
    'MT': [(44.36, 49.00, -116.05, -104.04)], 'NE': [(40.00, 43.00, -104.05, -95.31)],
    'NV': [(35.00, 42.00, -120.01, -114.04)], 'NH': [(42.70, 45.31, -72.56, -70.61)],
    'NJ': [(38.93, 41.36, -75.56, -73.89)], 'NM': [(31.33, 37.00, -109.05, -103.00)],
    'NY': [(40.50, 45.02, -79.76, -71.86)], 'NC': [(33.84, 36.59, -84.32, -75.46)],
    'ND': [(45.94, 49.00, -104.05, -96.55)], 'OH': [(38.40, 41.98, -84.82, -80.52)],
    'OK': [(33.62, 37.00, -103.00, -94.43)], 'OR': [(41.99, 46.29, -124.57, -116.46)],
    'PA': [(39.72, 42.27, -80.52, -74.69)], 'RI': [(41.15, 42.02, -71.86, -71.12)],
    'SC': [(32.03, 35.22, -83.35, -78.54)], 'SD': [(42.48, 45.95, -104.06, -96.44)],
    'TN': [(34.98, 36.68, -90.31, -81.65)], 'TX': [(25.84, 36.50, -106.65, -93.51)],
    'UT': [(37.00, 42.00, -114.05, -109.04)], 'VT': [(42.73, 45.02, -73.44, -71.46)],
    'VA': [(36.54, 39.47, -83.68, -75.24)], 'WA': [(45.54, 49.00, -124.85, -116.92)],
    'WV': [(37.20, 40.64, -82.64, -77.72)], 'WI': [(42.49, 47.31, -92.89, -86.25)],
    'WY': [(40.99, 45.01, -111.06, -104.05)],
    'PR': [(17.88, 18.53, -67.95, -65.22)], 'VI': [(17.67, 18.42, -65.09, -64.56)],
    'GU': [(13.23, 13.66, 144.61, 145.01)], 'MP': [(14.10, 20.56, 144.89, 146.07)],
    'AS': [(-14.55, -11.04, -171.09, -168.14)],
}
EAST_OK = ('AK', 'GU', 'MP')       # the only codes where an east longitude can be right
SOUTH = ('AS',)
HIGH_STATES = ('CO', 'WY', 'UT', 'NM', 'NV', 'AZ', 'MT', 'ID', 'CA', 'AK')  # 1,500+ m sites are common
STATE_ALIASES = {'NB': 'NE'}       # pre-1969 Post Office code for Nebraska

# Measured norms (see the module docstring of the report); anything else is checked below.
NORMAL = {
    'SES': r'E\d{6}|E\d{4}|[KW][A-Z]\d{2,3}', 'SAT': r'S\d{4,5}|KS\d{2}|S\d{4}/S?\d{4}', 'ITC': r'T\d{6}',
    'SPC': r'C\d{6}', 'SCL': r'S\d{6}', 'ISP': r'P\d{6}', 'ROA': r'R\d{6}', 'DNC': r'D\d{6}',
    'IHF': r'[KW][A-Z]{2,3}', '325': r'\d{9}',
}
NORMAL = {k: re.compile('^(%s)$' % v) for k, v in NORMAL.items()}
BENIGN = 'likely benign: '
PLACEHOLDER = re.compile(r'^(NEW\s*\w*|N/?A|VARIOUS|STA|TBD|NONE|CALL\d+|ESTA\d*|EX-TEST|[A-Z]{1,2}/TEST|E0+)$')
PLACEHOLDER_NAMES = 10  # an FRN used by this many distinct applicant names is a shared placeholder, not a licensee
# Blanket / mobile / remote-terminal sites: no fixed coordinates expected.
MOBILE = re.compile(r'\b(VARIOUS|CONUS|REMOTES?|MOBILE|TRANSPORT\w*|ESVS?|AES|UNITS|OCEANS?|WATERS?|GLOBAL|MULTIPLE|'
                    r'NATIONWIDE|POSSESSIONS|USP|TR|TF|VSATS?|AIRCRAFT|VEHICLES?|MARITIME|THROUGHOUT|BLANKET|ESIM\S*|'
                    r'AIRSPACE|AIRBORNE|VMES|BEAMS?|ALL US|ANTENNA \d+|UT\d)\b', re.I)
# 312_class_of_station values that describe a fixed point (a coordinate is expected); '' = legacy, unknown
FIXED_CLASS = ('FES', 'HUB', 'a. FSS Fixed Earth Station (single location)',
               'b. FSS Fixed Earth Stations ( multiple locations with specified coordinates)', 'i. EESS Fixed Earth Stations')
ANTENNA_SIZE = re.compile(r'^\.?\d+(\.\d+)?\s*M?\b', re.I)
OFFSHORE = re.compile(r'GULF|CANYON|ISLAND\s*\d|OFFSHORE|BLOCK|PLATFORM|REMOTE|EUGENE|CAMERON|TIMBALIER|VERMILION|'
                      r'MISSISSIPPI CANYON|HUB|USNS', re.I)
CORPORATE = re.compile(r'\b(INC|LLC|L\.L\.C|CORP|CORPORATION|COMPANY|CO|LTD|LIMITED|LP|L\.P|LLP|PLC|UNIVERSITY|COLLEGE|'
                       r'STATE|DEPT|DEPARTMENT|CHURCH|S\.A|SA|BOARD|COUNTY|CITY|NETWORK|NETWORKS|COMMUNICATIONS|'
                       r'BROADCASTING|SERVICES|SYSTEMS|TRUST|ASSOCIATION|FOUNDATION|AUTHORITY|GOVERNMENT|AGENCY|'
                       r'ADMINISTRATION|SCHOOL|INSTITUTE|MINISTRIES|HOLDINGS|GROUP|PARTNERS|TELEVISION|TV|RADIO|CABLE|'
                       r'SATELLITE|TECHNOLOGIES|ENTERPRISES|INTERNATIONAL|AMERICA|AMERICAN|OF|THE)\b', re.I)
SUFFIXES = {'INC', 'INCORPORATED', 'LLC', 'LTD', 'LIMITED', 'CORP', 'CORPORATION', 'CO', 'COMPANY', 'LP',
            'LLP', 'PLC', 'THE', 'OF', 'AND', 'NA', 'SA', 'AG', 'GMBH'}


# --- locations -------------------------------------------------------------------------------

def in_state(state, lat, lon):
    return any(a - SLACK <= lat <= b + SLACK and c - SLACK <= lon <= d + SLACK for a, b, c, d in BBOX[state])


def any_state(lat, lon):
    return [s for s in BBOX if in_state(s, lat, lon)]


def fmt(lat, lon):
    def dms(v, pos, neg, width):
        a = abs(v)
        d, rem = int(a), (a - int(a)) * 60
        m, s = int(rem), (rem - int(rem)) * 60
        if round(s, 1) >= 60:
            m, s = m + 1, 0.0
        if m >= 60:
            d, m = d + 1, 0
        return '%0*d %02d %04.1f %s' % (width, d, m, s, pos if v >= 0 else neg)
    return '%s / %s (%.6f, %.6f)' % (dms(lat, 'N', 'S', 2), dms(lon, 'E', 'W', 3), lat, lon)


def raw_coord(s):
    return '%s %s %s %s / %s %s %s %s' % tuple(s.get(k) or '' for k in (
        'site_lat_deg', 'site_lat_min', 'site_lat_sec', 'site_lat_hemi',
        'site_long_deg', 'site_long_min', 'site_long_sec', 'site_long_hemi'))


def parts(s):
    return [num(s.get(k)) for k in ('site_lat_deg', 'site_lat_min', 'site_lat_sec',
                                     'site_long_deg', 'site_long_min', 'site_long_sec')]


def decimal(p, lat_hemi, lon_hemi):
    lat = (p[0] or 0) + (p[1] or 0) / 60.0 + (p[2] or 0) / 3600.0
    lon = (p[3] or 0) + (p[4] or 0) / 60.0 + (p[5] or 0) / 3600.0
    return (-lat if lat_hemi == 'S' else lat), (-lon if lon_hemi == 'W' else lon)


def site_problems(s, state, sibling):
    """[(rule, confidence, after_value, expected, detail, uses_sibling)] for one Schedule B site.
    sibling: (file_number, lat, lon) of the same site in another granted filing, or None."""
    out = []
    p = parts(s)
    lat_h = (s.get('site_lat_hemi') or '').strip().upper()[:1]
    lon_h = (s.get('site_long_hemi') or '').strip().upper()[:1]
    raw = raw_coord(s)
    sib = fmt(sibling[1], sibling[2]) if sibling else ''
    if not any(p):
        what = 'no coordinates' if all(v is None for v in p) else 'coordinates are 0/0'
        if mobile(s, state):
            return [('coords_absent_mobile_site', 'low', raw, '', BENIGN + 'blanket/mobile/remote site has no fixed '
                     'point; at most blank the 0/0 placeholder; ' + what, False)]
        return [('coords_missing' if what == 'no coordinates' else 'coords_zero', None, raw, sib, what, True)]
    if not any(p[3:]) or not any(p[:3]):
        which = 'longitude' if not any(p[3:]) else 'latitude'
        return [('coords_missing', 'medium', raw, sib, '%s blank or 0 0 0 while the %s is present' % (
            which, 'latitude' if which == 'longitude' else 'longitude'), True)]
    if lat_h not in ('N', 'S') or lon_h not in ('E', 'W'):
        guess = []
        for a in ([lat_h] if lat_h in ('N', 'S') else ['N', 'S']):
            for b in ([lon_h] if lon_h in ('E', 'W') else ['W', 'E']):
                la, lo = decimal(p, a, b)
                if state in BBOX and in_state(state, la, lo):
                    guess.append((la, lo))
        if not guess and state not in BBOX:
            guess = [decimal(p, lat_h if lat_h in ('N', 'S') else 'N', lon_h if lon_h in ('E', 'W') else 'W')]
        conf = 'high' if len(guess) == 1 and state in BBOX else 'medium'
        out.append(('coords_hemisphere_missing', conf, raw, fmt(*guess[0]) if len(guess) == 1 else sib,
                    'hemisphere lat=%r long=%r' % (lat_h, lon_h), not guess))
        lat_h = lat_h if lat_h in ('N', 'S') else 'N'
        lon_h = lon_h if lon_h in ('E', 'W') else 'W'
    over = [k for k, v in zip(('lat_min', 'lat_sec', 'long_min', 'long_sec'), (p[1], p[2], p[4], p[5]))
            if v is not None and v >= 60]
    if over:
        if any(v is None for v in p):
            out.append(('coords_minutes_seconds_60', 'low', raw, '', BENIGN + 'junk record with blank coordinate '
                        'parts; %s >= 60' % ', '.join(over), False))
        else:
            out.append(('coords_minutes_seconds_60', 'high', raw, fmt(*decimal(p, lat_h, lon_h)),
                        '%s >= 60; expected carries the overflow (check other filings of the call sign)' % ', '.join(over),
                        False))
    lat, lon = decimal(p, lat_h, lon_h)
    if abs(lat) > 90 or abs(lon) > 180 or any(v is not None and v < 0 for v in p):
        wrap = fmt(lat, lon + 360 if lon < -180 else lon - 360) if abs(lat) <= 90 and 180 < abs(lon) < 360 else ''
        out.append(('coords_out_of_range', 'high', raw, wrap or sib, 'decoded (%.4f, %.4f)%s' % (
            lat, lon, '; expected wraps the longitude across 180 (0-360 convention)' if wrap else ''), not wrap))
        return out
    if state not in BBOX:
        near = any_state(lat, lon)
        if not near and lon > 0 and any_state(lat, -lon):
            out.append(('longitude_hemisphere', 'medium', raw, fmt(lat, -lon),
                        'no state declared; east longitude lies in no US state, west lies in %s'
                        % '/'.join(any_state(lat, -lon)), False))
        return out
    if in_state(state, lat, lon):
        return out
    city = (s.get('site_site_city') or '').upper()
    if 'WAKE' in city or (19.0 <= lat <= 19.5 and 166.0 <= lon <= 167.0):
        return out  # Wake Island: a US possession east of 180, correctly East
    if in_state(state, lon if lon > 0 else -lon, lat if lat < 0 else -lat) and lat > 66 and 24 < abs(lon) < 50:
        out.append(('coords_outside_state', 'high', raw, fmt(abs(lon), -abs(lat)),
                    'latitude and longitude swapped: swapped point falls in %s' % state, False))
        return out
    if in_state(state, lat, -lon):
        out.append(('longitude_hemisphere', 'high', raw, fmt(lat, -lon), 'sign-flipped longitude falls in %s' % state, False))
    elif in_state(state, -lat, lon):
        out.append(('latitude_hemisphere', 'high', raw, fmt(-lat, lon), 'sign-flipped latitude falls in %s' % state, False))
    elif in_state(state, -lat, -lon):
        out.append(('latitude_hemisphere', 'high', raw, fmt(-lat, -lon), 'both signs flipped falls in %s' % state, False))
    elif lon > 0 and state not in EAST_OK and not any_state(lat, lon):
        out.append(('longitude_hemisphere', 'medium', raw, '',
                    'east longitude impossible for %s and the flipped point is still outside it: the whole coordinate '
                    'set (placeholder/test values?) needs research' % state, False))
    elif lat < 0 and state not in SOUTH and not any_state(lat, lon):
        out.append(('latitude_hemisphere', 'medium', raw, '',
                    'south latitude impossible for %s and the flipped point is still outside it: re-enter the whole '
                    'coordinate set' % state, False))
    else:
        inside = [x for x in any_state(lat, lon)]
        good_sib = sibling and in_state(state, sibling[1], sibling[2])
        label = ' '.join(s.get(k) or '' for k in ('site_site_id', 'site_site_city'))
        if state in ('LA', 'TX', 'AL', 'MS', 'FL') and 25.5 <= lat <= 29.6 and -94.5 <= lon <= -87.5 \
                and (not inside or OFFSHORE.search(label)):
            out.append(('coords_outside_state', 'low', raw, '', BENIGN + 'Gulf of Mexico offshore (OCS) site: the '
                        'coordinates are plausible, the state is the nearest coast; declared %s' % state, False))
        else:
            out.append(('coords_outside_state', 'high', raw, sib if good_sib else '',
                        'declared %s; point is in %s: fix the state if the city matches the point, else the coordinates'
                        % (state, '/'.join(inside) or 'no US state box'), bool(good_sib)))
    return out


def mobile(s, state):
    """A blanket / mobile / remote-terminal site: no fixed coordinates expected."""
    city = (s.get('site_site_city') or '').strip().upper()
    label = ' '.join(s.get(k) or '' for k in ('site_site_id', 'site_area_of_operation_code'))
    if city and not (city == 'MOBILE' and state == 'AL'):
        label += ' ' + city
    return bool(MOBILE.search(label) or (s.get('site_area_of_operation_code') or '').strip())


# --- call signs and names --------------------------------------------------------------------

def call_sign_problem(sub, cs, known=frozenset()):
    """(rule, confidence, expected, detail) for an unusual call sign, else None.  known: call signs that
    exist in this subsystem (to confirm a derived correction).  confidence None = decided per filing."""
    norm = NORMAL.get(sub)
    if not cs or norm is None or norm.match(cs):
        return None
    cleaned = re.sub(r'[\s()+]+', '', cs)
    if cleaned != cs and (norm.match(cleaned) or cleaned in known):
        return 'call_sign_stray_characters', 'high', cleaned, 'whitespace/punctuation around a call sign'
    if 'O' in cs and norm.match(cs[0] + cs[1:].replace('O', '0')):
        return 'call_sign_letter_o', 'high', cs[0] + cs[1:].replace('O', '0'), 'letter O where a digit belongs'
    if PLACEHOLDER.match(cs):
        return 'call_sign_placeholder', None, '', 'placeholder, not an assigned call sign'
    other = [k for k, rx in NORMAL.items() if k != sub and rx.match(cleaned) and k != 'IHF']
    if other == ['ITC'] and sub == 'SES' and cs.startswith('T8'):
        return 'call_sign_foreign_format', 'low', '', BENIGN + '1980s temporary (STA) designation T8xxxxx, kept across renewals'
    if other:
        return 'call_sign_foreign_format', None, '', 'shape of a %s call sign' % '/'.join(other)
    prefix = {'SES': 'E', 'SAT': 'S'}.get(sub)
    if prefix and cs.isdigit() and prefix + cs in known:
        return 'call_sign_unusual_shape', 'medium', prefix + cs, 'subsystem prefix missing; %s exists' % (prefix + cs)
    if re.match(r'^E\d{5}$', cs):
        fixes = [cs[:k] + '0' + cs[k:] for k in range(1, 6) if cs[:k] + '0' + cs[k:] in known]
        if len(fixes) == 1:
            return 'call_sign_unusual_shape', 'medium', fixes[0], '5-digit E number; inserting a 0 gives existing %s' % fixes[0]
    m = re.match(r'^(.*\d)[A-Z]$', cs)
    if m and norm.match(m.group(1)):
        return 'call_sign_unusual_shape', 'low', '', BENIGN + 'FCC-assigned companion call sign (normal call sign plus %s); '\
            'do not merge with %s' % (cs[-1], m.group(1))
    if sub == 'SAT' and re.match(r'^[A-Z][A-Z0-9 \-/]*$', cs):
        return 'call_sign_unusual_shape', 'low', '', BENIGN + 'legacy satellite designation (pre S-number), a valid call sign'
    return 'call_sign_unusual_shape', 'medium', '', 'matches no measured %s call sign shape' % sub


def multi_subsystem_suspects(cs, counts):
    """({subsystems whose filings likely carry the wrong call sign}, reason) for a shared call sign."""
    odd = {s for s in counts if NORMAL.get(s) is not None and not NORMAL[s].match(cs)}
    if odd and odd != set(counts):
        return odd, 'shape is not normal for %s' % '/'.join(sorted(odd))
    few = {s for s, n in counts.items() if n == min(counts.values())}
    return few, 'fewest filings in %s' % '/'.join(sorted(few))


def norm_name(name):
    n = (name or '').upper()
    n = re.split(r'\bD/?B/?A\b|\(|\bC/O\b', n)[0].replace('&', ' AND ')
    words = re.sub(r'[^A-Z0-9 ]', ' ', re.sub(r'(?<=\b[A-Z])\.(?=[A-Z]\b)', '', n)).split()
    return ' '.join(w for w in words if w not in SUFFIXES)


def norm_frn(frn):
    f = re.sub(r'\D', '', str(frn or ''))
    return f.zfill(10) if f else ''


def identity_steps(history):
    """history: granted filings of one (subsystem, call sign) sorted by filing date, as dicts with
    fn, type, frn, name.  Yields (rule, confidence, before, after) per unexplained change."""
    for a, b in zip(history, history[1:]):
        if a['type'] in TRANSFERS or b['type'] in TRANSFERS:
            continue
        fa, fb, na, nb = norm_frn(a['frn']), norm_frn(b['frn']), norm_name(a['name']), norm_name(b['name'])
        frn = bool(fa and fb and fa != fb)
        sa, sb = na.replace(' ', ''), nb.replace(' ', '')  # prefix = truncated or suffix-only difference
        name = bool(sa and sb and sa != sb and not (min(len(sa), len(sb)) >= 8 and (sa.startswith(sb) or sb.startswith(sa))))
        if frn and name:
            yield 'frn_and_name_changed', 'high', a, b
        elif frn:
            yield 'frn_changed', 'medium', a, b
        elif name:
            yield 'name_changed', 'low', a, b


def frn_text(f):
    return 'FRN %s | %s' % (f['frn'] or '-', f['name'].strip())


def ident_row(rule, conf, sub, target, other, detail):
    """Identity row: file_number/after_* = target (the filing to repair), before_* = the other filing."""
    return ['identity', rule, 'low' if detail.startswith(BENIGN) else conf, sub, target['cs'], target['fn'],
            target['type'], target['state'], other['fn'], frn_text(other), target['fn'], frn_text(target), '', detail]


def shared_placeholder_rows(filings, min_names=PLACEHOLDER_NAMES):
    """frn_shared_placeholder: one row per filing whose FRN is used by >= min_names distinct applicant names.
    expected_value = the one other FRN the same call sign or the same name uses, when there is exactly one."""
    names = collections.defaultdict(set)
    for f in filings:
        if norm_frn(f['frn']) and norm_name(f['name']):
            names[norm_frn(f['frn'])].add(norm_name(f['name']))
    shared = {fr for fr, ns in names.items() if len(ns) >= min_names}
    by_key = collections.defaultdict(set)
    for f in filings:
        fr = norm_frn(f['frn'])
        if fr and fr not in shared:
            if f['cs'].strip():
                by_key[('cs', f['sub'], f['cs'].strip())].add(fr)
            if norm_name(f['name']):
                by_key[('name', norm_name(f['name']))].add(fr)
    out = []
    for f in filings:
        fr = norm_frn(f['frn'])
        if fr not in shared:
            continue
        alt = by_key.get(('cs', f['sub'], f['cs'].strip()), set()) | by_key.get(('name', norm_name(f['name'])), set())
        out.append(['applicant_frn', 'frn_shared_placeholder', 'high', f['sub'], f['cs'], f['fn'], f['type'], f['state'],
                    '', '', f['fn'], frn_text(f), alt.pop() if len(alt) == 1 else '',
                    "FRN %s is shared by %d unrelated applicant names; restore the licensee's real FRN%s" % (
                        fr, len(names[fr]), '' if len(alt) == 1 else '; other FRNs for this call sign/name: %s'
                        % (', '.join(sorted(alt)) or 'none'))])
    return out


# --- driver ----------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=os.path.join(HERE, 'data', 'icfs.db'))
    ap.add_argument('--out', default=os.path.join(HERE, 'reports', 'sites.csv'))
    a = ap.parse_args()
    conn = sqlite3.connect('file:%s?mode=ro' % a.db, uri=True)

    filings, sites, located = {}, [], set()
    history = collections.defaultdict(list)
    subs_of = collections.defaultdict(collections.Counter)
    for r in records(conn):
        fn = r.get('file_number') or ''
        sub, cs = r.get('subsystem') or '', (r.get('call_sign') or '').strip()
        f = {'fn': fn, 'sub': sub, 'cs': r.get('call_sign') or '', 'type': r.get('filing_type') or '',
             'state': r.get('state') or '', 'action': r.get('last_action') or '', 'date': filing_date(r),
             'frn': r.get('applicant_frn') or '', 'name': r.get('applicant_name') or '',
             'cls': r.get('312_class_of_station') or ''}
        filings[fn] = f
        if cs:
            subs_of[cs][sub] += 1
            if f['action'] in GRANTED:
                history[(sub, cs)].append(f)
        for sb in r.get('schedule_b') or []:
            for s in sb.get('site') or []:
                d = {k: v for k, v in s.items() if not isinstance(v, list)}
                d['_n_esaf'] = len(s.get('esaf') or [])
                sites.append((f, d))
                if any(parts(s)):
                    located.add(fn)

    rows = []

    def row(cls, rule, conf, f, before_file='', before_value='', after_value='', expected='', detail=''):
        if detail.startswith(BENIGN):
            conf = 'low'
        rows.append([cls, rule, conf, f['sub'], f['cs'], f['fn'], f['type'], f['state'], before_file, before_value,
                     f['fn'], after_value, expected, detail])

    def sid_key(v):
        return re.sub(r'[^A-Z0-9]', '', (v or '').upper())

    # sibling locations: latest granted technical filing per (call sign, site id) with usable coordinates
    sib = {}
    for f, s in sorted(sites, key=lambda x: (x[0]['date'] is not None, x[0]['date'] or 0, x[0]['fn'])):
        if f['action'] in GRANTED and f['type'] in TECHNICAL and f['cs']:
            p = parts(s)
            lat_h, lon_h = (s.get('site_lat_hemi') or '')[:1], (s.get('site_long_hemi') or '')[:1]
            if any(p) and lat_h in ('N', 'S') and lon_h in ('E', 'W'):
                lat, lon = decimal(p, lat_h, lon_h)
                elev = num(s.get('site_site_elevation'))
                sib[(f['cs'], sid_key(s.get('site_site_id')))] = (f['fn'], lat, lon)
                sib.setdefault(('elev', f['cs']), []).append((f['fn'], s.get('site_site_id') or '', lat, lon, elev))

    feet_done = set()
    for f, s in sites:
        sid = s.get('site_site_id') or ''
        where = 'site %s %s, %s' % (sid, (s.get('site_site_city') or '').strip(), s.get('site_site_state') or '')
        state = (s.get('site_site_state') or '').strip().upper()
        test = 'TEST' in f['name'].upper() and 'ACCOUNT' in f['name'].upper()
        if state and state not in BBOX:
            if state in STATE_ALIASES:
                row('location', 'state_code_unknown', 'medium', f, after_value=state, expected=STATE_ALIASES[state],
                    detail=where + '; legacy code, the coordinates confirm the state')
            elif mobile(s, state) or (s.get('site_lat_hemi') or '').strip().upper() == 'X' or not any(parts(s)):
                row('location', 'state_code_unknown', 'low', f, after_value=state, expected='(blank)',
                    detail=BENIGN + 'filler on a site with no fixed location; ' + where)
            else:
                row('location', 'state_code_unknown', 'medium', f, after_value=state, detail=where)
        state = STATE_ALIASES.get(state, state)
        sibling = sib.get((f['cs'], sid_key(sid)))
        if sibling and sibling[0] == f['fn']:
            sibling = None
        for rule, conf, after, expected, detail, used in site_problems(s, state, sibling):
            label = ' '.join(s.get(k) or '' for k in ('site_site_id', 'site_site_city'))
            if detail.startswith(BENIGN):
                pass
            elif test:
                detail, expected, used = BENIGN + 'test-account junk filing; ' + detail, '', False
            elif rule == 'coords_zero':
                fixed = f['cls'] in FIXED_CLASS or not f['cls'].strip()
                if f['type'] in TECHNICAL and fixed and s['_n_esaf'] and not MOBILE.search(label):
                    conf = 'high' if f['fn'] in located else 'medium'
                    detail += '; fixed-site technical filing with antennas' + (
                        '; other sites of this filing have coordinates' if f['fn'] in located else '')
                else:
                    why = ('%s filing: the licensed location is in the technical filing' % f['type']) if f['type'] in NON_TECH \
                        else 'class %r has no fixed point' % f['cls'] if not fixed else \
                        'legacy stub site with no antennas' if not s['_n_esaf'] else 'blanket/area site'
                    detail, conf = BENIGN + why + '; ' + detail, 'low'
            elif rule == 'coords_missing' and conf is None:
                ref = (s.get('site_coordinate_reference') or '').strip().upper()
                if f['cls'] in FIXED_CLASS and ref != 'N/A' and not MOBILE.search(label) \
                        and not ANTENNA_SIZE.match((s.get('site_site_id') or '').strip()):
                    conf = 'high' if f['type'] in TECHNICAL and f['action'] in GRANTED else 'medium'
                else:
                    why = 'coordinate reference N/A: the filer declared no fixed point' if ref == 'N/A' else \
                        'class %r has no fixed point' % f['cls'] if f['cls'] not in FIXED_CLASS else \
                        'site entry is an antenna type / blanket / beam, not a place'
                    detail, conf = BENIGN + why + '; ' + detail, 'low'
            if conf is None:
                conf = 'medium'
            if rule == 'coords_missing' and 'blank or 0 0 0 while' in detail:
                conf = 'medium'
            row('location', rule, conf, f, sibling[0] if sibling and used else '',
                fmt(sibling[1], sibling[2]) if sibling and used else '', after, expected,
                BENIGN + '%s; %s' % (where, detail[len(BENIGN):]) if detail.startswith(BENIGN) else '%s; %s' % (where, detail))

        elev = num(s.get('site_site_elevation'))
        if elev is None:
            continue
        if elev < -100 or elev > 5000:
            exp = '%.1f' % (elev / FEET) if state in HIGH_STATES and elev / FEET <= 5000 else ''
            row('elevation', 'elevation_implausible', 'high' if exp else 'medium', f, after_value=s.get('site_site_elevation'),
                expected=exp, detail=(BENIGN + 'test-account junk filing; ' if test else '') + '%s; %s m%s' % (
                    where, s.get('site_site_elevation'), '; entered in feet: expected is metres' if exp else
                    '; no derivable value: look up the ground elevation at the coordinates'))
            continue
        here = parts(s)
        if not any(here) or elev < MIN_SIBLING_ELEV_M * FEET_BAND[0]:
            continue
        lat, lon = decimal(here, (s.get('site_lat_hemi') or 'N')[:1], (s.get('site_long_hemi') or 'W')[:1])
        best = None
        for sfn, ssid, slat, slon, selev in sib.get(('elev', f['cs']), []):
            if sfn == f['fn'] or not selev or selev < MIN_SIBLING_ELEV_M:
                continue
            if km((lat, lon), (slat, slon)) < 1.0 and FEET_BAND[0] <= elev / selev <= FEET_BAND[1]:
                if best is None or abs(elev / selev - FEET) < abs(elev / best[1] - FEET):
                    best = (sfn, selev)
        if best:
            note = ('%.3f x the elevation %s gives the same site (within 1 km): one of the two is in feet; look up the '
                    'ground elevation and fix the one that disagrees' % (elev / best[1], best[0]))
            row('elevation', 'elevation_feet_suspected', 'medium', f, best[0], '%g' % best[1],
                s.get('site_site_elevation'), 'check DEM: %.1f (if this one is feet) or %g' % (elev / FEET, best[1]),
                '%s; larger value of the pair; %s' % (where, note))
            other = filings.get(best[0])
            if other and (('elevation_feet_suspected', best[0]) not in feet_done):
                feet_done.add(('elevation_feet_suspected', best[0]))
                row('elevation', 'elevation_feet_suspected', 'medium', other, f['fn'], s.get('site_site_elevation'),
                    '%g' % best[1], 'check DEM: %g (if this one is metres divided twice) or %.1f' % (elev, best[1]),
                    '%s; smaller value of the pair; %s' % (where, note))

    # what the identity rules need across the whole database
    transferred_cs = {f['cs'].strip() for f in filings.values() if f['type'] in TRANSFERS and f['action'] in GRANTED}
    transfers_by_frn = collections.defaultdict(list)
    names_by_frn = collections.defaultdict(set)
    for f in filings.values():
        fr = norm_frn(f['frn'])
        if fr:
            names_by_frn[fr].add(norm_name(f['name']))
            if f['type'] in TRANSFERS and f['date']:
                transfers_by_frn[fr].append(f['date'])

    def ident(rule, conf, sub, target, other, detail):
        rows.append(ident_row(rule, conf, sub, target, other, detail))

    for (sub, cs), hist in history.items():
        hist.sort(key=lambda x: (x['date'] is None, x['date'] or 0, x['fn']))
        index = {id(h): k for k, h in enumerate(hist)}
        for rule, conf, b, c in identity_steps(hist):
            k = index[id(b)]
            detail = '%s %s (%s) -> %s %s (%s); no granted Assignment/Transfer of Control under this call sign between' % (
                b['type'], b['fn'], b['date'], c['type'], c['fn'], c['date'])
            fb, fc = norm_frn(b['frn']), norm_frn(c['frn'])
            if rule == 'name_changed':
                ta, tb = set(norm_name(b['name']).split()), set(norm_name(c['name']).split())
                jac = len(ta & tb) / len(ta | tb) if ta | tb else 1
                flip = k + 2 < len(hist) and norm_name(hist[k + 2]['name']) == norm_name(b['name'])
                if fb and fc and fb == fc and c['date'] and c['date'].year > 2005 and jac < 0.5 \
                        and cs not in transferred_cs and not flip:
                    ident(rule, 'medium', sub, c, b, detail + '; same FRN, different name, no transfer on record')
                else:
                    why = ('A->B->A name flip-flop: legacy backfill' if flip else 'no FRN on either side (legacy)' if not (fb and fc)
                           else 'a transfer exists for this call sign' if cs in transferred_cs else
                           'spelling/abbreviation variant' if jac >= 0.5 else 'rename under the same FRN, before 2006')
                    ident(rule, 'low', sub, c, b, BENIGN + why + '; ' + detail)
            elif rule == 'frn_changed':
                placeholder = [x for x, fr in ((b, fb), (c, fc)) if len(names_by_frn.get(fr, ())) > 3]
                backfill = [x for x, fr in ((b, fb), (c, fc)) if fr >= '0020000000' and x['date'] and x['date'].year < 2010]
                bad = placeholder or backfill
                if bad:
                    x = bad[0]
                    why = ('FRN %s is shared by %d unrelated applicant names' % (norm_frn(x['frn']), len(names_by_frn[norm_frn(x['frn'])]))
                           if placeholder else 'FRN %s was issued after this %s filing (backfilled)' % (norm_frn(x['frn']), x['date'].year))
                    ident(rule, 'medium', sub, x, c if x is b else b, '%s; %s; restore the licensee\'s real FRN' % (why, detail))
                else:
                    back = any(norm_frn(h['frn']) == fb for h in hist[k + 2:])
                    why = ('the earlier FRN comes back on a later filing: one licensee, two FRNs' if back else
                           'bankruptcy debtor-in-possession entity' if 'POSSESSION' in c['name'].upper() + b['name'].upper()
                           else 'licensee files under more than one registered FRN')
                    ident(rule, 'low', sub, c, b, BENIGN + why + '; ' + detail)
            else:  # frn_and_name_changed
                lo_d = (b['date'] or datetime.date.min) - datetime.timedelta(days=365)
                hi_d = c['date'] or datetime.date.max
                moved = any(lo_d <= d <= hi_d for fr in (fb, fc) for d in transfers_by_frn.get(fr, ()))
                person = [x for x in (b, c) if x['name'].strip() and not CORPORATE.search(x['name'])]
                blip_c = k + 2 < len(hist) and norm_frn(hist[k + 2]['frn']) == fb != fc
                blip_b = k > 0 and norm_frn(hist[k - 1]['frn']) == fc != fb
                if PLACEHOLDER.match(cs):
                    ident(rule, 'low', sub, c, b, BENIGN + 'placeholder call sign joins unrelated licensees; ' + detail)
                elif moved:
                    ident(rule, 'low', sub, c, b, BENIGN + 'one of the FRNs filed an Assignment/Transfer of Control '
                          '(any call sign) in the window; ' + detail)
                elif person:
                    x = person[0]
                    ident(rule, 'medium', sub, x, c if x is b else b, 'applicant looks like a natural person '
                          '(officer/contact entered as licensee); restore the licensee from the neighbouring filings; ' + detail)
                elif blip_c or blip_b:
                    x = c if blip_c else b
                    ident(rule, 'medium', sub, x, c if x is b else b, 'single-filing blip: the filings either '
                          'side share an FRN this one lacks; ' + detail)
                else:
                    ident(rule, 'low', sub, c, b, 'unexplained change; check for a transfer filed elsewhere or a '
                          'rename; ' + detail)

    rows.extend(shared_placeholder_rows(filings.values()))

    known = collections.defaultdict(set)
    owners = collections.defaultdict(set)
    for f in filings.values():
        known[f['sub']].add(f['cs'].strip())
        owners[(f['sub'], f['cs'].strip())].update({norm_frn(f['frn']), norm_name(f['name'])} - {''})
    for f in filings.values():
        cs = f['cs']
        got = call_sign_problem(f['sub'], cs, known[f['sub']])
        live = f['action'] in GRANTED or f['state'].startswith('Pending')
        if got:
            rule, conf, expected, detail = got
            if rule == 'call_sign_placeholder':
                modern = f['date'] and f['date'].year >= 2010 and f['type'] in ('Amendment', 'Modification')
                if re.match(r'^CALL\d+$', cs) or modern:
                    conf, detail = 'medium', detail + '; modern temporary number or amendment: replace with the parent ' \
                        "filing's assigned call sign"
                else:
                    conf, detail = 'low', BENIGN + 'legacy/STA practice, no call sign was ever assigned; ' + detail
            elif rule == 'call_sign_foreign_format' and conf is None:
                if live:
                    conf, detail = 'medium', detail + '; granted/pending: check whether it was filed in the wrong subsystem'
                else:
                    conf, detail = 'low', BENIGN + 'misfiling that was withdrawn or dismissed; ' + detail
            elif rule == 'call_sign_letter_o':
                mine = {norm_frn(f['frn']), norm_name(f['name'])} - {''}
                if not (owners.get((f['sub'], expected), set()) & mine):
                    detail += '; %s is not held by this licensee: the true digit is unknown, confirm from the record' % expected
                    expected = ''
            row('call_sign', rule, conf, f, after_value=cs, expected=expected, detail=detail)
        key = cs.strip()
        if key and len(subs_of[key]) > 1 and not PLACEHOLDER.match(key):
            suspect, why = multi_subsystem_suspects(key, subs_of[key])
            if f['sub'] in suspect:
                right = [k for k, rx in NORMAL.items() if rx.match(key)]
                detail = 'used in %s; %s' % (', '.join('%s x%d' % kv for kv in sorted(subs_of[key].items())), why)
                if live:
                    row('call_sign', 'call_sign_multi_subsystem', 'medium', f, after_value=f['sub'],
                        expected='subsystem %s' % '/'.join(right) if right else '',
                        detail=detail + '; the filing probably belongs in the subsystem matching its call sign')
                else:
                    row('call_sign', 'call_sign_multi_subsystem', 'low', f, after_value=f['sub'],
                        detail=BENIGN + 'misfiling that was withdrawn or dismissed; ' + detail)

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        w.writerows(rows)
    counts = collections.Counter((r[1], r[2]) for r in rows)
    print('wrote %s (%d rows)' % (a.out, len(rows)))
    for (rule, conf), n in sorted(counts.items()):
        print('  %-28s %-6s %7d' % (rule, conf, n))


if __name__ == '__main__':
    main()
