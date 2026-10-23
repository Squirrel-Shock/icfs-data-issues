# ICFS data issues

A data-quality audit of every filing in the FCC International Communications Filing System (ICFS),
harvested from the public [ICFS Public API](https://api.fcc.gov/icfs-public/exp/api/v1/filing) on
2026-10-22 (140,592 filings).

**Download:** [`ICFS_data_issues.xlsx`](../../releases/latest/download/ICFS_data_issues.xlsx) from the
[latest release](../../releases/latest).

## The workbook

- **Summary**: the source, the harvest date, caveats, and one line per rule. Each line gives a plain
  description, the verified confidence, the share of flagged rows judged real, row and filing
  counts, and repair guidance.
- **Filing index**: one row per affected file number (73,808).
- **Call sign index**: one row per affected call sign (31,781).
- **One sheet per issue class**, with every row (119,678 in all).

Each row has these columns:

| Column | Meaning |
|---|---|
| issue_class, rule | What kind of problem it is, and which check found it |
| confidence | high, medium or low |
| subsystem, call_sign, file_number, filing_type, state | The filing to repair |
| before_file / before_value | The earlier, related or reference filing and its value |
| after_file / after_value | The flagged filing and the value it records |
| expected_value | The correct value, when it can be worked out |
| detail | Why the row was flagged |
| likely_benign | yes / no |
| repair_guidance | What to do about it |

Every affected filing gets a row, including known-benign patterns: legacy emission designators,
blanket or mobile sites, a 2009-2011 FCN migration batch, and pre-2001 filings without an FRN.
**Filter `likely_benign = no` for the repair list** (19,546 filings across 7,216 call signs).

All dates are judged as of the harvest date. Everything flagged here is in the FCC's published data.
A row means the data looks wrong; it is not proof.

## Reproducing it

You need Python 3.11+ and nothing outside the standard library.

```bash
python harvest.py            # every filing from the ICFS Public API -> data/icfs.db (resumable, ~1,400 pages)
python profiler.py           # fill rates and value distributions -> reports/profile.md
python rules.py              # per-filing date, status and identity rules -> reports/findings.csv
python freqs.py              # earth-station Schedule B checks -> reports/frequencies.csv
python walk.py               # per-call-sign Schedule B history jumps -> reports/walk.csv
python swaps.py              # Schedule B swapped or copied between call signs -> reports/swaps.csv
python sites.py              # coordinates, elevation, applicant identity, call-sign shapes -> reports/sites.csv
python lifecycle.py          # license lifecycle per call sign -> reports/lifecycle.csv
python sat.py                # space-station Schedule S checks and history -> reports/sat.csv
python poc.py                # earth-station points of communication vs SAT records -> reports/poc.csv
python build_workbook.py     # every reports/*.csv -> reports/ICFS_data_issues.xlsx (validated on write)
python -m unittest test_rules test_freqs test_walk test_swaps test_sites test_lifecycle test_sat test_poc
```

`harvest.py` strips contact fields (phone, email, street address, points of contact) before it
stores anything. `build_workbook.py` writes the xlsx with `zipfile`, so openpyxl is not needed.
After writing, it re-opens the file and reconciles the row count for each rule against the CSVs.
`rule_notes.py` holds the description, verified confidence and repair guidance for each rule.

Some ICFS patterns look like errors but are legitimate:
- After 2020, renewals narrow 3700-4200 MHz to 4000-4200 MHz because of the C-band transition.
- Pro forma assignments are filed after consummation.
- STAs are often granted after their requested period ends.
- Federal "Database Entry" filings (FAA C-band earth stations) carry no FCC term.
