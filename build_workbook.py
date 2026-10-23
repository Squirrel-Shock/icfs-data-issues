"""Build the ICFS data-repair workbook from every checker CSV in reports/.

    python build_workbook.py [--reports reports] [--db data/icfs.db] [--out reports/ICFS_data_issues.xlsx]

Sheets: Summary (source, caveats, one line per rule), Filing index (one row per affected file
number), Call sign index (one row per affected call sign), then one sheet per issue class holding
every CSV row exactly once (split into "... (2)" past Excel's row limit).  Written as SpreadsheetML
with zipfile and inline strings: stdlib only, no openpyxl.  validate() re-opens the file, parses
every part and reconciles row counts against the CSVs.
"""
import argparse
import collections
import csv
import glob
import os
import re
import sqlite3
import zipfile
from xml.sax.saxutils import escape

from profiler import HERE
from rule_notes import NOTES

STD = ['issue_class', 'rule', 'confidence', 'subsystem', 'call_sign', 'file_number', 'filing_type', 'state',
       'before_file', 'before_value', 'after_file', 'after_value', 'expected_value', 'detail']
ISSUE_COLUMNS = STD + ['severity_or_scope', 'likely_benign', 'repair_guidance', 'source_csv']
WIDTHS = {'issue_class': 22, 'rule': 30, 'confidence': 11, 'subsystem': 10, 'call_sign': 13, 'file_number': 25,
          'filing_type': 24, 'state': 14, 'before_file': 25, 'before_value': 40, 'after_file': 25, 'after_value': 40,
          'expected_value': 30, 'detail': 90, 'severity_or_scope': 14, 'likely_benign': 12, 'repair_guidance': 70,
          'source_csv': 16}
RANK = {'high': 0, 'medium': 1, 'low': 2}
MAX_ROWS = 1048576 - 1  # data rows per sheet under the header
BENIGN = 'likely benign'
CAP_BELOW = 0.3  # verified precision under this caps every row of the rule at the verified confidence
CLASS_NAMES = {
    ('findings', 'dates'): 'Filing dates', ('findings', 'status'): 'Filing status', ('findings', 'file_number'): 'File number',
    ('findings', 'identity'): 'Applicant FRN', ('findings', 'applicant_identity'): 'Applicant identity',
    ('findings', 'duplicate'): 'Duplicate filings', ('sites', 'applicant_frn'): 'Applicant FRN', ('lifecycle', 'stale_status'): 'Stale status',
    ('lifecycle', 'orphan_renewal'): 'Orphan renewals', ('lifecycle', 'superseded_still_current'): 'Superseded still Current',
    ('lifecycle', 'renewal_term_mismatch'): 'Renewal term mismatch',
    ('lifecycle', 'surrendered_still_active'): 'Surrendered still active',
    ('lifecycle', 'modification_after_expiry'): 'Modification after expiry',
    ('lifecycle', 'expired_not_closed'): 'Expired not closed', ('lifecycle', 'closed_in_error'): 'Closed in error',
    ('sat', 'band'): 'SAT bands', ('sat', 'direction'): 'SAT bands', ('sat', 'beam'): 'SAT beams',
    ('sat', 'lifetime'): 'SAT lifetime', ('sat', 'orbit'): 'SAT orbit', ('sat', 'history'): 'SAT history',
    ('sites', 'location'): 'Site location', ('sites', 'elevation'): 'Site elevation',
    ('sites', 'identity'): 'Applicant identity', ('sites', 'call_sign'): 'Call sign',
    ('swaps', 'Schedule B swapped between call signs'): 'Schedule B swapped',
    ('swaps', 'Schedule B copied from another call sign'): 'Schedule B copied',
    ('walk', 'Spectrum replaced between versions'): 'Spectrum replaced (history)',
}
ILLEGAL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]')


# --- reading ---------------------------------------------------------------------------------------

def note(source, rule):
    return NOTES.get('%s:%s' % (source, rule)) or NOTES.get(rule) or ('low', None, rule.replace('_', ' '), '')


def normalize(source, row):
    """One CSV row as the workbook's issue row."""
    out = {k: (row.get(k) or '').strip() if k != 'detail' else (row.get(k) or '') for k in STD}
    out['file_number'] = out['file_number'] or out['after_file']
    out['after_file'] = out['after_file'] or out['file_number']
    cls = out['issue_class'] or 'Other'
    out['issue_class'] = CLASS_NAMES.get((source, cls), cls[:1].upper() + cls[1:].replace('_', ' '))
    vconf, prec, _, guidance = note(source, out['rule'])
    out['confidence'] = out['confidence'] if out['confidence'] in RANK else vconf
    if prec is not None and prec < CAP_BELOW and RANK[out['confidence']] < RANK.get(vconf, 2):
        out['confidence'] = vconf  # verification found most of this rule's rows not real
    out['severity_or_scope'] = row.get('severity') or row.get('scope') or ''
    out['likely_benign'] = 'yes' if out['detail'].lstrip().lower().startswith(BENIGN) else 'no'
    out['repair_guidance'] = guidance
    out['source_csv'] = source + '.csv'
    return out


def load(reports):
    rows, per_csv = [], collections.Counter()
    for path in sorted(glob.glob(os.path.join(reports, '*.csv'))):
        source = os.path.splitext(os.path.basename(path))[0]
        with open(path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            if 'rule' not in (reader.fieldnames or []):
                continue
            for row in reader:
                rows.append(normalize(source, row))
                per_csv[(source, row['rule'])] += 1
    return rows, per_csv


# --- xlsx writing ---------------------------------------------------------------------------------

def col(n):
    s = ''
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def cell(ref, v, style=0):
    st = ' s="%d"' % style if style else ''
    if isinstance(v, bool):
        v = 'yes' if v else 'no'
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return '<c r="%s"%s><v>%s</v></c>' % (ref, st, repr(v) if isinstance(v, float) else v)
    text = ILLEGAL.sub('', str(v if v is not None else ''))
    if not text:
        return ''
    return '<c r="%s" t="inlineStr"%s><is><t xml:space="preserve">%s</t></is></c>' % (ref, st, escape(text))


def sheet_xml(rows, widths, header_row=1, freeze=True, autofilter=True, bold_rows=()):
    """rows: lists of cell values; header_row (1-based) is bold, frozen and filtered."""
    ncol = max((len(r) for r in rows), default=1)
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">']
    out.append('<dimension ref="A1:%s%d"/>' % (col(ncol), max(len(rows), 1)))
    if freeze:
        out.append('<sheetViews><sheetView workbookViewId="0"><pane ySplit="%d" topLeftCell="A%d" activePane="bottomLeft" '
                   'state="frozen"/><selection pane="bottomLeft"/></sheetView></sheetViews>' % (header_row, header_row + 1))
    else:
        out.append('<sheetViews><sheetView workbookViewId="0"/></sheetViews>')
    out.append('<sheetFormatPr defaultRowHeight="15"/>')
    out.append('<cols>%s</cols>' % ''.join('<col min="%d" max="%d" width="%g" customWidth="1"/>' % (i, i, w)
                                           for i, w in enumerate(widths, 1)))
    out.append('<sheetData>')
    for i, r in enumerate(rows, 1):
        style = 1 if i == header_row or i in bold_rows else 0
        cells = ''.join(cell('%s%d' % (col(j), i), v, style) for j, v in enumerate(r, 1))
        out.append('<row r="%d">%s</row>' % (i, cells))
    out.append('</sheetData>')
    if autofilter and len(rows) >= header_row:
        out.append('<autoFilter ref="A%d:%s%d"/>' % (header_row, col(ncol), len(rows)))
    out.append('<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/></worksheet>')
    return ''.join(out), ('A%d:%s%d' % (header_row, col(ncol), len(rows)) if autofilter else None)


STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
          '<fonts count="2"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
          '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>'
          '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
          '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
          '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
          '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
          '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
          '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def write_xlsx(path, sheets):
    """sheets: [(name, xml, filter_ref)]."""
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    ct += ['<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.'
           'spreadsheetml.worksheet+xml"/>' % i for i in range(1, len(sheets) + 1)]
    ct.append('</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>')
    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><bookViews><workbookView/></bookViews><sheets>']
    wb += ['<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (escape(n, {'"': '&quot;'}), i, i)
           for i, (n, _, _) in enumerate(sheets, 1)]
    wb.append('</sheets><definedNames>')
    wb += ['<definedName name="_xlnm._FilterDatabase" localSheetId="%d" hidden="1">\'%s\'!%s</definedName>' % (
        i, escape(n.replace("'", "''")), re.sub(r'([A-Z]+)(\d+)', r'$\1$\2', ref))
        for i, (n, _, ref) in enumerate(sheets) if ref]
    wb.append('</definedNames></workbook>')
    wbrels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    wbrels += ['<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
               'Target="worksheets/sheet%d.xml"/>' % (i, i) for i in range(1, len(sheets) + 1)]
    wbrels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                  'Target="styles.xml"/></Relationships>' % (len(sheets) + 1))
    tmp = path + '.tmp'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', ''.join(ct))
        z.writestr('_rels/.rels', rels)
        z.writestr('xl/workbook.xml', ''.join(wb))
        z.writestr('xl/_rels/workbook.xml.rels', ''.join(wbrels))
        z.writestr('xl/styles.xml', STYLES)
        for i, (_, xml, _) in enumerate(sheets, 1):
            z.writestr('xl/worksheets/sheet%d.xml' % i, xml)
    os.replace(tmp, path)


def sheet_name(name, used):
    base = re.sub(r'[\[\]:*?/\\]', '-', name)[:31].strip() or 'Sheet'
    n, cand = 1, base
    while cand.lower() in used:
        n += 1
        suffix = ' (%d)' % n
        cand = base[:31 - len(suffix)] + suffix
    used.add(cand.lower())
    return cand


# --- workbook ---------------------------------------------------------------------------------------

def db_facts(db):
    facts = {'harvested_at': '?', 'filings': 0, 'api_total': 0, 'by_sub': {}, 'totals': {}}
    try:
        conn = sqlite3.connect('file:%s?mode=ro' % db, uri=True)
        meta = dict(conn.execute('SELECT key, value FROM meta'))
        facts['harvested_at'] = meta.get('harvested_at', '?')
        facts['api_total'] = int(meta.get('api_total_records') or 0)
        facts['by_sub'] = dict(conn.execute('SELECT subsystem, COUNT(*) FROM filings GROUP BY subsystem'))
        facts['filings'] = sum(facts['by_sub'].values())
        facts['totals'] = dict(conn.execute('SELECT subsystem, total_records FROM totals'))
    except sqlite3.Error:
        pass
    return facts


def best(confs):
    return min(confs, key=lambda c: RANK.get(c, 3)) if confs else ''


def build(rows, per_csv, facts, out):
    used, sheets = set(), []
    by_class = collections.defaultdict(list)
    for r in rows:
        by_class[r['issue_class']].append(r)
    class_sheets = {}
    names = {}
    for cls in sorted(by_class, key=lambda c: (-len(by_class[c]), c)):  # largest classes first after the indexes
        group = sorted(by_class[cls], key=lambda r: (r['likely_benign'] == 'yes', RANK.get(r['confidence'], 3),
                                                     r['rule'], r['file_number'], r['detail']))
        parts = [group[i:i + MAX_ROWS] for i in range(0, len(group), MAX_ROWS)] or [[]]
        names[cls] = []
        for k, part in enumerate(parts):
            nm = sheet_name(cls if k == 0 else '%s (%d)' % (cls, k + 1), used)
            names[cls].append(nm)
            class_sheets[nm] = part
    # per rule
    stats = {}
    for r in rows:
        key = (r['source_csv'][:-4], r['rule'])
        s = stats.setdefault(key, {'rows': 0, 'files': set(), 'cs': set(), 'benign': 0, 'conf': collections.Counter(),
                                   'cls': r['issue_class']})
        s['rows'] += 1
        s['files'].add(r['file_number'])
        if r['call_sign']:
            s['cs'].add(r['call_sign'])
        s['benign'] += r['likely_benign'] == 'yes'
        s['conf'][r['confidence']] += 1
    for k in NOTES:  # verified rules that no longer produce rows still get a summary line
        rule = k.split(':')[-1]
        src = k.split(':')[0] if ':' in k else ''
        if (src, rule) not in stats if src else not any(key[1] == rule for key in stats):
            stats[(src, rule)] = {'rows': 0, 'files': set(), 'cs': set(), 'benign': 0, 'conf': collections.Counter(),
                                  'cls': ''}

    def benign_label(s):
        b, n = s['benign'], s['rows']
        return 'no' if b == 0 else 'yes (all %d rows)' % n if b == n else 'partly (%d of %d rows)' % (b, n)

    table, empty = [], []
    for (src, rule), s in stats.items():
        vconf, prec, desc, guide = note(src, rule)
        if not s['rows']:
            empty.append([rule, desc, src + '.csv' if src else ''])
            continue
        table.append([s['cls'], rule, desc, vconf, prec if prec is not None else 'n/a (new or split rule)', benign_label(s),
                      len(s['files']), len(s['cs']), s['rows'], s['rows'] - s['benign'], s['conf']['high'],
                      s['conf']['medium'], s['conf']['low'], ', '.join(names.get(s['cls'], [])),
                      src + '.csv' if src else '', guide])
    table.sort(key=lambda t: (RANK.get(t[3], 3), -t[8], t[1]))

    missing = facts['api_total'] - facts['filings'] if facts['api_total'] else 0
    short = ', '.join('%s %d of %d' % (sub, facts['by_sub'].get(sub, 0), n) for sub, n in sorted(facts['totals'].items())
                      if facts['by_sub'].get(sub, 0) < n)
    distinct_files = {r['file_number'] for r in rows}
    repair = [r for r in rows if r['likely_benign'] == 'no']
    summary = [['ICFS data issues - repair reference'], [],
               ['Data source', 'FCC ICFS Public API, every filing harvested into data/icfs.db (contact fields stripped)'],
               ['Harvest date', facts['harvested_at']],
               ['Filings checked', facts['filings']],
               ['API total records', facts['api_total']],
               ['Filings the API pages never returned', '%d (%s): paging over a live table skipped them; they are not '
                'checked' % (missing, short or 'none') if missing else 'none'],
               ['Issue rows', len(rows)], ['Affected filings', len(distinct_files)],
               ['Affected call signs', len({r['call_sign'] for r in rows if r['call_sign']})],
               ['Filings needing repair', '%d (at least one row not marked likely benign)'
                % len({r['file_number'] for r in repair})],
               ['Call signs needing repair', len({r['call_sign'] for r in repair if r['call_sign']})],
               [], ['How to read'],
               ['confidence', 'high / medium / low per row: the checker\'s row-level value where it set one, else the '
                'verified rule confidence.'],
               ['likely_benign', 'yes = the verification pass judged the row not to need repair (detail starts "likely '
                'benign: <why>"); kept so every flagged filing is listed. Filter to "no" for the repair list.'],
               ['before / after', 'after_file is the flagged filing (the one to repair); before_file is the earlier, '
                'related or reference filing; expected_value is the probable correct value when derivable.'],
               ['Pairs', 'when both filings of a pair may need repair, each gets its own row.'],
               [], ['Caveats'],
               ['', 'Dates are judged as of the harvest date, not the day the scripts ran.'],
               ['', 'Only SES Schedule B carries site coordinates; only ~700 SAT filings carry a structured Schedule S.'],
               ['', 'ICFS SAT location history mostly starts around 2017, so POC orbit comparisons are a review list.'],
               ['', 'Benign groups (legacy designators, blanket sites, licensees with several FRNs...) are kept, marked '
                'likely_benign = yes, so the list of affected filings is complete.'],
               ['', 'Rows are one per affected filing per issue; emission rows are one per (filing, designator) with '
                'every location listed; collapsed benign POC rows list every satellite named.'],
               ['', 'FCN: about 6,100 filings from 2009-2011 are Closed with no last_action or dates, one migration batch; '
                'closed_without_disposition lists each of them at low confidence.'],
               ['', 'Filings made in the last 60 days before the harvest may lack grant/action dates only because the '
                'API lags; those rows carry low confidence.'],
               ['', 'Where verification judged under 30% of the rows of a rule real, every row of that rule is capped at the '
                'verified confidence.'],
               ['', 'Rows without a call sign (many ITC/FCN/SPC filings have none) appear in the Filing index only.']]
    if empty:
        summary += [[], ['Checks with no findings'], ['rule', 'plain description', 'source CSV']]
        summary += sorted(empty)
    summary += [[], ['Rules']]
    header_at = len(summary) + 1
    summary.append(['issue class', 'rule', 'plain description', 'confidence (verified)',
                    'share of flagged rows judged real',
                    'likely benign?', 'affected filings', 'affected call signs', 'rows', 'rows needing repair (not benign)',
                    'rows high', 'rows medium', 'rows low', 'sheet', 'source CSV', 'repair guidance'])
    summary += table
    xml, ref = sheet_xml(summary, [26, 30, 60, 12, 12, 18, 10, 10, 9, 12, 8, 8, 8, 24, 16, 90], header_row=header_at,
                         freeze=False, bold_rows={i for i, r in enumerate(summary, 1) if len(r) == 1
                                                  or r and r[0] == 'rule' and i < header_at})
    sheets.append((sheet_name('Summary', used), xml, ref))

    # filing index
    fidx = collections.OrderedDict()
    for r in sorted(rows, key=lambda r: r['file_number']):
        f = fidx.setdefault(r['file_number'], {'r': r, 'n': 0, 'repair': 0, 'confs': [], 'rconfs': [],
                                               'rules': collections.Counter(), 'sheets': set()})
        f['n'] += 1
        f['confs'].append(r['confidence'])
        if r['likely_benign'] == 'no':
            f['repair'] += 1
            f['rconfs'].append(r['confidence'])
        f['rules'][r['rule']] += 1
        f['sheets'].add(r['issue_class'])
    frows = [['file_number', 'subsystem', 'call_sign', 'filing_type', 'state', 'issues', 'issues needing repair',
              'highest confidence (needing repair)', 'highest confidence (all)', 'rules', 'issue classes']]
    for fn, f in fidx.items():
        r = f['r']
        frows.append([fn, r['subsystem'], r['call_sign'], r['filing_type'], r['state'], f['n'], f['repair'],
                      best(f['rconfs']), best(f['confs']),
                      '; '.join('%s x%d' % kv if kv[1] > 1 else kv[0] for kv in sorted(f['rules'].items())),
                      '; '.join(sorted(f['sheets']))])
    xml, ref = sheet_xml(frows, [25, 10, 13, 24, 14, 8, 10, 14, 12, 80, 50])
    sheets.append((sheet_name('Filing index', used), xml, ref))

    # call sign index
    cidx = collections.OrderedDict()
    for r in sorted((r for r in rows if r['call_sign']), key=lambda r: r['call_sign']):
        c = cidx.setdefault(r['call_sign'], {'subs': set(), 'files': set(), 'n': 0, 'repair': 0, 'confs': [],
                                             'rconfs': [], 'rules': collections.Counter()})
        c['subs'].add(r['subsystem'])
        c['files'].add(r['file_number'])
        c['n'] += 1
        c['confs'].append(r['confidence'])
        if r['likely_benign'] == 'no':
            c['repair'] += 1
            c['rconfs'].append(r['confidence'])
        c['rules'][r['rule']] += 1
    crows = [['call_sign', 'subsystems', 'affected filings', 'issues', 'issues needing repair',
              'highest confidence (needing repair)', 'highest confidence (all)', 'rules']]
    for cs, c in cidx.items():
        crows.append([cs, ', '.join(sorted(c['subs'])), len(c['files']), c['n'], c['repair'], best(c['rconfs']),
                      best(c['confs']), '; '.join('%s x%d' % kv if kv[1] > 1 else kv[0] for kv in sorted(c['rules'].items()))])
    xml, ref = sheet_xml(crows, [14, 12, 10, 8, 10, 14, 12, 90])
    sheets.append((sheet_name('Call sign index', used), xml, ref))

    for nm, part in class_sheets.items():
        data = [ISSUE_COLUMNS] + [[r[k] for k in ISSUE_COLUMNS] for r in part]
        xml, ref = sheet_xml(data, [WIDTHS[k] for k in ISSUE_COLUMNS])
        sheets.append((nm, xml, ref))
    write_xlsx(out, sheets)
    return [(n, xml.count('<row ') - 1) for n, xml, _ in sheets], class_sheets, len(fidx), len(cidx)


# --- validation ------------------------------------------------------------------------------------

def validate(path, rows, per_csv):
    """Re-open the xlsx: every part well-formed; issue-sheet rows per (csv, rule) equal the CSVs; the filing
    index has one row per distinct file number.  Returns (ok, lines)."""
    import xml.etree.ElementTree as ET
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    lines, ok = [], True
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            ET.fromstring(z.read(name))  # raises on malformed XML
        lines.append('%d XML parts parse cleanly' % len(z.namelist()))
        wb = ET.fromstring(z.read('xl/workbook.xml'))
        names = [s.get('name') for s in wb.find('m:sheets', ns)]
        got, fidx_rows = collections.Counter(), None
        for i, nm in enumerate(names, 1):
            root = ET.fromstring(z.read('xl/worksheets/sheet%d.xml' % i))
            data = root.find('m:sheetData', ns)
            table = []
            for row in data:
                vals = {}
                for c in row:
                    t = c.find('m:is/m:t', ns)
                    v = c.find('m:v', ns)
                    vals[re.match(r'[A-Z]+', c.get('r')).group(0)] = t.text if t is not None else (v.text if v is not None else '')
                table.append(vals)
            if nm == 'Filing index':
                fidx_rows = len(table) - 1
            elif nm not in ('Summary', 'Call sign index'):
                head = table[0]
                cols = {v: k for k, v in head.items()}
                for vals in table[1:]:
                    got[(vals.get(cols['source_csv'], '')[:-4], vals.get(cols['rule'], ''))] += 1
    want = per_csv
    bad = {k: (want.get(k, 0), got.get(k, 0)) for k in set(want) | set(got) if want.get(k, 0) != got.get(k, 0)}
    if bad:
        ok = False
        lines.append('MISMATCH per (csv, rule): %s' % bad)
    lines.append('rows per (csv, rule) reconcile: %d keys, %d rows in CSVs, %d rows in issue sheets' % (
        len(want), sum(want.values()), sum(got.values())))
    files = len({r['file_number'] for r in rows})
    if fidx_rows != files:
        ok = False
    lines.append('Filing index rows %s vs distinct file numbers %d' % (fidx_rows, files))
    return ok, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reports', default=os.path.join(HERE, 'reports'))
    ap.add_argument('--db', default=os.path.join(HERE, 'data', 'icfs.db'))
    ap.add_argument('--out', default=os.path.join(HERE, 'reports', 'ICFS_data_issues.xlsx'))
    a = ap.parse_args()
    rows, per_csv = load(a.reports)
    facts = db_facts(a.db)
    counts, _, nfiles, ncs = build(rows, per_csv, facts, a.out)
    print('wrote %s' % a.out)
    for name, n in counts:
        print('  %-32s %8d rows' % (name, n))
    print('  issue rows %d, affected filings %d, call signs %d' % (len(rows), nfiles, ncs))
    ok, lines = validate(a.out, rows, per_csv)
    for line in lines:
        print('  ' + line)
    print('validation %s' % ('OK' if ok else 'FAILED'))
    if not ok:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
