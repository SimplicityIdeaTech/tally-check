# tally-check

A small, stdlib-only Python tool that compares an OLD export with a NEW export and prints a tie-out sheet: row counts, column totals, and (optionally) which records went missing or changed.

## The problem

Firms that move records off an old CRM, ERP or database often cannot prove that nothing was lost on the way. Two requests, from public freelance job posts, Oct 2026:

> "Send us record counts for contacts, companies, jobs, and notes ... run the full import, then confirm record counts and spot-check"

> "A fully reconciled, year-by-year replica ... identical balances"

Spot-checking a few records by eye does not prove much. A tie-out sheet does: old count next to new count, old total next to new total, difference in a third column, PASS or FAIL at the end.

## What it does

- Reads two sources: each is a folder of CSV files (one file = one table) or a SQLite `.db` file.
- Matches tables by file name (without `.csv`) or table name. `--map old=new` handles a renamed table.
- Per table: row count old vs new.
- Per numeric column present on both sides: the sum, old vs new, using exact decimal arithmetic (no float rounding).
- Per column: the number of filled-in values, old vs new (a column that went empty is a FAIL). Non-numeric values in a numeric column are counted and reported, never silently skipped.
- Optional `--key table=col` (or `a,b` for a composite key): lists keys missing in new, extra in new, and keys whose row content differs (values are trimmed of leading and trailing spaces before hashing). `--ignore-cols` leaves out columns that are expected to change, such as `updated_at`.
- Optional `--group-by table=col`: row counts and sums per group, for example per year or per account.
- Prints PASS / WARN / FAIL per table and an OVERALL line. `--csv` writes the sheet for Excel, `--md` writes markdown.
- Never writes to your data. SQLite files are opened read-only.

## What it does NOT do

- It does not migrate, fix or repair anything. It only reports.
- It does not connect to live databases, other than reading a SQLite file. For Postgres or MySQL, export each table to CSV first:

```
psql -d mydb -c "\copy contacts TO 'old/contacts.csv' WITH (FORMAT csv, HEADER)"
```

For MySQL, use your client's CSV export (for example MySQL Workbench "Export", or `SELECT ... INTO OUTFILE` with `FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"'`). Do not turn `mysql -B` tab output into CSV with `sed`: NULL becomes the literal text `NULL` and quoted text breaks.

- It is not a general diff tool. Why this instead of csvdiff, daff or datacompy: it produces a client-facing tie-out sheet (counts, exact totals, per-group totals, one status per line) and needs zero installs.
- It does not understand your business rules. Equal totals do not prove every field is right.

## Quick start

Requires Python 3.10+. No dependencies. Single file: `tally_check.py`.

```
python3 tally_check.py example/old example/new
python3 tally_check.py example/old example/new --key contacts=id --key invoices=id --ignore-cols updated_at --group-by invoices=year
python3 tally_check.py example/old example/new --csv tie-out.csv --md tie-out.md
```

## Example output

The example folders hold a made-up small CRM. The new copy is missing one contact and one invoice amount was changed (2250.75 became 2205.75). Output of the second command above:

```
tally-check  old=example/old  new=example/new

WARN  companies
      [PASS] row count: old 4  new 4  diff 0
      [WARN] row-level content: old -  new -  (not checked (no --key): counts and totals only)
FAIL  contacts
      [FAIL] row count: old 8  new 7  diff -1
      [FAIL] non-empty values in 'id': old 8  new 7  diff -1  (different number of filled-in values)
      [FAIL] non-empty values in 'company_id': old 8  new 7  diff -1  (different number of filled-in values)
      [FAIL] non-empty values in 'full_name': old 8  new 7  diff -1  (different number of filled-in values)
      [FAIL] non-empty values in 'title': old 8  new 7  diff -1  (different number of filled-in values)
      [FAIL] non-empty values in 'updated_at': old 8  new 7  diff -1  (different number of filled-in values)
      [FAIL] keys missing in new: old 8  new 7  diff 1  (107)
      [PASS] keys extra in new: old -  new 0  diff 0
      [PASS] rows with changed content: old -  new 0  diff 0
FAIL  invoices
      [PASS] row count: old 8  new 8  diff 0
      [FAIL] sum(amount): old 12461.85  new 12416.85  diff -45.00
      [PASS] rows [year=2025]: old 4  new 4  diff 0
      [PASS] rows [year=2026]: old 4  new 4  diff 0
      [PASS] sum(amount) [year=2025]: old 7760.70  new 7760.70  diff 0.00
      [FAIL] sum(amount) [year=2026]: old 4701.15  new 4656.15  diff -45.00
      [PASS] keys missing in new: old 8  new 8  diff 0
      [PASS] keys extra in new: old -  new 0  diff 0
      [FAIL] rows with changed content: old -  new 1  diff 1  (5004)

OVERALL: FAIL  (2 FAIL, 1 WARN, 0 PASS of 3 tables)
```

The `--csv` sheet has the columns `table, check, old, new, diff, status, detail`.

## Options

| Option | Meaning |
|--------|---------|
| `old`, `new` | Folder of CSV files, or a SQLite `.db` file. Each side can be either kind. |
| `--map OLD=NEW` | Table was renamed in the new source. Repeatable. |
| `--key TABLE=COLS` | Key column(s) of the table (old name), comma separated. Turns on the missing / extra / changed row checks. Repeatable. |
| `--ignore-cols COLS` | Columns left out of the row content hash. Applies to all tables. |
| `--group-by TABLE=COL` | Also compare row counts and sums per value of COL. Repeatable. |
| `--sum-cols TABLE=COLS` | Sum exactly these columns (also id columns). By default `id` and `*_id` columns are not summed. Repeatable. |
| `--delimiter CHAR` | CSV delimiter. Default: detected per file from `,` `;` tab `|` using the header line. Use `\t` for tab. |
| `--encoding NAME` | CSV text encoding. Default `utf-8-sig` (UTF-8, BOM allowed), e.g. `latin-1`. |
| `--csv FILE` | Write the tie-out sheet as CSV (UTF-8 with BOM, so Excel opens it cleanly). |
| `--md FILE` | Write the tie-out sheet as markdown. |

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Everything matches |
| 1 | Warnings only: table on one side only, column dropped or added, non-numeric values in a numeric column (sums equal), wrong field counts, duplicate keys, SQLite views not compared, or no `--key` given (row content not checked) |
| 2 | Any mismatch (counts, sums, empty columns, missing/extra/changed rows) |
| 3 | Bad arguments or unreadable input |

## Limits

- Numbers are strict. A value counts as numeric only if Python's `Decimal` accepts it. Values such as `1,000.00`, `$5` or `N/A` in a numeric column are counted, shown as an example, and give a WARN (the sum of the other values is still compared, and a difference there is a FAIL). Clean the export first for a full total. Decimal comma (`1.234,56`) is not supported: that column is treated as non-numeric.
- Without `--key`, only counts, totals and filled-in counts are checked, so the result is at best WARN. Give a `--key` for every table that matters.
- Delimiter detection looks at the header line only; if it is wrong, pass `--delimiter`.
- Text that looks like a number (phone numbers, postcodes) is summed too. The sum is harmless but not meaningful.
- Key columns, the `--group-by` column and `id` / `*_id` columns are not summed by default (`--sum-cols` overrides).
- SQLite views are not compared; a WARN says how many were skipped.
- In the `--csv` sheet, text cells starting with `=`, `+`, `-`, `@` are prefixed with an apostrophe so Excel does not run them as formulas.
- Memory: files are streamed row by row. With `--key`, one 16-byte hash per key is held in memory for each side, so a table with 10 million rows needs roughly 1 to 2 GB. Without `--key`, memory use is small.
- Row content is compared after trimming spaces only. `10.0` and `10.00`, or different date formats, count as changed. Only columns present on both sides are compared.
- CSV: first row is the header. The delimiter is detected from the header (or set with `--delimiter`). UTF-8 unless `--encoding` (a BOM is fine). Empty files are read as zero rows. CSV and SQLite values are compared as text, so a type difference such as `5` vs `5.0` can show up as a change in key checks.
- Matching is by name. It cannot discover which old table became which new one; use `--map`.
- With duplicate keys, the last row per key is used for the content check, and a WARN is raised.
- It checks that the data arrived. It does not check that the data was right before the move.

## Licence

MIT, see `LICENSE`. Run the tests with `python3 -m pytest tests`.

Need this done for your business? Simplicity Idea Tech — info@simplicityideatech.com
