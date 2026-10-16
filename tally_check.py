#!/usr/bin/env python3
"""tally-check - compare an OLD export with a NEW export and print a tie-out sheet.

Stdlib only, Python 3.10+. Read-only on inputs.
Exit codes: 0 = all match, 1 = warnings only, 2 = mismatch, 3 = bad arguments or unreadable input.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

PASS, WARN, FAIL = 0, 1, 2
NAMES = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}
SAMPLE = 5  # how many example keys to show in the detail column

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


class UsageError(Exception):
    """Bad arguments or unreadable input. Exit code 2."""


# ---------------------------------------------------------------- sources

DELIMS = [",", ";", "\t", "|"]
NUMNOISE = str.maketrans("", "", ",$\u20ac\u00a3% ")


class Source:
    """A folder of CSV files or one SQLite file. Rows come out as (line, list of str)."""

    def __init__(self, path: str, encoding: str = "utf-8-sig", delimiter: str | None = None):
        p = Path(path)
        self.path = p
        self.con = None
        self.encoding = encoding
        self.delimiter = delimiter
        self.views: list[str] = []
        self._delims: dict[str, str] = {}
        if p.is_dir():
            self.tables = {f.stem: f for f in sorted(p.iterdir())
                           if f.is_file() and f.suffix.lower() == ".csv"}
        elif p.is_file():
            with p.open("rb") as f:
                if f.read(16) != b"SQLite format 3\x00":
                    raise UsageError(f"{path}: not a folder and not a SQLite file")
            uri = "file:" + quote(str(p.resolve())) + "?mode=ro"
            try:
                self.con = sqlite3.connect(uri, uri=True)
                names = [(r[0], r[1]) for r in self.con.execute(
                    "select name, type from sqlite_master where type in ('table','view') "
                    "and name not like 'sqlite_%' order by name")]
            except sqlite3.Error as e:
                raise UsageError(f"{path}: cannot read SQLite file: {e}")
            self.tables = {n: n for n, t in names if t == "table"}
            self.views = [n for n, t in names if t == "view"]
        else:
            raise UsageError(f"{path}: no such folder or file")

    def _delim(self, name) -> str:
        """Explicit --delimiter, else the candidate that splits the header line into most fields."""
        if self.delimiter:
            return self.delimiter
        if name not in self._delims:
            try:
                with self.tables[name].open("r", encoding=self.encoding, newline="") as f:
                    line = f.readline()
            except UnicodeDecodeError as e:
                raise UsageError(f"{self.tables[name]}: cannot decode with {self.encoding}: {e}")
            best, n = ",", 1
            for d in DELIMS:
                try:
                    k = len(next(csv.reader([line], delimiter=d), []))
                except csv.Error:
                    continue
                if k > n:
                    best, n = d, k
            self._delims[name] = best
        return self._delims[name]

    def _csv(self, name):
        f = self.tables[name].open("r", encoding=self.encoding, newline="")
        rd = csv.reader(f, delimiter=self._delim(name))
        try:
            header = next(rd, None)
        except (csv.Error, UnicodeDecodeError) as e:
            f.close()
            raise UsageError(f"{self.tables[name]}: bad CSV: {e}")
        return f, rd, header

    def columns(self, name) -> list[str]:
        if self.con is None:
            f, _rd, header = self._csv(name)
            f.close()
            return [h.strip() for h in header] if header else []
        cur = self.con.execute('select * from "%s" limit 0' % name.replace('"', '""'))
        return [d[0] for d in cur.description]

    def rows(self, name):
        if self.con is None:
            f, rd, _header = self._csv(name)
            try:
                for row in rd:
                    if row:
                        yield rd.line_num, row
            except (csv.Error, UnicodeDecodeError) as e:
                raise UsageError(f"{self.tables[name]}: bad CSV: {e}")
            finally:
                f.close()
            return
        cur = self.con.execute('select * from "%s"' % name.replace('"', '""'))
        for i, row in enumerate(cur, 1):
            yield i, [_sql_text(v) for v in row]


def _sql_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return format(Decimal(repr(v)), "f")
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


def to_decimal(s: str):
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


# ---------------------------------------------------------------- scanning

class Stats:
    def __init__(self):
        self.count = 0
        self.sums: dict[str, Decimal] = {}
        self.filled: dict[str, int] = {}     # numeric values seen
        self.bad: dict[str, int] = {}        # non-numeric values seen in summed columns
        self.bad_ex: dict[str, str] = {}
        self.numlike: dict[str, int] = {}    # bad values that look like formatted numbers (1,000.00 / $5)
        self.nonempty: dict[str, int] = {}
        self.gcount: dict[str, int] = {}
        self.gsums: dict[str, dict[str, Decimal]] = {}
        self.keys: dict[tuple, bytes] = {}
        self.dups = 0
        self.ragged = 0
        self.ragged_first = 0


def scan(src: Source, table: str, common: list[str], sumcols: list[str], key: list[str],
         group: str | None, hashcols: list[str]) -> Stats:
    cols = src.columns(table)
    pos = {}
    for i, c in enumerate(cols):
        pos.setdefault(c, i)
    st = Stats()
    sumset = set(sumcols)
    for c in common:
        st.sums[c] = Decimal(0)
        st.gsums[c] = {}
        st.nonempty[c] = 0
    kidx = [pos[c] for c in key] if key else []
    hidx = [pos[c] for c in hashcols]
    gi = pos[group] if group else None
    width = len(cols)

    def val(row, i):
        return row[i].strip() if i < len(row) else ""

    for line, row in src.rows(table):
        st.count += 1
        if len(row) != width:
            st.ragged += 1
            st.ragged_first = st.ragged_first or line
        g = None
        if gi is not None:
            g = val(row, gi)
            st.gcount[g] = st.gcount.get(g, 0) + 1
        for c in common:
            v = val(row, pos[c])
            if v == "":
                continue
            st.nonempty[c] += 1
            if c not in sumset:
                continue
            d = to_decimal(v)
            if d is None:
                st.bad[c] = st.bad.get(c, 0) + 1
                st.bad_ex.setdefault(c, v)
                if to_decimal(v.translate(NUMNOISE)) is not None:
                    st.numlike[c] = st.numlike.get(c, 0) + 1
                continue
            st.filled[c] = st.filled.get(c, 0) + 1
            st.sums[c] += d
            if g is not None:
                gs = st.gsums[c]
                gs[g] = gs.get(g, Decimal(0)) + d
        if key:
            k = tuple(val(row, i) for i in kidx)
            h = hashlib.blake2b("\x1f".join(val(row, i) for i in hidx).encode("utf-8", "replace"),
                                digest_size=16).digest()
            if k in st.keys:
                st.dups += 1
            st.keys[k] = h
    return st


# ---------------------------------------------------------------- comparing

def fmt(d: Decimal) -> str:
    return format(d, "f")


def split_cols(s: str) -> list[str]:
    return [c.strip() for c in s.split(",") if c.strip()]


def is_id_col(c: str) -> bool:
    c = c.lower()
    return c == "id" or c.endswith("_id")


def compare_table(old: Source, new: Source, oname: str, nname: str, key: list[str],
                  group: str | None, ignore: set[str], sum_override: list[str] | None = None) -> list[tuple]:
    """Return check rows: (check, old, new, diff, status, detail)."""
    out = []

    def add(check, o="", n="", diff="", status=PASS, detail=""):
        out.append((check, str(o), str(n), str(diff), status, detail))

    ocols, ncols = old.columns(oname), new.columns(nname)
    common = list(dict.fromkeys(c for c in ocols if c in set(ncols)))
    for c in ocols:
        if c not in ncols:
            add(f"column '{c}'", "present", "missing", "", WARN, "column dropped in new")
    for c in ncols:
        if c not in ocols:
            add(f"column '{c}'", "missing", "present", "", WARN, "column added in new")
    for side, cols in (("old", ocols), ("new", ncols)):
        if len(cols) == 1 and (";" in cols[0] or "\t" in cols[0]):
            add("delimiter", "", "", "", WARN,
                f"{side} file has one column whose header contains ';' or a tab; try --delimiter")

    key_requested = bool(key)
    for label, cols in (("key", key), ("group-by", [group] if group else [])):
        miss = [c for c in cols if c not in common]
        if miss:
            add(f"{label} column(s) {','.join(miss)}", "", "", "", FAIL,
                "not present on both sides; check skipped")
            if label == "key":
                key = []
            else:
                group = None
    hashcols = sorted(c for c in common if c not in ignore)
    if sum_override is not None:
        sumcols = [c for c in common if c in sum_override]
    else:
        sumcols = [c for c in common if c not in key and c != group and not is_id_col(c)]
    so = scan(old, oname, common, sumcols, key, group, hashcols)
    sn = scan(new, nname, common, sumcols, key, group, hashcols)

    add("row count", so.count, sn.count, sn.count - so.count,
        PASS if so.count == sn.count else FAIL)

    if so.ragged or sn.ragged:
        add("rows with wrong field count", so.ragged, sn.ragged, "", WARN,
            f"first at line {so.ragged_first or '-'} (old), {sn.ragged_first or '-'} (new)")

    for c in common:
        a, b = so.nonempty[c], sn.nonempty[c]
        if a != b:
            why = ("all values empty in new" if b == 0 else "all values empty in old" if a == 0
                   else "different number of filled-in values")
            add(f"non-empty values in '{c}'", a, b, b - a, FAIL, why)

    numeric_cols = []
    for c in sumcols:
        fo, fn, bo, bn = so.filled.get(c, 0), sn.filled.get(c, 0), so.bad.get(c, 0), sn.bad.get(c, 0)
        if not (fo > 0 or fn > 0 or so.numlike.get(c) or sn.numlike.get(c)):
            continue  # text column (or empty on both sides)
        numeric_cols.append(c)
        d = sn.sums[c] - so.sums[c]
        if bo == 0 and bn == 0:
            add(f"sum({c})", fmt(so.sums[c]), fmt(sn.sums[c]), fmt(d), PASS if d == 0 else FAIL)
        else:
            ex = "; ".join(f"{s} e.g. '{x.bad_ex[c]}'" for s, x in (("old", so), ("new", sn)) if c in x.bad_ex)
            add(f"sum({c})", fmt(so.sums[c]), fmt(sn.sums[c]), fmt(d), FAIL if d != 0 else WARN,
                f"skipped: {bo} (old) / {bn} (new) non-numeric values {ex}; sums above cover numeric values only")

    if group:
        for g in sorted(set(so.gcount) | set(sn.gcount)):
            co, cn = so.gcount.get(g, 0), sn.gcount.get(g, 0)
            add(f"rows [{group}={g}]", co, cn, cn - co, PASS if co == cn else FAIL)
        for c in numeric_cols:
            for g in sorted(set(so.gsums[c]) | set(sn.gsums[c])):
                a = so.gsums[c].get(g, Decimal(0))
                b = sn.gsums[c].get(g, Decimal(0))
                add(f"sum({c}) [{group}={g}]", fmt(a), fmt(b), fmt(b - a), PASS if a == b else FAIL)

    if key:
        ko, kn = so.keys, sn.keys
        missing = sorted(set(ko) - set(kn))
        extra = sorted(set(kn) - set(ko))
        changed = sorted(k for k in set(ko) & set(kn) if ko[k] != kn[k])
        show = lambda ks: ", ".join("/".join(k) for k in ks[:SAMPLE]) + (" ..." if len(ks) > SAMPLE else "")
        add("keys missing in new", len(ko), len(ko) - len(missing), len(missing),
            FAIL if missing else PASS, show(missing))
        add("keys extra in new", "", len(extra), len(extra), FAIL if extra else PASS, show(extra))
        add("rows with changed content", "", len(changed), len(changed),
            FAIL if changed else PASS, show(changed))
        for side, s in (("old", so), ("new", sn)):
            if s.dups:
                add(f"duplicate keys in {side}", "", s.dups, s.dups, WARN,
                    "last row per key was used for the content check")
    elif not key_requested:
        add("row-level content", "", "", "", WARN,
            "not checked (no --key): counts and totals only")
    return out


def run(args) -> tuple[list[tuple], int]:
    enc = args.encoding or "utf-8-sig"
    delim = args.delimiter
    if delim == "\\t":
        delim = "\t"
    if delim is not None and len(delim) != 1:
        raise UsageError("--delimiter must be a single character (use \\t for tab)")
    try:
        "".encode(enc)
    except LookupError:
        raise UsageError(f"unknown encoding '{enc}'")
    old, new = Source(args.old, enc, delim), Source(args.new, enc, delim)
    mapping = {}
    for m in args.map or []:
        if "=" not in m:
            raise UsageError(f"--map expects old_name=new_name, got '{m}'")
        a, b = (x.strip() for x in m.split("=", 1))
        if a not in old.tables:
            raise UsageError(f"--map: no table '{a}' in old source")
        if b not in new.tables:
            raise UsageError(f"--map: no table '{b}' in new source")
        mapping[a] = b

    def per_table(opts, flag):
        d = {}
        for o in opts or []:
            if "=" not in o:
                raise UsageError(f"{flag} expects table=value, got '{o}'")
            t, v = (x.strip() for x in o.split("=", 1))
            if t not in old.tables:
                raise UsageError(f"{flag}: no table '{t}' in old source (use the old name)")
            d[t] = v
        return d

    keys = {t: split_cols(v) for t, v in per_table(args.key, "--key").items()}
    groups = per_table(args.group_by, "--group-by")
    sumcols = {t: split_cols(v) for t, v in per_table(args.sum_cols, "--sum-cols").items()}
    ignore = set(split_cols(args.ignore_cols or ""))

    rows, used_new = [], set()
    for t in sorted(set(old.tables) | set(new.tables)):
        if t in old.tables:
            n = mapping.get(t, t)
            if n in new.tables:
                used_new.add(n)
                label = t if n == t else f"{t} -> {n}"
                for r in compare_table(old, new, t, n, keys.get(t, []), groups.get(t), ignore,
                                       sumcols.get(t)):
                    rows.append((label,) + r)
            else:
                rows.append((t, "table", "present", "missing", "", WARN, "table only in old"))
    for t in sorted(set(new.tables) - used_new):
        rows.append((t, "table", "missing", "present", "", WARN, "table only in new"))
    if old.views or new.views:
        rows.append(("(views)", "views", len(old.views), len(new.views), "", WARN,
                     f"{len(old.views) + len(new.views)} SQLite views not compared"))
    return rows, max([r[5] for r in rows], default=PASS)


# ---------------------------------------------------------------- output

HEADER = ("table", "check", "old", "new", "diff", "status", "detail")


def table_status(rows):
    st = {}
    for r in rows:
        st[r[0]] = max(st.get(r[0], PASS), r[5])
    return st


def text_report(rows, overall, a, b) -> str:
    lines = [f"tally-check  old={a}  new={b}", ""]
    ts = table_status(rows)
    for t, s in ts.items():
        lines.append(f"{NAMES[s]:4}  {t}")
        for r in rows:
            if r[0] == t:
                diff = f"  diff {r[4]}" if r[4] != "" else ""
                det = f"  ({r[6]})" if r[6] else ""
                lines.append(f"      [{NAMES[r[5]]}] {r[1]}: old {r[2] or '-'}  new {r[3] or '-'}{diff}{det}")
    tt = {t: s for t, s in ts.items() if t != "(views)"}
    lines += ["", f"OVERALL: {NAMES[overall]}  ({sum(1 for s in tt.values() if s == FAIL)} FAIL, "
              f"{sum(1 for s in tt.values() if s == WARN)} WARN, "
              f"{sum(1 for s in tt.values() if s == PASS)} PASS of {len(tt)} tables)"]
    return "\n".join(lines)


def safe_cell(x):
    """Spreadsheet formula guard: text starting with = + - @ tab or CR gets a leading apostrophe.
    Plain numbers (including negatives) are left alone."""
    x = str(x)
    if x and x[0] in "=+-@\t\r" and to_decimal(x) is None:
        return "'" + x
    return x


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for r in rows:
            w.writerow([safe_cell(x) for x in r[:5] + (NAMES[r[5]], r[6])])


def write_md(path, rows, overall):
    esc = lambda s: str(s).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Tie-out sheet\n\n| " + " | ".join(HEADER) + " |\n|" + "---|" * len(HEADER) + "\n")
        for r in rows:
            f.write("| " + " | ".join(esc(x) for x in r[:5] + (NAMES[r[5]], r[6])) + " |\n")
        f.write(f"\n**OVERALL: {NAMES[overall]}**\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="tally_check.py",
        description="Compare an OLD export with a NEW export (CSV folders or SQLite files) "
                    "and produce a tie-out sheet. Read-only.")
    p.add_argument("old", help="folder of CSV files, or a SQLite .db file")
    p.add_argument("new", help="folder of CSV files, or a SQLite .db file")
    p.add_argument("--map", action="append", metavar="OLD=NEW", help="table renamed in new (repeatable)")
    p.add_argument("--key", action="append", metavar="TABLE=COLS",
                   help="key column(s), comma separated; enables missing/extra/changed row checks (repeatable)")
    p.add_argument("--ignore-cols", metavar="COLS",
                   help="comma-separated columns left out of the row content hash (e.g. updated_at)")
    p.add_argument("--group-by", action="append", metavar="TABLE=COL",
                   help="also compare row counts and sums per value of COL (repeatable)")
    p.add_argument("--sum-cols", action="append", metavar="TABLE=COLS",
                   help="sum exactly these columns of the table (also id columns); default skips id and *_id")
    p.add_argument("--delimiter", metavar="CHAR", help="CSV delimiter (default: detected per file from , ; tab |)")
    p.add_argument("--encoding", metavar="NAME", help="CSV text encoding (default utf-8-sig: UTF-8, BOM allowed)")
    p.add_argument("--csv", metavar="FILE", help="write the tie-out sheet as CSV")
    p.add_argument("--md", metavar="FILE", help="write the tie-out sheet as markdown")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 3 if e.code else 0
    try:
        rows, overall = run(args)
        print(text_report(rows, overall, args.old, args.new))
        if args.csv:
            write_csv(args.csv, rows)
        if args.md:
            write_md(args.md, rows, overall)
    except (UsageError, OSError) as e:
        print(f"tally-check: error: {e}", file=sys.stderr)
        return 3
    return overall


if __name__ == "__main__":
    sys.exit(main())
