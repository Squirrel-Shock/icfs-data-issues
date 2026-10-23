"""Download every ICFS filing from the FCC ICFS Public API into a local SQLite file.

    python harvest.py [--db data/icfs.db] [--workers 4] [--subsystem SES ...] [--rescan]

Resumable: pages already stored are skipped, so re-running after a crash continues.
Paging over a live table can skip a record, so the end of each subsystem compares the
distinct file numbers stored with total_records; run again with --rescan to refetch
every page of a subsystem that came up short.  Stdlib only.

Contact details (phone, email, mailing address, points of contact) are stripped
before anything is stored; the stripped key names are recorded in the meta table.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
import zlib

API = 'https://api.fcc.gov/icfs-public/exp/api/v1/filing'
PAGE_SIZE = 100  # 500 times out at the gateway
SUBSYSTEMS = ['SES', 'ITC', 'SAT', 'FCN', 'SCL', 'SPC', 'ISP', 'IHF', '325', 'ROA', 'DNC']
CONTACT_KEY = re.compile(r'contact|phone|telephone|email|e_mail|fax|mailing|street|address|zip|postal|_poc', re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings(
  file_number TEXT PRIMARY KEY, subsystem TEXT, call_sign TEXT, filing_type TEXT,
  page INTEGER, raw BLOB);
CREATE TABLE IF NOT EXISTS pages(subsystem TEXT, page INTEGER, n INTEGER, fetched_at TEXT,
  PRIMARY KEY(subsystem, page));
CREATE TABLE IF NOT EXISTS totals(subsystem TEXT PRIMARY KEY, total_records INTEGER,
  total_pages INTEGER, checked_at TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def post(body):
    data = json.dumps(body).encode()
    for attempt in range(6):
        try:
            req = urllib.request.Request(API, data, {'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)['result']
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, KeyError) as e:
            if attempt == 5:
                raise
            wait = 10 * (attempt + 1)
            print('  retry %s page %s in %ds: %s' % (body.get('subsystem'), body.get('page'), wait,
                                                     str(e)[:80].encode('ascii', 'replace').decode()))
            time.sleep(wait)


def strip_contacts(obj, dropped):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if CONTACT_KEY.search(k):
                dropped.add(k)
            else:
                out[k] = strip_contacts(v, dropped)
        return out
    if isinstance(obj, list):
        return [strip_contacts(v, dropped) for v in obj]
    return obj


def now():
    return datetime.datetime.now().isoformat(timespec='seconds')


def harvest(conn, sub, workers, rescan):
    first = post({'page': 1, 'page_size': PAGE_SIZE, 'subsystem': sub})
    total, pages = int(first['metadata']['total_records']), int(first['metadata']['total_pages'])
    conn.execute('INSERT OR REPLACE INTO totals VALUES (?,?,?,?)', (sub, total, pages, now()))
    if rescan:
        conn.execute('DELETE FROM pages WHERE subsystem=?', (sub,))
    done = {p for (p,) in conn.execute('SELECT page FROM pages WHERE subsystem=?', (sub,))}
    todo = [p for p in range(1, pages + 1) if p not in done]
    print('%s: %d records, %d pages, %d to fetch' % (sub, total, pages, len(todo)))
    dropped = set(json.loads(dict(conn.execute('SELECT key, value FROM meta')).get('dropped_keys', '[]')))

    def store(page, records):
        rows = []
        for i, rec in enumerate(records):
            rec = strip_contacts(rec, dropped)
            fn = rec.get('file_number') or '_no_file_number_%s_%d_%d' % (sub, page, i)
            rows.append((fn, rec.get('subsystem'), rec.get('call_sign'), rec.get('filing_type'), page,
                         zlib.compress(json.dumps(rec, separators=(',', ':')).encode(), 6)))
        conn.executemany('INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?,?)', rows)
        conn.execute('INSERT OR REPLACE INTO pages VALUES (?,?,?,?)', (sub, page, len(records), now()))
        conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('dropped_keys', json.dumps(sorted(dropped))))
        conn.commit()

    if 1 in todo:
        store(1, first['records'])
        todo.remove(1)
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(post, {'page': p, 'page_size': PAGE_SIZE, 'subsystem': sub}): p for p in todo}
        for n, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            store(futures[fut], fut.result()['records'])
            if n % 50 == 0:
                print('  %s %d/%d pages' % (sub, n, len(todo)))
    have = conn.execute('SELECT COUNT(*) FROM filings WHERE subsystem=?', (sub,)).fetchone()[0]
    flag = '' if have >= total else '  SHORT - run again with --rescan --subsystem %s' % sub
    print('%s: %d stored of %d%s' % (sub, have, total, flag))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'icfs.db'))
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--subsystem', action='append')
    ap.add_argument('--rescan', action='store_true')
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.db), exist_ok=True)
    conn = sqlite3.connect(a.db)
    conn.executescript(SCHEMA)
    for sub in a.subsystem or SUBSYSTEMS:
        harvest(conn, sub, a.workers, a.rescan)
    unfiltered = int(post({'page': 1, 'page_size': 1})['metadata']['total_records'])
    summed = conn.execute('SELECT SUM(total_records) FROM totals').fetchone()[0]
    conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('harvested_at', now()))
    conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('api_total_records', str(unfiltered)))
    conn.commit()
    print('API total %d, subsystem totals sum %d%s' % (unfiltered, summed,
          '' if unfiltered == summed else '  - a subsystem is missing from SUBSYSTEMS'))


if __name__ == '__main__':
    main()
