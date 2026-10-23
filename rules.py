"""Flag suspect ICFS filings: one CSV row per (filing, rule) broken.  Reads data/icfs.db.

    python rules.py [--db data/icfs.db] [--out reports/findings.csv]

Severity:
  error    - impossible as recorded (placeholder date, term ends before it begins)
  suspect  - possible but unlikely; worth a person's look
  info     - not wrong by itself, but explains a status (e.g. Current with no term)
Each row also carries a confidence and, where derivable, an expected value.  Rows the
verification pass judged not to need repair keep confidence low and a detail starting
"likely benign: <why>".  Dates are judged as of the harvest date (profiler.TODAY).
Also writes <out>.md: counts per rule and subsystem.  Stdlib only.
"""
import argparse
import collections
import csv
import datetime
import hashlib
import json
import os
import re
import sqlite3

from profiler import DATE_FIELDS, EARLIEST, FILE_NUMBER, HERE, TODAY, parse_date, records

# Subsystems whose authorizations carry a license term; elsewhere (214, FCN, SPC, ISP...)
# no term_end is by design.
TERMED = ('SES', 'SAT', 'SCL', 'IHF')
FILE_DATE_SLACK_DAYS = 30  # most gaps are under 30 days and look systematic, not wrong
DAY = datetime.timedelta(days=1)

RULES = {  # rule: (severity, issue_class, default confidence, meaning)
    'placeholder_date': ('error', 'dates', 'high', 'a date field is unparseable, before 1960, or implausibly far ahead'),
    'term_end_before_term_begin': ('error', 'dates', 'high', 'term_end earlier than term_begin'),
    'file_number_bad_date': ('error', 'file_number', 'high', 'the date inside the file number is not a real date'),
    'subsystem_prefix_mismatch': ('error', 'file_number', 'high', 'file number prefix names a different subsystem'),
    'current_term_expired': ('suspect', 'status', 'medium', 'state Current but term_end has passed and no renewal is pending'),
    'grant_before_filing': ('suspect', 'dates', 'medium', 'grant_date earlier than the filing date'),
    'term_end_before_grant': ('suspect', 'dates', 'low', 'term_end earlier than grant_date'),
    'last_action_before_filing': ('suspect', 'dates', 'low', 'last_action_date earlier than the filing date'),
    'granted_but_pending': ('suspect', 'status', 'low', 'grant_date set while the filing is still pending'),
    'current_term_expired_renewal_pending': ('info', 'status', 'low', 'term_end passed but a renewal for the call sign is pending'),
    'current_no_term': ('info', 'status', 'low', 'state Current with no term_end in a subsystem whose licenses have terms'),
    'filed_date_differs_from_file_number': ('info', 'dates', 'low', 'date_filed more than %d days from the file-number date'
                                            % FILE_DATE_SLACK_DAYS),
    'date_filed_missing': ('error', 'dates', 'high', 'date_filed is blank'),
    'granted_no_grant_date': ('suspect', 'dates', 'medium', 'last_action GRA but grant_date is blank'),
    'last_action_without_date': ('suspect', 'dates', 'medium', 'a last_action is recorded but last_action_date is blank'),
    'frn_missing_on_current': ('suspect', 'identity', 'medium', 'Current filing with a blank or malformed applicant FRN'),
    'closed_without_disposition': ('suspect', 'status', 'medium', 'state Closed with no last_action (no disposition)'),
    'accepted_pn_before_filing': ('suspect', 'dates', 'medium', 'accepted_pn_date more than 1 day before date_filed'),
    'action_pn_before_grant': ('suspect', 'dates', 'medium', 'GRA with action_pn_date more than 7 days before grant_date'),
    'applicant_name_missing': ('suspect', 'applicant_identity', 'medium', 'applicant_name is blank'),
    'type_code_mismatch': ('suspect', 'file_number', 'medium', 'file-number type code disagrees with filing_type '
                           '(minority filing_type for that subsystem and code)'),
    'duplicate_filing': ('suspect', 'duplicate', 'low', 'record identical to another filing except for the file number'),
}
BENIGN = 'likely benign: '  # detail prefix on rows the verification judged not to need repair
BULK_STAMPS = ('2025-06-22', '2025-06-23', '2026-04-22')
TYPE_CODE = {'Assignment': 'ASG', 'Transfer of Control': 'T/C', 'License': 'LIC', 'Modification': 'MOD',
             'Renewal': 'RWL', 'Special Temporary Authority': 'STA', 'Amendment': 'AMD',
             'Registration of a receive-only earth station': 'REG', 'MFS Modification': 'MFS',
             'AFS - Amendment': 'AFS'}
LAG_DAYS = 90  # SES expiries are closed in batches months later
RECENT_DAYS = 60  # filings this young may just not have their action dates in the API yet
FRN_START = datetime.date(2001, 1, 1)  # FRNs (CORES) began in 2001: older authorizations never had one
FRN = re.compile(r'^\d{10}$')
PN_FILE_SLACK, PN_GRANT_SLACK = 1, 7  # days
STREAMLINED_214 = (14, 21)  # ITC streamlined 214 grants take effect 14 days after the PN


def file_number_date(fn):
    """(date or None, matched?) for SUB-TYPE-YYYYMMDD-NNNNN; INTRyyyy numbers carry no full date."""
    m = FILE_NUMBER.match(fn)
    if not m:
        return None, False
    ymd = m.group(3)
    try:
        return datetime.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:])), True
    except ValueError:
        return None, True


def plus_years(d, n):
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)


def light(r):
    """What the per-call-sign context needs from one filing."""
    fn = r.get('file_number') or ''
    return {'fn': fn, 'type': r.get('filing_type') or '', 'state': r.get('state') or '',
            'action': r.get('last_action') or '', 'te': parse_date(r.get('term_end')),
            'dd': file_number_date(fn)[0] or parse_date(r.get('date_filed')),
            'frn': (r.get('applicant_frn') or '').strip(), 'name': ' '.join((r.get('applicant_name') or '').split())}


def fingerprint(r):
    """Hash of the whole record with its own file number blanked (duplicate_filing)."""
    fn = r.get('file_number') or ''
    return hashlib.sha1(json.dumps(r, sort_keys=True).replace(fn, '#').encode()).hexdigest() if fn else ''


class Context:
    """Facts across filings: per (subsystem, call sign) light records, and per (subsystem, filing type)
    how many Current filings carry a term_end."""
    def __init__(self, recs=()):
        self.by_cs = collections.defaultdict(list)
        self.termed = collections.defaultdict(lambda: [0, 0])
        self.keys = None
        self.types = collections.defaultdict(collections.Counter)  # (subsystem, type code) -> filing_type counts
        self.prints, self.dups = {}, collections.defaultdict(list)
        for r in recs:
            self.add(r)

    def add(self, r):
        fn = r.get('file_number') or ''
        if FILE_NUMBER.match(fn) and fn.split('-')[0] == r.get('subsystem'):
            self.types[(r.get('subsystem'), fn.split('-')[1])][r.get('filing_type') or ''] += 1
        if r.get('call_sign') or r.get('schedule_b'):  # sparse stubs (no call sign, no Schedule B) match by accident
            h = fingerprint(r)
            self.prints[fn] = h
            self.dups[h].append(fn)
        if r.get('call_sign'):
            self.by_cs[(r.get('subsystem'), r.get('call_sign'))].append(light(r))
        if r.get('state') == 'Current':
            t = self.termed[(r.get('subsystem'), r.get('filing_type') or '')]
            t[0] += 1
            t[1] += bool(r.get('term_end'))

    def pending_renewal(self, sub, cs):
        if self.keys is not None:
            return (sub, cs) in self.keys
        return any(x['type'] == 'Renewal' and x['state'].startswith('Pending') for x in self.by_cs.get((sub, cs), ()))

    def others(self, sub, cs, fn):
        return [x for x in self.by_cs.get((sub, cs), ()) if x['fn'] != fn]

    def frn_of(self, sub, cs, fn):
        """(frn, file) from the call sign's latest other filing with a well-formed FRN, or ('', '')."""
        good = [x for x in self.others(sub, cs, fn) if FRN.match(x['frn'])]
        if not good:
            return '', ''
        x = max(good, key=lambda x: (x['dd'] or datetime.date.min, x['fn']))
        return x['frn'], x['fn']

    def neighbour_name(self, sub, cs, fn, dd):
        """The applicant name on the call sign's nearest earlier and later named filings, when they agree."""
        named = sorted((x for x in self.others(sub, cs, fn) if x['name'] and x['dd']), key=lambda x: (x['dd'], x['fn']))
        if not named or not dd:
            return '', ''
        near = [x for x in (max((x for x in named if x['dd'] <= dd), key=lambda x: x['dd'], default=None),
                            min((x for x in named if x['dd'] > dd), key=lambda x: x['dd'], default=None)) if x]
        if len({x['name'].upper() for x in near}) == 1:
            return near[0]['name'], near[0]['fn']
        return '', ''

    def majority_type(self, sub, code, ftype):
        """The usual filing_type for (subsystem, code) when ftype is a small minority of it, else ''."""
        c = self.types.get((sub, code))
        if not c or ftype not in c:
            return ''
        top, n = c.most_common(1)[0]
        return top if top != ftype and c[ftype] * 10 < n else ''

    def term_usual(self, sub, ftype):
        if self.keys is not None and not self.termed:
            return True
        n, with_term = self.termed.get((sub, ftype), (0, 0))
        return bool(n) and with_term / n >= 0.5


def check(r, ctx, today=TODAY):
    """[dict(rule, detail, conf, expected, after_value, before_file, before_value)] broken by one filing.
    ctx: Context, or a set of pending-renewal (subsystem, call_sign) keys (tests)."""
    if not isinstance(ctx, Context):
        keys, ctx = ctx, Context()
        ctx.keys = set(keys)
    out = []

    def add(rule, detail, conf=None, expected='', after='', before_file='', before_value='', benign=''):
        out.append({'rule': rule, 'detail': (BENIGN + benign + '; ' if benign else '') + detail,
                    'conf': 'low' if benign else (conf or RULES[rule][2]), 'expected': expected,
                    'after_value': after or detail, 'before_file': before_file, 'before_value': before_value})

    sub = r.get('subsystem') or ''
    fn = r.get('file_number') or ''
    cs = r.get('call_sign') or ''
    state = r.get('state') or ''
    action = r.get('last_action') or ''
    ftype = r.get('filing_type') or ''
    d = {k: parse_date(r.get(k)) for k in DATE_FIELDS}
    fn_date, matched = file_number_date(fn)

    bad = []
    for k in DATE_FIELDS:
        v = r.get(k)
        if v in (None, ''):
            continue
        ahead = today + datetime.timedelta(days=366 * (30 if k == 'term_end' else 1))
        if d[k] is None or d[k] < EARLIEST or d[k] > ahead:
            bad.append((k, v))
            d[k] = None  # never reason from a placeholder below
    for k, v in bad:
        exp, note = '', ''
        if k == 'date_filed' and fn_date:
            exp = str(fn_date)
        elif k == 'grant_date' and d['last_action_date']:
            exp = str(d['last_action_date'])
        elif k == 'last_action_date' and d['grant_date']:
            exp = str(d['grant_date'])
        if k in ('term_begin', 'term_end') and (r.get('grant_date') or '')[:7] == '2026-04':
            note = '; 2026-04 bulk term shift: set term_begin to the previous renewal term_end, term_end one cycle later'
        elif k == 'term_end' and ftype == 'Renewal' and not state == 'Current':
            note = '; probably derived from the parent license term: fix the parent first'
        add('placeholder_date', '%s=%s%s' % (k, v, note), expected=exp, after='%s=%s' % (k, v))

    if matched and fn_date is None:
        add('file_number_bad_date', fn, after=fn)
    if matched and fn.split('-')[0] != sub:
        parts = fn.split('-')
        exp = '-'.join([sub, TYPE_CODE[ftype]] + parts[2:]) if ftype in TYPE_CODE else ''
        add('subsystem_prefix_mismatch', 'prefix %s, subsystem %s' % (parts[0], sub), expected=exp, after=fn)
    if fn_date and d['date_filed'] and abs((d['date_filed'] - fn_date).days) > FILE_DATE_SLACK_DAYS:
        gap = (d['date_filed'] - fn_date).days
        detail = 'date_filed=%s, file-number date %s (%+d days)' % (r.get('date_filed'), fn_date, gap)
        after = 'date_filed=%s' % r.get('date_filed')
        if re.match(r'^\d\d:00:00', (r.get('date_filed') or '')[11:]):
            add('filed_date_differs_from_file_number', detail + '; legacy midnight timestamp: compare both with '
                'accepted_pn_date/grant_date and fix the one that breaks the sequence', 'medium', after=after)
        else:
            add('filed_date_differs_from_file_number', detail, after=after,
                benign='modern timestamp: resubmission or numbering lag')

    filed = fn_date or d['date_filed']  # the file-number date is the original filing date
    gd, ld = d['grant_date'], d['last_action_date']
    if filed and gd and gd < filed - DAY:
        gap = (filed - gd).days
        detail = 'filed %s, granted %s, last action %s %s' % (filed, gd, action or '-', ld or '-')
        after = 'grant_date=%s' % gd
        if state.startswith('Pending'):
            add('grant_before_filing', detail + '; pending filing carries the license grant date', 'medium',
                expected='(blank)', after=after)
        elif ld and ld >= filed - DAY and abs((ld - gd).days) > 3:
            add('grant_before_filing', detail + '; stale or mistyped grant_date', 'medium', expected=str(ld), after=after)
        elif ld and abs((ld - gd).days) <= 3 and gap <= 31:
            add('grant_before_filing', detail, after=after, benign='post-consummation pro forma or emergency STA')
        elif ld and abs((ld - gd).days) <= 3 and filed.year < 2000:
            add('grant_before_filing', detail, after=after,
                benign='legacy record: date_filed / file-number date is the suspect field')
        else:
            add('grant_before_filing', detail, after=after)
    if d['term_begin'] and d['term_end'] and d['term_end'] < d['term_begin']:
        tb, te = d['term_begin'], d['term_end']
        te1 = plus_years(te, 1)
        detail = '%s .. %s' % (tb, te)
        if te1 > tb and (te1 - tb).days <= 200:
            add('term_end_before_term_begin', detail + '; year wrap: term_end one year too low (%d-day term)'
                % (te1 - tb).days, 'high', expected='term_end=%s' % te1)
        elif ftype == 'License' and fn_date and fn_date.year < 1982 and gd and tb == plus_years(gd, 3):
            add('term_end_before_term_begin', detail + '; legacy conversion: the two dates look mapped into the wrong '
                'fields', 'medium', expected='term_begin=%s term_end=%s (check grant)' % (te, tb))
        else:
            add('term_end_before_term_begin', detail, 'medium')
    if gd and d['term_end'] and d['term_end'] < gd:
        detail = 'granted %s, term_end %s, last action %s' % (gd, d['term_end'], ld or '-')
        if d['term_begin'] and d['term_end'] < d['term_begin']:
            add('term_end_before_grant', detail + '; term_end is also before term_begin (fix term_end, see '
                'term_end_before_term_begin)', 'medium', after='term_end=%s' % d['term_end'])
        elif not ld or ld != gd or (r.get('grant_date') or '')[:10] in BULK_STAMPS:
            add('term_end_before_grant', detail + '; grant_date is a placeholder or disagrees with last_action_date',
                'medium', expected=str(ld or d['term_begin'] or ''), after='grant_date=%s' % gd)
        else:
            add('term_end_before_grant', detail, after='term_end=%s' % d['term_end'],
                benign='late or after-the-fact grant, or term carried from the parent')
    if filed and ld and ld < filed - DAY:
        detail = 'filed %s, last action %s %s, granted %s' % (filed, action or '-', ld, gd or '-')
        after = 'last_action_date=%s' % ld
        if action == 'CON':
            add('last_action_before_filing', detail, after=after,
                benign='consummation reported in a post-consummation notification')
        elif gd and gd == ld:
            add('last_action_before_filing', detail + '; grant agrees with last action: date_filed / file-number date '
                'is the suspect field', 'medium', after=after)
        elif gd:
            add('last_action_before_filing', detail + '; last_action_date disagrees with grant_date', 'medium',
                expected=str(gd), after=after)
        else:
            add('last_action_before_filing', detail, 'medium', after=after)
    if gd and state.startswith('Pending') and not state.startswith('Pending Consummation'):
        detail = 'state=%s last_action=%s grant_date=%s filed %s' % (state, action, gd, filed or '-')
        after = 'state=%s' % state
        if filed and gd >= filed and action not in ('DFG', 'DIP'):
            add('granted_but_pending', detail + '; granted after filing but still pending: stale state', 'medium',
                expected='Closed/Current per the grant', after=after)
        elif action in ('DFG', 'DIP'):
            add('granted_but_pending', detail, after=after, benign='partial grant, remainder deferred')
        else:
            add('granted_but_pending', detail, after=after, benign='grant date inherited from the existing license')

    filed_ok = filed if filed and EARLIEST <= filed <= today else None
    recent = bool(filed_ok and (today - filed_ok).days < RECENT_DAYS)
    lag = '; filed %s, under %d days before the harvest: may be API lag' % (filed_ok, RECENT_DAYS) if recent else ''
    if r.get('date_filed') in (None, ''):
        add('date_filed_missing', 'date_filed blank%s' % ('; file-number date %s' % fn_date if fn_date else
                                                            '; file number carries no full date'),
            expected=str(fn_date or ''), after='date_filed blank')
    if action == 'GRA' and r.get('grant_date') in (None, ''):
        add('granted_no_grant_date', 'state=%s last_action GRA, grant_date blank, last_action_date %s%s' % (
            state, ld or 'blank', lag), 'low' if recent else None, expected=str(ld or ''), after='grant_date blank')
    if action and r.get('last_action_date') in (None, ''):
        add('last_action_without_date', 'last_action %s with blank last_action_date, grant_date %s%s' % (
            action, gd or 'blank', lag), 'low' if recent else None, expected=str(gd) if gd and action == 'GRA' else '',
            after='last_action_date blank')
    frn = (r.get('applicant_frn') or '').strip()
    if state == 'Current' and not FRN.match(frn):
        exp, src = ctx.frn_of(sub, cs, fn) if cs else ('', '')
        start = gd or filed_ok
        detail = 'Current with applicant_frn %s%s' % (repr(frn) if frn else 'blank', '; call sign %s has FRN %s on %s'
                                                      % (cs, exp, src) if exp else '')
        add('frn_missing_on_current', detail, expected=exp, after='applicant_frn=%s' % (frn or 'blank'),
            before_file=src, before_value='applicant_frn=%s' % exp if exp else '',
            benign='legacy authorization from %s, before FRNs existed (2001)' % start
            if start and start < FRN_START else '')
    if state == 'Closed' and not action:
        legacy = sub == 'FCN' and filed_ok and 2009 <= filed_ok.year <= 2011
        detail = 'Closed with no last_action, last_action_date %s, grant_date %s' % (ld or 'blank', gd or 'blank')
        if legacy:
            detail += '; part of the 2009-2011 FCN batch closed without dispositions (systemic migration gap)'
        add('closed_without_disposition', detail, 'low' if legacy else None, after='last_action blank',
            expected='GRA (grant_date %s)' % gd if gd else '')

    df, ap, xp = d['date_filed'], d['accepted_pn_date'], d['action_pn_date']
    if df and ap and ap < df - PN_FILE_SLACK * DAY:
        gap = (df - ap).days
        detail = 'filed %s, accepted_pn_date %s (%d days earlier)' % (df, ap, gap)
        if gap > 365:
            add('accepted_pn_before_filing', detail + '; probably inherited from a parent filing: clear or correct from '
                'the PN', 'high', after='accepted_pn_date=%s' % ap)
        else:
            add('accepted_pn_before_filing', detail, after='accepted_pn_date=%s' % ap)
    if action == 'GRA' and gd and xp and xp < gd - PN_GRANT_SLACK * DAY:
        gap = (gd - xp).days
        detail = 'granted %s, action_pn_date %s (%d days earlier)' % (gd, xp, gap)
        if sub == 'ITC' and STREAMLINED_214[0] <= gap <= STREAMLINED_214[1]:
            add('action_pn_before_grant', detail, after='action_pn_date=%s' % xp,
                benign='ITC streamlined grant, effective 14 days after the public notice')
        elif sub == 'ITC' and 42 <= gap <= 49:  # ~430 ITC rows cluster at 43-46 days: a process pattern, not typos
            add('action_pn_before_grant', detail + '; systematic ITC 42-49 day gap (most at 45-46): verify the PN '
                'meaning before repairing', 'low', after='action_pn_date=%s' % xp)
        else:
            add('action_pn_before_grant', detail + '; check both dates against the action PN', after='action_pn_date=%s' % xp)
    if not (r.get('applicant_name') or '').strip():
        name, src = ctx.neighbour_name(sub, cs, fn, filed) if cs else ('', '')
        add('applicant_name_missing', 'applicant_name blank%s%s' % ('; call sign %s neighbours are %s (%s)' % (cs, name, src)
                                                                    if name else '', lag),
            'low' if recent else None, expected=name, after='applicant_name blank', before_file=src,
            before_value='applicant_name=%s' % name if name else '')
    if matched and fn.split('-')[0] == sub:
        top = ctx.majority_type(sub, fn.split('-')[1], ftype)
        if top:
            add('type_code_mismatch', 'type code %s, filing_type %r; most %s-%s filings are %r' % (
                fn.split('-')[1], ftype, sub, fn.split('-')[1], top), expected=top, after='filing_type=%s' % ftype)
    group = ctx.dups.get(ctx.prints.get(fn), ())
    if len(group) > 1:
        first = min(group)
        add('duplicate_filing', 'identical in every field except file_number to %s' % ', '.join(sorted(set(group) - {fn})),
            before_file=first if first != fn else sorted(group)[1], after='duplicate record')

    if state == 'Current' and sub in TERMED:
        te = d['term_end']
        others = ctx.others(sub, cs, fn) if cs else []
        if te and te < today:
            after = 'Current, term_end=%s' % te
            newer = [x for x in others if x['te'] and x['te'] > te and not x['state'].startswith('Pending')
                     and x['action'] in ('GRA', 'CON') and x['type'] in ('Renewal', 'License', 'Modification')]
            if ctx.pending_renewal(sub, cs):
                if newer:
                    y = max(newer, key=lambda x: x['te'])
                    add('current_term_expired_renewal_pending', 'term_end=%s; stale Current: replaced by granted %s %s '
                        '(term to %s)' % (te, y['type'], y['fn'], y['te']), 'high', expected='Closed', after=after,
                        before_file=y['fn'], before_value='%s %s/%s term_end=%s' % (y['type'], y['state'], y['action'], y['te']))
                else:
                    add('current_term_expired_renewal_pending', 'term_end=%s' % te, after=after,
                        benign='timely renewal pending keeps the license in force')
            else:
                live = [x for x in others if x['state'] == 'Current' and x['te'] and x['te'] > te]
                ext = [x for x in others if sub == 'SAT' and x['type'] == 'Modification' and x['dd']
                       and x['dd'] >= te - datetime.timedelta(days=60)]
                if live:
                    y = max(live, key=lambda x: x['te'])
                    add('current_term_expired', 'term_end=%s; superseded: %s %s is Current to %s, close this filing'
                        % (te, y['type'], y['fn'], y['te']), 'high', expected='Closed', after=after, before_file=y['fn'],
                        before_value='%s %s/%s term_end=%s' % (y['type'], y['state'], y['action'], y['te']))
                elif ext:
                    y = max(ext, key=lambda x: x['dd'])
                    if y['state'].startswith('Pending'):
                        add('current_term_expired', 'term_end=%s; extension Modification %s filed %s is pending'
                            % (te, y['fn'], y['dd']), after=after, before_file=y['fn'], benign='term extension pending')
                    else:
                        add('current_term_expired', 'term_end=%s; extension Modification %s filed %s (%s/%s): '
                            'term_end probably not updated from that grant' % (te, y['fn'], y['dd'], y['state'],
                                                                              y['action']),
                            'medium', expected='term_end from the %s grant' % y['fn'], after=after, before_file=y['fn'])
                elif sub == 'SES' and te >= today - datetime.timedelta(days=LAG_DAYS):
                    add('current_term_expired', 'term_end=%s' % te, after=after,
                        benign='expired under %d days ago: FCC processing lag' % LAG_DAYS)
                else:
                    add('current_term_expired', 'term_end=%s; no renewal, extension or newer Current filing' % te,
                        'medium', expected='Closed (expired)', after=after)
        elif r.get('term_end') in (None, ''):
            if ctx.term_usual(sub, ftype):
                add('current_no_term', '%s with no term_end (most Current %s %s filings have one)' % (ftype, sub, ftype),
                    'medium', after='term_end blank')
            else:
                add('current_no_term', ftype, after='term_end blank',
                    benign='%s %s filings normally carry no term in ICFS' % (sub, ftype))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=os.path.join(HERE, 'data', 'icfs.db'))
    ap.add_argument('--out', default=os.path.join(HERE, 'reports', 'findings.csv'))
    a = ap.parse_args()
    conn = sqlite3.connect('file:%s?mode=ro' % a.db, uri=True)
    ctx = Context(records(conn))
    counts = collections.Counter()
    filings = collections.Counter()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['rule', 'severity', 'subsystem', 'file_number', 'call_sign', 'filing_type', 'state',
                    'last_action', 'applicant_name', 'detail', 'issue_class', 'confidence', 'before_file',
                    'before_value', 'after_file', 'after_value', 'expected_value'])
        for r in records(conn):
            sub = r.get('subsystem') or ''
            filings[sub] += 1
            for x in check(r, ctx):
                rule = x['rule']
                counts[(rule, sub)] += 1
                w.writerow([rule, RULES[rule][0], sub, r.get('file_number'), r.get('call_sign'),
                            r.get('filing_type'), r.get('state'), r.get('last_action'), r.get('applicant_name'),
                            x['detail'], RULES[rule][1], x['conf'], x['before_file'], x['before_value'],
                            r.get('file_number'), x['after_value'], x['expected']])
    subs = sorted(filings)
    md = ['# ICFS findings summary', '', '%d filings checked as of %s; rows in %s.' % (
        sum(filings.values()), TODAY, os.path.basename(a.out)), '',
        '| rule | severity | meaning | ' + ' | '.join(subs) + ' | total |',
        '|---|---|---|' + '---|' * (len(subs) + 1)]
    for rule, (sev, _, _, meaning) in RULES.items():
        row = [counts[(rule, s)] for s in subs]
        md.append('| %s | %s | %s | %s | %d |' % (rule, sev, meaning, ' | '.join(str(n or '') for n in row), sum(row)))
    with open(os.path.splitext(a.out)[0] + '.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(md) + '\n')
    print('wrote %s (%d rows)' % (a.out, sum(counts.values())))
    by = collections.Counter()
    with open(a.out, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            by[(row['rule'], row['confidence'], row['detail'].startswith(BENIGN))] += 1
    for (rule, conf, benign), n in sorted(by.items()):
        print('  %-40s %-6s %-6s %6d' % (rule, conf, 'benign' if benign else '', n))


if __name__ == '__main__':
    main()
