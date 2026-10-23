"""License LIFECYCLE consistency per call sign across its whole ICFS filing history.

    python lifecycle.py [--db data/icfs.db] [--out reports/lifecycle.csv]

rules.py judges each filing alone (Current with a past term_end, Current with no term_end,
placeholder dates).  This script groups filings by (subsystem, call_sign), orders the granted
authorizations by grant date, and flags states that contradict the rest of the chain.
One row per affected filing per issue; before_* = the related/superseding filing, after_* =
the flagged filing.  STAs, amendments, assignments and transfers are never authorizations.
Rows the verification judged benign keep confidence low and a detail starting "likely benign:".
Dates are judged as of the harvest date (profiler.TODAY), not the run date.

What the data shows (2026-10 harvest): in SES the latest granted authorization (by grant date,
not filing date) is the one Current filing and every earlier grant is Closed/GRA; only 2 of
39,835 SES call signs carry two Current filings.  A surrender (SUR) or revocation (RVC) is
recorded on the latest authorization only; earlier grants stay Closed/GRA.  Modifications and
renewals copy the license term; SCL carries no term dates at all.  SAT extends terms by
Modification (often without recording the new term_end).  Stdlib only.
"""
import argparse
import collections
import csv
import datetime
import os
import re
import sqlite3

from freqs import filing_date
from profiler import EARLIEST, HERE, TODAY, parse_date, records

COLUMNS = ['issue_class', 'rule', 'confidence', 'subsystem', 'call_sign', 'file_number', 'filing_type', 'state',
           'before_file', 'before_value', 'after_file', 'after_value', 'expected_value', 'detail']

BASE = {  # original authorizations: what a renewal or modification presupposes
    'License', 'Registration of a receive-only earth station', 'License to communicate with a Foreign Satellite',
    'Launch and Operating Authority', 'Authorization to Operate', 'Launch Authority',
    'Replacement Satellite Application (no new frequency bands)', 'Petition for Declaratory Ruling',
    'Petition for Declaratory Ruling to be Added to the Permitted List', 'Letter of Intent',
    'Construction Permit', 'New Application', '214'}
MODS = {'Modification', 'MFS Modification', 'Modification to PDR/PPL'}
AUTH = BASE | MODS | {'Renewal'}
PPL_FAMILY = {'Petition for Declaratory Ruling', 'Petition for Declaratory Ruling to be Added to the Permitted List',
              'Modification to PDR/PPL'}
NOT_GRANTED = {'', 'DIS', 'WTD', 'DEN', 'DDA', 'D/A', 'RNA', 'DNV', 'RTS'}
ENDED = {'SUR', 'RVC'}  # authorization surrendered / revoked
SEASONAL = ('Pending - Upcoming Season', 'Pending - Current Season', 'In progress')  # IHF schedules: pending by design
YEAR = 365
CYCLES = (1826, 3652, 3653, 5479)  # 5, 10, 15-year license terms in days
BULK_STAMPS = ('2025-06-22', '2025-06-23')
LEGACY = datetime.date(1995, 1, 1)  # before this, ICFS chains are migrated paper records: originals/terms often missing
BENIGN = 'likely benign: '
EXTEND = re.compile(r'extend\w*\s+(the\s+)?(license\s+)?term|renewal', re.I)
DATE_IN_TEXT = re.compile(r'(January|February|March|April|May|June|July|August|September|October|November|December)'
                          r'\s+(\d{1,2}),?\s+(\d{4})')

RULES = {  # rule: (issue_class, default confidence, meaning)
    'current_superseded': ('superseded_still_current', 'medium',
                           'Current although a later granted authorization for the call sign supersedes it'),
    'latest_grant_not_current': ('superseded_still_current', 'low',
                                 'latest granted authorization is Closed while an older one holds Current'),
    'current_but_surrendered': ('surrendered_still_active', 'high',
                                'state Current although the filing itself was surrendered/revoked'),
    'current_after_surrender': ('surrendered_still_active', 'medium',
                                'Current although the call sign\'s original authorization was surrendered/revoked'),
    'pending_after_surrender': ('surrendered_still_active', 'medium',
                                'application still pending although the call sign was surrendered/revoked'),
    'renewal_term_gap': ('renewal_term_mismatch', 'low',
                         'granted Renewal term_begin more than 1 year from every prior term_end'),
    'duplicate_renewal': ('renewal_term_mismatch', 'low',
                          'two granted Renewals for the call sign carry the identical term'),
    'orphan_renewal': ('orphan_renewal', 'low',
                       'granted Renewal with no earlier granted original authorization for the call sign'),
    'modification_after_expiry': ('modification_after_expiry', 'low',
                                  'Modification granted after the license term ended, with no renewal filed'),
    'current_on_expired_license': ('expired_not_closed', 'low',
                                   'Current with no own term_end while the call sign\'s latest term ended'),
    'pending_on_expired_license': ('expired_not_closed', 'low',
                                   'application still pending on a call sign whose latest term ended, no renewal'),
    'closed_in_term': ('closed_in_error', 'low',
                       'every filing Closed but the last granted authorization\'s term runs into the future'),
    'current_but_dismissed': ('stale_status', 'high',
                              'state Current although the filing itself was withdrawn, dismissed or denied'),
    'transaction_not_consummated': ('stale_status', 'medium',
                                    'assignment / transfer of control still Current: consummation never recorded'),
    'stale_pending': ('stale_status', 'low', 'Pending Review for more than 5 years with no action'),
    'current_with_non_grant_action': ('stale_status', 'medium',
                                      'state Current with a last_action that is not a grant (or none at all)'),
}
DISMISSED = {'WTD', 'DIS', 'DEN', 'D/A', 'DDA'}
TRANSACTIONS = {'Assignment', 'Transfer of Control'}
STALE_YEARS = 5
CURRENT_OK = {'GRA', 'CON', 'DBE'}
UNVERIFIED = {'SRQ', 'DSG', 'DFG', 'D/G', 'DIP', 'ADM'}  # code meaning not confirmed: may legitimately be Current


def day(v):
    """A usable date, or None for blanks and placeholders (rules.py reports those)."""
    d = parse_date(v)
    if d is None or d < EARLIEST or d > TODAY + datetime.timedelta(days=366 * 30):
        return None
    return d


def F(r):
    """The top-level fields this script reasons from, with parsed dates."""
    f = {k: r.get(k) or '' for k in ('subsystem', 'call_sign', 'file_number', 'filing_type', 'state',
                                      'last_action', 'grant_date', 'term_begin', 'term_end', 'date_filed',
                                      'last_action_date', 'action_pn_date', 'applicant_frn')}
    f['desc'] = ' '.join(s.get('s1_description') or '' for s in r.get('s1') or [])
    for short, k in (('gd', 'grant_date'), ('tb', 'term_begin'), ('te', 'term_end'), ('dd', 'date_filed'),
                     ('ld', 'last_action_date'), ('pn', 'action_pn_date')):
        f[short] = day(f[k])
    f['dd'] = day(str(filing_date(r) or '')) or f['dd']  # file-number date first: date_filed has placeholders
    if f['gd'] and f['gd'] > TODAY + datetime.timedelta(days=366):
        f['gd'] = None
    if f['grant_date'][:10] in BULK_STAMPS and f['ld'] != f['gd']:
        f['gd'] = f['ld']  # 200 SAT/SES grant dates stamped by a 2025-06-22/23 bulk load, none matching last action
    if f['gd'] and f['dd'] and f['gd'] < f['dd'] - datetime.timedelta(days=30):
        f['gd'] = f['ld'] if f['ld'] and f['ld'] >= f['dd'] else None  # granted before filed: stale grant_date
    if f['gd'] and f['ld'] and f['gd'] > f['ld'] + datetime.timedelta(days=1):
        f['gd'] = f['ld']  # granted after the last action is impossible: 2025-06-22/23 bulk-load stamps (DSG, DFG, D/G)
    return f


def granted(f):
    return (f['filing_type'] in AUTH and f['gd'] is not None and f['last_action'] not in NOT_GRANTED
            and not f['state'].startswith('Pending'))


def was_granted(f):
    """Granted at some point, even when grant_date is missing (legacy records, later surrenders)."""
    return f['filing_type'] in AUTH and f['last_action'] not in NOT_GRANTED and not f['state'].startswith('Pending')


def key(f):
    return (f['gd'] or datetime.date.min, f['dd'] or datetime.date.min, f['file_number'])


def pending(f):
    """An open application that a dead license should not carry (STAs/amendments may legitimately follow)."""
    return (f['state'].startswith('Pending') and f['state'] not in SEASONAL
            and f['filing_type'] in AUTH | {'Assignment', 'Transfer of Control'})


def desc(f, *fields):
    """'type state/action grant_date=.. te=..' for before_value/after_value; grant_date is the corrected one."""
    s = '%s %s/%s' % (f['filing_type'], f['state'], f['last_action'] or '-')
    for k in fields:
        v = f[k][:10] if f[k] else ''
        if k == 'grant_date' and str(f['gd'] or '') != v:
            v = '%s (recorded %s)' % (f['gd'] or '?', v or 'blank')
        s += ' %s=%s' % (k, v)
    return s


def plus_years(d, n):
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)


def stop_of(group, grants, types):
    """The latest surrender/revocation among filings of the given types, unless a later grant revived the call sign."""
    ended = sorted((f for f in group if f['filing_type'] in types and f['last_action'] in ENDED),
                   key=lambda f: f['ld'] or f['gd'] or datetime.date.min)
    if not ended:
        return None, None
    stop = ended[-1]
    stop_day = stop['ld'] or stop['gd']
    if stop_day and any(g['gd'] > stop_day and g['last_action'] not in ENDED for g in grants):
        return None, None  # a grant after the surrender: re-licensed, or only part of the license was surrendered
    return stop, stop_day


def check(group, today=TODAY, ctx=None):
    """[(rule, flagged, related, expected, detail, confidence)] for one call sign's filings.
    ctx: {(subsystem, frn, term_end): {call signs}} of granted authorizations, for orphan renewals."""
    out = []

    def add(rule, x, y, exp, detail, conf=None, benign=''):
        out.append((rule, x, y, exp, (BENIGN + benign + '; ' if benign else '') + detail,
                    'low' if benign else conf or RULES[rule][1]))

    grants = sorted((f for f in group if granted(f)), key=key)
    sub = group[0]['subsystem']
    termed = [g for g in grants if g['te']]
    latest_te = max((g['te'] for g in termed), default=None)
    stop, stop_day = stop_of(group, grants, AUTH)            # anything surrendered: pending applications die with it
    bstop, bstop_day = stop_of(group, grants, BASE | {'Renewal'})  # the license itself surrendered (not one mod)
    flagged, dup, moved = set(), set(), set()

    # surrendered / revoked: nothing else should stay Current or pending
    for f in group:
        if f['state'] == 'Current' and f['last_action'] in ENDED:
            detail = 'state Current but last_action %s on %s' % (f['last_action'], f['last_action_date'][:10])
            if f['ld'] and f['gd'] and f['ld'] <= f['gd']:
                add('current_but_surrendered', f, None, 'check last_action (may be GRA)',
                    detail + '; the surrender date is not after the grant date: the action code may be wrong', 'medium')
            else:
                add('current_but_surrendered', f, None, 'Closed', detail)
            flagged.add(f['file_number'])
    for f in group:
        if f['file_number'] in flagged:
            continue
        if bstop and f is not bstop and f['state'] == 'Current':
            what = '%s %s on %s (%s)' % (bstop['filing_type'], bstop['last_action'], bstop['last_action_date'][:10],
                                         bstop['file_number'])
            add('current_after_surrender', f, bstop, 'Closed',
                '%s; this filing granted %s, before it, so the surrender ended it too.%s' % (
                    what, f['gd'] or '?', ' SAT call signs can cover several authorizations, check scope.'
                    if sub == 'SAT' else ''), 'medium' if sub == 'SAT' else 'high')
            flagged.add(f['file_number'])
        elif stop and f is not stop and pending(f):
            after = stop_day and f['dd'] and f['dd'] > stop_day
            what = '%s %s on %s (%s)' % (stop['filing_type'], stop['last_action'], stop['last_action_date'][:10],
                                         stop['file_number'])
            if after:
                add('pending_after_surrender', f, stop, '', '%s; this application filed %s after the surrender '
                    '(re-application?)' % (what, f['dd']), 'low')
            elif f['state'].startswith('Pending Consummation') and f['last_action'] == 'GRA':
                add('pending_after_surrender', f, stop, 'Closed (consummated or lapsed)',
                    '%s; this transaction was granted %s but consummation was never recorded: record CON if a notice '
                    'was filed, else mark lapsed' % (what, f['gd'] or '?'), 'medium')
            else:
                add('pending_after_surrender', f, stop, 'Closed (dismiss as moot)',
                    '%s; this application filed %s before the surrender and never acted on' % (what, f['dd'] or '?'),
                    'high' if not f['last_action'] else 'medium')
            flagged.add(f['file_number'])

    # superseded but still Current; the superseding grant itself may wrongly be Closed
    live = [g for g in grants if g['last_action'] in ('GRA', 'CON')]
    for x in grants:
        if x['state'] != 'Current' or x['file_number'] in flagged:
            continue
        later = [y for y in live if key(y) > key(x) and y['gd'] >= x['gd']
                 and not (x['filing_type'] in MODS and y['filing_type'] in BASE)]  # a base granted after its own mod
        if not later:
            continue
        y = later[-1]
        same_day = y['gd'] == x['gd']
        detail = '%s %s granted %s supersedes this %s granted %s; %d later grant(s) in all.%s' % (
            y['filing_type'], y['file_number'], y['gd'], x['filing_type'], x['gd'], len(later),
            ' Later grant is also Current (two Current filings).' if y['state'] == 'Current' else '')
        benign = ('same-day grants: order uncertain, the Current filing usually carries the newest term' if same_day else
                  'cumulative Permitted-List / market-access petitions can all stay in force'
                  if sub == 'SAT' and x['filing_type'] in PPL_FAMILY and y['filing_type'] in PPL_FAMILY else
                  'a 214 Modification modifies the 214 rather than replacing it'
                  if sub == 'ITC' and y['filing_type'] in MODS else
                  'granted within 60 days of the harvest: not yet rolled over'
                  if (today - y['gd']).days <= 60 else '')
        add('current_superseded', x, y, 'Closed', detail, 'high' if sub in ('SES', 'SCL', 'IHF') else 'medium', benign)
        flagged.add(x['file_number'])
        if (y is live[-1] and y['state'] == 'Closed' and y['last_action'] == 'GRA' and not stop
                and y['file_number'] not in moved and (y['te'] is None or y['te'] >= today)):
            moved.add(y['file_number'])
            fam = sub == 'SAT' and (x['filing_type'] in PPL_FAMILY) != (y['filing_type'] in PPL_FAMILY) or (
                x['filing_type'] == 'Modification to PDR/PPL' and y['filing_type'] in PPL_FAMILY - MODS)
            add('latest_grant_not_current', y, x, 'Current',
                'this %s granted %s is the latest grant for the call sign but Closed, while older %s %s '
                '(granted %s) holds Current: the Current flag sits on the wrong filing' % (
                    y['filing_type'], y['gd'], x['filing_type'], x['file_number'], x['gd']), 'medium',
                'same-day grants: which one is Current is arbitrary' if same_day else
                'different authorization families (PPL vs PDR, or a modification of the PDR) can both be in force'
                if fam else '')

    # renewals: term continuity and an original to renew
    for i, r in enumerate(grants):
        if r['filing_type'] != 'Renewal':
            continue
        earlier = grants[:i]
        had_base = any(g['filing_type'] in BASE for g in earlier) or any(
            f['filing_type'] in BASE and was_granted(f) and f['dd'] and f['dd'] < r['gd'] for f in group)
        first = not any(g['filing_type'] == 'Renewal' for g in earlier)  # later renewals inherit the same gap
        if not had_base and first:
            bases = [f for f in group if f['filing_type'] in BASE and (f['dd'] or datetime.date.max) < r['gd']]
            later_bases = [f for f in group if f['filing_type'] in BASE and granted(f)]
            implied = plus_years(r['tb'] or r['gd'], -10)
            elsewhere = sorted(ctx.get((sub, r['applicant_frn'], r['tb']), set()) - {r['call_sign']}) \
                if ctx and r['tb'] and r['applicant_frn'] else []
            if len(elsewhere) == 1 and r['dd'] and r['dd'].year >= 2000:
                add('orphan_renewal', r, None, elsewhere[0], 'Renewal granted %s; the same licensee\'s %s has a granted '
                    'term ending %s, exactly where this renewal begins: probably filed under the wrong call sign'
                    % (r['gd'], elsewhere[0], r['tb']), 'high')
            elif earlier:
                add('orphan_renewal', r, earlier[-1], '', 'Renewal granted %s; earlier grants %s show the license existed'
                    % (r['gd'], ' '.join(g['file_number'] for g in earlier)), benign='only the original record is absent')
            elif bases:
                bad = [b for b in bases if b['te'] or b['tb']]
                add('orphan_renewal', r, bases[0], '', 'Renewal granted %s; original filing(s) %s exist but none is '
                    'recorded as granted' % (r['gd'], ' '.join('%s(%s)' % (b['file_number'], b['last_action'] or b['state'])
                                                             for b in bases)), 'medium')
                for b in bad:  # an original that carries term dates was evidently granted: its last_action is wrong
                    add('orphan_renewal', b, r, 'last_action GRA / grant_date from the record',
                        'original %s shows %s but carries term %s..%s that the renewal %s continues: it was evidently '
                        'granted' % (b['file_number'], b['last_action'] or b['state'], b['term_begin'][:10],
                                     b['term_end'][:10], r['file_number']), 'medium')
            elif implied < LEGACY or (r['dd'] and r['dd'] < LEGACY):
                add('orphan_renewal', r, None, '', 'Renewal granted %s; the original (about %d) predates ICFS'
                    % (r['gd'], implied.year), benign='pre-ICFS original never migrated')
            elif later_bases:
                add('orphan_renewal', r, later_bases[0], '', 'Renewal granted %s; original(s) %s granted only after it'
                    % (r['gd'], ' '.join('%s(gd=%s)' % (b['file_number'], b['gd']) for b in later_bases)), 'medium')
            else:
                add('orphan_renewal', r, None, '', 'Renewal granted %s; no original authorization and no earlier grant '
                    'in ICFS for this call sign' % r['gd'], 'medium')
        twin = [g for g in earlier if g['filing_type'] == 'Renewal' and g['tb'] and g['tb'] == r['tb']
                and g['te'] == r['te']]
        if twin:
            d = twin[0]
            for x, y in ((r, d), (d, r)):
                if x['file_number'] in dup:
                    continue
                dup.add(x['file_number'])
                detail = ('two granted Renewals carry the same term %s .. %s: %s (filed %s, granted %s, %s) and %s '
                          '(filed %s, granted %s, %s)' % (r['term_begin'][:10], r['term_end'][:10], d['file_number'],
                                                          d['dd'], d['gd'], d['state'], r['file_number'], r['dd'],
                                                          r['gd'], r['state']))
                overwritten = x['tb'] and x['dd'] and (x['tb'] - x['dd']).days > 180 or not x['date_filed']
                if overwritten:
                    prior = [g for g in grants if g['te'] and x['te'] and g['te'] < x['te'] and g is not x]
                    p = max(prior, key=lambda g: g['te']) if prior else None
                    exp = 'term %s .. %s (from %s), and restore grant/filing dates' % (
                        p['te'], plus_years(p['te'], 15 if sub == 'SES' else 10), p['file_number']) if p else ''
                    add('duplicate_renewal', x, y, exp, detail + '; this filing\'s term starts long after it was filed '
                        '(or date_filed is blank): it was overwritten with its successor\'s term', 'medium')
                else:
                    add('duplicate_renewal', x, y, '', detail, benign='double submission or the clean partner of a pair')
            continue
        prior = [g for g in earlier if g['te'] and r['te'] and g['te'] < r['te']]  # earlier term periods only
        if r['tb'] and prior and not any(abs((r['tb'] - g['te']).days) <= YEAR for g in earlier if g['te']):
            p = min(prior, key=lambda g: abs((r['tb'] - g['te']).days))
            gap = (r['tb'] - p['te']).days
            legacy = p['gd'] < LEGACY
            exact = any(abs(abs(gap) - k) <= 3 for k in CYCLES)
            detail = 'Renewal term_begin %s is %d days %s the prior term_end %s of %s %s (granted %s)' % (
                r['term_begin'][:10], abs(gap), 'after' if gap > 0 else 'before', p['term_end'][:10],
                p['filing_type'], p['file_number'], p['gd'])
            if legacy:
                add('renewal_term_gap', r, p, p['term_end'][:10], detail, benign='legacy pre-1995 chain: missing history')
            elif exact:
                cycle = min(CYCLES, key=lambda k: abs(abs(gap) - k))
                add('renewal_term_gap', r, p, 'term %s .. %s' % (p['te'], p['te'] + datetime.timedelta(days=cycle)),
                    detail + '; exactly one license cycle off: the record carries the %s cycle\'s term (move it to the '
                    'filing it belongs to)' % ('next' if gap > 0 else 'previous'), 'medium')
            elif gap < 0:
                add('renewal_term_gap', r, p, '', detail, benign='compared against another license chain of the call sign')
            else:
                add('renewal_term_gap', r, p, p['term_end'][:10], detail + '; a lapse then a late grant, or one of the '
                    'two dates is wrong', 'low')

    # modifications granted after the term ran out
    for i, m in enumerate(grants):
        if m['filing_type'] not in MODS:
            continue
        prior = [g for g in grants[:i] if g['te']]
        if not prior:
            continue
        p = max(prior, key=lambda g: g['te'])
        if m['gd'] <= p['te'] + datetime.timedelta(days=30):
            continue
        renewed = [f for f in group if f['filing_type'] == 'Renewal' and f['dd'] and f['dd'] <= m['gd']
                   and f['dd'] >= (p['gd'] or datetime.date.min) and f['last_action'] not in NOT_GRANTED - {''}]
        if renewed:
            continue
        own = m['te'] and m['te'] > m['gd']
        timely = sub == 'SAT' and m['dd'] and m['dd'] <= p['te']
        between = [f for f in group if f is not m and f is not p and f['dd'] and f['dd'] <= p['te']
                   and f['dd'] >= (p['gd'] or datetime.date.min) and f['last_action'] not in NOT_GRANTED
                   and not f['state'].startswith('Pending') and (f['gd'] or f['dd']) < m['gd']
                   and f['filing_type'] in MODS | {'Special Temporary Authority', 'Renewal'} | PPL_FAMILY]
        detail = '%s granted %s, %d days after the term_end %s set by %s %s; no renewal filed in between' % (
            m['filing_type'], m['gd'], (m['gd'] - p['te']).days, p['term_end'][:10], p['filing_type'], p['file_number'])
        if own:
            add('modification_after_expiry', m, p, '', detail + '; carries its own term to %s' % m['term_end'][:10],
                benign='the modification set a new term (de facto renewal)')
        elif p['gd'] < LEGACY:
            add('modification_after_expiry', m, p, '', detail, benign='legacy pre-1995 chain: renewals often missing')
        elif between:
            x = between[-1]
            add('modification_after_expiry', m, p, '', detail + '; %s %s filed %s before the term ended continued the '
                'authorization' % (x['filing_type'], x['file_number'], x['dd']), benign='authorization continued by an '
                'earlier filing')
            if x['filing_type'] in MODS and not x['te'] and x['file_number'] not in flagged:
                flagged.add(x['file_number'])
                add('modification_after_expiry', x, p, 'term_end from the %s grant' % x['file_number'],
                    'extension %s filed %s, before the term_end %s of %s, has no term_end: the extended term was never '
                    'recorded' % (x['filing_type'], x['dd'], p['term_end'][:10], p['file_number']), 'medium')
        elif timely:
            add('modification_after_expiry', m, p, 'term_end from this grant', detail + '; filed %s, before the term '
                'ended: a term extension whose new term_end was not recorded' % m['dd'], 'medium')
        else:
            add('modification_after_expiry', m, p, '', detail + '; check for a missing renewal or a lapsed license',
                'medium')

    # term over, nothing renewing it, yet something is still open
    if latest_te and latest_te < today:
        renewing = any(f['filing_type'] == 'Renewal' and pending(f) for f in group) or (sub == 'SAT' and any(
            f['filing_type'] in MODS and pending(f) and f['dd'] and f['dd'] <= latest_te for f in group))
        alive = any(f['state'] == 'Current' and f['te'] and f['te'] >= today for f in group)
        revived = any(f['state'] == 'Current' and f['filing_type'] in AUTH and (
            (f['gd'] and f['gd'] > latest_te) or (not f['te'] and f['dd'] and f['dd'] <= latest_te)) for f in group)
        src = max(termed, key=lambda g: g['te'])
        if not renewing and not alive:
            for f in group:
                if f['file_number'] in flagged or f['filing_type'] == 'Renewal':
                    continue
                if f['state'] == 'Current' and f['term_end'] == '' and (f['gd'] or today) <= latest_te:
                    detail = ('Current %s has no term of its own; the call sign\'s latest term_end %s (%s %s) has passed '
                              'and no renewal is pending' % (f['filing_type'], src['term_end'][:10], src['filing_type'],
                                                             src['file_number']))
                    m = DATE_IN_TEXT.search(f['desc'])
                    if sub == 'SAT' and EXTEND.search(f['desc']):
                        add('current_on_expired_license', f, src, 'term_end %s' % ('%s %s %s' % m.groups() if m else
                                                                                   'from the grant'),
                            detail + '; this Modification extends/renews the term: keep Current and record its new '
                            'term_end', 'medium')
                    elif (today - latest_te).days > YEAR and not any(x['dd'] and x['dd'] > latest_te for x in group):
                        add('current_on_expired_license', f, src, 'Closed', detail + '; nothing filed since', 'medium')
                    else:
                        add('current_on_expired_license', f, src, '', detail + '; recent expiry or later activity: check '
                            'for an extension', 'low')
                elif pending(f):
                    late = f['dd'] and f['dd'] > latest_te
                    detail = '%s %s filed %s; the call sign\'s latest term_end %s (%s %s) has passed and no renewal is ' \
                             'pending' % (f['filing_type'], f['state'], f['dd'] or '?', src['term_end'][:10],
                                          src['filing_type'], src['file_number'])
                    if sub == 'SAT':
                        add('pending_on_expired_license', f, src, '', detail,
                            benign='SAT terms are extended by Modification; the extension\'s term_end is usually what '
                            'is missing')
                    elif revived:
                        add('pending_on_expired_license', f, src, '', detail,
                            benign='a Current grant after the term end (or without a term) keeps the call sign alive')
                    elif late:
                        add('pending_on_expired_license', f, src, '', detail + ' (filed after expiry)',
                            benign='a new application or reinstatement for a lapsed call sign')
                    elif f['filing_type'] in BASE:
                        add('pending_on_expired_license', f, src, '', detail, 'low')
                    else:
                        add('pending_on_expired_license', f, src, 'Closed (dismiss as moot)',
                            detail + ' (filed while the license was alive, never acted on); close the expired '
                            'license too', 'medium')

    # all Closed, but the last grant is still within its term
    if grants and all(f['state'] == 'Closed' for f in group) and not stop:
        last = grants[-1]
        if last['last_action'] == 'GRA' and last['te'] and last['te'] > today:
            detail = ('every filing for the call sign is Closed, yet the last grant (%s, granted %s) runs to %s and '
                      'nothing surrendered, revoked or superseded it' % (last['filing_type'], last['gd'],
                                                                          last['term_end'][:10]))
            prev = grants[-2] if len(grants) > 1 else None
            if sub == 'SAT':
                add('closed_in_term', last, prev, '', detail, benign='satellite retired or lost without a recorded '
                    'surrender; Closed is probably right, term_end is stale')
            elif last['pn'] and last['gd'] and (last['pn'] - last['gd']).days > YEAR:
                add('closed_in_term', last, prev, 'record the cancellation (last_action / term_end)',
                    detail + '; action PN %s, long after the grant: a later cancellation changed the state but not '
                    'last_action' % last['pn'], 'medium')
            else:
                add('closed_in_term', last, prev, 'Current, only if confirmed still authorized', detail +
                    '; check the record for an unrecorded cancellation first', 'low')
    return out


def single(f, today=TODAY):
    """[(rule, expected, detail, confidence)] judged from one filing alone (any filing, call sign or not)."""
    out = []
    act, state, typ = f['last_action'], f['state'], f['filing_type']
    if state == 'Current' and act in DISMISSED:
        out.append(('current_but_dismissed', 'Closed', 'state Current but last_action %s on %s: the filing was never '
                    'granted' % (act, f['last_action_date'][:10] or '?'), 'high'))
    if state == 'Current' and act not in CURRENT_OK | DISMISSED | ENDED:  # SUR/RVC: current_but_surrendered
        out.append(('current_with_non_grant_action', '' if act in UNVERIFIED else 'GRA or Closed per the record',
                    'state Current but last_action %s%s' % (act or 'blank', '; code meaning unverified' if act in UNVERIFIED
                                                            else ''), 'low' if act in UNVERIFIED else 'medium'))
    if state == 'Current' and typ in TRANSACTIONS:
        out.append(('transaction_not_consummated', 'Closed/CON (or Closed/GRA if it lapsed unconsummated)',
                    '%s granted %s is still Current: a transaction is closed by consummation, which was never recorded'
                    % (typ, f['gd'] or '?'), 'medium'))
    if state == 'Pending Review' and not act and f['dd'] and (today - f['dd']).days > STALE_YEARS * YEAR:
        out.append(('stale_pending', 'Closed (dismiss or record the action taken)', 'Pending Review since %s (%d years) '
                    'with no action' % (f['dd'], (today - f['dd']).days // YEAR), 'low'))
    return out


def rows(groups, today=TODAY, ctx=None, alone=()):
    """alone: every filing (F records), for the single-filing rules."""
    for (sub, cs), group in sorted(groups.items()):
        for rule, x, y, exp, detail, conf in check(group, today, ctx):
            yield [RULES[rule][0], rule, conf, sub, cs, x['file_number'], x['filing_type'], x['state'],
                   y['file_number'] if y else '', desc(y, 'grant_date', 'term_begin', 'term_end') if y else '',
                   x['file_number'], desc(x, 'grant_date', 'term_begin', 'term_end'), exp, detail]
    for x in alone:
        for rule, exp, detail, conf in single(x, today):
            yield [RULES[rule][0], rule, conf, x['subsystem'], x['call_sign'].strip(), x['file_number'],
                   x['filing_type'], x['state'], '', '', x['file_number'], desc(x, 'grant_date', 'last_action_date'),
                   exp, detail]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=os.path.join(HERE, 'data', 'icfs.db'))
    ap.add_argument('--out', default=os.path.join(HERE, 'reports', 'lifecycle.csv'))
    a = ap.parse_args()
    conn = sqlite3.connect('file:%s?mode=ro' % a.db, uri=True)
    groups = collections.defaultdict(list)
    ctx = collections.defaultdict(set)
    alone = []
    for r in records(conn):
        f = F(r)
        alone.append({k: f[k] for k in ('subsystem', 'call_sign', 'file_number', 'filing_type', 'state', 'last_action',
                                        'grant_date', 'last_action_date', 'gd', 'dd')})
        if r.get('call_sign'):
            groups[(f['subsystem'], f['call_sign'].strip())].append(f)
            if granted(f) and f['te'] and f['applicant_frn']:
                ctx[(f['subsystem'], f['applicant_frn'], f['te'])].add(f['call_sign'].strip())
    counts = collections.Counter()
    with open(a.out, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for row in rows(groups, ctx=ctx, alone=alone):
            w.writerow(row)
            counts[(row[1], row[2], row[13].startswith(BENIGN))] += 1
    print('wrote %s (%d rows, %d call signs)' % (a.out, sum(counts.values()), len(groups)))
    for (rule, conf, benign), n in sorted(counts.items()):
        print('  %-28s %-6s %-6s %6d' % (rule, conf, 'benign' if benign else '', n))


if __name__ == '__main__':
    main()
